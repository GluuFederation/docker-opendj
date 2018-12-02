import base64
import fcntl
import glob
import json
import logging
import os
import shlex
import socket
import struct
import subprocess
from contextlib import contextmanager

import pyDes

from gluu_config import ConfigManager

GLUU_CACHE_TYPE = os.environ.get("GLUU_CACHE_TYPE", 'IN_MEMORY')
GLUU_REDIS_URL = os.environ.get('GLUU_REDIS_URL', 'localhost:6379')
GLUU_REDIS_TYPE = os.environ.get('GLUU_REDIS_TYPE', 'STANDALONE')
GLUU_MEMCACHED_URL = os.environ.get('GLUU_MEMCACHED_URL', 'localhost:11211')
GLUU_LDAP_INIT = os.environ.get("GLUU_LDAP_INIT", False)
GLUU_LDAP_INIT_HOST = os.environ.get('GLUU_LDAP_INIT_HOST', 'localhost')
GLUU_LDAP_INIT_PORT = os.environ.get("GLUU_LDAP_INIT_PORT", 1636)
GLUU_LDAP_ADDR_INTERFACE = os.environ.get("GLUU_LDAP_ADDR_INTERFACE", "")
GLUU_LDAP_ADVERTISE_ADDR = os.environ.get("GLUU_LDAP_ADVERTISE_ADDR", "")
GLUU_OXTRUST_CONFIG_GENERATION = os.environ.get("GLUU_OXTRUST_CONFIG_GENERATION", False)

GLUU_LDAP_PORT = os.environ.get("GLUU_LDAP_PORT", 1389)
GLUU_LDAPS_PORT = os.environ.get("GLUU_LDAPS_PORT", 1636)
GLUU_ADMIN_PORT = os.environ.get("GLUU_ADMIN_PORT", 4444)
GLUU_REPLICATION_PORT = os.environ.get("GLUU_REPLICATION_PORT", 8989)
GLUU_JMX_PORT = os.environ.get("GLUU_JMX_PORT", 1689)

GLUU_CERT_ALT_NAME = os.environ.get("GLUU_CERT_ALT_NAME", "")

DEFAULT_ADMIN_PW_PATH = "/opt/opendj/.pw"

config_manager = ConfigManager()

logger = logging.getLogger("entrypoint")
logger.setLevel(logging.INFO)
ch = logging.StreamHandler()
fmt = logging.Formatter('%(levelname)s - %(asctime)s - %(message)s')
ch.setFormatter(fmt)
logger.addHandler(ch)


def get_ip_addr(ifname):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        addr = socket.inet_ntoa(fcntl.ioctl(
            sock.fileno(),
            0x8915,  # SIOCGIFADDR
            struct.pack('256s', ifname[:15])
        )[20:24])
    except IOError:
        addr = ""
    return addr


def guess_host_addr():
    addr = GLUU_LDAP_ADVERTISE_ADDR or get_ip_addr(GLUU_LDAP_ADDR_INTERFACE) or socket.getfqdn()
    return addr


def decrypt_text(encrypted_text, key):
    cipher = pyDes.triple_des(b"{}".format(key), pyDes.ECB,
                              padmode=pyDes.PAD_PKCS5)
    encrypted_text = b"{}".format(base64.b64decode(encrypted_text))
    return cipher.decrypt(encrypted_text)


def exec_cmd(cmd):
    """Executes shell command.

    :param cmd: String of shell command.
    :returns: A tuple consists of stdout, stderr, and return code
              returned from shell command execution.
    """
    args = shlex.split(cmd)
    popen = subprocess.Popen(args,
                             stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
    stdout, stderr = popen.communicate()
    retcode = popen.returncode
    return stdout, stderr, retcode


def install_opendj():
    logger.info("Installing OpenDJ.")

    # 1) render opendj-setup.properties
    ctx = {
        "ldap_hostname": guess_host_addr(),
        "ldap_port": config_manager.get("ldap_port"),
        "ldaps_port": config_manager.get("ldaps_port"),
        "ldap_jmx_port": GLUU_JMX_PORT,
        "ldap_admin_port": GLUU_ADMIN_PORT,
        "opendj_ldap_binddn": config_manager.get("ldap_binddn"),
        "ldapPassFn": DEFAULT_ADMIN_PW_PATH,
        "ldap_backend_type": "je",
    }
    with open("/opt/templates/opendj-setup.properties") as fr:
        content = fr.read() % ctx

        with open("/opt/opendj/opendj-setup.properties", "wb") as fw:
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
            decrypt_text(config_manager.get("encoded_ldapTrustStorePass"), config_manager.get("encoded_salt"))
        ),
        "--doNotStart",
    ])
    out, err, code = exec_cmd(cmd)
    if code and err:
        logger.warn(err)


