"""The quick start of the README this package carries to PyPI runs as it is written.

PyPI shows ``sdk/python/README.md`` as the package's description. Its quick
start once read ``os.environ`` without importing ``os``, so the block stopped
with a ``NameError`` before its first request. This test reads the block from
the README and runs it with the default HTTP layer replaced by
:class:`FakeTransport`: no network, the environment variables the block names,
and every request it documents.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import controlvar.client as client_module
from controlvar.testing import FakeTransport

README = Path(__file__).resolve().parents[1] / "README.md"

ASSIGN = {
    "experiment_key": "checkout_flow",
    "user_id": "user-123",
    "variant_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "variant_name": "treatment",
    "is_control": False,
    "configuration": {"layout": "one_page"},
    "assigned": True,
    "reason": "assigned",
}


def quick_start_block() -> str:
    """The one ``python`` block of the README's "Quick start" section."""
    text = README.read_text(encoding="utf-8")
    assert "\n## Quick start\n" in text, "the README has no Quick start section"
    section = text.split("\n## Quick start\n", 1)[1].split("\n## ", 1)[0]
    blocks = re.findall(r"^```python\n(.*?)^```[ \t]*$", section, re.S | re.M)
    assert len(blocks) == 1, f"the Quick start section has {len(blocks)} python blocks, not one"
    return blocks[0]


def fake_api() -> FakeTransport:
    transport = FakeTransport()
    transport.route("POST", "/api/v1/tracking/assign", json=ASSIGN)
    transport.route(
        "GET",
        "/api/v1/feature-flags/evaluate/new_search",
        json={"key": "new_search", "enabled": True, "config": None, "reason": "rollout"},
    )
    transport.route("GET", "/api/v1/feature-flags/user/user-123", json={"new_search": True})
    transport.route("POST", "/api/v1/tracking/track", json={"id": "evt-1"})
    transport.route(
        "POST", "/api/v1/tracking/batch", json={"success_count": 2, "failure_count": 0, "errors": []}
    )
    return transport


@pytest.mark.regression
def test_the_readme_quick_start_runs_as_written(monkeypatch, capsys):
    transport = fake_api()
    monkeypatch.setattr(client_module, "UrllibTransport", lambda: transport)
    monkeypatch.setenv("EXPERIMENTLY_API_KEY", "readme-key")
    monkeypatch.delenv("EXPERIMENTLY_API_URL", raising=False)

    exec(compile(quick_start_block(), str(README), "exec"), {"__name__": "readme_quick_start"})

    # get_assignment and get_feature_flag are served from the cache that
    # get_variant and is_feature_enabled filled; the key-less track fans out.
    assert transport.calls() == [
        "POST /api/v1/tracking/assign",
        "GET /api/v1/feature-flags/evaluate/new_search",
        "GET /api/v1/feature-flags/user/user-123",
        "POST /api/v1/tracking/track",
        "POST /api/v1/tracking/batch",
    ]
    assert all(r.url.startswith("http://localhost:8000/api/v1/") for r in transport.requests)
    assert all(r.headers["X-API-Key"] == "readme-key" for r in transport.requests)
    assert transport.requests[0].json()["context"] == {"plan": "pro"}
    assert transport.requests[1].query == {"user_id": "user-123", "context": '{"plan":"pro"}'}
    assert transport.requests[3].json()["experiment_key"] == "checkout_flow"
    assert {e["event_name"] for e in transport.requests[4].json()["events"]} == {"page_view"}
    assert capsys.readouterr().out.splitlines() == ["show the new checkout", "use the new search"]


def test_the_block_stops_without_its_key(monkeypatch):
    """The block reads the key it documents, and nothing else supplies one."""
    transport = fake_api()
    monkeypatch.setattr(client_module, "UrllibTransport", lambda: transport)
    monkeypatch.delenv("EXPERIMENTLY_API_KEY", raising=False)
    with pytest.raises(KeyError, match="EXPERIMENTLY_API_KEY"):
        exec(compile(quick_start_block(), str(README), "exec"), {"__name__": "readme_quick_start"})
    assert transport.requests == []
