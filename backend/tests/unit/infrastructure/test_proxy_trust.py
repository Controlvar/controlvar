"""The list of proxies the API's server is started with (``proxy_trust``).

``backend/docker-entrypoint.sh`` passes uvicorn ``--forwarded-allow-ips`` with
what ``python -m backend.app.core.proxy_trust`` prints: loopback, the networks
the container is directly attached to, then the entries of
``FORWARDED_ALLOW_IPS``. These tests drive the module on the kernel's own text
(``_proc_net_fixtures.py``) and check every list with the pinned uvicorn's own
parser, ``_TrustedHosts``: membership and the parse only, never a header.
"""

from __future__ import annotations

import io
import ipaddress
import json
import random
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from uvicorn.middleware.proxy_headers import _TrustedHosts

from backend.app.core import proxy_trust
from backend.tests.unit.infrastructure import _proc_net_fixtures as fx

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
CHART = REPO_ROOT / "charts" / "experimently"
FARGATE_STACK = (
    REPO_ROOT / "infrastructure" / "cdk" / "stacks" / "fargate_service_stack.py"
)


def own(route: str = "", if_inet6: str = "") -> list:
    """The own networks of fixture text, printed by a little-endian kernel."""
    return proxy_trust.own_networks(route, if_inet6, byteorder="little")


def strings(networks) -> list:
    return [str(network) for network in networks]


def no_literals(value: str) -> None:
    """uvicorn reads every entry of *value* as an address or a network."""
    hosts = _TrustedHosts(value)
    assert not hosts.always_trust, f"{value!r} makes uvicorn trust every peer"
    assert hosts.trusted_literals == set(), (
        f"entries {sorted(hosts.trusted_literals)} of {value!r} are not an address "
        "or a network; uvicorn would trust nothing for them"
    )


# ---------------------------------------------------------------------------
# The own networks, by topology
# ---------------------------------------------------------------------------

TOPOLOGIES = {
    "docker-bridge": (fx.DOCKER_BRIDGE_ROUTE, fx.IF_INET6_LO, ["172.19.0.0/16"]),
    "fargate": (fx.FARGATE_ROUTE, "", ["10.0.2.0/24"]),
    "calico": (fx.CALICO_ROUTE, "", []),
    "kind-ptp": (fx.KIND_PTP_ROUTE, "", ["10.244.0.1/32"]),
    "flannel": (fx.FLANNEL_ROUTE, "", ["10.244.1.0/24"]),
    # Host networking: the host's LAN is "own". A documented residual: the
    # image cannot tell host networking from a bridge.
    "host-network": (
        fx.HOST_NETWORK_ROUTE,
        "",
        ["192.168.1.0/24", "172.17.0.0/16"],
    ),
    "ipv6": (
        fx.DOCKER_BRIDGE_ROUTE,
        fx.IF_INET6_DUAL,
        ["172.19.0.0/16", "fd00:0:0:1::/64"],
    ),
    "reject-routes": (fx.REJECT_ROUTE, "", []),
}


@pytest.mark.regression
@pytest.mark.parametrize("name", sorted(TOPOLOGIES))
def test_the_own_networks_of_each_topology(name):
    route, if_inet6, expected = TOPOLOGIES[name]
    assert strings(own(route, if_inet6)) == expected


@pytest.mark.parametrize("name", sorted(TOPOLOGIES))
def test_every_list_parses_with_no_literal(name):
    route, if_inet6, _ = TOPOLOGIES[name]
    no_literals(proxy_trust.build("", own(route, if_inet6)))
    no_literals(
        proxy_trust.build("10.0.0.5, 10.0.0.0/16,fd00::5", own(route, if_inet6))
    )


@pytest.mark.regression
def test_compose_believes_web_and_the_gateway_and_no_lan_client():
    hosts = _TrustedHosts(proxy_trust.build("", own(fx.DOCKER_BRIDGE_ROUTE)))
    for proxy in ("172.19.0.3", "172.19.0.1", "127.0.0.1", "::1"):
        assert proxy in hosts, f"{proxy} is not believed"
    for client in ("192.168.1.50", "10.0.3.10", "172.20.0.5"):
        assert client not in hosts, f"{client} is believed as a proxy"


