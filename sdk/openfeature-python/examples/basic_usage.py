"""
Basic usage example: Controlvar OpenFeature Python Provider.

Run from the repository root with the virtual environment activated:
    source venv/bin/activate
    CONTROLVAR_API_KEY=<key> python sdk/openfeature-python/examples/basic_usage.py

(earlier versions of this example read EXPERIMENTLY_API_URL / EXPERIMENTLY_API_KEY.)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Import from the source tree when the packages are not installed.
_HERE = Path(__file__).resolve()
for _path in (_HERE.parents[1] / "src", _HERE.parents[2] / "python"):
    sys.path.insert(0, str(_path))

from openfeature import api  # noqa: E402
from openfeature.evaluation_context import EvaluationContext  # noqa: E402

from controlvar_openfeature import ExperimentationProvider  # noqa: E402


def main() -> None:
    # 1. Create and register the provider. Flags are evaluated by the server and cached
    #    per user + flag for cache_ttl seconds.
    provider = ExperimentationProvider(
        api_key=os.environ.get("CONTROLVAR_API_KEY", "your-api-key"),
        base_url=os.environ.get("CONTROLVAR_API_URL", "http://localhost:8000"),
        cache_ttl=300,
        timeout=10,
    )
    api.set_provider(provider)

    # 2. Get a client.
    client = api.get_client()

    # 3. Build the evaluation context. targeting_key is the platform user_id and is
    #    required; attributes are kept for your own hooks (they are not sent to the
    #    evaluate endpoint).
    ctx = EvaluationContext(targeting_key="user-12345", attributes={"country": "US", "plan": "pro"})

    # 4. Evaluate flags — same API regardless of provider.
    print("dark-mode enabled:", client.get_boolean_value("dark-mode", False, ctx))
    print("checkout-experiment variant:", client.get_string_value("checkout-experiment", "control", ctx))
    print("cart-max-items:", client.get_integer_value("cart-max-items", 10, ctx))
    print("price-multiplier:", client.get_float_value("price-multiplier", 1.0, ctx))
    print("feature-config:", client.get_object_value("feature-config", {}, ctx))

    # 5. Experiments and tracking are outside OpenFeature: use the SDK client underneath.
    assignment = provider.client.get_variant("checkout_flow", "user-12345", {"plan": "pro"})
    print("checkout_flow variant:", assignment)
    provider.client.track("user-12345", "page_view", properties={"page": "/"})


if __name__ == "__main__":
    main()
