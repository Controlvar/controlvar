"""A judge call that fails or gives no score leaves the evaluation unscored (#1192).

Through the API on PostgreSQL: ``POST /api/v1/llm-experiments/{id}/judge``
with the Anthropic client replaced (it answers each evaluation from a table,
or raises), then ``GET /api/v1/llm-experiments/{id}/results``. The statistics
are compared with scipy on the scored values alone, and the stored column is
read with SQL, because a NaN stored there comes out of the API as ``null``
and would look exactly like the right answer.
"""

from __future__ import annotations

import statistics
import uuid
from types import SimpleNamespace
from typing import Dict, List, Union
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from scipy import stats
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.app.core.database_config import get_schema_name
from backend.app.models.llm_experiment import LLMEvaluation
from backend.app.services.llm_analytics_service import (
    JUDGE_CALL_FAILED_REASONING,
    JUDGE_NO_SCORE_REASONING,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_db,
    pytest.mark.regression,
]

JUDGE_MODEL = "claude-judge-under-test"


def _experiment(client: TestClient, name: str) -> tuple[str, str, str]:
    payload = {
        "name": name,
        "task_type": "chat_completion",
        "evaluation_metric": "relevance",
        "variants": [
            {
                "name": "control",
                "is_control": True,
                "traffic_split": 0.5,
                "provider": "anthropic",
                "model_name": "claude-sonnet-5",
                "prompt_template": "Answer: {{question}}",
            },
            {
                "name": "treatment",
                "is_control": False,
                "traffic_split": 0.5,
                "provider": "openai",
                "model_name": "gpt-4o",
                "prompt_template": "Short answer: {{question}}",
            },
        ],
    }
    response = client.post("/api/v1/llm-experiments/", json=payload)
    assert response.status_code == 201, response.text
    body = response.json()
    by_name = {v["name"]: v["id"] for v in body["variants"]}
    return body["id"], by_name["control"], by_name["treatment"]


def _evaluation(
    db: Session,
    experiment_id: str,
    variant_id: str,
    response: str,
    auto_eval_score: float | None = None,
) -> str:
    evaluation = LLMEvaluation(
        llm_experiment_id=uuid.UUID(experiment_id),
        variant_id=uuid.UUID(variant_id),
        user_id=f"user-{response}",
        input_variables={},
        rendered_prompt="A question",
        model_response=response,
        latency_ms=100,
        input_tokens=5,
        output_tokens=5,
        estimated_cost_usd=0.001,
        auto_eval_score=auto_eval_score,
    )
    db.add(evaluation)
    db.commit()
    db.refresh(evaluation)
    return str(evaluation.id)


class _Judge:
    """``anthropic.AsyncAnthropic`` replaced: the reply to each judge call is
    looked up by the response the judge is shown, and raised when it is an
    exception. ``calls`` counts the calls made."""

    def __init__(self, replies: Dict[str, Union[str, BaseException]]):
        self.replies = replies
        self.calls: List[str] = []

    async def create(self, *, model: str, max_tokens: int, messages: list):
        content = messages[0]["content"]
        (response,) = [r for r in self.replies if f"Response:\n{r}\n" in content]
        self.calls.append(response)
        reply = self.replies[response]
        if isinstance(reply, BaseException):
            raise reply
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=reply)])

    def patch(self):
        client = MagicMock()
        client.messages.create = self.create
        return patch("anthropic.AsyncAnthropic", return_value=client)


def _stored(db: Session, evaluation_id: str) -> tuple:
    """(auto_eval_score IS NULL, auto_eval_score as text), straight from SQL."""
    row = db.execute(
        text(
            f"SELECT auto_eval_score IS NULL, auto_eval_score::text "
            f"FROM {get_schema_name()}.llm_evaluations WHERE id = :id"
        ),
        {"id": evaluation_id},
    ).one()
    return tuple(row)


