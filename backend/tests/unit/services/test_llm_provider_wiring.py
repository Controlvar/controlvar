"""Where the LLM evaluation providers send their calls, and what comes back.

The provider classes run as ``POST /llm-experiments/{id}/complete`` and the
judge run them; only the network is replaced (an ``httpx`` transport that
records each request), so the SDKs read their own environment as they do in
the API:

* the OpenAI SDK reads ``OPENAI_API_KEY``; without it an OpenAI variant's call
  fails; ``OPENAI_BASE_URL`` and ``ANTHROPIC_BASE_URL`` send the calls to a
  gateway;
* the local provider calls Ollama at ``http://localhost:11434`` whatever the
  environment says, counts words as tokens, and is priced at $0.00;
* the judge is called through the Anthropic SDK, with ``helpfulness`` and
  ``LLM_DEFAULT_JUDGE_MODEL`` unless the request names others;
* the results' p-value compares the business metric, or the auto-eval score
  when either variant has no business metric;
* the dashboard has no LLM screen: no page, and no call to the LLM routes.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import httpx
import httpx2
import pytest
from scipy import stats

from backend.app.core.config import settings
from backend.app.core.llm_pricing import estimate_cost
from backend.app.services import llm_proxy_service
from backend.app.services.llm_analytics_service import LLMEvaluationAnalyticsService
from backend.tests.unit.services.test_llm_analytics import (
    _make_eval,
    _make_experiment_with_data,
)

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]

OPENAI_REPLY = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 0,
    "model": "gpt-4o",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "a short answer"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
}


def anthropic_reply(text: str) -> Dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 7, "output_tokens": 3},
    }


class Recorder:
    """A transport that answers every request with ``reply``.

    ``http`` is the HTTP package the SDK is built on: the OpenAI SDK takes an
    ``httpx`` client and the Anthropic SDK an ``httpx2`` one.
    """

    def __init__(self, reply: Dict[str, Any], http: Any = httpx):
        self.reply = reply
        self.http = http
        self.requests: List[Any] = []

    def __call__(self, request: Any) -> Any:
        self.requests.append(request)
        return self.http.Response(200, json=self.reply)

    def client(self) -> Any:
        return self.http.AsyncClient(transport=self.http.MockTransport(self))


def _sdk_on(monkeypatch, module, name: str, recorder: Recorder) -> None:
    """The SDK client class, unchanged except that its network is ``recorder``."""
    real = getattr(module, name)

    class Recorded(real):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any):
            kwargs.setdefault("http_client", recorder.client())
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(module, name, Recorded)


def _complete(provider: str, model: str):
    return asyncio.run(
        llm_proxy_service.get_provider(provider).complete(
            model=model,
            messages=[{"role": "user", "content": "say something short please"}],
            temperature=0.5,
            max_tokens=50,
        )
    )


def test_openai_variants_use_the_key_and_gateway_the_openai_sdk_reads(monkeypatch):
    import openai

    recorder = Recorder(OPENAI_REPLY)
    _sdk_on(monkeypatch, openai, "AsyncOpenAI", recorder)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-the-environment")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://gateway.test/openai/v1")

    answer = _complete("openai", "gpt-4o")
    assert answer.text == "a short answer"
    (request,) = recorder.requests
    assert str(request.url) == "http://gateway.test/openai/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-from-the-environment"

    monkeypatch.delenv("OPENAI_API_KEY")
    with pytest.raises(openai.OpenAIError):
        _complete("openai", "gpt-4o")
    assert len(recorder.requests) == 1


def test_anthropic_variants_go_to_the_gateway_anthropic_base_url_names(monkeypatch):
    import anthropic

    recorder = Recorder(anthropic_reply("a short answer"), httpx2)
    _sdk_on(monkeypatch, anthropic, "AsyncAnthropic", recorder)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ak-from-the-environment")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://gateway.test/anthropic")

    answer = _complete("anthropic", "claude-sonnet-5")
    assert answer.text == "a short answer"
    (request,) = recorder.requests
    assert str(request.url) == "http://gateway.test/anthropic/v1/messages"
    assert request.headers["x-api-key"] == "ak-from-the-environment"


def test_the_local_provider_calls_ollama_on_the_api_host_counting_words(monkeypatch):
    recorder = Recorder({"response": "three short words"})
    real_client = httpx.AsyncClient

    def recorded_client(*args: Any, **kwargs: Any):
        return real_client(transport=httpx.MockTransport(recorder))

    monkeypatch.setattr(httpx, "AsyncClient", recorded_client)
    for name in ("OLLAMA_HOST", "OLLAMA_BASE_URL", "LOCAL_LLM_URL", "LLM_LOCAL_URL"):
        monkeypatch.setenv(name, "http://elsewhere.test:9999")

    answer = _complete("local", "llama3")
    (request,) = recorder.requests
    assert str(request.url) == "http://localhost:11434/api/generate"
    assert answer.input_tokens == len("say something short please".split()) == 4
    assert answer.output_tokens == 3
    for model in ("llama3", "mistral:7b", "anything-at-all"):
        assert estimate_cost("local", model, 1_000_000, 1_000_000) == 0.0


def _judge(monkeypatch, recorder: Recorder, **kwargs: Any):
    import anthropic

    _sdk_on(monkeypatch, anthropic, "AsyncAnthropic", recorder)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ak-judge")
    exp, _ = _make_experiment_with_data()
    evaluation = _make_eval(exp.id, exp.variants[0].id)
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = [evaluation]
    results = asyncio.run(
        LLMEvaluationAnalyticsService().run_llm_as_judge(db, exp.id, **kwargs)
    )
    return results, json.loads(recorder.requests[0].content)


def test_the_judge_defaults_to_helpfulness_and_the_configured_model(monkeypatch):
    monkeypatch.setattr(settings, "LLM_DEFAULT_JUDGE_MODEL", "claude-judge-default")
    recorder = Recorder(anthropic_reply('{"score": 0.8, "reasoning": "fine"}'), httpx2)
    results, sent = _judge(monkeypatch, recorder)
    assert sent["model"] == "claude-judge-default"
    assert (
        "Rate the following AI response on 'helpfulness'"
        in sent["messages"][0]["content"]
    )
    assert str(recorder.requests[0].url).endswith("/v1/messages")
    assert results[0].score == 0.8 and results[0].judge_model == "claude-judge-default"


def test_the_judge_uses_the_criteria_and_model_a_request_names(monkeypatch):
    recorder = Recorder(anthropic_reply('{"score": 0.3, "reasoning": "terse"}'), httpx2)
    results, sent = _judge(
        monkeypatch, recorder, judge_criteria="accuracy", judge_model="claude-named"
    )
    assert sent["model"] == "claude-named"
    assert "on 'accuracy'" in sent["messages"][0]["content"]
    assert results[0].score == 0.3


def test_the_judge_request_defaults_are_helpfulness_and_the_setting(monkeypatch):
    from backend.app.schemas.llm_experiments import LLMJudgeRequest

    monkeypatch.setattr(settings, "LLM_DEFAULT_JUDGE_MODEL", "claude-from-settings")
    request = LLMJudgeRequest()
    assert request.criteria == "helpfulness"
    assert request.judge_model == "claude-from-settings"


def _results(evals_spec):
    exp, _ = _make_experiment_with_data()
    control, treatment = exp.variants
    evals = []
    for variant, kind, values in evals_spec:
        target = control if variant == "control" else treatment
        for value in values:
            evals.append(_make_eval(exp.id, target.id, **{kind: value}))
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = exp
    db.query.return_value.filter.return_value.all.return_value = evals
    results = LLMEvaluationAnalyticsService().get_experiment_results(db, exp.id)
    return next(vs for vs in results.variant_stats if not vs.is_control)


def _welch(a, b) -> float:
    return float(stats.ttest_ind(a, b, equal_var=False).pvalue)


BM_C, BM_T = [0.1, 0.3, 0.2, 0.25, 0.15], [0.4, 0.35, 0.5, 0.45, 0.3]
AE_C, AE_T = [0.6, 0.7, 0.65, 0.8], [0.62, 0.68, 0.7, 0.75]


def test_the_p_value_is_on_the_business_metric_when_both_variants_have_one():
    stats_ = _results(
        [
            ("control", "bm", BM_C),
            ("treatment", "bm", BM_T),
            ("control", "auto_eval", AE_C),
            ("treatment", "auto_eval", AE_T),
        ]
    )
    assert stats_.p_value == pytest.approx(_welch(BM_C, BM_T), rel=1e-9)


@pytest.mark.parametrize("without", ["control", "treatment"])
def test_the_p_value_falls_back_to_the_auto_eval_score(without):
    spec = [("control", "auto_eval", AE_C), ("treatment", "auto_eval", AE_T)]
    spec.append(("treatment" if without == "control" else "control", "bm", BM_T))
    stats_ = _results(spec)
    assert stats_.p_value == pytest.approx(_welch(AE_C, AE_T), rel=1e-9)


#: A call to the LLM routes (``/api/v1/llm-experiments``, ``/api/v1/llm``), in
#: any spelling. A link to the LLM evaluation documentation is not one.
_LLM_ROUTE = re.compile(r"llm[-_]?experiments|/llm\b", re.IGNORECASE)


def test_the_dashboard_has_no_llm_screen():
    base = REPO_ROOT / "frontend" / "src"
    pages = base / "pages"
    names = [p.relative_to(pages).as_posix() for p in pages.rglob("*.tsx")]
    assert len(names) > 20
    assert not [n for n in names if "llm" in n.lower()], names
    checked = 0
    for path in base.rglob("*"):
        if path.suffix not in {".ts", ".tsx", ".js"} or "tests" in path.parts:
            continue
        checked += 1
        text = path.read_text(encoding="utf-8", errors="replace")
        assert not _LLM_ROUTE.search(text), path
    assert checked > 100
