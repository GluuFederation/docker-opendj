import os
import sys

from jans.pycloudlib import get_manager
from jans.pycloudlib.persistence.ldap import LdapClient

from ldap_replicator import peers_from_serf_membership


def get_ldap_entries(manager):
    persistence_type = os.environ.get("CN_PERSISTENCE_TYPE", "ldap")
    ldap_mapping = os.environ.get("CN_PERSISTENCE_LDAP_MAPPING", "default")

    # a minimum service stack is having config-api client
    jca_client_id = manager.config.get("jca_client_id")
    default_search = (
         f"inum={jca_client_id},ou=clients,o=jans",
        "(objectClass=jansClnt)",
    )

    if persistence_type == "hybrid":
        # `cache` and `token` mapping only have base entries
        search_mapping = {
            "default": default_search,
            "user": ("inum=60B7,ou=groups,o=jans", "(objectClass=jansGrp)"),
            "site": ("ou=cache-refresh,o=site", "(ou=people)"),
            "cache": ("o=jans", "(ou=cache)"),
            "token": ("ou=tokens,o=jans", "(ou=tokens)"),
            "session": ("ou=sessions,o=jans", "(ou=sessions)"),
        }
        search = search_mapping[ldap_mapping]
    else:
        search = default_search

    # connect to localhost
    client = LdapClient(manager, host="localhost")
    return client.get(search[0], filter_=search[1], attributes=["objectClass"])


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
        result = get_ldap_entries(manager)
        if result:
            sys.exit(0)
        else:
            sys.exit(1)


if __name__ == "__main__":
    main()
