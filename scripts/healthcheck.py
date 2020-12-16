import os
import sys

import ldap3
from pygluu.containerlib import get_manager
from pygluu.containerlib.utils import decode_text

from ldap_replicator import peers_from_serf_membership


def get_ldap_entries(host, user, password):
    persistence_type = os.environ.get("GLUU_PERSISTENCE_TYPE", "ldap")
    ldap_mapping = os.environ.get("GLUU_PERSISTENCE_LDAP_MAPPING", "default")
    ldap_server = ldap3.Server(host, 1636, use_ssl=True)

    # a minimum service stack is having oxTrust, hence check whether entry
    # for oxTrust exists in LDAP
    default_search = (
        "ou=oxtrust,ou=configuration,o=gluu",
        "(objectClass=oxTrustConfiguration)",
    )

    if persistence_type == "hybrid":
        # `cache` and `token` mapping only have base entries
        search_mapping = {
            "default": default_search,
            "user": ("inum=60B7,ou=groups,o=gluu", "(objectClass=gluuGroup)"),
            "site": ("ou=cache-refresh,o=site", "(ou=people)"),
            "cache": ("o=gluu", "(objectClass=gluuOrganization)"),
            "token": ("ou=tokens,o=gluu", "(ou=tokens)"),
            "session": ("ou=sessions,o=gluu", "(ou=sessions)"),
        }
        search = search_mapping[ldap_mapping]
    else:
        search = default_search

    with ldap3.Connection(ldap_server, user, password) as conn:
        conn.search(
            search_base=search[0],
            search_filter=search[1],
            search_scope=ldap3.SUBTREE,
            attributes=["objectClass"],
            size_limit=1,
        )
        return conn.entries


def main():
    # check how many member in ldap cluster,
    peers_num = len(peers_from_serf_membership())

    if peers_num == 0:
        sys.exit(1)
    elif peers_num == 1:
        # if there's only 1 alive member, mark the server as ready to allow
        # data injection to persistence
        sys.exit(0)
    else:
        # if there are more than 1 instances, determine the server readiness by
        # checking entries in persistence
        manager = get_manager()
        host = "localhost:1636"
        user = manager.config.get("ldap_binddn")
        password = decode_text(
            manager.secret.get("encoded_ox_ldap_pw"),
            manager.secret.get("encoded_salt")
        )

        result = get_ldap_entries(host, user, password)
        if result:
            sys.exit(0)
        else:
            sys.exit(1)


if __name__ == "__main__":
    main()
