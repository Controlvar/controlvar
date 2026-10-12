#!/usr/bin/env python3
"""Local-evaluation smoke for the Python SDK (#226).

Run by the live contract (tests/sdk-contract/live/run_live_contract.py), which checks the server
side of it: that these evaluations made no evaluate call, and that the counts sent on close()
are the safety monitor's denominator for the errors reported afterwards.

The errors are reported with the same sdk:ruleset key, the way a server forwards them: only
reports sent with a server-side key count toward a flag's error rate and automatic rollback.

Run from the repository root (no install needed):

    EXPERIMENTLY_LOCAL_API_KEY=<sdk:ruleset key> python sdk/python/examples/local_eval_smoke.py

Env: EXPERIMENTLY_API_URL (default http://localhost:8000), EXPERIMENTLY_LOCAL_API_KEY (a key
with the sdk:ruleset scope, required; also used for POST /api/v1/tracking/errors),
CONTRACT_FLAG_KEY (default sdk_contract_flag), CONTRACT_USER_ID (default local-smoke-<uuid>),
LOCAL_EVALUATIONS (default 40), LOCAL_ERRORS (default 2).

Prints exactly one JSON line on stdout and exits 0; on failure prints one line to stderr and
exits 1.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import List, NoReturn

# Make ``experimentation`` importable straight from the source tree.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from experimentation import ExperimentationClient  # noqa: E402


def fail(message: str) -> NoReturn:
    print(f"python local-evaluation smoke failed: {message}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    api_url = os.environ.get("EXPERIMENTLY_API_URL", "http://localhost:8000").rstrip("/")
    local_key = os.environ.get("EXPERIMENTLY_LOCAL_API_KEY")
    if not local_key:
        fail("EXPERIMENTLY_LOCAL_API_KEY (a key with the sdk:ruleset scope) is required")
    flag_key = os.environ.get("CONTRACT_FLAG_KEY", "sdk_contract_flag")
    user_id = os.environ.get("CONTRACT_USER_ID") or f"local-smoke-{uuid.uuid4()}"
    evaluations = int(os.environ.get("LOCAL_EVALUATIONS", "40"))
    errors_to_post = int(os.environ.get("LOCAL_ERRORS", "2"))

    swallowed: List[str] = []
    client = ExperimentationClient(
        api_url,
        local_key,
        evaluation="local",
        on_error=lambda error, operation: swallowed.append(f"{operation}: {error}"),
    )
    ready = client.ready(timeout_seconds=15)
    if not ready:
        fail(f"ready(): {ready.error}")

    enabled = 0
    for i in range(evaluations):
        result = client.get_feature_flag(flag_key, user_id, {"source": "local-smoke"})
        if result.source != "local":
            fail(f"evaluation {i} was made on the server (source {result.source})")
        enabled += int(result.enabled)
    version = client.status().ruleset_version

    # close() sends the evaluation counts.
    client.close()
    if swallowed:
        fail(swallowed[0])

    # Errors against the same flag, reported with the server-side key: the safety monitor
    # divides these by the counts above.
    for i in range(errors_to_post):
        body = json.dumps(
            {
                "feature_flag_key": flag_key,
                "user_id": user_id,
                "error_type": "local_smoke",
                "message": f"local-evaluation smoke error {i + 1}",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{api_url}/api/v1/tracking/errors",
            data=body,
            headers={"X-API-Key": local_key, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        if status != 201:
            fail(f"POST /api/v1/tracking/errors answered {status}")

    print(
        json.dumps(
            {
                "sdk": "python",
                "mode": "local",
                "flag_key": flag_key,
                "user_id": user_id,
                "evaluations": evaluations,
                "enabled": enabled,
                "errors_posted": errors_to_post,
                "ruleset_version": version,
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        fail(str(exc))
