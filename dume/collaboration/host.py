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
import subprocess

RELAY_PORT = 3000


def lan_address() -> str:
    """This host's address on the local network.

    Found by asking the routing table which source address would be used to
    reach the outside world — not by resolving the hostname, which on this
    machine answers 127.0.1.1 and would reintroduce exactly the bug this
    exists to fix. No packet is sent; connect() on a UDP socket only selects
    a route.
    """
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


def tailscale_address() -> str | None:
    """This host's address on the tailnet, if it is on one.

    Preferred over the LAN address because it is stable and reachable from
    outside the building. The relay binds its community to exactly one host and
    treats that host as the authoritative selector, so the address has to be
    one that keeps meaning the same thing — and a DHCP lease does not.
    """
    try:
        result = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                                text=True, timeout=4)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = (result.stdout or "").strip().splitlines()
    return lines[0].strip() if result.returncode == 0 and lines else None


def address() -> str:
    """The one address everything states: the relay's config, the desktop's
    default, an invite, a pairing QR.

    One, because two addresses would be two communities — the relay creates a
    community per host — and the messages would live in whichever was used
    first. Moving between them is a single-row change rather than a migration,
    but it is a change somebody has to make deliberately.
    """
    override = os.environ.get("AETHRIONIS_HOST", "").strip()
    if override:
        return override
    return tailscale_address() or lan_address()


def relay_ws(host: str | None = None) -> str:
    return f"ws://{host or address()}:{RELAY_PORT}"


def relay_http(host: str | None = None) -> str:
    return f"http://{host or address()}:{RELAY_PORT}"


def is_reachable_by_other_devices(url: str) -> bool:
    """Whether an address means anything to a device that is not this one."""
    return not any(token in url for token in
                   ("localhost", "127.0.0.1", "0.0.0.0", "::1"))