def run_dsjavaproperties():
    _, err, code = exec_cmd("/opt/opendj/bin/dsjavaproperties")
    if code and err:
        logger.warn(err)


def configure_opendj():
    logger.info("Configuring OpenDJ.")

    opendj_prop_name = 'global-aci:\'(targetattr!="userPassword||authPassword||debugsearchindex||changes||changeNumber||changeType||changeTime||targetDN||newRDN||newSuperior||deleteOldRDN")(version 3.0; acl "Anonymous read access"; allow (read,search,compare) userdn="ldap:///anyone";)\''
    config_mods = [
        'set-global-configuration-prop --set single-structural-objectclass-behavior:accept',
        'set-attribute-syntax-prop --syntax-name "Directory String" --set allow-zero-length-values:true',
        'set-password-policy-prop --policy-name "Default Password Policy" --set allow-pre-encoded-passwords:true',
        'set-log-publisher-prop --publisher-name "File-Based Audit Logger" --set enabled:true',
        'create-backend --backend-name site --set base-dn:o=site --type je --set enabled:true',
        'set-connection-handler-prop --handler-name "LDAP Connection Handler" --set enabled:false',
        'set-connection-handler-prop --handler-name "LDAPS Connection Handler" --set enabled:true --set listen-address:0.0.0.0',
        'set-administration-connector-prop --set listen-address:0.0.0.0',
        'set-access-control-handler-prop --remove {}'.format(opendj_prop_name),
        'set-global-configuration-prop --set reject-unauthenticated-requests:true',
        'set-password-policy-prop --policy-name "Default Password Policy" --set default-password-storage-scheme:"Salted SHA-512"',
        'create-plugin --plugin-name "Unique mail address" --type unique-attribute --set enabled:true --set base-dn:o=gluu --set type:mail',
        'create-plugin --plugin-name "Unique uid entry" --type unique-attribute --set enabled:true --set base-dn:o=gluu --set type:uid',
        # 'set-crypto-manager-prop --set ssl-encryption:true',
    ]
    hostname = guess_host_addr()
    binddn = config_manager.get("ldap_binddn")

    for config in config_mods:
        cmd = " ".join([
            "/opt/opendj/bin/dsconfig",
            "--trustAll",
            "--no-prompt",
            "--hostname {}".format(hostname),
            "--port {}".format(GLUU_ADMIN_PORT),
            "--bindDN '{}'".format(binddn),
            "--bindPasswordFile {}".format(DEFAULT_ADMIN_PW_PATH),
            "{}".format(config)
        ])
        _, err, code = exec_cmd(cmd)
        if code:
            logger.warn(err)


