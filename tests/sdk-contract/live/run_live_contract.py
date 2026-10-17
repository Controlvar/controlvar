#!/usr/bin/env python3
"""
Live SDK contract runner.

Runs every SDK's ``contract_smoke`` entry point against a running backend and
checks that each one reports a sticky assignment, a flag evaluation and
successful event tracking. This is the test that proves an SDK actually talks
to the endpoints the backend serves (the golden-vector tests only cover
hashing).

Before the SDKs, it checks the local-evaluation ruleset endpoint
(``GET /api/v1/sdk/ruleset``) once: the seed's plain key gets 403, its
``sdk:ruleset`` key gets the ruleset with ``sdk_contract_flag`` in it, and
sending the ETag back gets 304.

After the SDKs, each SDK with a local-evaluation mode (``LOCAL_MANIFEST``) runs
its ``local_eval_smoke`` with the ``sdk:ruleset`` key: N evaluations of
``sdk_contract_flag`` answered in-process, the counts sent on close, then E
errors reported with the same key. The runner reads the database around it (the
same ``POSTGRES_*`` the backend uses, through the backend's own
``SafetyService.get_error_metrics``) and requires that the step added no
per-user server evaluation (no evaluate call was made), exactly N evaluations
to the safety denominator and exactly E counted errors (``error_count``, the
number automatic rollback acts on), so the flag's error rate is non-zero and
computed over the local traffic. It then reports one error itself with the
seed's unscoped key and requires that it is stored but not counted
(``error_count`` +0, ``uncounted_error_count`` +1).

Prerequisites
    1. A backend at ``$CONTROLVAR_API_URL`` (default http://localhost:8000)
       seeded with ``python backend/scripts/seed_sdk_contract.py`` (creates the
       ``sdk_contract_ab`` experiment, the ``sdk_contract_flag`` flag and two API
       keys, written to tests/sdk-contract/live/.api_key and, with the
       ``sdk:ruleset`` scope, .api_key_local).
    2. The toolchains of the SDKs you want to run (missing ones are skipped
       unless ``--strict``).

Usage
    python tests/sdk-contract/live/run_live_contract.py            # all available
    python tests/sdk-contract/live/run_live_contract.py --sdk go --sdk python
    python tests/sdk-contract/live/run_live_contract.py --strict   # missing toolchain = failure
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
KEY_FILE = Path(__file__).resolve().parent / ".api_key"
# The seed's second key, with the sdk:ruleset scope (server-side local evaluation).
LOCAL_KEY_FILE = Path(__file__).resolve().parent / ".api_key_local"
RULESET_PATH = "/api/v1/sdk/ruleset"

# sdk name -> (shell command run from the repo root, required executables)
MANIFEST: dict[str, tuple[str, tuple[str, ...]]] = {
    "python": ("python sdk/python/examples/contract_smoke.py", ("python",)),
    "openfeature-python": ("python sdk/openfeature-python/examples/contract_smoke.py", ("python",)),
    "js": ("cd sdk/js && npm run build --silent && node examples/contract_smoke.mjs", ("node", "npm")),
    "openfeature": (
        "cd sdk/openfeature && npm run build --silent && node examples/contract_smoke.mjs",
        ("node", "npm"),
    ),
    "edge": ("cd sdk/edge && npm run build --silent && node examples/contract_smoke.mjs", ("node", "npm")),
    # The React SDK through its SSR entry point (ServerClient): no DOM, no React
    # render, so it runs under plain node like the other JS SDKs.
    "react": ("cd sdk/react && npm run build --silent && node examples/contract_smoke.mjs", ("node", "npm")),
    # React Native ships no build output and its client is plain TypeScript over
    # fetch, so the smoke is a Jest test under `testEnvironment: node` (no device,
    # no emulator, no Metro). Jest owns stdout, so its output goes to stderr and
    # the report is read back from the file the test writes.
    "react-native": (
        "cd sdk/react-native && npx jest --config jest.contract.config.js --runInBand 1>&2 "
        "&& cat .contract_smoke.json",
        ("node", "npx"),
    ),
    "go": ("cd sdk/go && go run ./examples/contract_smoke", ("go",)),
    "java": ("bash sdk/java/examples/contract_smoke.sh", ("java",)),  # Maven via ./mvnw
    # The Android SDK's Kotlin sources touch no android.* API, so sdk/android/jvm
    # compiles them for a plain JVM and the smoke runs on a Linux runner with no
    # Android SDK and no emulator (the Gradle/AAR build still runs nightly).
    "android": ("bash sdk/android/examples/contract_smoke.sh", ("java",)),  # Maven via ./mvnw
    "ios": ("cd sdk/ios && swift run -q contract-smoke", ("swift",)),
    "ruby": ("ruby -Isdk/ruby/lib sdk/ruby/examples/contract_smoke.rb", ("ruby",)),
    "php": ("php sdk/php/examples/contract_smoke.php", ("php",)),
    "dotnet": ("dotnet run --project sdk/dotnet/examples/ContractSmoke", ("dotnet",)),
    "elixir": ("cd sdk/elixir && mix run examples/contract_smoke.exs", ("mix",)),
    "flutter": ("cd sdk/flutter && dart run example/contract_smoke.dart", ("dart",)),
}

# SDKs with a local-evaluation mode -> the command that runs their local smoke
# (with CONTROLVAR_LOCAL_API_KEY set). Run after every SDK above, because the
# errors it posts count against the shared contract flag.
LOCAL_MANIFEST: dict[str, tuple[str, tuple[str, ...]]] = {
    "python": ("python sdk/python/examples/local_eval_smoke.py", ("python",)),
    "js": ("cd sdk/js && npm run build --silent && node examples/local_eval_smoke.mjs", ("node", "npm")),
}

EXPECTED_VARIANTS = {"control", "treatment"}


def backend_healthy(api_url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{api_url}/health", timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def validate(sdk: str, payload: dict) -> list[str]:
    problems: list[str] = []
    if payload.get("sdk") != sdk:
        problems.append(f"sdk field is {payload.get('sdk')!r}, expected {sdk!r}")
    assign = payload.get("assign") or {}
    if assign.get("variant_name") not in EXPECTED_VARIANTS:
        problems.append(f"assign.variant_name={assign.get('variant_name')!r}")
    if not isinstance(assign.get("is_control"), bool):
        problems.append("assign.is_control is not a bool")
    if assign.get("sticky") is not True:
        problems.append("assignment was not sticky")
    flag = payload.get("flag") or {}
    if flag.get("enabled") is not True:
        problems.append(f"flag.enabled={flag.get('enabled')!r} (seeded flag is 100% on)")
    if (payload.get("track") or {}).get("ok") is not True:
        problems.append("track failed")
    if (payload.get("fanout") or {}).get("ok") is not True:
        problems.append("fan-out track failed")
    return problems


def _ruleset_request(api_url: str, key: str, etag: str = "") -> tuple[int, dict, bytes]:
    headers = {"X-API-Key": key}
    if etag:
        headers["If-None-Match"] = etag
    request = urllib.request.Request(f"{api_url}{RULESET_PATH}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:  # 304 and 4xx arrive here
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


def check_ruleset(api_url: str, plain_key: str, scoped_key: str, flag_key: str) -> tuple[str, str]:
    """The server side of local evaluation, on the running stack.

    The seed's unscoped key is refused (403); its sdk:ruleset key gets the
    ruleset with the contract flag in it, and the ETag sent back answers 304.
    """
    problems: list[str] = []
    status, _headers, _body = _ruleset_request(api_url, plain_key)
    if status != 403:
        problems.append(f"the unscoped key got {status} from {RULESET_PATH}, expected 403")
    status, headers, body = _ruleset_request(api_url, scoped_key)
    if status != 200:
        problems.append(f"the sdk:ruleset key got {status} from {RULESET_PATH}, expected 200")
        return "FAIL", "; ".join(problems)
    doc = json.loads(body)
    if doc.get("schema") != 1 or doc.get("bucketing") != "md5-mod100-v1":
        problems.append(f"schema/bucketing are {doc.get('schema')!r}/{doc.get('bucketing')!r}")
    etag = headers.get("etag", "")
    if etag != f'"{doc.get("version")}"':
        problems.append(f"ETag {etag!r} is not the body version")
    flag = next((f for f in doc.get("flags", []) if f.get("key") == flag_key), None)
    expected = {"active": True, "evaluation": "local", "rollout_percentage": 100}
    if flag is None or any(flag.get(k) != v for k, v in expected.items()):
        problems.append(f"flag {flag_key!r} is {flag!r}")
    status, _headers, body = _ruleset_request(api_url, scoped_key, etag)
    if status != 304 or body:
        problems.append(f"If-None-Match with the current ETag got {status} ({len(body)} bytes), expected 304")
    if problems:
        return "FAIL", "; ".join(problems)
    return "PASS", f"403 unscoped, 200 scoped ({len(doc['flags'])} flags), 304 on ETag"


def _flag_counters(flag_key: str) -> dict[str, int]:
    """What the server has recorded for *flag_key*, read from the database.

    ``total_evaluations``, ``error_count`` and ``uncounted_error_count`` come
    from the safety monitor's own ``get_error_metrics`` (its 15-minute window),
    so a local evaluation or an error that is not counted there is invisible to
    automatic rollback. ``server_user_rows`` counts the per-user evaluation rows
    only the evaluate endpoints write.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from backend.app.db.session import SessionLocal
    from backend.app.models import register_core_models
    from backend.app.models.feature_flag import FeatureFlag
    from backend.app.models.metrics.metric import MetricType, RawMetric
    from backend.app.services.safety_service import SafetyService

    register_core_models()
    db = SessionLocal()
    try:
        flag = db.query(FeatureFlag).filter(FeatureFlag.key == flag_key).first()
        if flag is None:
            raise RuntimeError(f"flag {flag_key!r} is not in the database")
        metrics = SafetyService(db).get_error_metrics(db, flag.id)
        server_user_rows = (
            db.query(RawMetric)
            .filter(
                RawMetric.feature_flag_id == flag.id,
                RawMetric.metric_type == MetricType.FLAG_EVALUATION.value,
                RawMetric.user_id.isnot(None),
            )
            .count()
        )
        return {
            "total_evaluations": int(metrics["total_evaluations"]),
            "error_count": int(metrics["error_count"]),
            "uncounted_error_count": int(metrics["uncounted_error_count"]),
            "server_user_rows": int(server_user_rows),
        }
    finally:
        db.close()


