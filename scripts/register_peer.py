import json
import logging
import logging.config
import os

from pygluu.containerlib import get_manager
from pygluu.containerlib.utils import as_boolean
from pygluu.containerlib.utils import exec_cmd

from settings import LOGGING_CONFIG
from utils import guess_serf_addr
from utils import get_serf_peers
from utils import register_serf_peer

logging.config.dictConfig(LOGGING_CONFIG)
logger = logging.getLogger("ldap_peer")


def peers_from_file():
    file_ = "/etc/gluu/conf/serf-peers-static.json"
    logger.info(f"Loading initial Serf peers from {file_}")
    peers = []
    try:
        with open(file_) as f:
            peers = json.loads(f.read())
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logger.warning(f"Unable to load initial Serf peers from {file_}; reason={exc}")
    return peers


def main():
    manager = get_manager()

    for addr in peers_from_file():
        register_serf_peer(manager, addr)

    addr = guess_serf_addr()
    register_serf_peer(manager, addr)

    mcast = as_boolean(os.environ.get("GLUU_SERF_MULTICAST_DISCOVER", False))
    if mcast:
        # join Serf cluster using multicast (no extra code needed)
        return

    # join Serf cluster manually
    peers = " ".join(get_serf_peers(manager))
    out, err, code = exec_cmd(f"serf join {peers}")
    err = err or out

    if code != 0:
        logger.warning(f"Unable to join Serf cluster; reason={err}")


if __name__ == "__main__":
    main()