def render_ldif():
    ctx = {
        # o_site.ldif
        # has no variables

        # appliance.ldif
        'cache_provider_type': GLUU_CACHE_TYPE,
        'redis_url': GLUU_REDIS_URL,
        'redis_type': GLUU_REDIS_TYPE,
        'memcached_url': GLUU_MEMCACHED_URL,
        # oxpassport-config.ldif
        'inumAppliance': config_manager.get('inumAppliance'),
        'ldap_hostname': config_manager.get('ldap_init_host'),
        # TODO: currently using std ldaps port 1636 as ldap port.
        # after basic testing we need to do it right, and remove this hack.
        # to do this properly we need to update all templates.
        'ldaps_port': config_manager.get('ldap_init_port'),
        'ldap_binddn': config_manager.get('ldap_binddn'),
        'encoded_ox_ldap_pw': config_manager.get('encoded_ox_ldap_pw'),
        'jetty_base': config_manager.get('jetty_base'),

        # asimba.ldif
        # attributes.ldif
        # groups.ldif
        # oxidp.ldif
        # scopes.ldif
        'inumOrg': r"{}".format(config_manager.get('inumOrg')),  # raw string

        # base.ldif
        'orgName': config_manager.get('orgName'),

        # clients.ldif
        'oxauth_client_id': config_manager.get('oxauth_client_id'),
        'oxauthClient_encoded_pw': config_manager.get('oxauthClient_encoded_pw'),
        'hostname': config_manager.get('hostname'),
        'idp_client_id': config_manager.get('idp_client_id'),
        'idpClient_encoded_pw': config_manager.get('idpClient_encoded_pw'),

        # configuration.ldif
        'oxauth_config_base64': config_manager.get('oxauth_config_base64'),
        'oxauth_static_conf_base64': config_manager.get('oxauth_static_conf_base64'),
        'oxauth_openid_key_base64': config_manager.get('oxauth_openid_key_base64'),
        'oxauth_error_base64': config_manager.get('oxauth_error_base64'),
        'oxtrust_config_base64': config_manager.get('oxtrust_config_base64'),
        'oxtrust_cache_refresh_base64': config_manager.get('oxtrust_cache_refresh_base64'),
        'oxtrust_import_person_base64': config_manager.get('oxtrust_import_person_base64'),
        'oxidp_config_base64': config_manager.get('oxidp_config_base64'),
        # 'oxcas_config_base64': config_manager.get('oxcas_config_base64'),
        'oxasimba_config_base64': config_manager.get('oxasimba_config_base64'),

        # passport.ldif
        'passport_rs_client_id': config_manager.get('passport_rs_client_id'),
        'passport_rs_client_base64_jwks': config_manager.get('passport_rs_client_base64_jwks'),
        'passport_rp_client_id': config_manager.get('passport_rp_client_id'),
        'passport_rp_client_base64_jwks': config_manager.get('passport_rp_client_base64_jwks'),
        "passport_rp_client_jks_fn": config_manager.get("passport_rp_client_jks_fn"),
        "passport_rp_client_jks_pass": config_manager.get("passport_rp_client_jks_pass"),

        # people.ldif
        "encoded_ldap_pw": config_manager.get('encoded_ldap_pw'),

        # scim.ldif
        'scim_rs_client_id': config_manager.get('scim_rs_client_id'),
        'scim_rs_client_base64_jwks': config_manager.get('scim_rs_client_base64_jwks'),
        'scim_rp_client_id': config_manager.get('scim_rp_client_id'),
        'scim_rp_client_base64_jwks': config_manager.get('scim_rp_client_base64_jwks'),

        # scripts.ldif
        "person_authentication_usercertexternalauthenticator": config_manager.get("person_authentication_usercertexternalauthenticator"),
        "person_authentication_passportexternalauthenticator": config_manager.get("person_authentication_passportexternalauthenticator"),
        "dynamic_scope_dynamic_permission": config_manager.get("dynamic_scope_dynamic_permission"),
        "id_generator_samplescript": config_manager.get("id_generator_samplescript"),
        "dynamic_scope_org_name": config_manager.get("dynamic_scope_org_name"),
        "dynamic_scope_work_phone": config_manager.get("dynamic_scope_work_phone"),
        "cache_refresh_samplescript": config_manager.get("cache_refresh_samplescript"),
        "person_authentication_yubicloudexternalauthenticator": config_manager.get("person_authentication_yubicloudexternalauthenticator"),
        "uma_rpt_policy_uma_rpt_policy": config_manager.get("uma_rpt_policy_uma_rpt_policy"),
        "uma_claims_gathering_uma_claims_gathering": config_manager.get("uma_claims_gathering_uma_claims_gathering"),
        "person_authentication_basiclockaccountexternalauthenticator": config_manager.get("person_authentication_basiclockaccountexternalauthenticator"),
        "person_authentication_uafexternalauthenticator": config_manager.get("person_authentication_uafexternalauthenticator"),
        "person_authentication_otpexternalauthenticator": config_manager.get("person_authentication_otpexternalauthenticator"),
        "person_authentication_duoexternalauthenticator": config_manager.get("person_authentication_duoexternalauthenticator"),
        "update_user_samplescript": config_manager.get("update_user_samplescript"),
        "user_registration_samplescript": config_manager.get("user_registration_samplescript"),
        "user_registration_confirmregistrationsamplescript": config_manager.get("user_registration_confirmregistrationsamplescript"),
        "person_authentication_googleplusexternalauthenticator": config_manager.get("person_authentication_googleplusexternalauthenticator"),
        "person_authentication_u2fexternalauthenticator": config_manager.get("person_authentication_u2fexternalauthenticator"),
        "person_authentication_supergluuexternalauthenticator": config_manager.get("person_authentication_supergluuexternalauthenticator"),
        "person_authentication_basicexternalauthenticator": config_manager.get("person_authentication_basicexternalauthenticator"),
        "scim_samplescript": config_manager.get("scim_samplescript"),
        "person_authentication_samlexternalauthenticator": config_manager.get("person_authentication_samlexternalauthenticator"),
        "client_registration_samplescript": config_manager.get("client_registration_samplescript"),
        "person_authentication_twilio2fa": config_manager.get("person_authentication_twilio2fa"),
        "application_session_samplescript": config_manager.get("application_session_samplescript"),
        "uma_rpt_policy_umaclientauthzrptpolicy": config_manager.get("uma_rpt_policy_umaclientauthzrptpolicy"),
        "person_authentication_samlpassportauthenticator": config_manager.get("person_authentication_samlpassportauthenticator"),
        "consent_gathering_consentgatheringsample": config_manager.get("consent_gathering_consentgatheringsample"),
        "person_authentication_thumbsigninexternalauthenticator": config_manager.get("person_authentication_thumbsigninexternalauthenticator"),
    }

    ldif_template_base = '/opt/templates/ldif'
    pattern = '/*.ldif'
    for file_path in glob.glob(ldif_template_base + pattern):
        with open(file_path, 'r') as fp:
            template = fp.read()

        # render
        content = template % ctx

        # write to tmpdir
        with open("/tmp/{}".format(os.path.basename(file_path)), 'w') as fp:
            fp.write(content)


