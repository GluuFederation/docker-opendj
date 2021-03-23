import json
import logging
import logging.config
import os
import pathlib
import shutil
import socket
import sys
import time
from contextlib import contextmanager

from settings import LOGGING_CONFIG
from utils import guess_serf_addr

import javaproperties
from cryptography import x509
from cryptography.hazmat.backends import default_backend

from pygluu.containerlib import get_manager
from pygluu.containerlib.utils import decode_text
from pygluu.containerlib.utils import exec_cmd
from pygluu.containerlib.utils import as_boolean
from pygluu.containerlib.utils import generate_ssl_certkey
from pygluu.containerlib.persistence.ldap import LdapClient

DEFAULT_ADMIN_PW_PATH = "/opt/opendj/.pw"

manager = get_manager()

logging.config.dictConfig(LOGGING_CONFIG)
logger = logging.getLogger("entrypoint")


def guess_host_addr():
    return socket.getfqdn()


def install_opendj():
    logger.info("Installing OpenDJ.")

    # 1) render opendj-setup.properties
    admin_port = os.environ.get("GLUU_LDAP_ADVERTISE_ADMIN_PORT", "4444")

    ctx = {
        "ldap_hostname": guess_host_addr(),
        "ldap_port": manager.config.get("ldap_port"),
        "ldaps_port": manager.config.get("ldaps_port"),
        "ldap_jmx_port": 1689,
        "ldap_admin_port": admin_port,
        "opendj_ldap_binddn": manager.config.get("ldap_binddn"),
        "ldapPassFn": DEFAULT_ADMIN_PW_PATH,
        "ldap_backend_type": "je",
    }
    with open("/app/templates/opendj-setup.properties") as fr:
        content = fr.read() % ctx

        with open("/opt/opendj/opendj-setup.properties", "w") as fw:
            fw.write(content)

    # 2) run installer
    cmd = " ".join([
        "/opt/opendj/setup",
        "--no-prompt",
        "--cli",
        "--acceptLicense",
        "--propertiesFilePath /opt/opendj/opendj-setup.properties",
        "--usePkcs12keyStore /etc/certs/opendj.pkcs12",
        "--keyStorePassword {}".format(
            decode_text(manager.secret.get("encoded_ldapTrustStorePass"), manager.secret.get("encoded_salt")).decode()
        ),
        "--doNotStart",
    ])
    out, err, code = exec_cmd(cmd)
    if code and err:
        logger.warning(err.decode())

    if all([os.environ.get("JAVA_VERSION", "") >= "1.8.0",
            os.path.isfile("/opt/opendj/config/config.ldif")]):
        with open("/opt/opendj/config/java.properties", "a") as f:
            status_arg = "\nstatus.java-args=-Xms8m -client -Dcom.sun.jndi.ldap.object.disableEndpointIdentification=true"

            max_ram_percentage = os.environ.get("GLUU_MAX_RAM_PERCENTAGE", "75.0")
            repl_arg = f"\ndsreplication.java-args=-client -Dcom.sun.jndi.ldap.object.disableEndpointIdentification=true -XX:+UseContainerSupport -XX:MaxRAMPercentage={max_ram_percentage}"

            args = "".join([status_arg, repl_arg])
            f.write(args)


def run_dsjavaproperties():
    _, err, code = exec_cmd("/opt/opendj/bin/dsjavaproperties")
    if code and err:
        logger.warning(err.decode())


def sync_ldap_pkcs12():
    dest = manager.config.get("ldapTrustStoreFn")
    manager.secret.to_file("ldap_pkcs12_base64", dest, decode=True, binary_mode=True)


def sync_ldap_certs():
    """Gets opendj.crt, opendj.key, and opendj.pem
    """
    manager.secret.to_file("ldap_ssl_cert", "/etc/certs/opendj.crt", decode=True)
    manager.secret.to_file("ldap_ssl_key", "/etc/certs/opendj.key", decode=True)
    manager.secret.to_file("ldap_ssl_cacert", "/etc/certs/opendj.pem", decode=True)


@contextmanager
def ds_context():
    """Ensures Directory Server are up and teardown at the end of the context.
    """

    cmd = "/opt/opendj/bin/status -D '{}' --bindPasswordFile {} --connectTimeout 10000".format(
        manager.config.get("ldap_binddn"),
        DEFAULT_ADMIN_PW_PATH,
    )
    out, _, code = exec_cmd(cmd)
    running = out.decode().startswith("Unable to connect to the server")

    if not running:
        exec_cmd("/opt/opendj/bin/start-ds")

    try:
        yield
    except Exception:  # noqa: B902
        raise
    finally:
        exec_cmd("/opt/opendj/bin/stop-ds --quiet")


