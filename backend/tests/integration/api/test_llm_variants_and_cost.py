"""LLM experiments through the API, against a real database.

* adding and updating a variant take the DEVELOPER or ADMIN role: an ANALYST or
  a VIEWER is answered 403 and nothing changes;
* each variant's cost per call is in ``GET /api/v1/llm-experiments/{id}/results``
  as ``mean_cost_usd``, from the tokens the provider reported and the price
  table (the provider's network call is the only thing replaced).
"""

from __future__ import annotations

import uuid

import pytest

from backend.app.models.user import UserRole
from backend.app.services import llm_proxy_service
from backend.app.services.llm_proxy_service import ProviderResponse
from backend.tests.integration.api.route_helpers import (
    api_client,
    auth,
    local_sign_in,
    make_user,
)

pytestmark = [pytest.mark.integration, pytest.mark.regression]

BASE = "/api/v1/llm-experiments"


def _variant(name, is_control, provider, model, split=0.5):
    return {
        "name": name,
        "is_control": is_control,
        "traffic_split": split,
        "provider": provider,
        "model_name": model,
        "system_prompt": "Be brief.",
        "prompt_template": "Answer: {{question}}",
        "temperature": 0.5,
        "max_tokens": 100,
    }


def _experiment(client, owner):
    resp = client.post(
        f"{BASE}/",
        json={
            "name": f"LLM claims {uuid.uuid4().hex[:6]}",
            "description": "LLM page claims",
            "task_type": "chat_completion",
            "evaluation_metric": "business_metric",
            "variants": [
                _variant("control", True, "openai", "gpt-4o"),
                _variant("treatment", False, "openai", "gpt-4o-mini"),
            ],
        },
        headers=auth(owner),
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


@pytest.fixture(autouse=True)
def _sign_in(monkeypatch):
    local_sign_in(monkeypatch)


@pytest.mark.parametrize(
    "role, allowed",
    [
        (UserRole.VIEWER, False),
        (UserRole.ANALYST, False),
        (UserRole.DEVELOPER, True),
        (UserRole.ADMIN, True),
    ],
)
def test_adding_and_updating_variants_takes_developer_or_admin(
    db_session, role, allowed
):
    with api_client(db_session) as client:
        owner = make_user(db_session, UserRole.DEVELOPER, tag="llmown")
        experiment = _experiment(client, owner)
        caller = make_user(db_session, role, tag="llmcall")
        added = client.post(
            f"{BASE}/{experiment['id']}/variants",
            json=_variant("third", False, "anthropic", "claude-haiku-4-5", 0.2),
            headers=auth(caller),
        )
        variant_id = experiment["variants"][1]["id"]
        updated = client.put(
            f"{BASE}/{experiment['id']}/variants/{variant_id}",
            json={"temperature": 0.9},
            headers=auth(caller),
        )
        stored = client.get(f"{BASE}/{experiment['id']}", headers=auth(owner)).json()
        if allowed:
            assert added.status_code == 201, added.text
            assert updated.status_code == 200, updated.text
            assert len(stored["variants"]) == 3
        else:
            assert added.status_code == 403, added.text
            assert updated.status_code == 403, updated.text
            assert len(stored["variants"]) == 2
            treatment = next(v for v in stored["variants"] if v["id"] == variant_id)
            assert treatment["temperature"] == 0.5


class _FixedTokens(llm_proxy_service.BaseProvider):
    async def complete(self, model, messages, temperature=0.7, max_tokens=1000, **kw):
        return ProviderResponse(text="ok", input_tokens=2000, output_tokens=1000)


def test_each_variants_mean_cost_is_in_the_results(db_session, monkeypatch):
    monkeypatch.setitem(llm_proxy_service.PROVIDERS, "openai", _FixedTokens())
    with api_client(db_session) as client:
        owner = make_user(db_session, UserRole.DEVELOPER, tag="llmcost")
        experiment = _experiment(client, owner)
        started = client.post(f"{BASE}/{experiment['id']}/start", headers=auth(owner))
        assert started.status_code == 200, started.text
        # Users until both variants have answered at least twice (the split is
        # a hash of the experiment's id, so which users go where varies).
        seen = {}
        calls = 0
        while min(seen.get(v["id"], 0) for v in experiment["variants"]) < 2:
            assert calls < 60, seen
            resp = client.post(
                f"{BASE}/{experiment['id']}/complete",
                json={
                    "user_id": f"llm-user-{calls}",
                    "input_variables": {"question": "q"},
                },
                headers=auth(owner),
            )
            assert resp.status_code == 200, resp.text
            variant_id = str(resp.json()["variant_id"])
            seen[variant_id] = seen.get(variant_id, 0) + 1
            calls += 1
        results = client.get(f"{BASE}/{experiment['id']}/results", headers=auth(owner))
        assert results.status_code == 200, results.text
        by_name = {v["variant_name"]: v for v in results.json()["variant_stats"]}
        # gpt-4o: $0.0025 and $0.010 per 1k tokens; gpt-4o-mini: $0.00015 and $0.0006.
        expected = {"control": 2 * 0.0025 + 0.010, "treatment": 2 * 0.00015 + 0.0006}
        for name, cost in expected.items():
            assert by_name[name]["n_evaluations"] >= 2, by_name
            assert by_name[name]["mean_cost_usd"] == pytest.approx(cost, rel=1e-9)
        assert sum(v["n_evaluations"] for v in by_name.values()) == calls