def import_ldif():
    logger.info("Adding data into LDAP.")

    ldif_files = map(lambda x: os.path.join("/tmp", x), [
        'base.ldif',
        'appliance.ldif',
        'attributes.ldif',
        'scopes.ldif',
        'clients.ldif',
        'people.ldif',
        'groups.ldif',
        'o_site.ldif',
        'scripts.ldif',
        'configuration.ldif',
        'scim.ldif',
        'asimba.ldif',
        'passport.ldif',
        'oxpassport-config.ldif',
        'oxidp.ldif',
    ])

    for ldif_file_fn in ldif_files:
        cmd = " ".join([
            "/opt/opendj/bin/ldapmodify",
            "--hostname {}".format(guess_host_addr()),
            "--port {}".format(GLUU_ADMIN_PORT),
            "--bindDN '{}'".format(config_manager.get("ldap_binddn")),
            "-j {}".format(DEFAULT_ADMIN_PW_PATH),
            "--filename {}".format(ldif_file_fn),
            "--trustAll",
            "--useSSL",
            "--defaultAdd",
            "--continueOnError",
        ])
        _, err, code = exec_cmd(cmd)
        if code:
            logger.warn(err)


def index_opendj(backend, data):
    logger.info("Creating indexes for {} backend.".format(backend))

    for attr_map in data:
        attr_name = attr_map['attribute']

        for index_type in attr_map["index"]:
            for backend_name in attr_map["backend"]:
                if backend_name != backend:
                    continue

                index_cmd = " ".join([
                    "/opt/opendj/bin/dsconfig",
                    "create-backend-index",
                    "--backend-name {}".format(backend),
                    "--type generic",
                    "--index-name {}".format(attr_name),
                    "--set index-type:{}".format(index_type),
                    "--set index-entry-limit:4000",
                    "--hostName {}".format(guess_host_addr()),
                    "--port {}".format(GLUU_ADMIN_PORT),
                    "--bindDN '{}'".format(config_manager.get("ldap_binddn")),
                    "-j {}".format(DEFAULT_ADMIN_PW_PATH),
                    "--trustAll",
                    "--noPropertiesFile",
                    "--no-prompt",
                ])
                _, err, code = exec_cmd(index_cmd)
                if code:
                    logger.warn(err)


