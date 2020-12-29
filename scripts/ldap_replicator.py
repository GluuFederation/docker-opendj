import contextlib
import json
import logging
import logging.config
import os
import sys
import time
from collections import defaultdict

from pygluu.containerlib import get_manager
from pygluu.containerlib.utils import exec_cmd
from pygluu.containerlib.utils import as_boolean

from settings import LOGGING_CONFIG
from utils import guess_serf_addr

DEFAULT_ADMIN_PW_PATH = "/opt/opendj/.pw"

logging.config.dictConfig(LOGGING_CONFIG)
logger = logging.getLogger("ldap_replicator")

manager = get_manager()


@contextlib.contextmanager
def admin_password_bound(manager, password_file=DEFAULT_ADMIN_PW_PATH):
    if not os.path.isfile(password_file):
        manager.secret.to_file(
            "encoded_ox_ldap_pw", password_file, decode=True,
        )

    try:
        yield password_file
    except Exception:
        raise
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(password_file)


def replicate_from(peer, server, base_dn):
    """Configure replication between 2 LDAP servers.
    """
    ldap_binddn = manager.config.get("ldap_binddn")

    with admin_password_bound(manager) as password_file:
        # enable replication for specific backend
        logger.info(f"Enabling OpenDJ replication of {base_dn} between {peer['name']} and {server['name']}.")

        enable_cmd = " ".join([
            "/opt/opendj/bin/dsreplication",
            "enable",
            f"--host1 {peer['name']}",
            f"--port1 {peer['tags']['admin_port']}",
            f"--bindDN1 '{ldap_binddn}'",
            f"--bindPasswordFile1 {password_file}",
            f"--replicationPort1 {peer['tags']['replication_port']}",
            "--secureReplication1",
            f"--host2 {server['name']}",
            f"--port2 {server['tags']['admin_port']}",
            f"--bindDN2 '{ldap_binddn}'",
            f"--bindPasswordFile2 {password_file}",
            f"--replicationPort2 {server['tags']['replication_port']}",
            "--secureReplication2",
            "--adminUID admin",
            f"--adminPasswordFile {password_file}",
            f"--baseDN '{base_dn}'",
            "-X",
            "-n",
            "-Q",
        ])
        # logger.info(enable_cmd)
        out, err, code = exec_cmd(enable_cmd)
        if code:
            err = err or out
            logger.warning(err.decode().strip())

        # initialize replication for specific backend
        logger.info(f"Initializing OpenDJ replication of {base_dn} between {peer['name']} and {server['name']}.")

        init_cmd = " ".join([
            "/opt/opendj/bin/dsreplication",
            "initialize",
            f"--baseDN '{base_dn}'",
            "--adminUID admin",
            f"--adminPasswordFile {password_file}",
            f"--hostSource {peer['name']}",
            f"--portSource {peer['tags']['admin_port']}",
            f"--hostDestination {server['name']}",
            f"--portDestination {server['tags']['admin_port']}",
            "-X",
            "-n",
            "-Q",
        ])
        # logger.info(init_cmd)
        out, err, code = exec_cmd(init_cmd)
        if code:
            err = err or out
            logger.warning(err.decode().strip())


def check_required_entry(host, port, user, base_dn):
    """Checks if entry is exist.
    """
    if base_dn == "o=metric":
        dn = "ou=statistic,o=metric"
    elif base_dn == "o=site":
        dn = "ou=cache-refresh,o=site"
    else:
        client_id = manager.config.get('oxauth_client_id')
        dn = f"inum={client_id},ou=clients,{base_dn}"

    with admin_password_bound(manager) as password_file:
        cmd = " ".join([
            "/opt/opendj/bin/ldapsearch",
            f"--hostname {host}",
            f"--port {port}",
            f"--baseDN '{dn}'",
            f"--bindDN '{user}'",
            f"--bindPasswordFile {password_file}",
            "-Z",
            "-X",
            "--searchScope base",
            "'(objectClass=*)' 1.1",
        ])
        out, err, code = exec_cmd(cmd)
        return out.strip(), err.strip(), code


def get_ldap_status(bind_dn):
    with admin_password_bound(manager) as password_file:
        cmd = f"/opt/opendj/bin/status -D '{bind_dn}' --bindPasswordFile {password_file} --connectTimeout 10000 -X"
        out, err, code = exec_cmd(cmd)
        return out.strip(), err.strip(), code


