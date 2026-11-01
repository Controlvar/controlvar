"""The proxies whose ``X-Forwarded-For`` the API's server believes.

``backend/docker-entrypoint.sh`` starts uvicorn with
``--forwarded-allow-ips=<list>``, and the list is what ``python -m
backend.app.core.proxy_trust`` prints on its one line of standard output:

* ``127.0.0.1`` and ``::1``;
* every network this container is directly attached to, read from
  ``/proc/net/route`` (IPv4) and ``/proc/net/if_inet6`` (IPv6) when it starts.
  In Docker Compose that is the project network, so the dashboard container
  and the gateway a TLS proxy on the same host arrives from are on it;
* every entry of ``FORWARDED_ALLOW_IPS``, which adds to the two above and
  cannot take anything away from them.

On standard error it prints one line saying what it found, beginning
``[entrypoint] trusted proxies`` (or ``[entrypoint] WARNING`` when neither
``/proc`` file could be read: the list is then loopback plus the entries).

``FORWARDED_ALLOW_IPS`` is a comma-separated list of IP addresses
(``10.0.3.10``, ``fd00::5``) and networks written with their host bits zero
(``10.0.0.0/16``, ``fd00::/8``). Spaces around an entry and empty entries are
dropped; unset, empty and blank all add nothing. Anything else is refused by
``entry_problems``, one message per entry, numbered from 1 over the raw comma
split: uvicorn would keep such an entry as a literal string that no peer
address ever equals, so the proxy it was meant to name would silently not be
believed. ``*`` is refused in every environment (this list is added to, and
``*`` cannot be), and ``0.0.0.0/0`` and ``::/0`` in staging and production
(``every_address_problems``). The start-up check
(``backend/app/core/preflight.py``) applies both first; this module's own
entry point refuses the first kind with exit 78 too, should it ever run
without the check before it.

The own-network rules follow the kernel's printers: ``fib_route_seq_show`` and
``fib_flag_trans`` (net/ipv4/fib_trie.c) print each route of the main table as
host-order ``%08X`` addresses, so they are decoded in this machine's byte
order; ``if6_seq_show`` (net/ipv6/addrconf.c) prints the prefix length, the
scope and the flags in hexadecimal (``%02x``: a /64 is ``40``).

Standard library only, and it never imports ``backend.app.core.config``.
"""

from __future__ import annotations

import ipaddress
import os
import string
import sys
from typing import List, Mapping, Optional, Sequence, TextIO, Tuple, Union

#: The setting this module reads.
SETTING = "FORWARDED_ALLOW_IPS"

#: ``EX_CONFIG`` from sysexits.h, as the start-up check exits.
EX_CONFIG = 78

#: Always believed, first in the list.
LOOPBACK = ("127.0.0.1", "::1")

ROUTE = "/proc/net/route-planted"
IF_INET6 = "/proc/net/if_inet6-planted"

#: Read at most this much of each ``/proc`` file.
MAX_BYTES = 64 * 1024
#: Keep at most this many of the container's own networks.
MAX_NETWORKS = 32
#: A refused entry is echoed up to this many characters.
ECHO_LIMIT = 64

PREFIX = "[entrypoint]"

# /proc/net/route flags (include/uapi/linux/route.h).
RTF_UP = 0x0001
RTF_GATEWAY = 0x0002
RTF_REJECT = 0x0200
# /proc/net/if_inet6 flags (include/uapi/linux/if_addr.h) and scope.
IFA_F_DADFAILED = 0x08
SCOPE_GLOBAL = 0x00

#: IPv4 link-local, left out as IPv6 link-local is (by its scope).
LINK_LOCAL_V4 = ipaddress.IPv4Network("169.254.0.0/16")

Network = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]
Entry = Union[
    ipaddress.IPv4Address,
    ipaddress.IPv6Address,
    ipaddress.IPv4Network,
    ipaddress.IPv6Network,
]


# ---------------------------------------------------------------------------
# FORWARDED_ALLOW_IPS
# ---------------------------------------------------------------------------


def _parse(entry: str) -> Optional[Entry]:
    """*entry* as uvicorn reads one: a network if it has a ``/`` (strict, so
    no host bits), an address otherwise. None for anything else."""
    try:
        if "/" in entry:
            return ipaddress.ip_network(entry)
        return ipaddress.ip_address(entry)
    except ValueError:
        return None


def _echo(entry: str) -> str:
    shown = entry if len(entry) <= ECHO_LIMIT else entry[:ECHO_LIMIT] + "..."
    return repr(shown)


def _pieces(raw: Optional[str]):
    """(1-based position over the raw comma split, trimmed entry), skipping
    the empty ones."""
    for position, piece in enumerate((raw or "").split(","), start=1):
        entry = piece.strip()
        if entry:
            yield position, entry


