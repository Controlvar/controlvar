"""examples/contract_smoke.py: what it reads from the environment and what it sends.

The SDK Live Contract job runs the script against a real API with every
variable set. These run it in-process with the Python SDK's default HTTP layer
replaced by :class:`FakeTransport`: no network, so the defaults it falls back
to, and which steps go through OpenFeature and which through
``provider.client``, are held.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from openfeature import api
from openfeature.provider.no_op_provider import NoOpProvider

import controlvar.client as client_module
from controlvar.testing import FakeTransport

SMOKE = Path(__file__).resolve().parents[1] / "examples" / "contract_smoke.py"
VARIABLES = (
    "CONTROLVAR_API_URL",
    "CONTROLVAR_API_KEY",
    "EXPERIMENTLY_API_URL",
    "EXPERIMENTLY_API_KEY",
    "CONTRACT_EXPERIMENT_KEY",
    "CONTRACT_FLAG_KEY",
    "CONTRACT_USER_ID",
)


def fake_api(experiment_key: str, flag_key: str) -> FakeTransport:
    transport = FakeTransport()
    transport.route(
        "POST",
        "/api/v1/tracking/assign",
        json={
            "experiment_key": experiment_key,
            "user_id": "ignored",
            "variant_id": "var-1",
            "variant_name": "treatment",
            "is_control": False,
            "configuration": None,
        },
    )
    transport.route(
        "GET", f"/api/v1/feature-flags/evaluate/{flag_key}", json={"key": flag_key, "enabled": True, "config": None}
    )
    transport.route("POST", "/api/v1/tracking/track", json={})
    transport.route("POST", "/api/v1/tracking/batch", json={"success_count": 2, "failure_count": 0, "errors": []})
    return transport


@pytest.fixture
def smoke(monkeypatch):
    for name in VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CONTROLVAR_API_KEY", "smoke-key")
    spec = importlib.util.spec_from_file_location("openfeature_contract_smoke_under_test", SMOKE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    api.set_provider(NoOpProvider())


def test_it_reads_its_settings_from_the_environment_with_these_defaults(smoke, monkeypatch, capsys):
    transport = fake_api("sdk_contract_ab", "sdk_contract_flag")
    monkeypatch.setattr(client_module, "UrllibTransport", lambda: transport)
    smoke.main()
    assert all(r.url.startswith("http://localhost:8000/api/v1/") for r in transport.requests)
    assert all(r.headers["X-API-Key"] == "smoke-key" for r in transport.requests)
    assign = transport.requests[0].json()
    assert assign["experiment_key"] == "sdk_contract_ab"
    assert assign["user_id"].startswith("smoke-") and len(assign["user_id"]) == len("smoke-") + 36
    assert json.loads(capsys.readouterr().out) == {
        "sdk": "openfeature-python",
        "assign": {"variant_name": "treatment", "is_control": False, "sticky": True},
        "flag": {"enabled": True},
        "track": {"ok": True},
        "fanout": {"ok": True},
    }


def test_the_flag_goes_through_openfeature_and_the_rest_through_provider_client(smoke, monkeypatch, capsys):
    monkeypatch.setenv("CONTROLVAR_API_URL", "http://api.test")
    monkeypatch.setenv("CONTRACT_EXPERIMENT_KEY", "exp_a")
    monkeypatch.setenv("CONTRACT_FLAG_KEY", "flag_a")
    monkeypatch.setenv("CONTRACT_USER_ID", "user-a")
    transport = fake_api("exp_a", "flag_a")
    monkeypatch.setattr(client_module, "UrllibTransport", lambda: transport)
    resolved = []
    original = smoke.ExperimentationProvider.resolve_boolean_details

    def recording(self, flag_key, default_value, evaluation_context=None):
        resolved.append(flag_key)
        return original(self, flag_key, default_value, evaluation_context)

    monkeypatch.setattr(smoke.ExperimentationProvider, "resolve_boolean_details", recording)
    smoke.main()

    assert resolved == ["flag_a"], "the flag was not resolved through the OpenFeature API"
    assert transport.calls() == [
        "POST /api/v1/tracking/assign",
        "POST /api/v1/tracking/assign",
        "GET /api/v1/feature-flags/evaluate/flag_a",
        "POST /api/v1/tracking/track",
        "POST /api/v1/tracking/batch",
        "POST /api/v1/tracking/batch",
    ]
    assert transport.requests[2].query == {"user_id": "user-a"}
    assert len(capsys.readouterr().out.strip().splitlines()) == 1


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
def test_the_new_names_win_and_the_former_names_are_still_read(smoke, monkeypatch, names, url, key):
    monkeypatch.delenv("CONTROLVAR_API_KEY")
    for name, value in names.items():
        monkeypatch.setenv(name, value)
    transport = fake_api("sdk_contract_ab", "sdk_contract_flag")
    monkeypatch.setattr(client_module, "UrllibTransport", lambda: transport)

    smoke.main()

    assert transport.requests
    assert {r.url.split("/api/v1/")[0] for r in transport.requests} == {url}
    assert {r.headers["X-API-Key"] for r in transport.requests} == {key}