def as_boolean(val, default=False):
    truthy = set(('t', 'T', 'true', 'True', 'TRUE', '1', 1, True))
    falsy = set(('f', 'F', 'false', 'False', 'FALSE', '0', 0, False))

    if val in truthy:
        return True
    if val in falsy:
        return False
    return default


def get_ldap_peers():
    return json.loads(config_manager.get("ldap_peers", "[]"))


def register_ldap_peer(hostname):
    peers = set(get_ldap_peers())
    # add new hostname
    peers.add(hostname)
    config_manager.set("ldap_peers", list(peers))


def migrate_ldap_servers():
    # migrate ``ldap_servers`` to ``ldap_peers``
    adapter = os.environ.get("GLUU_CONFIG_ADAPTER", "")

    if adapter == "consul":
        # make unique peers
        peers = set([])

        for _, server in config_manager.adapter.find("ldap_servers").iteritems():
            peer = json.loads(server)
            peers.add(peer["host"])

        if peers:
            # convert set to list to satisfy ``config_manager.set``
            config_manager.set("ldap_peers", list(peers))


def replicate_from(peer, server):
    passwd = decrypt_text(config_manager.get("encoded_ox_ldap_pw"),
                          config_manager.get("encoded_salt"))

    for base_dn in ["o=gluu", "o=site"]:
        logger.info("Enabling OpenDJ replication of {} between {}:{} and {}:{}.".format(
            base_dn, peer, GLUU_LDAPS_PORT, server, GLUU_LDAPS_PORT,
        ))

        enable_cmd = " ".join([
            "/opt/opendj/bin/dsreplication",
            "enable",
            "--host1 {}".format(peer),
            "--port1 {}".format(GLUU_ADMIN_PORT),
            "--bindDN1 '{}'".format(config_manager.get("ldap_binddn")),
            "--bindPassword1 {}".format(passwd),
            "--replicationPort1 {}".format(GLUU_REPLICATION_PORT),
            "--secureReplication1",
            "--host2 {}".format(server),
            "--port2 {}".format(GLUU_ADMIN_PORT),
            "--bindDN2 '{}'".format(config_manager.get("ldap_binddn")),
            "--bindPassword2 {}".format(passwd),
            "--secureReplication2",
            "--adminUID admin",
            "--adminPassword {}".format(passwd),
            "--baseDN '{}'".format(base_dn),
            "-X",
            "-n",
            "-Q",
            "--trustAll",
        ])
        _, err, code = exec_cmd(enable_cmd)
        if code:
            logger.warn(err.strip())

        logger.info("Initializing OpenDJ replication of {} between {}:{} and {}:{}.".format(
            base_dn, peer, GLUU_LDAPS_PORT, server, GLUU_LDAPS_PORT,
        ))

        init_cmd = " ".join([
            "/opt/opendj/bin/dsreplication",
            "initialize",
            "--baseDN '{}'".format(base_dn),
            "--adminUID admin",
            "--adminPassword {}".format(passwd),
            "--hostSource {}".format(peer),
            "--portSource {}".format(GLUU_ADMIN_PORT),
            "--hostDestination {}".format(server),
            "--portDestination {}".format(GLUU_ADMIN_PORT),
            "-X",
            "-n",
            "-Q",
            "--trustAll",
        ])
        _, err, code = exec_cmd(init_cmd)
        if code:
            logger.warn(err.strip())


def check_connection(host, port):
    logger.info("Checking connection to {}:{}.".format(host, port))

    passwd = decrypt_text(config_manager.get("encoded_ox_ldap_pw"),
                          config_manager.get("encoded_salt"))

    cmd = " ".join([
        "/opt/opendj/bin/ldapsearch",
        "--hostname {}".format(host),
        "--port {}".format(port),
        "--baseDN ''",
        "--bindDN '{}'".format(config_manager.get("ldap_binddn")),
        "--bindPassword {}".format(passwd),
        "-Z",
        "-X",
        "--searchScope base",
        "'(objectclass=*)' 1.1",
    ])
    # stdout, stdin, code =
    return exec_cmd(cmd)
    # return code == 0