def _entry_problem(position: int, entry: str) -> Optional[str]:
    """The message for one trimmed, non-empty *entry*, or None."""
    if entry == "*":
        return (
            f"{SETTING} entry {position} is '*', which is not accepted: this "
            "setting adds proxies to the ones the API always believes, and * "
            "would believe every connection. Name your proxy's addresses or "
            "networks."
        )
    if ";" in entry or any(character.isspace() for character in entry):
        return (
            f"{SETTING} entry {position} ({_echo(entry)}) is not an IP address "
            "or a network. Separate entries with commas, not spaces or semicolons."
        )
    parsed = _parse(entry)
    if parsed is None:
        try:
            interface = ipaddress.ip_interface(entry) if "/" in entry else None
        except ValueError:
            interface = None
        if interface is not None:
            return (
                f"{SETTING} entry {position} ({_echo(entry)}) is an address with "
                "a mask, which matches no proxy. Write the network, "
                f"{interface.network}, or the address alone, {interface.ip}."
            )
        return (
            f"{SETTING} entry {position} ({_echo(entry)}) is not an IP address "
            "or a network, so it matches no proxy. Write addresses (10.0.0.5) or "
            "networks (10.0.0.0/16), separated by commas."
        )
    return None


def entry_problems(raw: Optional[str]) -> List[str]:
    """One message per entry of *raw* refused in every environment; empty
    when it is usable."""
    problems = (_entry_problem(position, entry) for position, entry in _pieces(raw))
    return [problem for problem in problems if problem]


def every_address_problems(raw: Optional[str]) -> List[str]:
    """One message per entry of *raw* that covers every address
    (``0.0.0.0/0``, ``::/0``): refused in staging and production."""
    problems: List[str] = []
    for position, entry in _pieces(raw):
        parsed = _parse(entry)
        if (
            isinstance(parsed, (ipaddress.IPv4Network, ipaddress.IPv6Network))
            and parsed.prefixlen == 0
        ):
            problems.append(
                f"{SETTING} entry {position} ({_echo(entry)}) covers every "
                "address and is refused in staging and production. Name your "
                "proxy's addresses or networks."
            )
    return problems


def entries(raw: Optional[str]) -> List[Entry]:
    """The entries of *raw*, parsed, in order. ValueError if any is refused."""
    problems = entry_problems(raw)
    if problems:
        raise ValueError("\n".join(problems))
    parsed = (_parse(entry) for _, entry in _pieces(raw))
    return [entry for entry in parsed if entry is not None]


# ---------------------------------------------------------------------------
# The container's own networks
# ---------------------------------------------------------------------------


def _hex(field: str, minimum: int, maximum: int) -> int:
    """*field* as hexadecimal digits only, *minimum* to *maximum* of them.

    ``int(x, 16)`` alone would also take a sign, ``0x``, underscores and
    surrounding space; the kernel prints none of those.
    """
    if not minimum <= len(field) <= maximum or not all(
        character in string.hexdigits for character in field
    ):
        raise ValueError(f"not {minimum}-{maximum} hex digits: {field!r}")
    return int(field, 16)


def _route_network(line: str, byteorder: str) -> Optional[ipaddress.IPv4Network]:
    """The network one ``/proc/net/route`` row is directly attached to, or None.

    Columns: Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window
    IRTT. ValueError for a row that does not parse (the header among them).
    """
    fields = line.split()
    if len(fields) < 8:
        raise ValueError("too few columns")
    interface = fields[0]
    destination = _hex(fields[1], 8, 8)
    gateway = _hex(fields[2], 8, 8)
    flags = _hex(fields[3], 4, 4)
    mask = _hex(fields[7], 8, 8)
    # `*`: no device -- an unreachable, prohibit or blackhole route.
    if interface in ("*", "lo"):
        return None
    if gateway != 0 or flags & RTF_GATEWAY:
        return None
    if not flags & RTF_UP:
        return None
    if flags & RTF_REJECT:
        return None
    if destination == 0 or mask == 0:
        return None
    address = ipaddress.IPv4Address(destination.to_bytes(4, byteorder))
    netmask = ipaddress.IPv4Address(mask.to_bytes(4, byteorder))
    network = ipaddress.IPv4Network(f"{address}/{netmask}")
    if network.subnet_of(LINK_LOCAL_V4):
        return None
    return network


def networks_from_route(
    text: str, *, byteorder: str = sys.byteorder
) -> List[ipaddress.IPv4Network]:
    """The directly attached IPv4 networks in *text*, a ``/proc/net/route``.

    A row counts when it has a device other than ``lo``, no gateway, RTF_UP
    and neither RTF_GATEWAY nor RTF_REJECT, a destination and a mask that are
    not zero (so not the default route), and is not link-local. A row that
    does not parse is skipped.
    """
    networks: List[ipaddress.IPv4Network] = []
    for line in text.splitlines():
        try:
            network = _route_network(line, byteorder)
        except ValueError:
            continue
        if network is not None:
            networks.append(network)
    return networks


