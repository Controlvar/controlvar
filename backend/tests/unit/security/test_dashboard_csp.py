"""The dashboard's nginx sets its own Content-Security-Policy.

This pins it (#125): the header is set at server level with `always`, so
every location that sets no `add_header` of its own inherits it; it starts
from `default-src 'self'`, forbids framing, and takes `connect-src` from
`CSP_CONNECT_SRC`, which frontend/docker-entrypoint.sh substitutes at
start-up.

The two API reference pages are the exception (#817): `location =
/api/v1/docs` and `location = /api/v1/redoc` proxy to the API and carry the
API's own policy only, because a second, stricter policy would block the
assets Swagger UI and ReDoc load. nginx inherits server-level `add_header`
lines only into a location that has none of its own, so each of those blocks
must set at least one `add_header` and no Content-Security-Policy.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
NGINX = REPO_ROOT / "frontend" / "nginx.conf"

_CSP = re.compile(r'^\s*add_header Content-Security-Policy "([^"]*)" always;\s*$', re.M)


def _policy() -> dict[str, str]:
    (value,) = _CSP.findall(NGINX.read_text(encoding="utf-8"))
    directives = {}
    for part in value.split(";"):
        name, _, rest = part.strip().partition(" ")
        if name:
            directives[name] = rest
    return directives


def test_nginx_sets_one_server_level_csp():
    assert _CSP.findall(NGINX.read_text(encoding="utf-8")), (
        f"{NGINX} no longer sets Content-Security-Policy with `always`"
    )
    _policy()  # exactly one


def test_the_directives_the_threat_model_names():
    policy = _policy()
    assert policy["default-src"] == "'self'"
    assert policy["frame-ancestors"] == "'none'"
    assert policy["connect-src"] == "${CSP_CONNECT_SRC}"


def test_the_entrypoint_substitutes_connect_src():
    entrypoint = (REPO_ROOT / "frontend" / "docker-entrypoint.sh").read_text(
        encoding="utf-8"
    )
    assert "CSP_CONNECT_SRC" in entrypoint


DOCS_LOCATIONS = ("/api/v1/docs", "/api/v1/redoc")

# The headers each docs block sets so that it keeps them: a location with any
# add_header of its own inherits none from the server level.
DOCS_BLOCK_HEADERS = (
    "X-Frame-Options",
    "X-Content-Type-Options",
    "Referrer-Policy",
    "Permissions-Policy",
)


def _location_block(path: str) -> str:
    text = NGINX.read_text(encoding="utf-8")
    # The body runs to the first line that is only "}" (`${API_UPSTREAM}`
    # inside it has braces of its own).
    blocks = re.findall(
        r"^\s*location\s*=\s*" + re.escape(path) + r"\s*\{\s*$(.*?)^\s*\}\s*$",
        text,
        re.M | re.S,
    )
    assert len(blocks) == 1, f"{NGINX} has {len(blocks)} `location = {path}` blocks"
    return blocks[0]


def _add_headers(block: str) -> list[str]:
    return re.findall(r"^\s*add_header\s+(\S+)", block, re.M)


@pytest.mark.regression
@pytest.mark.parametrize("path", DOCS_LOCATIONS)
def test_docs_block_proxies_to_the_api(path):
    block = _location_block(path)
    assert re.search(r"^\s*proxy_pass \$\{API_UPSTREAM\};", block, re.M), block


@pytest.mark.regression
@pytest.mark.parametrize("path", DOCS_LOCATIONS)
def test_docs_block_sets_no_csp_and_so_inherits_none(path):
    headers = _add_headers(_location_block(path))
    assert "Content-Security-Policy" not in headers, (
        f"`location = {path}` sets a Content-Security-Policy; the page then carries "
        "two policies and the stricter one blanks it"
    )
    assert headers, (
        f"`location = {path}` has no add_header, so it inherits the server-level "
        "dashboard Content-Security-Policy and the page is blank"
    )


@pytest.mark.regression
@pytest.mark.parametrize("path", DOCS_LOCATIONS)
def test_docs_block_keeps_the_other_dashboard_headers(path):
    headers = _add_headers(_location_block(path))
    for name in DOCS_BLOCK_HEADERS:
        assert name in headers, f"`location = {path}` no longer sets {name}"