@pytest.mark.regression
def test_a_network_reached_via_a_gateway_is_not_own():
    networks = strings(own(fx.FLANNEL_ROUTE))
    assert "10.244.0.0/16" not in networks, (
        "10.244.0.0/16 is reached via a gateway and must not be trusted"
    )
    networks = strings(own(fx.KIND_PTP_ROUTE))
    assert "10.244.0.0/16" not in networks, (
        "10.244.0.0/16 is reached via a gateway and must not be trusted"
    )


# ---------------------------------------------------------------------------
# Each exclusion rule, on a row only it excludes
# ---------------------------------------------------------------------------

#: (rule, the row it excludes, the same row without what the rule looks at,
#: and the network that row then gives; None when the rule is the row).
#: "kernel" rows are ones the kernel prints; "synthetic" rows are not (it sets
#: RTF_UP on every row, and a route with RTF_REJECT has no device), and stand
#: in for a rule that no printed row isolates.
ROUTE_RULES = [
    (
        "no device (blackhole, kernel)",
        ("*", "0000020A", "00000000", "0001", "0000FFFF"),
        ("eth0", "0000020A", "00000000", "0001", "0000FFFF"),
        "10.2.0.0/16",
    ),
    (
        "the lo device (kernel: ip route add 10.9.0.0/16 dev lo)",
        ("lo", "0000090A", "00000000", "0001", "0000FFFF"),
        ("eth0", "0000090A", "00000000", "0001", "0000FFFF"),
        "10.9.0.0/16",
    ),
    (
        "via a gateway (kernel: flannel's cluster route)",
        ("eth0", "0000F40A", "0101F40A", "0003", "0000FFFF"),
        ("eth0", "0000F40A", "00000000", "0001", "0000FFFF"),
        "10.244.0.0/16",
    ),
    (
        "RTF_REJECT with a device (synthetic)",
        ("eth0", "0000000A", "00000000", "0201", "000000FF"),
        ("eth0", "0000000A", "00000000", "0001", "000000FF"),
        "10.0.0.0/8",
    ),
    (
        "RTF_UP clear (synthetic)",
        ("eth0", "0000000A", "00000000", "0000", "000000FF"),
        ("eth0", "0000000A", "00000000", "0001", "000000FF"),
        "10.0.0.0/8",
    ),
    (
        "the default route, on-link (kernel: ip route add default dev eth0)",
        ("eth0", "00000000", "00000000", "0001", "00000000"),
        None,
        None,
    ),
    (
        "link-local (kernel: Calico's gateway)",
        ("eth0", "0101FEA9", "00000000", "0005", "FFFFFFFF"),
        None,
        None,
    ),
]


@pytest.mark.regression
@pytest.mark.parametrize(
    "rule, row, control, gives", ROUTE_RULES, ids=[r[0] for r in ROUTE_RULES]
)
def test_each_route_exclusion_rule_on_its_own_row(rule, row, control, gives):
    assert own(fx.route(row)) == [], f"{rule}: the row is trusted"
    if control is not None:
        assert strings(own(fx.route(control))) == [gives], (
            f"{rule}: without what the rule looks at the row should give {gives}"
        )


IF_INET6_RULES = [
    (
        "the lo interface (kernel: a global address on lo)",
        "20010db8000000000000000000000001 01 80 00 80       lo",
        "20010db8000000000000000000000001 01 80 00 80     eth0",
        "2001:db8::1/128",
    ),
    (
        "link scope (kernel)",
        "fe800000000000000042acfffe130003 02 40 20 80     eth0",
        "fe800000000000000042acfffe130003 02 40 00 80     eth0",
        "fe80::/64",
    ),
    (
        "duplicate address detection failed (kernel)",
        "fd00000000000002000000000000000a 02 40 00 c8     eth0",
        "fd00000000000002000000000000000a 02 40 00 80     eth0",
        "fd00:0:0:2::/64",
    ),
    (
        "prefix length zero (kernel: ip addr add 2001:db8::1/0)",
        "20010db8000000000000000000000001 03 00 00 80     eth1",
        "20010db8000000000000000000000001 03 40 00 80     eth1",
        "2001:db8::/64",
    ),
]