def parse_if_inet6_row(line: str) -> Tuple[ipaddress.IPv6Interface, int, int, str]:
    """One ``/proc/net/if_inet6`` row: (interface address, scope, flags, name).

    The kernel prints ``%pi6 %02x %02x %02x %02x %8s``: the address as 32 hex
    digits, then the interface index, the prefix length, the scope and the
    flags, each in HEXADECIMAL, then the interface name. ValueError for a row
    that does not parse.
    """
    fields = line.split()
    if len(fields) != 6:
        raise ValueError("not six columns")
    address, index, prefix_length, scope, flags, name = fields
    _hex(address, 32, 32)
    _hex(index, 2, 8)
    interface = ipaddress.IPv6Interface(
        (ipaddress.IPv6Address(bytes.fromhex(address)), _hex(prefix_length, 2, 2))
    )
    return interface, _hex(scope, 2, 2), _hex(flags, 2, 2), name


def networks_from_if_inet6(text: str) -> List[ipaddress.IPv6Network]:
    """The IPv6 networks of the global addresses in *text*.

    A row counts when its scope is global (which includes unique local
    addresses), its interface is not ``lo``, duplicate address detection did
    not fail, and its prefix length is not zero. Each is the network with the
    host bits zero: ``fd00:1::5/64`` is ``fd00:1::/64``, which uvicorn reads
    as a network rather than a literal. A row that does not parse is skipped.
    """
    networks: List[ipaddress.IPv6Network] = []
    for line in text.splitlines():
        try:
            interface, scope, flags, name = parse_if_inet6_row(line)
        except ValueError:
            continue
        if name == "lo" or scope != SCOPE_GLOBAL:
            continue
        if flags & IFA_F_DADFAILED:
            continue
        if interface.network.prefixlen == 0:
            continue
        networks.append(interface.network)
    return networks


def _read(path: str) -> Optional[str]:
    """At most MAX_BYTES of *path*, cut at its last complete line; None when
    it cannot be read."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_BYTES + 1)
    except OSError:
        return None
    if len(data) > MAX_BYTES:
        data = data[:MAX_BYTES]
        data = data[: data.rfind(b"\n") + 1]
    return data.decode("ascii", errors="replace")


def own_networks(
    route_text: str, if_inet6_text: str, *, byteorder: str = sys.byteorder
) -> List[Network]:
    """The IPv4 then the IPv6 networks, each once, at most MAX_NETWORKS."""
    found: List[Network] = [
        *networks_from_route(route_text, byteorder=byteorder),
        *networks_from_if_inet6(if_inet6_text),
    ]
    unique = list(dict.fromkeys(found))
    return unique[:MAX_NETWORKS]


def read_own_networks(
    route: str = ROUTE, if_inet6: str = IF_INET6
) -> Optional[List[Network]]:
    """This container's networks, or None when neither file can be read."""
    route_text = _read(route)
    if_inet6_text = _read(if_inet6)
    if route_text is None and if_inet6_text is None:
        return None
    return own_networks(route_text or "", if_inet6_text or "")


# ---------------------------------------------------------------------------
# The list and the start-up line
# ---------------------------------------------------------------------------


def build(raw: Optional[str], own: Sequence[Network]) -> str:
    """The value for ``--forwarded-allow-ips``: loopback, *own*, then the
    entries of *raw*, each once. ValueError if an entry is refused."""
    items = [
        *LOOPBACK,
        *(str(network) for network in own),
        *(str(entry) for entry in entries(raw)),
    ]
    return ",".join(dict.fromkeys(items))


def start_line(raw: Optional[str], own: Optional[Sequence[Network]]) -> str:
    """What the entrypoint logs about the list; *own* None when ``/proc``
    could not be read."""
    listed = " ".join(dict.fromkeys(str(entry) for entry in entries(raw)))
    setting = f"{SETTING}: {listed or '(none)'}"
    loopback = " ".join(LOOPBACK)
    if own is None:
        return (
            f"{PREFIX} WARNING: this container's networks could not be read from "
            f"{ROUTE} or {IF_INET6}; trusted proxies: {loopback}; {setting}"
        )
    networks = " ".join(str(network) for network in own) or "(none)"
    return (
        f"{PREFIX} trusted proxies: {loopback}; this container's networks: "
        f"{networks}; {setting}"
    )


def main(
    environ: Optional[Mapping[str, str]] = None,
    *,
    route: str = ROUTE,
    if_inet6: str = IF_INET6,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
) -> int:
    """Print the list on *stdout* and the start-up line on *stderr*; 0.

    A refused entry prints its message instead and returns EX_CONFIG.
    """
    environ = os.environ if environ is None else environ
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    raw = environ.get(SETTING)
    problems = entry_problems(raw)
    if problems:
        for problem in problems:
            stderr.write(f"{PREFIX} {problem}\n")
        stderr.flush()
        return EX_CONFIG
    own = read_own_networks(route, if_inet6)
    stderr.write(start_line(raw, own) + "\n")
    stderr.flush()
    stdout.write(build(raw, own or ()) + "\n")
    stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