def run_local(sdk: str, command: str, env: dict[str, str], timeout: int, flag_key: str) -> tuple[str, str]:
    """An SDK's local-evaluation smoke, judged by what the server recorded."""
    try:
        before = _flag_counters(flag_key)
    except Exception as exc:  # the backend package or its database is not reachable
        return "FAIL", f"cannot read the database for the flag counters: {exc}"
    status, detail, payload = _run_command(command, env, timeout)
    if status != "PASS":
        return status, detail
    problems: list[str] = []
    if payload.get("sdk") != sdk or payload.get("mode") != "local":
        problems.append(f"sdk/mode are {payload.get('sdk')!r}/{payload.get('mode')!r}")
    evaluations = payload.get("evaluations")
    errors = payload.get("errors_posted")
    if not isinstance(evaluations, int) or evaluations < 1 or not isinstance(errors, int) or errors < 1:
        return "FAIL", f"the smoke reported evaluations={evaluations!r}, errors_posted={errors!r}"
    after = _flag_counters(flag_key)
    delta = {key: after[key] - before[key] for key in after}
    if delta["server_user_rows"] != 0:
        problems.append(f"{delta['server_user_rows']} evaluate call(s) reached the server; local mode must make none")
    if delta["total_evaluations"] != evaluations:
        problems.append(
            f"the safety denominator grew by {delta['total_evaluations']}, expected the {evaluations} local evaluations"
        )
    if delta["error_count"] != errors:
        problems.append(f"the error count grew by {delta['error_count']}, expected {errors}")
    if delta["uncounted_error_count"] != 0:
        problems.append(f"{delta['uncounted_error_count']} of the sdk:ruleset key's error reports were not counted")
    if not after["total_evaluations"] or not after["error_count"]:
        problems.append(f"the error rate is not computable from {after}")
    if problems:
        return "FAIL", "; ".join(problems)
    rate = after["error_count"] / after["total_evaluations"]

    # One report with the seed's unscoped key: stored, not counted.
    report_status = _report_error(env["CONTROLVAR_API_URL"], env["CONTROLVAR_API_KEY"], flag_key, sdk)
    if report_status != 201:
        return "FAIL", f"POST /api/v1/tracking/errors with the unscoped key answered {report_status}, expected 201"
    unscoped = _flag_counters(flag_key)
    if unscoped["error_count"] != after["error_count"]:
        problems.append(
            f"a report sent with the unscoped key changed the error count by "
            f"{unscoped['error_count'] - after['error_count']}, expected 0"
        )
    if unscoped["uncounted_error_count"] != after["uncounted_error_count"] + 1:
        problems.append(
            f"the uncounted reports grew by {unscoped['uncounted_error_count'] - after['uncounted_error_count']}, "
            "expected 1"
        )
    if problems:
        return "FAIL", "; ".join(problems)
    return (
        "PASS",
        f"{evaluations} local evaluations, 0 evaluate calls; denominator +{evaluations}, "
        f"errors +{errors}, error rate {rate:.3f}; an unscoped key's report stored, not counted",
    )