@pytest.mark.regression
@pytest.mark.parametrize(
    "rule, row, control, gives", IF_INET6_RULES, ids=[r[0] for r in IF_INET6_RULES]
)
def test_each_if_inet6_exclusion_rule_on_its_own_row(rule, row, control, gives):
    assert own(if_inet6=row + "\n") == [], f"{rule}: the row is trusted"
    assert strings(own(if_inet6=control + "\n")) == [gives], rule


# ---------------------------------------------------------------------------
# The kernel's formats
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_the_if_inet6_prefix_length_is_hexadecimal():
    """``40`` is 64: read as decimal it would be /40, a wider network that is
    still a valid one, so nothing else would notice."""
    assert strings(own(if_inet6=fx.IF_INET6_ULA)) == ["fd00:0:0:1::/64"]
    interface, scope, flags, name = proxy_trust.parse_if_inet6_row(fx.IF_INET6_LO)
    assert str(interface) == "::1/128"
    assert (scope, flags, name) == (0x10, 0x80, "lo")


def test_the_route_addresses_are_read_in_the_byte_order_given():
    """Host order: these fixtures are a little-endian kernel's. The default,
    this machine's own order, is checked against ``ip`` on the Linux runner
    (TestTheRunnersOwnFiles)."""
    assert strings(
        proxy_trust.networks_from_route(fx.DOCKER_BRIDGE_ROUTE, byteorder="little")
    ) == ["172.19.0.0/16"]
    # Read in the other order the mask is 0.0.255.255, not a mask at all.
    assert (
        proxy_trust.networks_from_route(fx.DOCKER_BRIDGE_ROUTE, byteorder="big") == []
    )


# ---------------------------------------------------------------------------
# Bounds, and text that is not what the kernel prints
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_at_most_thirty_two_networks_are_kept():
    rows = [
        ("eth0", f"00{n:02X}000A", "00000000", "0001", "00FFFFFF") for n in range(40)
    ]
    networks = strings(own(fx.route(*rows)))
    assert networks == [f"10.0.{n}.0/24" for n in range(32)]


@pytest.mark.regression
def test_only_the_first_64_kib_of_a_file_is_read(tmp_path):
    head = fx.route(("eth0", "000013AC", "00000000", "0001", "0000FFFF"))
    # Up to 60 bytes short of the limit, so the limit falls inside a line.
    padding = "x" * (proxy_trust.MAX_BYTES - 60 - len(head) - 1) + "\n"
    late = fx.route(("eth0", "0000090A", "00000000", "0001", "0000FFFF")) * 10
    path = tmp_path / "route"
    path.write_text(head + padding + late)
    assert path.stat().st_size > 65 * 1024
    text = proxy_trust._read(str(path))
    assert len(text) <= proxy_trust.MAX_BYTES
    assert text.endswith("\n"), "the line cut at the limit was kept"
    assert strings(proxy_trust.networks_from_route(text, byteorder="little")) == [
        "172.19.0.0/16"
    ]


