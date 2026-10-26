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

import controlvar.client as client_module
from controlvar.testing import FakeTransport

SDK_ROOT = Path(__file__).resolve().parents[1]
SMOKE = SDK_ROOT / "examples" / "contract_smoke.py"
VARIABLES = (
    "CONTROLVAR_API_URL",
    "CONTROLVAR_API_KEY",
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
    monkeypatch.setenv("CONTROLVAR_API_KEY", "smoke-key")
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
    environment.setenv("CONTROLVAR_API_URL", "http://api.test")
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


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        (([], []), "expected one POST /api/v1/tracking/batch"),  # the event went untagged to /track
        ((["exp_a"], []), "expected the experiment 'exp_a' then the flag 'flag_a'"),
        ((["exp_a", "other"], ["flag_a"]), "expected the experiment 'exp_a' then the flag 'flag_a'"),
    ],
    ids=["untagged", "flag-missing", "extra-key"],
)
def test_it_fails_unless_the_key_less_track_is_one_batch_to_exactly_the_experiment_and_the_flag(
    environment, capsys, keys, expected
):
    environment.setenv("CONTRACT_EXPERIMENT_KEY", "exp_a")
    environment.setenv("CONTRACT_FLAG_KEY", "flag_a")
    transport = fake_api("exp_a", "flag_a")
    environment.setattr(client_module, "UrllibTransport", lambda: transport)
    environment.setattr("controlvar.attribution.AttributedKeys.keys", lambda self, user_id: keys)
    with pytest.raises(SystemExit) as stopped:
        load_smoke(environment).main()
    assert stopped.value.code == 1
    assert expected in capsys.readouterr().err


def test_it_fails_when_an_entry_of_the_fan_out_was_not_stored(environment, capsys):
    transport = fake_api("sdk_contract_ab", "sdk_contract_flag")
    transport.route(
        "POST",
        "/api/v1/tracking/batch",
        json={"success_count": 1, "failure_count": 1, "errors": [{"index": 0, "error": "nope"}]},
    )
    environment.setattr(client_module, "UrllibTransport", lambda: transport)
    with pytest.raises(SystemExit) as stopped:
        load_smoke(environment).main()
    assert stopped.value.code == 1
    assert "the fan-out's entries were not all stored" in capsys.readouterr().err


def test_it_stops_without_a_key(environment, capsys):
    environment.delenv("CONTROLVAR_API_KEY")
    transport = FakeTransport()
    environment.setattr(client_module, "UrllibTransport", lambda: transport)
    with pytest.raises(SystemExit) as stopped:
        load_smoke(environment).main()
    assert stopped.value.code == 1
    assert "CONTROLVAR_API_KEY is required" in capsys.readouterr().err
    assert transport.requests == []


#: Three distinct values per setting, none of them the default or what CI sets.
NEW_URL, OLD_URL, DEFAULT_URL = "http://127.0.0.1:9", "http://127.0.0.1:7", "http://localhost:8000"


@pytest.mark.parametrize(
    ("names", "url", "key"),
    [
        pytest.param(
            {"EXPERIMENTLY_API_URL": OLD_URL, "EXPERIMENTLY_API_KEY": "old-key"},
            OLD_URL,
            "old-key",
            id="former-names-only",
        ),
        pytest.param(
            {"CONTROLVAR_API_URL": NEW_URL, "CONTROLVAR_API_KEY": "new-key"},
            NEW_URL,
            "new-key",
            id="new-names-only",
        ),
        pytest.param(
            {
                "CONTROLVAR_API_URL": NEW_URL,
                "CONTROLVAR_API_KEY": "new-key",
                "EXPERIMENTLY_API_URL": OLD_URL,
                "EXPERIMENTLY_API_KEY": "old-key",
            },
            NEW_URL,
            "new-key",
            id="both-new-wins",
        ),
        pytest.param({"EXPERIMENTLY_API_KEY": "old-key"}, DEFAULT_URL, "old-key", id="no-url"),
    ],
)
def test_the_new_names_win_and_the_former_names_are_still_read(environment, names, url, key):
    environment.delenv("CONTROLVAR_API_KEY")
    for name, value in names.items():
        environment.setenv(name, value)
    transport = fake_api("sdk_contract_ab", "sdk_contract_flag")
    environment.setattr(client_module, "UrllibTransport", lambda: transport)

    load_smoke(environment).main()

    assert transport.requests
    assert {r.url.split("/api/v1/")[0] for r in transport.requests} == {url}
    assert {r.headers["X-API-Key"] for r in transport.requests} == {key}
