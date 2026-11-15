"""The feature keys both compose files pass to the API (#1253, #256).

``WAREHOUSE_CREDENTIALS_KEYS`` and the LLM provider keys reach the API from
``.env`` through both compose files, as ``${NAME:-}``: empty when ``.env`` does
not set them. ``test_production_compose.py`` and ``test_dev_compose_services.py``
hold each file's lines. This holds the rest:

* the LLM keys are derived, not typed: one per provider a variant may name
  (``llm_proxy_service.PROVIDERS``), so a provider added there, or a key left
  out of a compose file, fails here by name;
* an empty value is the same as an unset one to every reader of the keys, so
  passing them empty by default turns nothing on;
* ``scripts/check_compose_config.py``, which the Compose Production job runs
  through ``docker compose config``, checks the same keys and never prints a
  value.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Dict

import httpx
import pytest
import yaml

from backend.app.core.settings_rules import credential_keys_error
from backend.app.services import llm_proxy_service
from backend.app.services.ai_design_service import AIDesignService
from backend.tests.unit.infrastructure._compose_feature_keys import (
    FEATURE_KEYS,
    LLM_PROVIDER_KEYS,
    WAREHOUSE_KEYS,
)

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
COMPOSE_FILES = {
    "development": REPO_ROOT / "docker-compose.yml",
    "production": REPO_ROOT / "deploy" / "compose" / "compose.yml",
}
SCRIPT = REPO_ROOT / "scripts" / "check_compose_config.py"

#: A value that stands for a real key. Only its presence is ever checked.
SET = "sk-x"


def _api_environment(path: Path) -> Dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["services"]["api"][
        "environment"
    ]


# ---------------------------------------------------------------------------
# The provider keys are derived from the providers
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_the_table_names_every_llm_provider():
    providers = set(llm_proxy_service.PROVIDERS)
    assert set(LLM_PROVIDER_KEYS) == providers, (
        f"providers without a row in _compose_feature_keys.LLM_PROVIDER_KEYS: "
        f"{sorted(providers - set(LLM_PROVIDER_KEYS))}; rows for no provider: "
        f"{sorted(set(LLM_PROVIDER_KEYS) - providers)}"
    )


def test_the_gemini_row_is_the_variable_the_provider_reads():
    assert LLM_PROVIDER_KEYS["google"] == llm_proxy_service.GEMINI_API_KEY_ENV


@pytest.mark.regression
@pytest.mark.parametrize("stack", sorted(COMPOSE_FILES))
def test_each_compose_file_passes_exactly_the_provider_keys(stack):
    """Every ``*_API_KEY`` the api is given empty by default is a provider's
    key, and every provider's key is one of them."""
    environment = _api_environment(COMPOSE_FILES[stack])
    passed = {
        name
        for name, value in environment.items()
        if name.endswith("_API_KEY") and value == f"${{{name}:-}}"
    }
    expected = {key for key in LLM_PROVIDER_KEYS.values() if key}
    assert passed == expected, (
        f"{stack}: provider keys not passed as ${{NAME:-}}: "
        f"{sorted(expected - passed)}; passed but no provider's: "
        f"{sorted(passed - expected)}"
    )


def test_the_feature_keys_are_the_warehouse_keys_and_the_provider_keys():
    assert FEATURE_KEYS == (
        WAREHOUSE_KEYS,
        *(key for key in LLM_PROVIDER_KEYS.values() if key),
    )
    assert len(set(FEATURE_KEYS)) == len(FEATURE_KEYS) == 4


# ---------------------------------------------------------------------------
# Empty is unset, to every reader
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "   "])
def test_an_empty_warehouse_key_list_is_not_an_error(value):
    """Preflight and the modules' settings refuse only what this calls an
    error, so an empty WAREHOUSE_CREDENTIALS_KEYS starts the API, and the
    feature refuses at use (modules' test_credential_crypto.py)."""
    assert credential_keys_error(value) is None


@pytest.mark.parametrize("sdk", ["Anthropic", "AsyncAnthropic"])
def test_the_anthropic_sdk_reads_an_empty_key_as_none(monkeypatch, sdk):
    import anthropic

    client = getattr(anthropic, sdk)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert client().api_key is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    assert client().api_key is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", SET)
    assert client().api_key == SET


def test_the_openai_sdk_refuses_an_empty_key_as_it_refuses_none(monkeypatch):
    import openai

    monkeypatch.delenv("OPENAI_ADMIN_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(openai.OpenAIError, match="Missing credentials") as unset:
        openai.AsyncOpenAI()
    monkeypatch.setenv("OPENAI_API_KEY", "")
    with pytest.raises(openai.OpenAIError, match="Missing credentials") as empty:
        openai.AsyncOpenAI()
    assert str(empty.value) == str(unset.value)
    monkeypatch.setenv("OPENAI_API_KEY", SET)
    assert openai.AsyncOpenAI().api_key == SET


async def test_the_gemini_provider_refuses_an_empty_key_before_sending(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
        )

    provider = llm_proxy_service.GoogleProvider(transport=httpx.MockTransport(handler))
    messages = [{"role": "user", "content": "hi"}]
    monkeypatch.delenv("GEMINI_BASE_URL", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "")
    with pytest.raises(
        llm_proxy_service.GeminiError, match="GEMINI_API_KEY is not set"
    ):
        await provider.complete("gemini-1.5-flash", messages)
    assert seen == []
    monkeypatch.setenv("GEMINI_API_KEY", SET)
    assert (await provider.complete("gemini-1.5-flash", messages)).text == "ok"
    (request,) = seen
    assert request.headers["x-goog-api-key"] == SET


def test_ai_design_uses_its_templates_with_an_empty_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert AIDesignService.is_ai_available() is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    assert AIDesignService.is_ai_available() is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", SET)
    assert AIDesignService.is_ai_available() is True


# ---------------------------------------------------------------------------
# scripts/check_compose_config.py checks the same keys
# ---------------------------------------------------------------------------


def _load_script():
    spec = importlib.util.spec_from_file_location("check_compose_config", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = _load_script()


def test_the_config_check_names_the_same_keys():
    assert check.FEATURE_KEYS == FEATURE_KEYS


def test_the_config_check_passes_empty_keys_and_the_values_env_sets():
    empty = dict.fromkeys(FEATURE_KEYS, "")
    assert check.feature_key_failures(empty, {}) == []
    given = {name: f"value-of-{name}" for name in FEATURE_KEYS}
    assert check.feature_key_failures(dict(given), given) == []


@pytest.mark.parametrize("name", FEATURE_KEYS)
def test_the_config_check_refuses_a_default_and_names_no_value(name):
    """``${NAME:-x}`` resolves to "x" while .env leaves the key out."""
    resolved = dict.fromkeys(FEATURE_KEYS, "")
    resolved[name] = "a-default-nobody-set"
    failures = check.feature_key_failures(resolved, {})
    assert failures == [
        f"{name} on the api service is not empty, and .env does not set it"
    ]
    assert "a-default-nobody-set" not in " ".join(failures)


@pytest.mark.parametrize("name", FEATURE_KEYS)
def test_the_config_check_refuses_a_key_that_does_not_arrive(name):
    given = {key: f"value-of-{key}" for key in FEATURE_KEYS}
    resolved = {key: value for key, value in given.items() if key != name}
    assert check.feature_key_failures(resolved, given) == [
        f"{name} is not passed to the api service"
    ]
    resolved[name] = ""
    failures = check.feature_key_failures(resolved, given)
    assert failures == [f"{name} on the api service is not the value .env sets"]
    assert given[name] not in " ".join(failures)
