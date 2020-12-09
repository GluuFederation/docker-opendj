import json
import os
import socket


def guess_serf_addr():
    addr = os.environ.get("GLUU_SERF_ADVERTISE_ADDR", "")
    if not addr:
        addr = f"{socket.getfqdn()}:7946"
    return addr


def get_serf_peers(manager):
    return json.loads(manager.config.get("serf_peers", "[]"))


def register_serf_peer(manager, addr):
    peers = set(get_serf_peers(manager))
    peers.add(addr)
    manager.config.set("serf_peers", list(peers))


def deregister_serf_peer(manager, addr):
    peers = set(get_serf_peers(manager))

    try:
        peers.remove(addr)
        manager.config.set("serf_peers", list(peers))
    except KeyError:
        pass