def sync_ldap_pkcs12():
    pkcs = decrypt_text(config_manager.get("ldap_pkcs12_base64"),
                        config_manager.get("encoded_salt"))

    with open(config_manager.get("ldapTrustStoreFn"), "wb") as fw:
        fw.write(pkcs)


def reindent(text, num_spaces=1):
    text = [(num_spaces * " ") + line.lstrip() for line in text.splitlines()]
    text = "\n".join(text)
    return text


def generate_base64_contents(text, num_spaces=1):
    text = text.encode("base64").strip()
    if num_spaces > 0:
        text = reindent(text, num_spaces)
    return text


def oxtrust_config():
    # keeping redundent data in context of ldif ctx_data dict for now.
    # so that we can easily remove it from here
    ctx = {
        'inumOrg': r"{}".format(config_manager.get('inumOrg')),  # raw string
        'admin_email': config_manager.get('admin_email'),
        'inumAppliance': config_manager.get('inumAppliance'),
        'hostname': config_manager.get('hostname'),
        'shibJksFn': config_manager.get('shibJksFn'),
        'shibJksPass': config_manager.get('shibJksPass'),
        'jetty_base': config_manager.get('jetty_base'),
        'oxTrustConfigGeneration': config_manager.get('oxTrustConfigGeneration'),
        'encoded_shib_jks_pw': config_manager.get('encoded_shib_jks_pw'),
        'oxauth_client_id': config_manager.get('oxauth_client_id'),
        'oxauthClient_encoded_pw': config_manager.get('oxauthClient_encoded_pw'),
        'scim_rs_client_id': config_manager.get('scim_rs_client_id'),
        'scim_rs_client_jks_fn': config_manager.get('scim_rs_client_jks_fn'),
        'scim_rs_client_jks_pass_encoded': config_manager.get('scim_rs_client_jks_pass_encoded'),
        'passport_rs_client_id': config_manager.get('passport_rs_client_id'),
        'passport_rs_client_jks_fn': config_manager.get('passport_rs_client_jks_fn'),
        'passport_rs_client_jks_pass_encoded': config_manager.get('passport_rs_client_jks_pass_encoded'),
        'shibboleth_version': config_manager.get('shibboleth_version'),
        'idp3Folder': config_manager.get('idp3Folder'),
        'orgName': config_manager.get('orgName'),
        'ldap_site_binddn': config_manager.get('ldap_site_binddn'),
        'encoded_ox_ldap_pw': config_manager.get('encoded_ox_ldap_pw'),
        'ldap_hostname': config_manager.get('ldap_init_host'),
        'ldaps_port': config_manager.get('ldap_init_port'),
    }

    oxtrust_template_base = '/opt/templates/oxtrust'

    key_and_jsonfile_map = {
        'oxtrust_cache_refresh_base64': 'oxtrust-cache-refresh.json',
        'oxtrust_config_base64': 'oxtrust-config.json',
        'oxtrust_import_person_base64': 'oxtrust-import-person.json'
    }

    for key, json_file in key_and_jsonfile_map.iteritems():
        json_file_path = os.path.join(oxtrust_template_base, json_file)
        with open(json_file_path, 'r') as fp:
            config_manager.set(key, generate_base64_contents(fp.read() % ctx))


def sync_ldap_certs():
    """Gets opendj.crt, opendj.key, and opendj.pem
    """
    ssl_cert = decrypt_text(config_manager.get("ldap_ssl_cert"), config_manager.get("encoded_salt"))
    with open("/etc/certs/opendj.crt", "w") as fw:
        fw.write(ssl_cert)
    ssl_key = decrypt_text(config_manager.get("ldap_ssl_key"), config_manager.get("encoded_salt"))
    with open("/etc/certs/opendj.key", "w") as fw:
        fw.write(ssl_key)
    ssl_cacert = decrypt_text(config_manager.get("ldap_ssl_cacert"), config_manager.get("encoded_salt"))
    with open("/etc/certs/opendj.pem", "w") as fw:
        fw.write(ssl_cacert)


