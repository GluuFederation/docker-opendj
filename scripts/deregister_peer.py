import contextlib
# import json
import logging.config
import os

from pygluu.containerlib import get_manager
from pygluu.containerlib.utils import exec_cmd

from settings import LOGGING_CONFIG
from utils import deregister_serf_peer
from utils import guess_serf_addr

DEFAULT_ADMIN_PW_PATH = "/opt/opendj/.pw"

logging.config.dictConfig(LOGGING_CONFIG)
logger = logging.getLogger("ldap_peer")


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


def main():
    manager = get_manager()
    addr = guess_serf_addr()
    host = addr.split(":")[0]
    admin_port = os.environ.get("GLUU_LDAP_ADVERTISE_ADMIN_PORT", "4444")

    deregister_serf_peer(manager, addr)

    with admin_password_bound(manager) as password_file:
        cmd = " ".join([
            "/opt/opendj/bin/dsreplication",
            "disable",
            "--disableAll",
            f"--port {admin_port}",
            f"--hostname {host}",
            "--adminUID admin",
            f"--adminPasswordFile {password_file}",
            "-X",
            "-n",
            "-Q",
        ])
        out, err, code = exec_cmd(cmd)

        if code:
            err = err or out
            logger.warning(f"Unable to disable replication for current server; reason={err.decode()}")


if __name__ == "__main__":
    main()
