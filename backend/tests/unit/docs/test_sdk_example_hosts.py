"""An SDK example or page names its API origin with a reserved example domain.

The origin an example configures, or falls back to, uses a reserved example
domain: ``example.com`` and its subdomains, a reserved top-level name
(``RESERVED_SUFFIXES``), ``localhost``, a loopback or private address, or a
single-label test name. This holds for every file under ``sdk/`` and
``docs/sdk/`` and for ``docs/sdk-guide.md``.

What counts as an API origin: a URL on a line that configures one (``baseUrl``,
``base_url``, ``apiUrl``, ``api_url``, ``base-url``, ``WithBaseURL``,
``EP_BASE_URL`` and the like), and any URL whose host starts with ``api.``.
A client constructed with the origin as a positional argument counts too
when the call is on one line (``ExperimentationClient("https://...", key)``,
``Client.new(...)``). Links to documentation and registries are on neither
kind of line.

Not seen: an origin on its own line inside a multi-line call or object with
no keyword on that line and no ``api.`` host, or an origin built from parts.

Reads files only (no git), so it runs the same in core_build's copy.
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from typing import Iterator, List, Tuple

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
ROOTS = ("sdk", "docs/sdk", "docs/sdk-guide.md")
#: Build output, installed dependencies and lockfiles: not written by us.
SKIP_DIRS = frozenset(
    {
        "node_modules",
        "dist",
        "build",
        "target",
        "bin",
        "obj",
        "deps",
        "_build",
        "vendor",
        ".dart_tool",
        ".build",
        ".gradle",
        "__pycache__",
        ".pytest_cache",
    }
)
SKIP_SUFFIXES = (".lock", "-lock.json", ".lockb", ".png", ".jpg", ".ico", ".jar")
TEXT_SUFFIXES = (
    ".md", ".ts", ".tsx", ".js", ".mjs", ".py", ".go", ".java", ".kt", ".kts",
    ".cs", ".rb", ".php", ".ex", ".exs", ".dart", ".swift", ".yaml", ".yml",
    ".json", ".toml", ".xml", ".properties", ".sh", ".txt", ".gradle",
)  # fmt: skip

CONFIG_LINE = re.compile(
    r"base[_-]?url|api[_-]?url|baseurl|apiurl|withbaseurl|ep_base_url|"
    r"controlvar_api_url|sdkconfig|base-url|experimentationclient\(|client\.new\(",
    re.IGNORECASE,
)
URL = re.compile(r"https?://([A-Za-z0-9.-]{1,253})")
RESERVED_SUFFIXES = (
    ".example.com",
    ".example",
    ".test",
    ".invalid",
    ".localhost",
    ".internal",
)


def allowed(host: str) -> bool:
    host = host.lower().rstrip(".")
    if host in ("example.com", "localhost") or host.endswith(RESERVED_SUFFIXES):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host  # a single-label test name resolves nowhere public
    return address.is_loopback or address.is_private


def files(root: Path) -> Iterator[Path]:
    for rel in ROOTS:
        base = root / rel
        if base.is_file():
            yield base
            continue
        for path in sorted(base.rglob("*")):
            parts = set(path.relative_to(root).parts)
            if parts & SKIP_DIRS or not path.is_file():
                continue
            if path.name.endswith(SKIP_SUFFIXES) or not path.name.endswith(
                TEXT_SUFFIXES
            ):
                continue
            yield path


def api_hosts(root: Path) -> List[Tuple[str, int, str]]:
    """(file, line, host) for each API origin outside the reserved names."""
    found = []
    for path in files(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            config = CONFIG_LINE.search(line) is not None
            for match in URL.finditer(line):
                host = match.group(1)
                if (config or host.lower().startswith("api.")) and not allowed(host):
                    found.append((path.relative_to(root).as_posix(), number, host))
    return found


def test_every_example_api_origin_is_a_reserved_name():
    found = api_hosts(REPO_ROOT)
    assert found == [], "use a reserved example domain:\n" + "\n".join(
        f"{f}:{n}: {h}" for f, n, h in found
    )


def test_the_scan_reads_the_examples_it_exists_for():
    scanned = {p.relative_to(REPO_ROOT).as_posix() for p in files(REPO_ROOT)}
    assert "sdk/edge/examples/vercel-edge.ts" in scanned
    assert "docs/sdk/edge.md" in scanned
    assert "docs/sdk-guide.md" in scanned


@pytest.mark.parametrize(
    "line",
    [
        "  baseUrl: process.env.EP_BASE_URL ?? 'https://api.your-platform.com',",
        'client = ExperimentationClient(api_url="https://flags.mycompany.com", api_key=k)',
        "fetch('https://api.someone.io/api/v1/feature-flags/evaluate/x')",
        'client = ExperimentationClient("https://flags.someone.io", "key")',
    ],
)
def test_a_registrable_origin_is_refused(tmp_path, line):
    (tmp_path / "sdk" / "x" / "examples").mkdir(parents=True)
    (tmp_path / "docs" / "sdk").mkdir(parents=True)
    (tmp_path / "docs" / "sdk-guide.md").write_text("", encoding="utf-8")
    (tmp_path / "sdk" / "x" / "examples" / "a.ts").write_text(
        line + "\n", encoding="utf-8"
    )
    assert len(api_hosts(tmp_path)) == 1


@pytest.mark.parametrize(
    "line",
    [
        "  baseUrl: 'https://api.example.com',",
        "  baseUrl: 'https://api.controlvar.example.com',",
        '  base_url="http://localhost:8000",',
        '  baseUrl = "http://10.0.2.2:8000",',
        '  base-url: "https://custom-host.internal"',
        "  apiUrl: 'http://127.0.0.1:9',",
        "See https://github.com/Controlvar/controlvar and https://www.npmjs.com/package/x",
    ],
)
def test_a_reserved_origin_or_a_plain_link_is_not(tmp_path, line):
    (tmp_path / "sdk" / "x" / "examples").mkdir(parents=True)
    (tmp_path / "docs" / "sdk").mkdir(parents=True)
    (tmp_path / "docs" / "sdk-guide.md").write_text("", encoding="utf-8")
    (tmp_path / "sdk" / "x" / "examples" / "a.ts").write_text(
        line + "\n", encoding="utf-8"
    )
    assert api_hosts(tmp_path) == []