@contextmanager
def ds_context():
    """Ensures Directory Server are up and teardown at the end of the context.
    """

    cmd = "/opt/opendj/bin/status -D '{}' -j {} --connectTimeout 10000".format(
        config_manager.get("ldap_binddn"),
        DEFAULT_ADMIN_PW_PATH,
    )
    out, err, code = exec_cmd(cmd)
    running = out.startswith("Unable to connect to the server")

    if not running:
        exec_cmd("/opt/opendj/bin/start-ds")

    try:
        yield
    except Exception:
        raise
    finally:
        exec_cmd("/opt/opendj/bin/stop-ds --quiet")


def main():
    if not config_manager.get("ldap_peers"):
        migrate_ldap_servers()

    server = guess_host_addr()

    # the plain-text admin password is not saved in KV storage,
    # but we have the encoded one
    with open(DEFAULT_ADMIN_PW_PATH, "wb") as fw:
        admin_pw = decrypt_text(
            config_manager.get("encoded_ox_ldap_pw"),
            config_manager.get("encoded_salt"),
        )
        fw.write(admin_pw)

    logger.info("Syncing OpenDJ certs.")
    sync_ldap_certs()
    sync_ldap_pkcs12()

    logger.info("Checking certificate's Subject Alt Name (SAN)")
    san = get_certificate_san("/etc/certs/opendj.crt").replace("DNS:", "")

    if GLUU_CERT_ALT_NAME != san:
        logger.info("Re-generating OpenDJ certs with SAN support.")

        render_san_cnf(GLUU_CERT_ALT_NAME)
        regenerate_ldap_certs()

        salt = config_manager.get("encoded_salt")

        with open("/etc/certs/{}.pem".format("opendj"), "w") as fw:
            with open("/etc/certs/{}.crt".format("opendj")) as fr:
                ldap_ssl_cert = fr.read()

            with open("/etc/certs/{}.key".format("opendj")) as fr:
                ldap_ssl_key = fr.read()

            ldap_ssl_cacert = "".join([ldap_ssl_cert, ldap_ssl_key])
            fw.write(ldap_ssl_cacert)

            # update config
            config_manager.set("ldap_ssl_cert",
                               encrypt_text(ldap_ssl_cert, salt))
            config_manager.set("ldap_ssl_key",
                               encrypt_text(ldap_ssl_key, salt))
            config_manager.set("ldap_ssl_cacert",
                               encrypt_text(ldap_ssl_cacert, salt))

        regenerate_ldap_pkcs12()
        # update config
        with open(config_manager.get("ldapTrustStoreFn"), "rb") as fr:
            config_manager.set("ldap_pkcs12_base64",
                               encrypt_text(fr.read(), salt))

    if (os.path.isfile("/opt/opendj/config/config.ldif") and
            not os.path.isfile("/flag/ldap_upgraded")):
        logger.info("Trying to upgrade OpenDJ server")

        # backup old buildinfo
        exec_cmd("cp /opt/opendj/config/buildinfo /opt/opendj/config/buildinfo-3.0.0")
        _, err, retcode = exec_cmd("/opt/opendj/upgrade --acceptLicense")
        assert retcode == 0, "Failed to upgrade OpenDJ; reason={}".format(err)

        # backup current buildinfo
        exec_cmd("cp /opt/opendj/config/buildinfo /opt/opendj/config/buildinfo-3.0.1")
        exec_cmd("mkdir -p /flag")
        exec_cmd("touch /flag/ldap_upgraded")

    # install and configure Directory Server
    if not os.path.isfile("/opt/opendj/config/config.ldif"):
        install_opendj()

        with ds_context():
            run_dsjavaproperties()
            configure_opendj()

            with open("/opt/templates/index.json") as fr:
                data = json.load(fr)
                index_opendj("userRoot", data)
                index_opendj("site", data)

    if as_boolean(GLUU_LDAP_INIT):
        if not os.path.isfile("/flag/ldap_initialized"):
            config_manager.set('ldap_init_host', GLUU_LDAP_INIT_HOST)
            config_manager.set('ldap_init_port', GLUU_LDAP_INIT_PORT)
            config_manager.set("oxTrustConfigGeneration", as_boolean(GLUU_OXTRUST_CONFIG_GENERATION))

            oxtrust_config()
            render_ldif()

            with ds_context():
                import_ldif()

            exec_cmd("mkdir -p /flag")
            exec_cmd("touch /flag/ldap_initialized")
    else:
        with ds_context():
            for peer in get_ldap_peers():
                # skip if peer is current server
                if peer == server:
                    continue
                # if peer is not active, skip and try another one
                out, err, code = check_connection(peer, GLUU_LDAPS_PORT)
                if code != 0:
                    logger.warn("unable to connect to peer; reason={}".format(err))
                    continue
                # replicate from active server, no need to replicate from remaining peer
                replicate_from(peer, server)
                break

    # register current server for discovery
    register_ldap_peer(server)

    # post-installation cleanup
    for f in [DEFAULT_ADMIN_PW_PATH, "/opt/opendj/opendj-setup.properties"]:
        try:
            os.unlink(f)
        except OSError:
            pass


