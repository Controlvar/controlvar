#!/usr/bin/env python3
"""Contract smoke for the Python SDK against a live backend.

Run from the repository root (no install needed):

    CONTROLVAR_API_KEY=<key> python sdk/python/examples/contract_smoke.py

Env: CONTROLVAR_API_URL (default http://localhost:8000), CONTROLVAR_API_KEY (required;
the former names EXPERIMENTLY_API_URL / EXPERIMENTLY_API_KEY are read when these are unset),
CONTRACT_EXPERIMENT_KEY (default sdk_contract_ab), CONTRACT_FLAG_KEY (default sdk_contract_flag),
CONTRACT_USER_ID (default: random "smoke-<uuid>").

Prints exactly one JSON line on stdout and exits 0; on failure prints one line to stderr
and exits 1.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

# Make ``controlvar`` importable straight from the source tree.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from controlvar import ExperimentationClient, ExperimentationError  # noqa: E402


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    sys.exit(1)


def main() -> None:
    api_url = (
        os.environ.get("CONTROLVAR_API_URL")
        or os.environ.get("EXPERIMENTLY_API_URL")
        or "http://localhost:8000"
    )
    api_key = os.environ.get("CONTROLVAR_API_KEY") or os.environ.get("EXPERIMENTLY_API_KEY")
    if not api_key:
        fail("CONTROLVAR_API_KEY is required")
    experiment_key = os.environ.get("CONTRACT_EXPERIMENT_KEY", "sdk_contract_ab")
    flag_key = os.environ.get("CONTRACT_FLAG_KEY", "sdk_contract_flag")
    user_id = os.environ.get("CONTRACT_USER_ID") or f"smoke-{uuid.uuid4()}"
    attributes = {"source": "contract_smoke", "sdk": "python"}

    client = ExperimentationClient(api_url, api_key)

    # 1. Assign twice. The cache is cleared in between so the second call really
    #    goes to the server and proves the assignment is sticky there.
    try:
        first = client.get_assignment(experiment_key, user_id, attributes)
        client.clear_cache()
        second = client.get_assignment(experiment_key, user_id, attributes)
    except ExperimentationError as exc:
        fail(f"assign failed: {exc} (status={exc.status}, body={exc.body!r})")
    if first.variant_name not in ("control", "treatment"):
        fail(f"unexpected variant_name {first.variant_name!r}")
    sticky = first == second
    if not sticky:
        fail(f"assignment not sticky: {first.variant_name} then {second.variant_name}")

    # 2. Evaluate the flag.
    try:
        flag = client.get_feature_flag(flag_key, user_id)
    except ExperimentationError as exc:
        fail(f"flag evaluation failed: {exc} (status={exc.status}, body={exc.body!r})")
    if not isinstance(flag.enabled, bool):
        fail(f"flag.enabled is not a bool: {flag.enabled!r}")

    # 3. Track with an experiment key -> POST /tracking/track.
    if not client.track(user_id, "purchase", event_value=12.5, experiment_key=experiment_key):
        fail("track(purchase) with experiment_key was not accepted")

    # 4. Track without a key -> fans out to the cached assignment + flag via /tracking/batch,
    #    then an explicit 2-event batch.
    if not client.track(user_id, "page_view", properties={"page": "/smoke"}):
        fail("track(page_view) without a key was not accepted")
    batch = client.track_batch(
        [
            {"event_name": "page_view", "user_id": user_id, "experiment_key": experiment_key},
            {"event_name": "click", "user_id": user_id, "feature_flag_key": flag_key},
        ]
    )
    if not batch.ok:
        fail(f"track_batch failed: {batch.errors}")

    print(
        json.dumps(
            {
                "sdk": "python",
                "assign": {
                    "variant_name": first.variant_name,
                    "is_control": first.is_control,
                    "sticky": sticky,
                },
                "flag": {"enabled": flag.enabled},
                "track": {"ok": True},
                "fanout": {"ok": True},
            },
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - one line on stderr, exit 1
        fail(f"{type(exc).__name__}: {exc}")
