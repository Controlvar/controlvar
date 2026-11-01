"""The keys both compose files pass to the API, empty by default (#1253, #256).

Shared by ``test_production_compose.py``, ``test_dev_compose_services.py`` and
``test_compose_feature_keys.py``. Each key turns a feature on when it is set;
an empty value is the same as an unset one.

The LLM keys are not a list typed here: ``LLM_PROVIDER_KEYS`` has one entry
per provider a variant may name, and ``test_compose_feature_keys.py`` holds its
keys to ``llm_proxy_service.PROVIDERS``. A provider added there fails that test
until it is given its key here (or ``None``), and the key then has to be in
both compose files.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

#: Each LLM provider an LLM experiment variant may name, and the environment
#: variable its key is read from; ``None``: it takes no key.
LLM_PROVIDER_KEYS: Dict[str, Optional[str]] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GEMINI_API_KEY",
    "local": None,
}

#: The Fernet keys that encrypt stored warehouse credentials (full profile).
WAREHOUSE_KEYS = "WAREHOUSE_CREDENTIALS_KEYS"

#: Every key both compose files pass to the API service as ``${NAME:-}``.
FEATURE_KEYS: Tuple[str, ...] = (WAREHOUSE_KEYS,) + tuple(
    key for key in LLM_PROVIDER_KEYS.values() if key
)
