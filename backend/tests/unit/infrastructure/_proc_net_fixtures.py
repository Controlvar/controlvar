"""``/proc/net/route`` and ``/proc/net/if_inet6`` as the kernel prints them.

``backend/app/core/proxy_trust.py`` reads the API container's own networks
from these two files. The text here follows the kernel's own printers:

* ``fib_route_seq_show`` and ``fib_flag_trans`` (net/ipv4/fib_trie.c): a header,
  then one row per route of the main table,
  ``%s\\t%08X\\t%08X\\t%04X\\t%d\\t%u\\t%d\\t%08X\\t%d\\t%u\\t%u`` (Iface,
  Destination, Gateway, Flags, RefCnt, Use, Metric, Mask, MTU, Window, IRTT),
  padded with spaces to 127 characters. The addresses are the 32-bit values in
  HOST byte order, so on the little-endian machines these fixtures stand for
  172.19.0.0 prints as ``000013AC``. ``fib_flag_trans`` sets RTF_UP (0x1) on
  every row, RTF_GATEWAY (0x2) when there is a gateway, RTF_HOST (0x4) for a
  /32, and RTF_REJECT (0x200) for unreachable and prohibit routes; a route
  with no device (unreachable, prohibit, blackhole) prints ``*`` as its Iface.
* ``if6_seq_show`` (net/ipv6/addrconf.c): ``%pi6 %02x %02x %02x %02x %8s`` --
  the address as 32 hex digits, then the interface index, the prefix length,
  the scope and the flags, all four in HEXADECIMAL (a /64 prints ``40``,
  ::1/128 ``80``), then the interface name right-aligned in eight columns.

The topologies are written from the kernel's formats and from how each
runtime is known to lay out a container's network; none was captured from a
running container here. The Linux CI runner's real files are read by
``test_proxy_trust.py::TestTheRunnersOwnFiles``.
"""

from __future__ import annotations

ROUTE_HEADER = "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT"


def route(*rows: tuple) -> str:
    """A ``/proc/net/route``: the header, then each (iface, destination,
    gateway, flags, mask[, metric]) row, as fib_route_seq_show pads them."""
    lines = [ROUTE_HEADER]
    for row in rows:
        iface, destination, gateway, flags, mask, *rest = row
        metric = rest[0] if rest else 0
        lines.append(
            f"{iface}\t{destination}\t{gateway}\t{flags}\t0\t0\t{metric}\t{mask}\t0\t0\t0"
        )
    return "".join(f"{line:<127}\n" for line in lines)


# --- IPv4 ------------------------------------------------------------------

#: Docker (Compose's project network, or `docker run` on the default bridge):
#: default via the gateway 172.19.0.1, and the network 172.19.0.0/16 itself.
#: In Compose, `web` (172.19.0.3, say) and the gateway are both on it.
DOCKER_BRIDGE_ROUTE = route(
    ("eth0", "00000000", "010013AC", "0003", "00000000"),
    ("eth0", "000013AC", "00000000", "0001", "0000FFFF"),
)

#: ECS on Fargate (awsvpc): the task's interface in a private subnet,
#: 10.0.2.0/24, default via 10.0.2.1, plus the agent's link-local network
#: 169.254.172.0/22 on its own interface.
FARGATE_ROUTE = route(
    ("eth1", "00000000", "0102000A", "0003", "00000000"),
    ("eth1", "0002000A", "00000000", "0001", "00FFFFFF"),
    ("ecs-eth0", "00ACFEA9", "00000000", "0001", "00FCFFFF"),
)

#: Calico: default via the link-local 169.254.1.1, reached by a host route.
#: The pod's own address is in the local table, which this file does not show.
CALICO_ROUTE = route(
    ("eth0", "00000000", "0101FEA9", "0003", "00000000"),
    ("eth0", "0101FEA9", "00000000", "0005", "FFFFFFFF"),
)

#: kind's CNI (the ptp plugin): the pod network 10.244.0.0/16 is routed via
#: the gateway 10.244.0.1, which is itself a host route.
KIND_PTP_ROUTE = route(
    ("eth0", "00000000", "0100F40A", "0003", "00000000"),
    ("eth0", "0000F40A", "0100F40A", "0003", "0000FFFF"),
    ("eth0", "0100F40A", "00000000", "0005", "FFFFFFFF"),
)

#: flannel: the node's pod subnet 10.244.1.0/24 is attached; the rest of the
#: cluster's 10.244.0.0/16 is routed via the gateway 10.244.1.1.
FLANNEL_ROUTE = route(
    ("eth0", "00000000", "0101F40A", "0003", "00000000"),
    ("eth0", "0000F40A", "0101F40A", "0003", "0000FFFF"),
    ("eth0", "0001F40A", "00000000", "0001", "00FFFFFF"),
)

#: Host networking: the host's own LAN and its docker0 bridge.
HOST_NETWORK_ROUTE = route(
    ("eno1", "00000000", "0101A8C0", "0003", "00000000", 100),
    ("eno1", "0001A8C0", "00000000", "0001", "00FFFFFF", 100),
    ("docker0", "000011AC", "00000000", "0001", "0000FFFF"),
)

#: The routes that carry no traffic to a neighbour. `ip route add unreachable
#: 10.0.0.0/8` and `prohibit 10.1.0.0/16` print `*` AND RTF_REJECT; `blackhole
#: 10.2.0.0/16` prints `*` with RTF_UP only (fib_flag_trans gives RTF_REJECT to
#: unreachable and prohibit, not to blackhole).
REJECT_ROUTE = route(
    ("*", "0000000A", "00000000", "0201", "000000FF"),
    ("*", "0000010A", "00000000", "0201", "0000FFFF"),
    ("*", "0000020A", "00000000", "0001", "0000FFFF"),
)

# --- IPv6 ------------------------------------------------------------------

#: `lo`'s ::1/128, host scope (10).
IF_INET6_LO = "00000000000000000000000000000001 01 80 10 80       lo\n"

#: A unique local address, fd00:0:0:1::5/64, global scope (00): the network
#: is fd00:0:0:1::/64, and a decimal reading of `40` would make it /40.
IF_INET6_ULA = "fd000000000000010000000000000005 02 40 00 80     eth0\n"

#: The same interface's link-local address, link scope (20).
IF_INET6_LINK = "fe800000000000000042acfffe130003 02 40 20 80     eth0\n"

#: A container with IPv6: lo, a link-local and a unique local address.
IF_INET6_DUAL = IF_INET6_LO + IF_INET6_LINK + IF_INET6_ULA