def run_upgrade():
    buildinfo = "4.0.0"

    # check if we need to upgrade
    if os.path.isfile("/opt/opendj/config/buildinfo"):
        # example of buildinfo `3.0.1.c5ad2e4846d8aeb501ffdfe5ae2dfd35136dfa68`
        with open("/opt/opendj/config/buildinfo") as f:
            old_buildinfo = ".".join([
                num for num in f.read().split(".") if num.isdigit()
            ])

            if old_buildinfo < buildinfo:
                logger.info("Trying to upgrade OpenDJ server")

                # backup old buildinfo
                exec_cmd("cp /opt/opendj/config/buildinfo /opt/opendj/config/buildinfo-{}".format(old_buildinfo))
                _, err, retcode = exec_cmd("/opt/opendj/upgrade --acceptLicense")
                assert retcode == 0, "Failed to upgrade OpenDJ; reason={}".format(err.decode())

                # backup current buildinfo
                exec_cmd("cp /opt/opendj/config/buildinfo /opt/opendj/config/buildinfo-{}".format(buildinfo))


def require_site():
    persistence_type = os.environ.get("GLUU_PERSISTENCE_TYPE", "ldap")
    ldap_mapping = os.environ.get("GLUU_PERSISTENCE_LDAP_MAPPING", "default")

    if persistence_type == "ldap":
        return True
    if persistence_type == "hybrid" and ldap_mapping == "site":
        return True
    return False


def main():
    alt_name = os.environ.get("GLUU_CERT_ALT_NAME", "")

    # the plain-text admin password is not saved in KV storage,
    # but we have the encoded one
    manager.secret.to_file("encoded_ox_ldap_pw", DEFAULT_ADMIN_PW_PATH, decode=True)

    logger.info("Syncing OpenDJ certs.")
    sync_ldap_certs()
    sync_ldap_pkcs12()

    logger.info("Checking certificate's Subject Alt Name (SAN)")
    san = get_certificate_san("/etc/certs/opendj.crt")

    if alt_name not in san:
        logger.info("Re-generating OpenDJ certs with SAN support.")

        # add new alt name
        san.append(alt_name)
        regenerate_ldap_certs(extra_dns=san)

        # update secrets
        manager.secret.from_file("ldap_ssl_cert", "/etc/certs/opendj.crt", encode=True)
        manager.secret.from_file("ldap_ssl_key", "/etc/certs/opendj.key", encode=True)
        manager.secret.from_file("ldap_ssl_cacert", "/etc/certs/opendj.pem", encode=True)

        regenerate_ldap_pkcs12()
        # update secrets
        manager.secret.from_file(
            "ldap_pkcs12_base64",
            manager.config.get("ldapTrustStoreFn"),
            encode=True,
            binary_mode=True,
        )

    # update ldap_init_*
    manager.config.set("ldap_init_host", alt_name)
    if as_boolean(os.environ.get("GLUU_LDAP_USE_SSL", "True")):
        port = 1636
    else:
        port = 1389
    manager.config.set("ldap_init_port", port)

    # do upgrade if required
    run_upgrade()

    # patch for https://bugs.openjdk.java.net/browse/JDK-8217094
    disable_tls13()

    # Below we will check if there is a `/opt/opendj/config/config.ldif` or
    # `/opt/opendj/config/schema` directory with files signalling that OpenDJ
    # has already been successfully deployed and will launch as expected.
    if not any([os.path.isfile("/opt/opendj/config/config.ldif"),
                os.path.isdir("/opt/opendj/config/schema")]):
        cleanup_config_dir()
        install_opendj()

        with ds_context():
            # modify admin-keystore and ads-truststore (for replication), if required
            if os.environ.get("GLUU_SERF_ADVERTISE_ADDR", ""):
                logger.info("Advertise address is detected ...")
                logger.info("Reconfiguring keystore for admin")
                modify_admin_keystore()
                logger.info("Reconfiguring keystore for replication")
                modify_ads_truststore()

        with ds_context():
            create_backends()
            configure_opendj()
            configure_opendj_indexes()

    # prepare serf config
    configure_serf()

    # post-installation cleanup
    for f in [DEFAULT_ADMIN_PW_PATH, "/opt/opendj/opendj-setup.properties"]:
        try:
            os.unlink(f)
        except OSError:
            pass