def _report_error(api_url: str, key: str, flag_key: str, sdk: str) -> int:
    """POST one error report for *flag_key* with *key*; the HTTP status."""
    body = json.dumps(
        {
            "feature_flag_key": flag_key,
            "error_type": "live_contract",
            "message": f"live contract: a report sent with the unscoped key ({sdk})",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{api_url}/api/v1/tracking/errors",
        data=body,
        headers={"X-API-Key": key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:  # noqa: S310
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _run_command(command: str, env: dict[str, str], timeout: int) -> tuple[str, str, dict]:
    """Run a smoke command; (status, detail, the JSON object on its last stdout line)."""
    started = time.time()
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "FAIL", f"timed out after {timeout}s", {}
    elapsed = time.time() - started
    stdout_lines = [line for line in proc.stdout.strip().splitlines() if line.strip()]
    if proc.returncode != 0:
        tail = (proc.stderr.strip().splitlines() or stdout_lines or ["(no output)"])[-1]
        return "FAIL", f"exit {proc.returncode}: {tail[:300]}", {}
    if not stdout_lines:
        return "FAIL", "no JSON line on stdout", {}
    try:
        payload = json.loads(stdout_lines[-1])
    except json.JSONDecodeError:
        return "FAIL", f"last stdout line is not JSON: {stdout_lines[-1][:200]}", {}
    if not isinstance(payload, dict):
        return "FAIL", f"last stdout line is not a JSON object: {stdout_lines[-1][:200]}", {}
    return "PASS", f"{elapsed:.1f}s", payload


def run_one(sdk: str, command: str, env: dict[str, str], timeout: int) -> tuple[str, str]:
    """Return (status, detail) where status is PASS / FAIL."""
    status, detail, payload = _run_command(command, env, timeout)
    if status != "PASS":
        return status, detail
    problems = validate(sdk, payload)
    if problems:
        return "FAIL", "; ".join(problems)
    return "PASS", f"{payload['assign']['variant_name']} in {detail}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sdk", action="append", help="run only this SDK (repeatable)")
    parser.add_argument("--strict", action="store_true", help="treat a missing toolchain as a failure")
    parser.add_argument("--timeout", type=int, default=300, help="seconds per SDK (builds included)")
    parser.add_argument(
        "--api-url",
        # The name before the rename is still read; an empty value counts as unset.
        default=os.environ.get("CONTROLVAR_API_URL") or os.environ.get("EXPERIMENTLY_API_URL") or "http://localhost:8000",
    )
    args = parser.parse_args()

    api_key = os.environ.get("CONTROLVAR_API_KEY") or os.environ.get("EXPERIMENTLY_API_KEY") or (KEY_FILE.read_text().strip() if KEY_FILE.exists() else "")
    if not api_key:
        print("No API key: set CONTROLVAR_API_KEY or run backend/scripts/seed_sdk_contract.py", file=sys.stderr)
        return 2
    if not backend_healthy(args.api_url):
        print(f"Backend at {args.api_url} is not healthy", file=sys.stderr)
        return 2

    env = dict(os.environ)
    # Both names: the smokes under sdk/ read the name before the rename until
    # they move to the new one.
    env.update(
        {
            "CONTROLVAR_API_URL": args.api_url,
            "CONTROLVAR_API_KEY": api_key,
            "EXPERIMENTLY_API_URL": args.api_url,
            "EXPERIMENTLY_API_KEY": api_key,
        }
    )
    env.setdefault("CONTRACT_EXPERIMENT_KEY", "sdk_contract_ab")
    env.setdefault("CONTRACT_FLAG_KEY", "sdk_contract_flag")

    selected = args.sdk or list(MANIFEST)
    unknown = [s for s in selected if s not in MANIFEST]
    if unknown:
        print(f"Unknown SDK(s): {', '.join(unknown)}. Known: {', '.join(MANIFEST)}", file=sys.stderr)
        return 2

    results: list[tuple[str, str, str]] = []

    # The ruleset endpoint is checked once, before the SDKs, with the seed's two keys.
    local_key = os.environ.get("CONTROLVAR_LOCAL_API_KEY") or os.environ.get("EXPERIMENTLY_LOCAL_API_KEY") or (
        LOCAL_KEY_FILE.read_text().strip() if LOCAL_KEY_FILE.exists() else ""
    )
    if local_key:
        status, detail = check_ruleset(args.api_url, api_key, local_key, env["CONTRACT_FLAG_KEY"])
    else:
        status = "FAIL" if args.strict else "SKIP"
        detail = "no sdk:ruleset key: set CONTROLVAR_LOCAL_API_KEY or run seed_sdk_contract.py"
    results.append(("ruleset (server)", status, detail))
    print(f"{status:4} {'ruleset (server)':<20} {detail}", flush=True)

    for sdk in selected:
        command, tools = MANIFEST[sdk]
        missing = [t for t in tools if shutil.which(t) is None]
        if missing:
            results.append((sdk, "FAIL" if args.strict else "SKIP", f"missing toolchain: {', '.join(missing)}"))
            continue
        status, detail = run_one(sdk, command, env, args.timeout)
        results.append((sdk, status, detail))
        print(f"{status:4} {sdk:<20} {detail}", flush=True)

    # Local evaluation, after every SDK above (its errors count against the shared flag).
    for sdk in [s for s in selected if s in LOCAL_MANIFEST]:
        name = f"{sdk} (local)"
        command, tools = LOCAL_MANIFEST[sdk]
        missing = [t for t in tools if shutil.which(t) is None]
        if missing:
            results.append((name, "FAIL" if args.strict else "SKIP", f"missing toolchain: {', '.join(missing)}"))
            continue
        if not local_key:
            results.append((name, "FAIL" if args.strict else "SKIP", "no sdk:ruleset key"))
            continue
        local_env = dict(env, CONTROLVAR_LOCAL_API_KEY=local_key, EXPERIMENTLY_LOCAL_API_KEY=local_key)
        status, detail = run_local(sdk, command, local_env, args.timeout, env["CONTRACT_FLAG_KEY"])
        results.append((name, status, detail))
        print(f"{status:4} {name:<20} {detail}", flush=True)

    print("\nSummary")
    for sdk, status, detail in results:
        print(f"  {status:4} {sdk:<20} {detail}")
    failed = [r for r in results if r[1] == "FAIL"]
    passed = [r for r in results if r[1] == "PASS"]
    skipped = [r for r in results if r[1] == "SKIP"]
    print(f"\n{len(passed)} passed, {len(failed)} failed, {len(skipped)} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
