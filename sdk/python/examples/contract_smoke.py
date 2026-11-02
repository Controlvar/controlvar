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
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

# Make ``controlvar`` importable straight from the source tree.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import controlvar.client as client_module  # noqa: E402
from controlvar import (  # noqa: E402
    ExperimentationClient,
    ExperimentationError,
    Exposure,
)
from controlvar.transport import Response  # noqa: E402


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    sys.exit(1)


def _raise_hook_boom(exposure: Exposure) -> None:
    raise RuntimeError("hook boom")


class _Records(logging.Handler):
    """Keeps the log records it is given (and so keeps the raising hook's traceback off stderr)."""

    def __init__(self) -> None:
        super().__init__()
        self.records: List[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class Recording:
    """The SDK's own HTTP layer, keeping each request (method, path, JSON body) and its answer."""

    def __init__(self) -> None:
        self.inner = client_module.UrllibTransport()
        self.calls: List[Tuple[str, str, Any, Response]] = []

    def send(
        self, method: str, url: str, headers: Mapping[str, str], body: Optional[bytes], timeout: float
    ) -> Response:
        response = self.inner.send(method, url, headers, body, timeout)
        sent = None if body is None else json.loads(body.decode("utf-8"))
        self.calls.append((method.upper(), urlsplit(url).path, sent, response))
        return response


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

    recording = Recording()
    client = ExperimentationClient(api_url, api_key, transport=recording)

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

    # 4. Track without a key -> exactly one POST /tracking/batch, fanned out to the experiment and
    #    the flag answered above, every entry stored; then an explicit 2-event batch. track()
    #    alone would not tell: it is True for an untagged send too, and for a 2xx batch whose
    #    entries failed.
    mark = len(recording.calls)
    if not client.track(user_id, "page_view", properties={"page": "/smoke"}):
        fail("track(page_view) without a key was not accepted")
    sent = recording.calls[mark:]
    if [(method, path) for method, path, _, _ in sent] != [("POST", "/api/v1/tracking/batch")]:
        fail(f"track(page_view) without a key sent {[f'{m} {p}' for m, p, _, _ in sent]}, expected one POST /api/v1/tracking/batch")
    keys = [(e.get("experiment_key"), e.get("feature_flag_key")) for e in sent[0][2]["events"]]
    if keys != [(experiment_key, None), (None, flag_key)]:
        fail(f"the fan-out carried {keys}, expected the experiment {experiment_key!r} then the flag {flag_key!r}")
    answer = sent[0][3].json() if sent[0][3].body else {}
    if answer.get("failure_count") != 0:
        fail(f"the fan-out's entries were not all stored: {answer}")
    batch = client.track_batch(
        [
            {"event_name": "page_view", "user_id": user_id, "experiment_key": experiment_key},
            {"event_name": "click", "user_id": user_id, "feature_flag_key": flag_key},
        ]
    )
    if not batch.ok:
        fail(f"track_batch failed: {batch.errors}")

    # 5. The exposure hook (#1499): a NEW client with on_exposure assigns the same user twice,
    #    with no clear_cache() in between. The server records an exposure for the first request
    #    (assigned: true) and the second answer comes from the cache, so the hook is called once.
    #    The runner checks the count, the experiment key and the variant; the user id is checked
    #    here, the one place that knows it.
    exposures: List[Exposure] = []
    try:
        hooked = ExperimentationClient(api_url, api_key, on_exposure=exposures.append)
        hooked.get_assignment(experiment_key, user_id, attributes)
        hooked.get_assignment(experiment_key, user_id, attributes)
    except ExperimentationError as exc:
        fail(f"assign with on_exposure failed: {exc} (status={exc.status}, body={exc.body!r})")
    wrong_user = next((e for e in exposures if e.user_id != user_id), None)
    if wrong_user is not None:
        fail(f"on_exposure received user {wrong_user.user_id!r}, expected {user_id!r}")

    # 5b. A hook that raises: the answer is unchanged, the error is logged at ERROR and passed
    #     to on_error as "exposure" with code HOOK_ERROR.
    hook_errors: List[str] = []
    logged = _Records()
    hook_logger = logging.getLogger("controlvar.client")
    hook_logger.addHandler(logged)
    try:
        raising = ExperimentationClient(
            api_url,
            api_key,
            on_exposure=_raise_hook_boom,
            on_error=lambda error, operation: hook_errors.append(f"{operation}/{error.code}"),
        )
        from_raising = raising.get_assignment(experiment_key, user_id, attributes)
    except ExperimentationError as exc:
        fail(f"assign with a raising on_exposure failed: {exc} (status={exc.status}, body={exc.body!r})")
    finally:
        hook_logger.removeHandler(logged)
    if from_raising.variant_name != second.variant_name:
        fail(f"the client with a raising hook answered {from_raising.variant_name}, not {second.variant_name}")
    if hook_errors != ["exposure/HOOK_ERROR"]:
        fail(f"the raising hook was reported as {hook_errors}, expected ['exposure/HOOK_ERROR']")
    if [(r.levelname, r.getMessage().startswith("on_exposure failed: ")) for r in logged.records] != [("ERROR", True)]:
        fail(f"the raising hook was logged as {[(r.levelname, r.getMessage()) for r in logged.records]}")

    exposure = exposures[0] if exposures else None
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
                "exposure": {
                    "calls": len(exposures),
                    "payload": None
                    if exposure is None
                    else {
                        "experiment_key": exposure.experiment_key,
                        "variant_name": exposure.variant_name,
                        "variant_id": exposure.variant_id,
                        "user_id": exposure.user_id,
                    },
                },
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