def _main(environ, route_bytes, if_inet6_bytes, tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, data in (("route", route_bytes), ("if_inet6", if_inet6_bytes)):
        path = tmp_path / name
        if data is not None:
            path.write_bytes(data)
        paths.append(str(path))
    out, err = io.StringIO(), io.StringIO()
    code = proxy_trust.main(
        environ, route=paths[0], if_inet6=paths[1], stdout=out, stderr=err
    )
    return code, out.getvalue(), err.getvalue()


def _ugly_inputs():
    """Text no kernel prints: NULs, overlong lines, non-hex, signs, wrong
    widths, stray bytes. Fixed seed, so a failure reproduces."""
    good_route = fx.DOCKER_BRIDGE_ROUTE.encode()
    good_inet6 = fx.IF_INET6_DUAL.encode()
    cases = [
        (b"", b""),
        (b"\x00" * 4096, b"\x00" * 4096),
        (
            good_route.replace(b"0000FFFF", b"0000FF\x00F"),
            good_inet6.replace(b"40", b"4\x00"),
        ),
        (
            b"eth0\t-0000001\t00000000\t0001\t0\t0\t0\t0000FFFF\n",
            b"fd00 02 zz 00 80 eth0\n",
        ),
        (b"eth0\t0x13AC00\t00000000\t0001\t0\t0\t0\t0000FFFF\n", b"0" * 200_000),
        (b"eth0\t0000_3AC\t00000000\t0001\t0\t0\t0\t0000FFFF\n", b"\xff\xfe" * 1000),
        (
            b"a" * 200_000,
            good_inet6 + b"fd000000000000010000000000000005 02 ff 00 80 eth0\n",
        ),
        (good_route + b"\t" * 10 + b"\n" + b"eth0" * 30_000, b" \n" * 50_000),
        (b"eth0\t000013AC\t00000000\t0001\t0\t0\t0\tFFFF00FF\n", b"\xe2\x80\xa8" * 300),
    ]
    rng = random.Random(1573)
    for _ in range(60):
        size = rng.choice([1, 17, 128, 4096, 70_000])
        alphabet = rng.choice(
            [b"0123456789ABCDEFabcdef\t \n*lo", bytes(range(256)), b"\x00\n\t0F"]
        )
        cases.append(
            (
                bytes(rng.choice(alphabet) for _ in range(size)),
                good_inet6[: rng.randrange(len(good_inet6))]
                + bytes(rng.randrange(256) for _ in range(rng.choice([0, 9, 300]))),
            )
        )
    return cases


@pytest.mark.regression
def test_any_bytes_in_either_file_still_give_a_usable_list(tmp_path):
    """Under ``set -e`` a crash here is an API that does not start."""
    for number, (route_bytes, if_inet6_bytes) in enumerate(_ugly_inputs()):
        case = tmp_path / str(number)
        case.mkdir()
        code, out, err = _main({}, route_bytes, if_inet6_bytes, case)
        assert code == 0, (number, err)
        assert out.endswith("\n") and out.count("\n") == 1, (number, out)
        assert out.startswith("127.0.0.1,::1"), (number, out)
        no_literals(out.strip())
        assert err.startswith("[entrypoint] trusted proxies: 127.0.0.1 ::1; "), (
            number,
            err,
        )


# ---------------------------------------------------------------------------
# The list, the setting and the start-up line
# ---------------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("raw", [None, "", "   ", " , ,"])
def test_unset_empty_or_blank_adds_nothing(raw):
    networks = own(fx.DOCKER_BRIDGE_ROUTE)
    assert proxy_trust.build(raw, networks) == "127.0.0.1,::1,172.19.0.0/16"


def test_entries_add_to_the_own_networks_and_each_appears_once():
    networks = own(fx.FARGATE_ROUTE)
    built = proxy_trust.build(" 10.0.0.0/16 , 127.0.0.1,10.0.0.0/16,FD00::5", networks)
    assert built == "127.0.0.1,::1,10.0.2.0/24,10.0.0.0/16,fd00::5"


@pytest.mark.regression
def test_an_operator_value_is_read_from_the_environment(tmp_path):
    code, out, err = _main(
        {"FORWARDED_ALLOW_IPS": "192.0.2.10"},
        fx.DOCKER_BRIDGE_ROUTE.encode(),
        None,
        tmp_path,
    )
    assert code == 0, err
    assert out == "127.0.0.1,::1,172.19.0.0/16,192.0.2.10\n"
    assert err == (
        "[entrypoint] trusted proxies: 127.0.0.1 ::1; this container's networks: "
        "172.19.0.0/16; FORWARDED_ALLOW_IPS: 192.0.2.10\n"
    )


#: Every form of the start-up line (one form for unset, empty and blank).
START_LINES = [
    (
        None,
        fx.DOCKER_BRIDGE_ROUTE,
        "[entrypoint] trusted proxies: 127.0.0.1 ::1; this container's networks: "
        "172.19.0.0/16; FORWARDED_ALLOW_IPS: (none)",
    ),
    (
        "",
        fx.DOCKER_BRIDGE_ROUTE,
        "[entrypoint] trusted proxies: 127.0.0.1 ::1; this container's networks: "
        "172.19.0.0/16; FORWARDED_ALLOW_IPS: (none)",
    ),
    (
        "  ",
        fx.DOCKER_BRIDGE_ROUTE,
        "[entrypoint] trusted proxies: 127.0.0.1 ::1; this container's networks: "
        "172.19.0.0/16; FORWARDED_ALLOW_IPS: (none)",
    ),
    (
        "10.0.0.0/16",
        fx.FARGATE_ROUTE,
        "[entrypoint] trusted proxies: 127.0.0.1 ::1; this container's networks: "
        "10.0.2.0/24; FORWARDED_ALLOW_IPS: 10.0.0.0/16",
    ),
    (
        "10.0.0.5, 10.0.0.0/16",
        fx.CALICO_ROUTE,
        "[entrypoint] trusted proxies: 127.0.0.1 ::1; this container's networks: "
        "(none); FORWARDED_ALLOW_IPS: 10.0.0.5 10.0.0.0/16",
    ),
]


@pytest.mark.regression
@pytest.mark.parametrize("raw, route, line", START_LINES)
def test_the_start_up_line_says_what_is_believed(raw, route, line):
    assert proxy_trust.start_line(raw, own(route)) == line


@pytest.mark.regression
def test_when_neither_file_can_be_read_the_line_is_a_warning(tmp_path):
    code, out, err = _main({}, None, None, tmp_path)
    assert code == 0, err
    assert out == "127.0.0.1,::1\n"
    assert err == (
        "[entrypoint] WARNING: this container's networks could not be read from "
        "/proc/net/route or /proc/net/if_inet6; trusted proxies: 127.0.0.1 ::1; "
        "FORWARDED_ALLOW_IPS: (none)\n"
    )
    # One file is enough: a container without IPv6 has no if_inet6.
    code, out, err = _main({}, fx.DOCKER_BRIDGE_ROUTE.encode(), None, tmp_path / "x")
    assert "WARNING" not in err and out == "127.0.0.1,::1,172.19.0.0/16\n"


@pytest.mark.regression
def test_a_refused_entry_exits_78_and_prints_no_list(tmp_path):
    code, out, err = _main(
        {"FORWARDED_ALLOW_IPS": "10.0.0.5,172.18.0.3/16,*"},
        fx.DOCKER_BRIDGE_ROUTE.encode(),
        None,
        tmp_path,
    )
    assert code == proxy_trust.EX_CONFIG == 78
    assert out == ""
    lines = err.splitlines()
    assert [line.split(" (")[0].split(" is ")[0] for line in lines] == [
        "[entrypoint] FORWARDED_ALLOW_IPS entry 2",
        "[entrypoint] FORWARDED_ALLOW_IPS entry 3",
    ]
    assert "10.0.0.5" not in err


# ---------------------------------------------------------------------------
# Every value that ships parses with no literal
# ---------------------------------------------------------------------------


def _chart_values() -> dict:
    values = {
        "values.yaml": yaml.safe_load((CHART / "values.yaml").read_text())["api"][
            "forwardedAllowIps"
        ]
    }
    for path in sorted((CHART / "ci").glob("values-*.yaml")):
        api = (yaml.safe_load(path.read_text()) or {}).get("api") or {}
        if "forwardedAllowIps" in api:
            values[f"ci/{path.name}"] = api["forwardedAllowIps"]
    return values


def _cdk_value() -> str:
    """The literal the API container's environment gives FORWARDED_ALLOW_IPS."""
    import ast

    tree = ast.parse(FARGATE_STACK.read_text())
    found = [
        value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        for key, value in zip(node.keys, node.values)
        if isinstance(key, ast.Constant)
        and key.value == "FORWARDED_ALLOW_IPS"
        and isinstance(value, ast.Constant)
    ]
    assert len(found) == 1, f"FORWARDED_ALLOW_IPS literals in the stack: {found}"
    return found[0]


#: The example values the documentation gives.
DOCUMENTED = ["10.0.0.5,10.0.0.0/16", "192.0.2.10", "10.244.0.0/16", "10.0.0.0/16"]


@pytest.mark.regression
def test_every_shipped_value_parses_with_no_literal():
    shipped = {**_chart_values(), "cdk": _cdk_value()}
    shipped.update({f"docs {value}": value for value in DOCUMENTED})
    assert shipped["values.yaml"] == "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
    assert shipped["cdk"] == "10.0.0.0/16"
    for name, value in shipped.items():
        assert proxy_trust.entry_problems(value) == [], name
        assert proxy_trust.every_address_problems(value) == [], name
        no_literals(value)
        no_literals(proxy_trust.build(value, own(fx.FLANNEL_ROUTE)))


# ---------------------------------------------------------------------------
# Standard library only, with nothing but the module itself
# ---------------------------------------------------------------------------

_IMPORT_ALONE = """
import sys
sys.path.insert(0, sys.argv[1])
import backend.app.core.proxy_trust
names = sorted({n.split(".")[0] for n in sys.modules})
print(" ".join(n for n in names if n not in sys.stdlib_module_names and n != "__main__"))
print("backend.app.core.config" in sys.modules)
"""


@pytest.mark.regression
def test_it_imports_with_the_standard_library_alone():
    """``-I -S``: no site-packages, no environment. The start-up check uses it,
    and it must never build the settings (``backend.app.core.config``)."""
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", _IMPORT_ALONE, str(REPO_ROOT)],
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, result.stderr
    others, config = result.stdout.split("\n")[:2]
    assert others == "backend", f"proxy_trust imports {others}"
    assert config == "False", "proxy_trust must not import backend.app.core.config"


# ---------------------------------------------------------------------------
# The runner's own files (Linux only)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="reads this machine's /proc"
)
class TestTheRunnersOwnFiles:
    """The real files, against ``ip``: the only check of the kernel's format
    that is not written from it. Unit Tests fails on a skip in this directory,
    so on the Linux runner these run."""

    def test_the_ipv4_networks_are_the_ones_ip_reports(self):
        ip = shutil.which("ip")
        assert ip, "no `ip` on this Linux machine to compare /proc/net/route with"
        result = subprocess.run(
            [ip, "-j", "route", "show", "table", "main"],
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
        assert result.returncode == 0, result.stderr
        expected = []
        for entry in json.loads(result.stdout or "[]"):
            if entry.get("dst") in (None, "default") or "gateway" in entry:
                continue
            if entry.get("type", "unicast") != "unicast":
                continue
            if entry.get("dev") in (None, "lo"):
                continue
            network = ipaddress.ip_network(entry["dst"])
            if network.version != 4 or network.subnet_of(proxy_trust.LINK_LOCAL_V4):
                continue
            expected.append(network)
        text = proxy_trust._read(proxy_trust.ROUTE)
        assert text is not None, "/proc/net/route could not be read"
        found = proxy_trust.networks_from_route(text)
        assert sorted(found) == sorted(expected), (found, result.stdout)
        no_literals(proxy_trust.build("", found))

    def test_the_loopback_ipv6_row_reads_as_a_128(self):
        text = proxy_trust._read(proxy_trust.IF_INET6)
        assert text is not None, "/proc/net/if_inet6 could not be read"
        rows = [line for line in text.splitlines() if line.split()[-1:] == ["lo"]]
        assert rows, text
        parsed = {str(proxy_trust.parse_if_inet6_row(row)[0]) for row in rows}
        assert "::1/128" in parsed, rows