def render_san_cnf(name):
    ctx = {"alt_name": name}

    with open("/opt/templates/ssl/san.cnf") as fr:
        txt = fr.read() % ctx

        with open("/etc/ssl/san.cnf", "w")as fw:
            fw.write(txt)


def regenerate_ldap_certs():
    suffix = "opendj"
    passwd = decrypt_text(config_manager.get("encoded_ox_ldap_pw"),
                          config_manager.get("encoded_salt"))
    country_code = config_manager.get("country_code")
    state = config_manager.get("state")
    city = config_manager.get("city")
    org_name = config_manager.get("orgName")
    domain = config_manager.get("hostname")
    email = config_manager.get("admin_email")

    # create key with password
    _, err, retcode = exec_cmd(
        "openssl genrsa -des3 -out /etc/certs/{}.key.orig "
        "-passout pass:'{}' 2048".format(suffix, passwd))
    assert retcode == 0, "Failed to generate SSL key with password; reason={}".format(err)

    # create .key
    _, err, retcode = exec_cmd(
        "openssl rsa -in /etc/certs/{0}.key.orig "
        "-passin pass:'{1}' -out /etc/certs/{0}.key".format(suffix, passwd))
    assert retcode == 0, "Failed to generate SSL key; reason={}".format(err)

    # create .csr
    _, err, retcode = exec_cmd(
        "openssl req -new -key /etc/certs/{0}.key "
        "-out /etc/certs/{0}.csr "
        "-config /etc/ssl/san.cnf "
        "-subj /C='{1}'/ST='{2}'/L='{3}'/O='{4}'/CN='{5}'/emailAddress='{6}'".format(suffix, country_code, state, city, org_name, domain, email))
    assert retcode == 0, "Failed to generate SSL CSR; reason={}".format(err)

    # create .crt
    _, err, retcode = exec_cmd(
        "openssl x509 -req -days 365 -in /etc/certs/{0}.csr "
        "-extensions v3_req -extfile /etc/ssl/san.cnf "
        "-signkey /etc/certs/{0}.key -out /etc/certs/{0}.crt".format(suffix))
    assert retcode == 0, "Failed to generate SSL cert; reason={}".format(err)

    # return the paths
    return "/etc/certs/{}.crt".format(suffix), "/etc/certs/{}.key".format(suffix)


def regenerate_ldap_pkcs12():
    suffix = "opendj"
    passwd = config_manager.get("ldap_truststore_pass")
    hostname = config_manager.get("hostname")

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
    assert retcode == 0, "Failed to generate PKCS12 file; reason={}".format(err)


def encrypt_text(text, key):
    cipher = pyDes.triple_des(b"{}".format(key), pyDes.ECB,
                              padmode=pyDes.PAD_PKCS5)
    encrypted_text = cipher.encrypt(b"{}".format(text))
    return base64.b64encode(encrypted_text)


def get_certificate_san(certpath):
    openssl_proc = subprocess.Popen(
        shlex.split("openssl x509 -text -noout -in {}".format(certpath)),
        stdout=subprocess.PIPE,
    )
    grep_proc = subprocess.Popen(
        shlex.split("grep DNS"),
        stdout=subprocess.PIPE,
        stdin=openssl_proc.stdout,
    )
    san = grep_proc.communicate()[0]
    return san.strip()


if __name__ == "__main__":
    main()