def regenerate_ldap_certs(extra_dns=None):
    suffix = "opendj"
    country_code = manager.config.get("country_code")
    state = manager.config.get("state")
    city = manager.config.get("city")
    org_name = manager.config.get("orgName")
    domain = manager.config.get("hostname")
    email = manager.config.get("admin_email")

    cert, key = generate_ssl_certkey(
        suffix,
        email,
        domain,
        org_name,
        country_code,
        state,
        city,
        extra_dns=extra_dns,
    )

    with open("/etc/certs/{}.pem".format(suffix), "w") as fw:
        with open(cert) as fr:
            ldap_ssl_cert = fr.read()

        with open(key) as fr:
            ldap_ssl_key = fr.read()

        ldap_ssl_cacert = "".join([ldap_ssl_cert, ldap_ssl_key])
        fw.write(ldap_ssl_cacert)


def regenerate_ldap_pkcs12():
    suffix = "opendj"
    passwd = manager.secret.get("ldap_truststore_pass")
    hostname = manager.config.get("hostname")

    # Convert key to pkcs12
    cmd = " ".join([
        "openssl",
        "pkcs12",
        "-export",
        "-inkey /etc/certs/{}.key".format(suffix),
        "-in /etc/certs/{}.crt".format(suffix),
        "-out /etc/certs/{}.pkcs12".format(suffix),
        "-name {}".format(hostname),
        "-passout pass:{}".format(passwd),
    ])
    _, err, retcode = exec_cmd(cmd)
    assert retcode == 0, "Failed to generate PKCS12 file; reason={}".format(err.decode())


