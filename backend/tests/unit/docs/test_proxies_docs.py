"""What the documentation says about the proxies the API believes.

Two things are held here, because both drift silently:

* the pages state the rule the code applies: the chart's surfaces give the
  chart's default exactly as the chart's ``values.yaml`` sets it, and
  the image's surfaces describe the list the entrypoint builds (127.0.0.1,
  ::1, the container's networks and the setting) and none of them calls the
  private ranges the image's default any more;
* the command each page gives to count the addresses on the API's access
  lines works on real lines: they are rendered by the API's own log
  configuration (``configure_logging(json_logs=True)``), the page's pipeline
  is taken from the page with its log source swapped for a file, and it must
  print exactly the two client addresses, without the health check's.

Reads files and runs ``sh``; no Docker, no request, so it runs in the docs-only
gate's "Docs content tests" (this directory) and in ``scripts/core_build.sh``'s
copy.
"""

from __future__ import annotations

import io
import logging
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
VALUES = REPO_ROOT / "charts" / "experimently" / "values.yaml"
CHART_README = REPO_ROOT / "charts" / "experimently" / "README.md"
KUBERNETES = REPO_ROOT / "docs" / "self-hosting" / "kubernetes.md"
PROXIES = REPO_ROOT / "docs" / "self-hosting" / "proxies.md"
COMPOSE_README = REPO_ROOT / "deploy" / "compose" / "README.md"

#: The surfaces about the image's own list, and what each must say of it.
IMAGE_SURFACES = {
    ".env.example": "127.0.0.1, ::1 and the networks the API's container is",
    "docs/getting-started/environment-setup.md": (
        "in addition to 127.0.0.1, ::1 and the networks its container is attached to"
    ),
    "docs/self-hosting/proxies.md": (
        "The API always believes 127.0.0.1, ::1 and the networks its container is "
        "directly attached to."
    ),
    "deploy/compose/.env.example": "besides the dashboard",
    "deploy/compose/README.md": (
        "The API believes that header from the dashboard container and from a TLS "
        "proxy on this host"
    ),
    "backend/docker-entrypoint.sh": (
        "The default believes loopback and the networks this container is"
    ),
}
OLD_DEFAULT = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"


def _flat(path: Path) -> str:
    """The file as one line: comment marks, backticks and wrapping removed."""
    text = path.read_text(encoding="utf-8").replace("`", "")
    text = re.sub(r"^\s*#\s?", "", text, flags=re.M)
    return " ".join(text.split())


def _chart_default() -> str:
    return yaml.safe_load(VALUES.read_text(encoding="utf-8"))["api"][
        "forwardedAllowIps"
    ]


@pytest.mark.parametrize("path", [CHART_README, KUBERNETES])
def test_the_chart_pages_state_the_charts_default(path):
    """Every "private ranges, <list>" on the page is the default, whole."""
    default = _chart_default()
    stated = [
        value.rstrip(".,")
        for value in re.findall(r"private ranges, (\S+)", _flat(path))
    ]
    assert stated and all(value == default for value in stated), (
        f"{path.relative_to(REPO_ROOT)} does not state the chart's default as "
        f"values.yaml says it, {default}: it states {stated}"
    )


@pytest.mark.parametrize("name", sorted(IMAGE_SURFACES))
def test_the_image_pages_describe_the_list_the_entrypoint_builds(name):
    text = _flat(REPO_ROOT / name)
    assert " ".join(IMAGE_SURFACES[name].split()) in text, (
        f"{name} does not describe the list the image builds"
    )
    for old in ("RFC1918", "RFC 1918", OLD_DEFAULT):
        assert old not in text, (
            f"{name} still calls the private ranges ({old}) the image default"
        )


# ---------------------------------------------------------------------------
# The check commands, on real lines
# ---------------------------------------------------------------------------

#: The page and how its command starts: the one fenced block that starts so.
#: The log source is everything before the command's first `|`.
COMMANDS = [
    (COMPOSE_README, "docker compose logs api "),
    (PROXIES, "docker logs "),
    (PROXIES, "kubectl logs "),
]


def _command(page: Path, start: str) -> str:
    """The page's command with its log source replaced by `cat "$LOG"`."""
    blocks = re.findall(r"```[^\n]*\n(.*?)```", page.read_text(encoding="utf-8"), re.S)
    found = [b for b in blocks if b.lstrip().startswith(start)]
    assert len(found) == 1, f"{page.name}: {len(found)} blocks start with {start!r}"
    source, pipeline = found[0].replace("\\\n", " ").split("|", 1)
    assert "2>&1" in source or start.startswith("kubectl"), source
    return f'cat "$LOG" |{pipeline}'


def _access_lines() -> list:
    """Three access lines as the API writes them: an IPv4 and an IPv6 client
    on /api/, and the health check from loopback."""
    from backend.app.core.logger import configure_logging

    root = logging.getLogger()
    saved = (list(root.handlers), root.level)
    stream = io.StringIO()
    try:
        configure_logging(json_logs=True, stream=stream)
        access = logging.getLogger("uvicorn.access")
        # The call uvicorn's protocol makes for each response.
        for client, path, status in (
            ("203.0.113.9:51234", "/api/v1/experiments/", 401),
            ("203.0.113.9:51240", "/api/v1/auth/me", 200),
            ("::1:40001", "/api/v1/feature-flags/", 200),
            ("127.0.0.1:52100", "/health/ready", 200),
        ):
            access.info('%s - "%s %s HTTP/%s" %d', client, "GET", path, "1.1", status)
    finally:
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])
    lines = [line for line in stream.getvalue().splitlines() if line.strip()]
    assert len(lines) == 4 and all('"uvicorn.access"' in line for line in lines), lines
    return lines


@pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX sh")
@pytest.mark.parametrize("prefix", ["", "api-1  | "], ids=["plain", "compose-prefix"])
@pytest.mark.parametrize(
    "page, source", COMMANDS, ids=["compose-readme", "docker", "kubernetes"]
)
def test_the_check_command_counts_the_users_addresses(page, source, prefix, tmp_path):
    log = tmp_path / "api.log"
    log.write_text("".join(f"{prefix}{line}\n" for line in _access_lines()))
    command = _command(page, source)
    result = subprocess.run(
        ["sh", "-c", command],
        env={"PATH": "/usr/bin:/bin", "LOG": str(log)},
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, result.stderr
    counted = [line.split() for line in result.stdout.splitlines()]
    assert counted == [["2", "203.0.113.9"], ["1", "::1"]], result.stdout