def get_datasources(user, interval, non_repl_only=True):
    """Get backends.
    """
    # get status from LDAP server
    while True:
        out, _, code = get_ldap_status(user)
        if code != 0:
            logger.warning(
                f"Unable to get status from LDAP server; reason={out.decode()}; "
                f"retrying in {interval} seconds"
            )
            time.sleep(interval)
            continue
        break

    sources = out.decode().splitlines()
    src_index = 0

    # given sources as text:
    #
    #            --- Connection Handlers ---
    #    Address:Port : Protocol             : State
    #    -------------:----------------------:---------
    #    8989         : Replication (secure) : Enabled
    #    0.0.0.0:1636 : LDAPS                : Enabled
    #
    #            --- Data Sources ---
    #    Base DN:                      o=gluu
    #    Backend ID:                   userRoot
    #    Entries:                      174
    #    Replication:                  Enabled
    #    Missing Changes:              <not available>
    #    Age of Oldest Missing Change: <not available>
    #
    # parse the text to get backends and their status
    for index, line in enumerate(sources):
        if line.find("--- Data Sources ---") > 0:
            src_index = index + 1
            break

    datasources = defaultdict(dict)
    dn = ""

    # the result (if found) would be in the following structure, for example:
    #
    #    {
    #        "o=gluu": {"replicated": True, "entries": 1},
    #        "o=site": {"replicated": False, "entries": 0},
    #        "o=metric": {"replicated": True, "entries": 0},
    #    }
    for src in sources[src_index:]:
        if src.startswith("Base DN"):
            dn = src.split(":")[-1].strip()
            datasources[dn] = {}

        if src.startswith("Replication"):
            repl = src.split(":")[-1].strip()
            status = bool(repl.lower() == "enabled")
            datasources[dn]["repl_enabled"] = status

        if src.startswith("Entries"):
            entry_num = src.split(":")[-1].strip()
            datasources[dn]["entries"] = int(entry_num)

    datasources = {
        k: v for k, v in datasources.items()
        if k in ("o=gluu", "o=site", "o=metric")
    }

    if non_repl_only:
        datasources = {
            k: v for k, v in datasources.items()
            if any([v["repl_enabled"] is False,
                    v["entries"] == 0])
        }
    return datasources


def get_repl_interval():
    try:
        interval = int(os.environ.get("GLUU_LDAP_REPL_CHECK_INTERVAL", 10))
        if interval < 1:
            interval = 10
    except TypeError:
        interval = 10
    return interval


def get_repl_max_retries():
    try:
        max_retries = int(os.environ.get("GLUU_LDAP_REPL_MAX_RETRIES", 30))
        if max_retries < 1:
            max_retries = 30
    except TypeError:
        max_retries = 30
    return max_retries


def peers_from_serf_membership():
    out, err, code = exec_cmd("serf members -tag role=ldap -status=alive -format json")
    if code != 0:
        err = err or out
        logger.warning(f"Unable to get peers; reason={err.decode()}")
        return []

    members = json.loads(out.decode())["members"]
    return [
        {
            "name": member["name"],
            "addr": member["addr"],
            "tags": member["tags"],
        }
        for member in members
    ]


def get_server_info():
    server = {}
    attempt = 1

    logger.info("Getting current server info")

    while attempt <= 3:
        out, err, code = exec_cmd("serf info -format json")

        if code != 0:
            err = err or out
            logger.warning(f"Unable to get current server info from Serf; reason={err.decode()} ... retrying in 10 seconds")
        else:
            try:
                info = json.loads(out.decode())
                server = {
                    "name": info["agent"]["name"],
                    "addr": guess_serf_addr(),
                    "tags": info["tags"],
                }
                return server
            except json.decoder.JSONDecodeError as exc:
                logger.warning(f"Unable to decode JSON output from Serf command; reason={exc} ... retrying in 10 seconds")

        # bump the counter
        time.sleep(10)
        attempt += 1

    if not server:
        logger.error("Unable to get info for current server after 3 attempts ... exiting")
        sys.exit(1)

    # return the server info
    return server


def main():
    auto_repl = as_boolean(os.environ.get("GLUU_LDAP_AUTO_REPLICATE", True))
    if not auto_repl:
        logger.warning("Auto replication is disabled; skipping replication check")
        return

    server = get_server_info()
    ldap_user = manager.config.get("ldap_binddn")

    interval = get_repl_interval()
    max_retries = get_repl_max_retries()
    retry = 0

    while retry < max_retries:
        logger.info(f"Checking replicated backends (attempt {retry + 1})")

        peers = peers_from_serf_membership()

        for peer in peers:
            if peer["name"] == server["name"]:
                continue

            datasources = get_datasources(ldap_user, interval)

            # if there's no backend that need to be replicated, skip the rest of the process;
            # note, in some cases the Generation ID will be different due to mismatched data structure
            # to fix this issue we can re-init replication manually; please refer to
            # https://backstage.forgerock.com/knowledge/kb/article/a36616593 for details
            if not datasources:
                logger.info("All required backends have been replicated")
                return

            logger.info(f"Found peer at {peer['name']}")
            for dn, _ in datasources.items():
                _, err, code = check_required_entry(
                    peer["name"], peer["tags"]["ldaps_port"], ldap_user, dn,
                )
                if code != 0:
                    logger.warning(
                        f"Unable to get required entry at LDAP server {peer['name']}; reason={err.decode()}"
                    )
                    continue

                # replicate from server that has data; note: can't assume the
                # whole replication process is succeed, hence subsequence checks
                # will be executed
                replicate_from(peer, server, dn)

        # delay between next check
        time.sleep(interval)
        retry += 1


if __name__ == "__main__":
    main()
