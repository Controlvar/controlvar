"""examples/contract_smoke.py: what it reads from the environment and what it sends.

The SDK Live Contract job runs the script against a real API with every
variable set. These run it in-process with the default HTTP layer replaced by
:class:`FakeTransport`, so the defaults it falls back to and the four contract
steps are held without a network.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

import experimentation.client as client_module
from experimentation.testing import FakeTransport

SDK_ROOT = Path(__file__).resolve().parents[1]
SMOKE = SDK_ROOT / "examples" / "contract_smoke.py"
VARIABLES = (
    "EXPERIMENTLY_API_URL",
    "EXPERIMENTLY_API_KEY",
    "CONTRACT_EXPERIMENT_KEY",
    "CONTRACT_FLAG_KEY",
    "CONTRACT_USER_ID",
)


def load_smoke(monkeypatch):
    """Import the script as a module, from a sys.path without the SDK on it."""
    monkeypatch.setattr(sys, "path", [p for p in sys.path if Path(p or ".").resolve() != SDK_ROOT])
    spec = importlib.util.spec_from_file_location("contract_smoke_under_test", SMOKE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_api(experiment_key: str, flag_key: str) -> FakeTransport:
    transport = FakeTransport()
    transport.route(
        "POST",
        "/api/v1/tracking/assign",
        json={
            "experiment_key": experiment_key,
            "user_id": "ignored",
            "variant_id": "var-1",
            "variant_name": "control",
            "is_control": True,
            "configuration": None,
        },
    )
    transport.route(
        "GET",
        f"/api/v1/feature-flags/evaluate/{flag_key}",
        json={"key": flag_key, "enabled": True, "config": None},
    )
    transport.route("POST", "/api/v1/tracking/track", json={})
    transport.route(
        "POST", "/api/v1/tracking/batch", json={"success_count": 2, "failure_count": 0, "errors": []}
    )
    return transport


@pytest.fixture
def environment(monkeypatch):
    for name in VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EXPERIMENTLY_API_KEY", "smoke-key")
    return monkeypatch


def test_it_runs_from_the_source_tree_with_its_defaults(environment, capsys):
    transport = fake_api("sdk_contract_ab", "sdk_contract_flag")
    environment.setattr(client_module, "UrllibTransport", lambda: transport)

    smoke = load_smoke(environment)
    assert sys.path[0] == str(SDK_ROOT), "the script does not put sdk/python on sys.path"
    smoke.main()

    assert all(r.url.startswith("http://localhost:8000/api/v1/") for r in transport.requests)
    assert all(r.headers["X-API-Key"] == "smoke-key" for r in transport.requests)
    assign = transport.requests[0].json()
    assert assign["experiment_key"] == "sdk_contract_ab"
    assert assign["user_id"].startswith("smoke-") and len(assign["user_id"]) == len("smoke-") + 36
    assert transport.requests[2].path == "/api/v1/feature-flags/evaluate/sdk_contract_flag"
    line = capsys.readouterr().out.strip()
    assert json.loads(line) == {
        "sdk": "python",
        "assign": {"variant_name": "control", "is_control": True, "sticky": True},
        "flag": {"enabled": True},
        "track": {"ok": True},
        "fanout": {"ok": True},
    }


def test_it_runs_the_four_contract_steps(environment, capsys):
    environment.setenv("EXPERIMENTLY_API_URL", "http://api.test")
    environment.setenv("CONTRACT_EXPERIMENT_KEY", "exp_a")
    environment.setenv("CONTRACT_FLAG_KEY", "flag_a")
    environment.setenv("CONTRACT_USER_ID", "user-a")
    transport = fake_api("exp_a", "flag_a")
    environment.setattr(client_module, "UrllibTransport", lambda: transport)

    load_smoke(environment).main()

    assert transport.calls() == [
        "POST /api/v1/tracking/assign",  # sticky: assigned twice,
        "POST /api/v1/tracking/assign",  # the cache cleared in between
        "GET /api/v1/feature-flags/evaluate/flag_a",
        "POST /api/v1/tracking/track",  # purchase, with the experiment's key
        "POST /api/v1/tracking/batch",  # page_view without a key: fanned out
        "POST /api/v1/tracking/batch",  # the 2-event batch
    ]
    assert all(r.url.startswith("http://api.test/") for r in transport.requests)
    track = transport.requests[3].json()
    assert (track["event_name"], track["experiment_key"]) == ("purchase", "exp_a")
    fanned = transport.requests[4].json()["events"]
    assert {(e["event_name"], e.get("experiment_key"), e.get("feature_flag_key")) for e in fanned} == {
        ("page_view", "exp_a", None),
        ("page_view", None, "flag_a"),
    }
    assert len(transport.requests[5].json()["events"]) == 2
    assert len(capsys.readouterr().out.strip().splitlines()) == 1


def test_it_stops_without_a_key(environment, capsys):
    environment.delenv("EXPERIMENTLY_API_KEY")
    transport = FakeTransport()
    environment.setattr(client_module, "UrllibTransport", lambda: transport)
    with pytest.raises(SystemExit) as stopped:
        load_smoke(environment).main()
    assert stopped.value.code == 1
    assert "EXPERIMENTLY_API_KEY is required" in capsys.readouterr().err
    assert transport.requests == []