def get_certificate_san(certpath) -> str:
    with open(certpath, "rb") as f:
        crt = x509.load_pem_x509_certificate(f.read(), backend=default_backend())

    san_obj = crt.extensions.get_extension_for_oid(x509.oid.ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
    sans = [f"{name.value}" for name in san_obj.value]
    return sans


def cleanup_config_dir():
    if not os.path.exists("/opt/opendj/config"):
        return

    # When mounting certain volumes, OpenDJ installation will fail to install
    # as the mounted volume may have some residual information for some reason
    # (i.e. Amazon ElasticBlockStorage's "lost+found" directory). This only
    # occurs on the first installation. Otherwise the volume can be used as
    # a successfully deployed persistent disk.
    subtree = os.listdir("/opt/opendj/config")

    for obj in subtree:
        path = "/opt/opendj/config/{0}".format(obj)
        logger.warning(
            "Found {0} in '/opt/opendj/config/' volume mount. "
            "/opt/opendj/config should be empty for a successful "
            "installation.".format(path)
        )

        if obj != "lost+found":
            logger.warning(
                "{0} will not be removed. Please manually remove any "
                "data from the volume mount for /opt/opendj/config/.".format(path)
            )
            continue

        logger.info("Removing {0}".format(path))
        try:
            # delete directory
            shutil.rmtree(path)
        except OSError:
            # delete file
            os.unlink(path)
        except Exception as exc:  # noqa: B902
            # Unforeseen information in the config/ dir will be logged and
            # prompt the administrator to deal with their issue.
            logger.warning(exc)


def resolve_serf_key():
    def key_from_file():
        keygen = ""
        keygen_file = os.environ.get("GLUU_SERF_KEY_FILE", "/etc/gluu/conf/serf-key")

        if os.path.isfile(keygen_file):
            try:
                logger.info(f"Loading Serf key from {keygen_file}")
                with open(keygen_file) as f:
                    keygen = f.read().strip()
                    # save it for subsequent access
                    manager.secret.set("serf_gluu_ldap_key", keygen)
            except UnicodeDecodeError as exc:
                logger.warning(f"Invalid Serf key; reason={exc}")
        return keygen

    def key_from_cmd():
        keygen = ""

        logger.info("Loading Serf key from serf keygen command")

        out, err, code = exec_cmd("serf keygen")
        if code != 0:
            logger.warning(f"Unable to self-generate Serf key; reason={err.decode()}")
            return keygen

        keygen = out.decode().strip()
        # save it for subsequent access
        manager.secret.set("serf_gluu_ldap_key", keygen)
        return keygen

    # load from secrets (if any)
    logger.info("Loading Serf key from secrets")
    keygen = manager.secret.get("serf_gluu_ldap_key")

    # no key from secrets
    if not keygen:
        logger.warning("Unable to load Serf key from secrets")
        # try loading it from file or from `serf keygen` command
        keygen = key_from_file() or key_from_cmd()
    return keygen


def configure_serf():
    conf_fn = pathlib.Path("/etc/gluu/conf/serf.json")

    # skip if config exists
    if conf_fn.is_file():
        return

    advertise = guess_serf_addr()

    conf = {
        "node_name": advertise.split(":")[0],
        "tags": {
            "role": "ldap",
            "admin_port": os.environ.get("GLUU_LDAP_ADVERTISE_ADMIN_PORT", "4444"),
            "replication_port": os.environ.get("GLUU_LDAP_ADVERTISE_REPLICATION_PORT", "8989"),
            "ldaps_port": os.environ.get("GLUU_LDAP_ADVERTISE_LDAPS_PORT", "1636"),
            "ldap_port": os.environ.get("GLUU_LDAP_ADVERTISE_LDAP_PORT", "1389"),
        },
        "log_level": os.environ.get("GLUU_SERF_LOG_LEVEL", "warn"),
        "profile": os.environ.get("GLUU_SERF_PROFILE", "lan"),
        "encrypt_key": resolve_serf_key(),
        "advertise": advertise,
    }

    mcast = as_boolean(os.environ.get("GLUU_SERF_MULTICAST_DISCOVER", False))
    if mcast:
        conf["discover"] = "gluu-ldap"

    conf_fn.write_text(json.dumps(conf))


def configure_opendj_indexes():
    logger.info("Configuring indexes for available backends.")

    with open("/app/templates/index.json") as f:
        data = json.load(f)

    backends = ["userRoot", "metric"]
    if require_site():
        backends.append("site")

    # connect to localhost
    client = LdapClient(manager, host="localhost")
    for attr_map in data:
        for backend in attr_map["backend"]:
            if backend not in backends:
                continue

            dn = f"ds-cfg-attribute={attr_map['attribute']},cn=Index,ds-cfg-backend-id={backend},cn=Backends,cn=config"
            attrs = {
                'objectClass': ['top', 'ds-cfg-backend-index'],
                'ds-cfg-attribute': [attr_map['attribute']],
                'ds-cfg-index-type': attr_map['index'],
                'ds-cfg-index-entry-limit': ['4000']
            }

            added, msg = client.add(dn, attributes=attrs)
            if not added:
                logger.warning(msg)


def create_backends():
    logger.info("Creating backends.")
    mods = [
        "create-backend --backend-name metric --set base-dn:o=metric --type je --set enabled:true --set db-cache-percent:10",
    ]
    if require_site():
        mods.append(
            "create-backend --backend-name site --set base-dn:o=site --type je --set enabled:true --set db-cache-percent:20",
        )

    hostname = guess_host_addr()
    binddn = manager.config.get("ldap_binddn")
    admin_port = os.environ.get("GLUU_LDAP_ADVERTISE_ADMIN_PORT", "4444")

    for mod in mods:
        cmd = " ".join([
            "/opt/opendj/bin/dsconfig",
            "--trustAll",
            "--no-prompt",
            f"--hostname {hostname}",
            f"--port {admin_port}",
            f"--bindDN '{binddn}'",
            f"--bindPasswordFile {DEFAULT_ADMIN_PW_PATH}",
            mod,
        ])
        _, err, code = exec_cmd(cmd)
        if code:
            logger.warning(err.decode())
            sys.exit(1)


def configure_opendj():
    logger.info("Configuring OpenDJ.")

    mods = [
        ('ds-cfg-backend-id=userRoot,cn=Backends,cn=config', 'ds-cfg-db-cache-percent', '70', LdapClient.MODIFY_REPLACE),
        ('cn=config', 'ds-cfg-single-structural-objectclass-behavior', 'accept', LdapClient.MODIFY_REPLACE),
        ('cn=config', 'ds-cfg-reject-unauthenticated-requests', 'true', LdapClient.MODIFY_REPLACE),
        ('cn=Default Password Policy,cn=Password Policies,cn=config', 'ds-cfg-allow-pre-encoded-passwords', 'true', LdapClient.MODIFY_REPLACE),
        ('cn=Default Password Policy,cn=Password Policies,cn=config', 'ds-cfg-default-password-storage-scheme', 'cn=Salted SHA-512,cn=Password Storage Schemes,cn=config', LdapClient.MODIFY_REPLACE),
        ('cn=File-Based Audit Logger,cn=Loggers,cn=config', 'ds-cfg-enabled', 'true', LdapClient.MODIFY_REPLACE),
        ('cn=LDAP Connection Handler,cn=Connection Handlers,cn=config', 'ds-cfg-enabled', 'true', LdapClient.MODIFY_REPLACE),
        ('cn=JMX Connection Handler,cn=Connection Handlers,cn=config', 'ds-cfg-enabled', 'false', LdapClient.MODIFY_REPLACE),
        ('cn=Access Control Handler,cn=config', 'ds-cfg-global-aci', '(targetattr!="userPassword||authPassword||debugsearchindex||changes||changeNumber||changeType||changeTime||targetDN||newRDN||newSuperior||deleteOldRDN")(version 3.0; acl "Anonymous read access"; allow (read,search,compare) userdn="ldap:///anyone";)', LdapClient.MODIFY_DELETE),
        ("cn=Core Schema,cn=Schema Providers,cn=config", "ds-cfg-allow-zero-length-values-directory-string", "true", LdapClient.MODIFY_REPLACE)
    ]

    # connect to localhost
    client = LdapClient(manager, host="localhost")
    for dn, attr, value, mod_type in mods:
        modified, msg = client.modify(dn, {attr: [mod_type, value]})
        if not modified:
            logger.warning(msg)

    # Create uniqueness for attributes
    attrs = [
        ("mail", "Unique mail address"),
        ("uid", "Unique uid entry"),
    ]

    for attr, cn in attrs:
        added, msg = client.add(
            'cn={},cn=Plugins,cn=config'.format(cn),
            attributes={
                'objectClass': ['top', 'ds-cfg-plugin', 'ds-cfg-unique-attribute-plugin'],
                'ds-cfg-java-class': ['org.opends.server.plugins.UniqueAttributePlugin'],
                'ds-cfg-enabled': ['true'],
                'ds-cfg-plugin-type': [
                    'postoperationadd',
                    'postoperationmodify',
                    'postoperationmodifydn',
                    'postsynchronizationadd',
                    'postsynchronizationmodify',
                    'postsynchronizationmodifydn',
                    'preoperationadd',
                    'preoperationmodify',
                    'preoperationmodifydn',
                ],
                'ds-cfg-type': [attr],
                'cn': [cn],
                'ds-cfg-base-dn': ['o=gluu']
            }
        )
        if not added:
            logger.warning(msg)


def disable_tls13():
    security_file = "/usr/lib/jvm/default-jvm/jre/conf/security/java.security"

    with open(security_file) as f:
        data = javaproperties.loads(f.read())

        if "TLSv1.3" in data["jdk.tls.disabledAlgorithms"]:
            return

    with open(security_file, "w") as f:
        data["jdk.tls.disabledAlgorithms"] = "TLSv1.3, " + data["jdk.tls.disabledAlgorithms"]
        f.write(javaproperties.dumps(data))


def modify_ads_truststore():
    def export_ads_certificate():
        cmd = " ".join([
            "keytool -export",
            "-alias ads-certificate",
            "-keystore /opt/opendj/config/ads-truststore",
            "-storepass:file /opt/opendj/config/ads-truststore.pin",
            "-keypass:file /opt/opendj/config/ads-truststore.pin",
            "-file /opt/opendj/config/ads-cert.crt",
            "-rfc",
        ])
        out, err, code = exec_cmd(cmd)
        if code != 0:
            err = err or out
            logger.error(f"Unable to export ads-certificate; reason={err.decode()}")
            sys.exit(1)

    def delete_instance_key():
        export_ads_certificate()

        cmd = "openssl x509 -fingerprint -md5 -noout -in /opt/opendj/config/ads-cert.crt"
        out, err, code = exec_cmd(cmd)
        if code != 0:
            err = err or out
            logger.error(f"Unable to get ads-cert fingerprint; reason={err.decode()}")
            sys.exit(1)

        cfg_key = out.decode().split("=")[-1].replace(":", "")

        # connect to localhost
        client = LdapClient(manager, host="localhost")
        deleted, msg = client.delete(f"ds-cfg-key-id={cfg_key},cn=instance keys,cn=admin data")
        if not deleted:
            logger.warning(msg)

    def recreate_ads_truststore():
        os.unlink("/opt/opendj/config/ads-truststore")

        addr = guess_serf_addr().split(":")[0]
        hostname = socket.getfqdn()

        cmd = " ".join([
            "keytool -genkeypair",
            "-alias ads-certificate",
            "-keyalg RSA",
            "-validity 365",
            "-keysize 2048",
            "-storetype JKS",
            "-keystore /opt/opendj/config/ads-truststore",
            "-storepass:file /opt/opendj/config/ads-truststore.pin",
            "-keypass:file /opt/opendj/config/ads-truststore.pin",
            f"-dname 'CN={addr}, O=OpenDJ RSA Certificate'",
            f"-ext san=dns:{hostname},dns:{addr}",
        ])
        out, err, code = exec_cmd(cmd)
        if code != 0:
            err = err or out
            logger.error(f"Unable to create ads-truststore; reason={err.decode()}")
            sys.exit(1)

    def add_new_cert():
        export_ads_certificate()

        cmd = "openssl x509 -fingerprint -md5 -noout -in /opt/opendj/config/ads-cert.crt"
        out, err, code = exec_cmd(cmd)
        if code != 0:
            err = err or out
            logger.error(f"Unable to get ads-cert fingerprint; reason={err.decode()}")
            sys.exit(1)

        alias = out.decode().split("=")[-1].replace(":", "").lower()
        cmd = " ".join([
            "keytool -import -trustcacerts",
            f"-alias {alias}",
            "-keystore /opt/opendj/config/ads-truststore",
            "-storepass:file /opt/opendj/config/ads-truststore.pin",
            "-keypass:file /opt/opendj/config/ads-truststore.pin",
            "-file /opt/opendj/config/ads-cert.crt",
            "-noprompt",
        ])
        out, err, code = exec_cmd(cmd)
        if code != 0:
            err = err or out
            logger.error(f"Unable to add new cert; reason={err.decode()}")
            sys.exit(1)

    delete_instance_key()
    time.sleep(2)
    recreate_ads_truststore()
    add_new_cert()


def modify_admin_keystore():
    def export_admin_certificate():
        cmd = " ".join([
            "keytool -export",
            "-alias admin-cert",
            "-keystore /opt/opendj/config/admin-keystore",
            "-storepass:file /opt/opendj/config/admin-keystore.pin",
            "-keypass:file /opt/opendj/config/admin-keystore.pin",
            "-file /opt/opendj/config/admin-cert.crt",
            "-rfc",
        ])
        out, err, code = exec_cmd(cmd)
        if code != 0:
            err = err or out
            logger.error(f"Unable to export admin-cert; reason={err.decode()}")
            sys.exit(1)

    def recreate_admin_keystore():
        os.unlink("/opt/opendj/config/admin-keystore")

        addr = guess_serf_addr().split(":")[0]
        hostname = socket.getfqdn()

        cmd = " ".join([
            "keytool -genkeypair",
            "-alias admin-cert",
            "-keyalg RSA",
            "-validity 365",
            "-keysize 2048",
            "-storetype JKS",
            "-keystore /opt/opendj/config/admin-keystore",
            "-storepass:file /opt/opendj/config/admin-keystore.pin",
            "-keypass:file /opt/opendj/config/admin-keystore.pin",
            f"-dname 'CN={addr}, O=Administration Connector RSA Self-Signed Certificate'",
            f"-ext san=dns:{hostname},dns:{addr}",
        ])
        out, err, code = exec_cmd(cmd)
        if code != 0:
            err = err or out
            logger.error(f"Unable to create admin-keystore; reason={err.decode()}")
            sys.exit(1)

    def recreate_admin_truststore():
        export_admin_certificate()

        os.unlink("/opt/opendj/config/admin-truststore")

        cmd = " ".join([
            "keytool -import -trustcacerts",
            "-alias admin-cert",
            "-keystore /opt/opendj/config/admin-truststore",
            "-storepass:file /opt/opendj/config/admin-keystore.pin",
            "-keypass:file /opt/opendj/config/admin-keystore.pin",
            "-file /opt/opendj/config/admin-cert.crt",
            "-noprompt",
        ])
        out, err, code = exec_cmd(cmd)
        if code != 0:
            err = err or out
            logger.error(f"Unable to add new cert; reason={err.decode()}")
            sys.exit(1)

    recreate_admin_keystore()
    recreate_admin_truststore()


if __name__ == "__main__":
    main()