def test_unscored_evaluations_are_left_out_of_the_statistics_and_counted(
    admin_client: TestClient, db_session: Session
):
    exp_id, control_id, treatment_id = _experiment(admin_client, "Unscored count")
    control_scores = [0.62, 0.71, 0.55, 0.68, 0.74, 0.59]
    treatment_scores = [0.81, 0.77, 0.90, 0.72, 0.86, 0.79]
    replies: Dict[str, Union[str, BaseException]] = {}
    judged: List[str] = []
    for i, score in enumerate(control_scores):
        replies[f"c{i}"] = f'{{"score": {score}, "reasoning": "ok"}}'
        judged.append(_evaluation(db_session, exp_id, control_id, f"c{i}"))
    for i, score in enumerate(treatment_scores):
        replies[f"t{i}"] = f'{{"score": {score}, "reasoning": "ok"}}'
        judged.append(_evaluation(db_session, exp_id, treatment_id, f"t{i}"))
    # Three the judge fails on: two calls that raise, one reply with no score
    replies["fail0"] = RuntimeError("provider down")
    replies["fail1"] = TypeError("Could not resolve authentication method.")
    replies["noscore"] = "I would give it 1 out of 5."
    failed = {
        r: _evaluation(db_session, exp_id, treatment_id, r)
        for r in ("fail0", "fail1", "noscore")
    }
    judged += failed.values()
    # And one the judge is never run on
    _evaluation(db_session, exp_id, treatment_id, "never")

    judge = _Judge(replies)
    with judge.patch():
        response = admin_client.post(
            f"/api/v1/llm-experiments/{exp_id}/judge",
            json={"judge_model": JUDGE_MODEL, "evaluation_ids": judged},
        )
    assert response.status_code == 200, response.text
    assert len(judge.calls) == 15
    items = {item["evaluation_id"]: item for item in response.json()}
    assert items[failed["fail0"]]["error"] == "call_failed"
    assert items[failed["fail1"]]["error"] == "call_failed"
    assert items[failed["noscore"]]["error"] == "no_score"
    assert sum(1 for item in items.values() if item["score"] is None) == 3

    response = admin_client.get(f"/api/v1/llm-experiments/{exp_id}/results")
    assert response.status_code == 200, response.text
    results = response.json()
    by_name = {vs["variant_name"]: vs for vs in results["variant_stats"]}
    control, treatment = by_name["control"], by_name["treatment"]

    # The count: exact, per variant, and the total is their sum
    assert control["n_auto_eval_unscored"] == 0
    assert treatment["n_auto_eval_unscored"] == 4
    assert results["total_auto_eval_unscored"] == 4
    assert treatment["n_evaluations"] == 10
    assert results["total_evaluations"] == 16

    # The statistics: on the scored values only
    assert control["mean_auto_eval_score"] == statistics.mean(control_scores)
    assert treatment["mean_auto_eval_score"] == statistics.mean(treatment_scores)
    expected_p = stats.ttest_ind(
        control_scores, treatment_scores, equal_var=False
    ).pvalue
    assert abs(treatment["p_value"] - expected_p) <= 1e-9, (
        treatment["p_value"],
        expected_p,
    )
    lower, upper = stats.t.interval(
        0.95,
        len(treatment_scores) - 1,
        loc=statistics.mean(treatment_scores),
        scale=stats.sem(treatment_scores),
    )
    assert abs(treatment["auto_eval_ci_lower"] - lower) <= 1e-9
    assert abs(treatment["auto_eval_ci_upper"] - upper) <= 1e-9
    pooled = (
        (len(control_scores) - 1) * statistics.variance(control_scores)
        + (len(treatment_scores) - 1) * statistics.variance(treatment_scores)
    ) / (len(control_scores) + len(treatment_scores) - 2)
    expected_d = (
        statistics.mean(treatment_scores) - statistics.mean(control_scores)
    ) / pooled**0.5
    assert abs(treatment["effect_size"] - expected_d) <= 1e-9


def test_the_judge_entry_is_null_with_its_reason_and_the_stored_score_is_null(
    admin_client: TestClient, db_session: Session
):
    exp_id, control_id, _ = _experiment(admin_client, "Null not NaN")
    nan_reply = '{"score": NaN, "reasoning": "unsure"}'
    # One scored 0.8 by an earlier run, whose new run fails
    earlier = _evaluation(db_session, exp_id, control_id, "earlier", 0.8)
    nan = _evaluation(db_session, exp_id, control_id, "nan")
    judge = _Judge({"earlier": RuntimeError("provider down"), "nan": nan_reply})
    with judge.patch():
        response = admin_client.post(
            f"/api/v1/llm-experiments/{exp_id}/judge",
            json={"judge_model": JUDGE_MODEL},
        )
    assert response.status_code == 200, response.text
    # The column first: NULL, not 'NaN' (PostgreSQL stores a NaN float, and the
    # API would show it as null, exactly like the right answer), and the
    # earlier 0.8 is replaced: the stored value is the latest judge run's
    assert _stored(db_session, nan) == (True, None)
    assert _stored(db_session, earlier) == (True, None)
    items = {item["evaluation_id"]: item for item in response.json()}
    assert items == {
        earlier: {
            "evaluation_id": earlier,
            "score": None,
            "reasoning": JUDGE_CALL_FAILED_REASONING,
            "judge_model": JUDGE_MODEL,
            "error": "call_failed",
        },
        nan: {
            "evaluation_id": nan,
            "score": None,
            "reasoning": JUDGE_NO_SCORE_REASONING + nan_reply,
            "judge_model": JUDGE_MODEL,
            "error": "no_score",
        },
    }


def test_zero_spread_gives_a_null_p_value_and_effect_size(
    admin_client: TestClient, db_session: Session
):
    exp_id, control_id, treatment_id = _experiment(admin_client, "Zero spread")
    for i in range(3):
        _evaluation(db_session, exp_id, control_id, f"c{i}", 0.8)
    for i in range(2):
        _evaluation(db_session, exp_id, treatment_id, f"t{i}", 0.9)

    response = admin_client.get(f"/api/v1/llm-experiments/{exp_id}/results")
    assert response.status_code == 200, response.text
    treatment = next(
        vs for vs in response.json()["variant_stats"] if not vs["is_control"]
    )
    # The keys are there, and null: not computed, rather than 1.0 and 0.0
    assert "p_value" in treatment and treatment["p_value"] is None
    assert "effect_size" in treatment and treatment["effect_size"] is None
    assert treatment["mean_auto_eval_score"] == 0.9
