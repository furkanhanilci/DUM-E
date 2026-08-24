"""The address this deployment can be reached at.

Everything that hands somebody a way in — an invite link, a pairing QR, the
relay's own idea of where it is — has to state an address. Three of them stated
`localhost`, which is correct for the machine running them and wrong for every
other device: on a phone, `localhost` is the phone. A pairing QR carrying it
fails with "could not reach the pairing relay", and the failure names the relay
rather than the address, so it reads as the relay being down.

The address is derived rather than configured, because a configured one goes
stale the first time DHCP hands out a different lease and nobody notices until
somebody tries to join.
"""
from __future__ import annotations

import os
import socket

RELAY_PORT = 3000


def lan_address() -> str:
    """This host's address on the local network.

    Found by asking the routing table which source address would be used to
    reach the outside world — not by resolving the hostname, which on this
    machine answers 127.0.1.1 and would reintroduce exactly the bug this
    exists to fix. No packet is sent; connect() on a UDP socket only selects
    a route.
    """
    override = os.environ.get("AETHRION_HOST", "").strip()
    if override:
        return override
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("1.1.1.1", 80))
        return probe.getsockname()[0]
    except OSError:
        # No route out. A machine with no network still has a working
        # deployment for whoever is sitting at it, and saying so beats
        # inventing an address nothing answers on.
        return "127.0.0.1"
    finally:
        probe.close()


def relay_ws(host: str | None = None) -> str:
    return f"ws://{host or lan_address()}:{RELAY_PORT}"


def relay_http(host: str | None = None) -> str:
    return f"http://{host or lan_address()}:{RELAY_PORT}"


def is_reachable_by_other_devices(url: str) -> bool:
    """Whether an address means anything to a device that is not this one."""
    return not any(token in url for token in
                   ("localhost", "127.0.0.1", "0.0.0.0", "::1"))
