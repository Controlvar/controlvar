#!/usr/bin/env python3
"""
Live SDK contract runner.

Runs every SDK's ``contract_smoke`` entry point against a running backend and
checks that each one reports a sticky assignment, a flag evaluation and
successful event tracking. This is the test that proves an SDK actually talks
to the endpoints the backend serves (the golden-vector tests only cover
hashing). An SDK listed in ``EVENT_ID_SDKS`` must also send one event twice
with one event_id, and the runner requires the database to hold one row for it.
An SDK listed in ``EXPOSURE_SDKS`` must also report what its
exposure hook received, and no other SDK may report it. An SDK listed in
``ATTRIBUTE_SDKS`` must also report how it evaluated the seed's
``sdk_contract_targeted`` flag (0% rollout, one rule ``plan`` equals ``pro`` at
100%) with ``{"plan": "pro"}`` and ``{"plan": "free"}``: on with the server's
reason ``targeting_rule``, and off. No other SDK may report it. An SDK listed
in ``VALUE_SDKS`` must also report what its typed getters returned for the
seed's string, number and json flags, each on at 100%: exactly their on
values. No other SDK may report them. An SDK listed in ``ALIAS_SDKS`` must also
link a fresh anonymous id to a fresh known id with its ``alias`` method, using
the seed's ``sdk:ruleset`` key, which every smoke is given: the runner then
requires one assignment row among the two ids, the anonymous id's, and the
key-less event sent under the known id stored under the anonymous id with the
experiment and its variant. No other SDK may report it. An SDK listed in
``MULTIWAY_SDKS`` (the SDKs of ``VALUE_SDKS``) must also report what its string
getter returned for the seed's ``sdk_contract_multiway`` flag (on at 100%, a
split of three values) for each user in ``CONTRACT_MULTIWAY_USERS``, and for
``sdk_contract_rule_value`` with ``{"plan": "pro"}``: each user's own value of
the split, and the rule's own value. No other SDK may report them.

After the react row, the React SDK's bootstrap runs for one new user
(``BOOTSTRAP_SMOKE``): the documented ``ServerClient.bootstrap()`` call and a
server render with it, then the one assignment the page's hook sends after
hydration. The runner reads the database after each step and requires exactly
one exposure row after the first and one more after the second, and requires
that the bootstrap example on docs/sdk/react.md has the live bootstrap's
fields.

Before the SDKs, it checks the local-evaluation ruleset endpoint
(``GET /api/v1/sdk/ruleset``) once: the seed's plain key gets 403, its
``sdk:ruleset`` key gets the ruleset with ``sdk_contract_flag`` in it, and
sending the ETag back gets 304. Then, also once, that a key answers its own
flag environment (#2011): the seed's staging keys are answered what staging
serves for ``sdk_contract_flag`` (off) and ``sdk_contract_targeted`` (on),
through the evaluate route and through the ruleset, while its production key
is answered the other way round. The SDK rows use the production keys only.
Then, also once, the seed's two multi-way flags (#2012, ``multiway (server)``):
the evaluate route answers the three users of ``CONTRACT_MULTIWAY_USERS`` three
distinct values, each the one the seed picked the user for, and a user with
``{"plan": "pro"}`` the rule's own value; ``/feature-flags/user/{id}`` answers
both flags with booleans. The seed's ``multiway_users()`` picks the three users
with the server's own split function, one per value of the split.

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

Last, each release in ``PUBLISHED_LOCAL`` -- the published JavaScript and
Python 0.2.0, fetched from npm and PyPI and verified by digest through
``tests/sdk-contract/published_sdks.py`` -- runs in local mode against this
server (#1497, G3): it loads the ruleset and answers each of the seed's value
flags (string, number and json) locally as on; the runner turns those flags
off through the API, as a DEVELOPER; within one refresh each answers
``inactive``, still locally; the runner turns them back on. A release that
cannot read the ruleset the server sends would answer from the server, or
keep answering on. The published Python 0.3.0 row reads the seed's multi-way
flag instead (#2012): its ruleset entry is ``remote``, so the release answers
one of the seed's users from the server, with the value of the split the
server picked; turned off through the API, within one refresh it answers
``inactive`` locally, with the off value. A release that answered the flag
locally would serve its on value to every user.

Prerequisites
    1. A backend at ``$CONTROLVAR_API_URL`` (default http://localhost:8000)
       seeded with ``python backend/scripts/seed_sdk_contract.py`` (creates the
       ``sdk_contract_ab`` experiment, the ``sdk_contract_flag`` flag and two API
       keys, written to tests/sdk-contract/live/.api_key and, with the
       ``sdk:ruleset`` scope, .api_key_local, and the same two for staging,
       .api_key_staging and .api_key_local_staging).
    2. The toolchains of the SDKs you want to run (missing ones are skipped
       unless ``--strict``).

Usage
    python tests/sdk-contract/live/run_live_contract.py            # all available
    python tests/sdk-contract/live/run_live_contract.py --sdk go --sdk python
    python tests/sdk-contract/live/run_live_contract.py --strict   # missing toolchain = failure
"""

from __future__ import annotations

import argparse
import functools
import importlib.util
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
KEY_FILE = Path(__file__).resolve().parent / ".api_key"
# The seed's second key, with the sdk:ruleset scope (server-side local evaluation).
LOCAL_KEY_FILE = Path(__file__).resolve().parent / ".api_key_local"
# The same two keys of the staging environment (#2011); used by no SDK row.
STAGING_KEY_FILE = Path(__file__).resolve().parent / ".api_key_staging"
STAGING_LOCAL_KEY_FILE = Path(__file__).resolve().parent / ".api_key_local_staging"
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

#: Published releases with a local evaluator, run against this server (#1497,
#: G3): (sdk name, the version pinned in tests/sdk-contract/published-sdks.json,
#: the flags its row reads). Each runs after LOCAL_MANIFEST when its sdk is
#: selected: a "values" row under the name "<sdk> <version> (published, local)"
#: reads the seed's value flags, the "multiway" row under "<sdk> <version>
#: (published, multi-way)" the seed's multi-way flag (#2012). Only the Python
#: harness reports values, so a multi-way row is a Python one.
PUBLISHED_LOCAL: tuple[tuple[str, str, str], ...] = (
    ("python", "0.2.0", "values"),
    ("js", "0.2.0", "values"),
    ("python", "0.3.0", "multiway"),
)
#: The seed's value flags (backend/scripts/seed_sdk_contract.py VALUE_FLAGS).
VALUE_FLAG_KEYS = ("sdk_contract_value_string", "sdk_contract_value_number", "sdk_contract_value_json")
#: Who turns them off and on: the seed's DEVELOPER (a non-superuser role that
#: can change any flag, D11), signing in through the API.
TOGGLE_USER = ("dev@demo.com", "Demo1234!")
#: 0.2.0's shortest refresh interval; the change must arrive within the wait.
PUBLISHED_REFRESH_SECONDS = 5
PUBLISHED_WAIT_SECONDS = 30
PUBLISHED_ON = {"enabled": True, "reason": "rollout", "source": "local"}
PUBLISHED_OFF = {"enabled": False, "reason": "inactive", "source": "local"}
#: The multi-way row: on, answered by the server, with the server's value of
#: the split; then off, answered locally with the off value (the seed's
#: string off value, VALUE_FLAGS["string"]).
PUBLISHED_SERVER = {"enabled": True, "reason": "rollout", "source": "server"}
MULTIWAY_OFF_VALUE = "off-copy"
SDK_CONTRACT_DIR = Path(__file__).resolve().parents[1]

EXPECTED_VARIANTS = {"control", "treatment"}

# The SDKs whose contract smoke must report its exposure hook (#1499). A listed
# SDK's smoke is to build a NEW client with a hook, assign the contract
# experiment twice with no clear in between, and report
#   "exposure": {"calls": <n>, "payload": {"experiment_key", "variant_name",
#                                          "variant_id", "user_id"}}
# validate() requires exactly one call, for the contract experiment and the
# variant the smoke was assigned. It does not check the user id: only the smoke
# knows the user it made, so that check is the smoke's. The check runs both
# ways: a listed SDK whose smoke reports no "exposure" fails, and so does an
# unlisted SDK whose smoke reports one. The pull request that gives an SDK the
# hook is to add its line here.
EXPOSURE_SDKS: frozenset[str] = frozenset(
    [
        # One line per SDK, in the form "js",
        "js",
        "python",
        "go",
        "java",
        "react",
        "react-native",
        "dotnet",
    ]
)

# The SDKs whose contract smoke must report that the user's attributes reach
# flag evaluation (#1520). A listed SDK's smoke is to evaluate the seed's
# targeted flag (CONTRACT_TARGETED_FLAG_KEY, default sdk_contract_targeted: 0%
# rollout, one rule plan equals pro at 100%) for one user with the attributes
# {"plan": "pro"}, then, with nothing cached, {"plan": "free"}, and report
#   "targeted": {"pro": <enabled>, "free": <enabled>,
#                "reason": <the server's reason for the pro answer>}
# validate() requires {"pro": true, "free": false, "reason": "targeting_rule"}:
# an SDK that drops the attributes gets pro off, with the reason "rollout". The
# check runs both ways, as EXPOSURE_SDKS does: a listed SDK whose smoke reports
# no "targeted" fails, and so does an unlisted SDK whose smoke reports one. CI
# runs every listed SDK with --strict, so a missing toolchain fails rather than
# skips (backend/tests/unit/scripts/test_run_live_contract.py pins both).
ATTRIBUTE_SDKS: frozenset[str] = frozenset(
    [
        # One line per SDK, in the form "js",
        "js",
        "react-native",
        "java",
        "dotnet",
    ]
)
TARGETED_FLAG_KEY = "sdk_contract_targeted"

# The SDKs whose contract smoke must report what its typed getters return
# (#1810). A listed SDK's smoke is to read the seed's three value flags
# (VALUE_FLAG_KEYS, each on at 100% while the SDK rows run) with its string,
# number and json getters, each given a default that is not a flag value, and
# report
#   "values": {"string": <returned>, "number": <returned>, "json": <returned>}
# validate() requires VALUE_EXPECTED, the seed's on values: an SDK that reads
# the off value, or falls back to its default, fails. A number is compared as
# a number (10.0 is 10; true and "10" are not). The check runs both ways, as
# EXPOSURE_SDKS does. The pull request that gives an SDK typed getters is to
# add its line here and its report to the smoke.
VALUE_SDKS: frozenset[str] = frozenset(
    [
        # One line per SDK, in the form "js",
        "js",
        "react",
        "react-native",
        "java",
        "dotnet",
    ]
)
#: The seed's on values (backend/scripts/seed_sdk_contract.py VALUE_FLAGS).
VALUE_EXPECTED = {"string": "on-copy", "number": 10, "json": {"color": "blue"}}

# The SDKs whose contract smoke must report what its string getter returns for
# the seed's two multi-way flags (#2012): every SDK of VALUE_SDKS, since a
# typed getter is what reads a split's value or a rule's own value (a unit test
# pins the two lists equal). A listed SDK's smoke is to read, with a default
# that is no flag value, MULTIWAY_FLAG_KEY (on at 100%, a split of three
# values) for each user in CONTRACT_MULTIWAY_USERS (comma-separated, given by
# the runner), and RULE_VALUE_FLAG_KEY (0%, one rule plan equals pro with a
# value of its own) for one user with {"plan": "pro"}, and report
#   "multiway": {"users": {<user>: <returned>, ...}, "rule": <returned>}
# validate() requires each user's value of the split, as the seed's
# multiway_users() picked it with the server's own split function, and
# RULE_VALUE: an SDK that reads the on value, or falls back to its default,
# fails. The check runs both ways, as EXPOSURE_SDKS does.
MULTIWAY_SDKS: frozenset[str] = frozenset(
    [
        # One line per SDK, in the form "js",
        "js",
        "react",
        "react-native",
        "java",
        "dotnet",
    ]
)
#: The seed's two multi-way flags and the rule's own value
#: (backend/scripts/seed_sdk_contract.py).
MULTIWAY_FLAG_KEY = "sdk_contract_multiway"
RULE_VALUE_FLAG_KEY = "sdk_contract_rule_value"
RULE_VALUE = "pro-copy"
#: The users given to the smokes, comma-separated.
MULTIWAY_USERS_ENV = "CONTRACT_MULTIWAY_USERS"
#: The user the server row asks the rule value flag about, with plan=pro.
RULE_USER = "contract-rule-user"


@functools.cache
def multiway_users() -> dict[str, str]:
    """``{user: value}``: the three users of the seed's multi-way flag and the
    value of the split each is served, as the seed picks them
    (``seed_sdk_contract.multiway_users``, the server's own split function)."""
    sys.path.insert(0, str(REPO_ROOT))
    from backend.scripts.seed_sdk_contract import multiway_users as picked

    return dict(picked())

# The React SDK's bootstrap (#1518), run after the react row (which builds
# sdk/react/dist) for one new user, in two steps: "server" is the documented
# ``ServerClient.bootstrap()`` call and the page's server render with it,
# "client" what the page's ``useExperiment`` sends once after hydration. The
# runner counts the exposure rows the server stored for (user, experiment)
# after each step: exactly BOOTSTRAP_SERVER_EXPOSURES after the server step
# (one assignment of a new user), exactly one more after the client step (the
# hook's own assignment). It also requires that the JSON example of a
# bootstrap on docs/sdk/react.md has the same fields as the bootstrap the live
# server just produced, and prints that bootstrap: the example is regenerated
# from this line when the server's response bodies change.
BOOTSTRAP_SMOKE = "cd sdk/react && node examples/bootstrap_smoke.mjs"
BOOTSTRAP_SERVER_EXPOSURES = 1
BOOTSTRAP_CLIENT_EXPOSURES = 1
REACT_PAGE = REPO_ROOT / "docs" / "sdk" / "react.md"

# The SDKs whose contract smoke must prove an event sent twice with the same
# event_id is stored once (#1543, case C4). A listed SDK's smoke sends one keyed
# event twice with one fixed id and reports
#   "event_id": {"user_id": <user>, "event_name": <name>, "id": <the id>, "sends": 2}
# validate() checks the shape; run_one() then counts the ``events`` rows for
# that user and event name in the database and requires exactly 1 (an SDK that
# makes a fresh id per send leaves 2). The check runs both ways, as
# EXPOSURE_SDKS does: a listed SDK whose smoke reports no "event_id" fails, and
# so does an unlisted one that reports it. The pull request that gives an SDK an
# event id option is to add its line here and its report to the smoke.
EVENT_ID_SDKS: frozenset[str] = frozenset(
    [
        # One line per SDK, in the form "js",
        "java",
        "js",
        "python",
        "react",
        "react-native",
        "dotnet",
        "android",
    ]
)

# The SDKs whose contract smoke must link two ids with alias (#449). The runner
# gives every smoke the seed's sdk:ruleset key as CONTROLVAR_LOCAL_API_KEY (the
# route needs a server-side key). A listed SDK's smoke is to build a NEW client
# with that key, assign a fresh anonymous id to the contract experiment, link it
# to a fresh known id twice (stored, then already stored), send one key-less
# event under the known id before anything assigned it, then assign the known
# id, and report
#   "alias": {"anonymous_id": <id>, "user_id": <id>, "created": [true, false],
#             "variant_before": <the anonymous id's>, "variant_after": <the known id's>,
#             "event_name": <the key-less event's>}
# validate() checks the shape and that the known id was answered the anonymous
# id's variant; run_one() then reads the database and requires exactly one
# assignment row among the two ids, held by the anonymous id, and exactly one
# row for the event, stored under the anonymous id with the experiment and that
# row's variant. An SDK that does not copy the anonymous id's keys to the known
# id stores the event untagged (``alias: 0 tagged events (experiment_id NULL)``);
# a server that does not follow the link at receipt stores it with no variant
# (``alias: event variant NULL``). The check runs both ways, as EXPOSURE_SDKS
# does. The pull request that gives an SDK an alias method is to add its line
# here and its step to the smoke.
ALIAS_SDKS: frozenset[str] = frozenset(
    [
        # One line per SDK, in the form "js",
        "js",
        "python",
    ]
)


def backend_healthy(api_url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{api_url}/health", timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def validate(sdk: str, payload: dict, experiment_key: str = "sdk_contract_ab") -> list[str]:
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
    problems.extend(_exposure_problems(sdk, payload, experiment_key))
    problems.extend(_targeting_problems(sdk, payload))
    problems.extend(_event_id_problems(sdk, payload))
    problems.extend(_value_problems(sdk, payload))
    problems.extend(_multiway_problems(sdk, payload))
    problems.extend(_alias_problems(sdk, payload))
    return problems


def _multiway_problems(sdk: str, payload: dict) -> list[str]:
    """What is wrong with the smoke's ``multiway`` report (see ``MULTIWAY_SDKS``)."""
    if sdk not in MULTIWAY_SDKS:
        return [f"multiway reported but {sdk} is not in MULTIWAY_SDKS"] if "multiway" in payload else []
    report = payload.get("multiway")
    if not isinstance(report, dict):
        return [f"multiway missing ({sdk} is in MULTIWAY_SDKS)"]
    problems: list[str] = []
    expected = multiway_users()
    users = report.get("users")
    if not isinstance(users, dict) or sorted(users) != sorted(expected):
        found = sorted(users) if isinstance(users, dict) else users
        problems.append(f"multiway.users has {found!r}, expected the users {sorted(expected)!r}")
    else:
        for user, want in expected.items():
            got = users[user]
            if type(got) is not str or got != want:
                problems.append(f"multiway.users[{user!r}]={got!r} (expected {want!r}, the split's value for the user)")
    rule = report.get("rule")
    if type(rule) is not str or rule != RULE_VALUE:
        problems.append(f"multiway.rule={rule!r} (expected {RULE_VALUE!r}, the rule's own value)")
    return problems


def _alias_problems(sdk: str, payload: dict) -> list[str]:
    """What is wrong with the smoke's ``alias`` report (see ``ALIAS_SDKS``)."""
    if sdk not in ALIAS_SDKS:
        return [f"alias reported but {sdk} is not in ALIAS_SDKS"] if "alias" in payload else []
    report = payload.get("alias")
    if not isinstance(report, dict):
        return [f"alias missing ({sdk} is in ALIAS_SDKS)"]
    problems: list[str] = []
    for field in ("anonymous_id", "user_id", "event_name"):
        value = report.get(field)
        if not isinstance(value, str) or not value:
            problems.append(f"alias.{field}={value!r} (expected a non-empty string)")
    if report.get("user_id") and report.get("user_id") == report.get("anonymous_id"):
        problems.append("alias.user_id is alias.anonymous_id (two different ids are to be linked)")
    created = report.get("created")
    # JSON 1 and 0 are not true and false.
    if not isinstance(created, list) or [type(c) for c in created] != [bool, bool] or created != [True, False]:
        problems.append(f"alias.created={created!r} (expected [true, false]: stored, then already stored)")
    before, after = report.get("variant_before"), report.get("variant_after")
    if before not in EXPECTED_VARIANTS:
        problems.append(f"alias.variant_before={before!r} (expected control or treatment)")
    elif after != before:
        problems.append(
            f"alias.variant_after={after!r} != alias.variant_before={before!r} "
            "(the known id is to be answered the anonymous id's variant)"
        )
    return problems


def _value_problems(sdk: str, payload: dict) -> list[str]:
    """What is wrong with the smoke's ``values`` report (see ``VALUE_SDKS``)."""
    if sdk not in VALUE_SDKS:
        return [f"values reported but {sdk} is not in VALUE_SDKS"] if "values" in payload else []
    values = payload.get("values")
    if not isinstance(values, dict):
        return [f"values missing ({sdk} is in VALUE_SDKS)"]
    problems: list[str] = []
    for kind, expected in VALUE_EXPECTED.items():
        got = values.get(kind)
        if kind == "number":
            # Any JSON number (an SDK that reads numbers as doubles may report 10.0);
            # never a bool or text.
            same = type(got) in (int, float) and got == expected
        else:
            same = type(got) is type(expected) and got == expected
        if not same:
            problems.append(f"values.{kind}={got!r} (expected {expected!r}, the seed's on value)")
    return problems


def _event_id_problems(sdk: str, payload: dict) -> list[str]:
    """What is wrong with the smoke's ``event_id`` report (see ``EVENT_ID_SDKS``)."""
    if sdk not in EVENT_ID_SDKS:
        return [f"event_id reported but {sdk} is not in EVENT_ID_SDKS"] if "event_id" in payload else []
    report = payload.get("event_id")
    if not isinstance(report, dict):
        return [f"event_id missing ({sdk} is in EVENT_ID_SDKS)"]
    problems: list[str] = []
    for field in ("user_id", "event_name", "id"):
        value = report.get(field)
        if not isinstance(value, str) or not value:
            problems.append(f"event_id.{field}={value!r} (expected a non-empty string)")
    sends = report.get("sends")
    if type(sends) is not int or sends != 2:  # JSON true and 2.0 are not a count of 2
        problems.append(f"event_id.sends={sends!r} (the same event is to be sent twice)")
    return problems


def _event_rows(user_id: str, event_name: str) -> int:
    """How many ``events`` rows the server holds for *user_id* and *event_name*."""
    sys.path.insert(0, str(REPO_ROOT))
    from backend.app.db.session import SessionLocal
    from backend.app.models import register_core_models
    from backend.app.models.event import Event

    register_core_models()
    db = SessionLocal()
    try:
        return db.query(Event).filter(Event.user_id == user_id, Event.event_type == event_name).count()
    finally:
        db.close()


def _stored_once_problems(sdk: str, payload: dict) -> list[str]:
    """The database half of C4: the one event sent twice is one row."""
    if sdk not in EVENT_ID_SDKS:
        return []
    report = payload["event_id"]
    try:
        rows = _event_rows(report["user_id"], report["event_name"])
    except Exception as exc:  # the backend package or its database is not reachable
        return [f"cannot count the stored events: {exc}"]
    if rows != 1:
        return [
            f"the event sent twice with event_id {report['id']!r} left {rows} stored row(s) "
            f"for user {report['user_id']!r}, expected 1"
        ]
    return []


def _alias_rows(anonymous_id: str, user_id: str, event_name: str, experiment_key: str) -> dict:
    """What the server stored for the smoke's alias step: who holds the
    experiment's assignment rows among the two ids, and each row of the
    key-less event under either id (its user id, whether it names the
    experiment, and its variant's name)."""
    sys.path.insert(0, str(REPO_ROOT))
    from backend.app.db.session import SessionLocal
    from backend.app.models import register_core_models
    from backend.app.models.assignment import Assignment
    from backend.app.models.event import Event
    from backend.app.models.experiment import Experiment, Variant

    register_core_models()
    ids = [anonymous_id, user_id]
    db = SessionLocal()
    try:
        experiment = db.query(Experiment).filter(Experiment.key == experiment_key).first()
        if experiment is None:
            raise RuntimeError(f"experiment {experiment_key!r} is not in the database")
        names = {v.id: v.name for v in db.query(Variant).filter(Variant.experiment_id == experiment.id)}
        holders = (
            db.query(Assignment.user_id)
            .filter(Assignment.experiment_id == experiment.id, Assignment.user_id.in_(ids))
            .order_by(Assignment.user_id)
        )
        events = db.query(Event).filter(Event.user_id.in_(ids), Event.event_type == event_name)
        return {
            "assignments": [row.user_id for row in holders],
            "events": [
                {
                    "user_id": row.user_id,
                    "tagged": row.experiment_id == experiment.id,
                    "variant": names.get(row.variant_id),
                }
                for row in events
            ],
        }
    finally:
        db.close()


def _alias_stored_problems(sdk: str, payload: dict, experiment_key: str) -> list[str]:
    """The database half of the alias step (see ``ALIAS_SDKS``)."""
    if sdk not in ALIAS_SDKS:
        return []
    report = payload["alias"]
    anonymous_id = report["anonymous_id"]
    try:
        stored = _alias_rows(anonymous_id, report["user_id"], report["event_name"], experiment_key)
    except Exception as exc:  # the backend package or its database is not reachable
        return [f"cannot read the stored alias rows: {exc}"]
    problems: list[str] = []
    if stored["assignments"] != [anonymous_id]:
        problems.append(
            f"alias: the assignment rows among the two ids are held by {stored['assignments']!r},"
            " expected one, held by the anonymous id"
        )
    events = stored["events"]
    if len(events) != 1:
        problems.append(f"alias: {len(events)} stored row(s) for the key-less event, expected 1")
        return problems
    (event,) = events
    if not event["tagged"]:
        problems.append("alias: 0 tagged events (experiment_id NULL)")
        return problems
    if event["variant"] is None:
        problems.append("alias: event variant NULL")
    elif event["variant"] != report["variant_before"]:
        problems.append(
            f"alias: the event counts for {event['variant']!r}, the anonymous id holds"
            f" {report['variant_before']!r}"
        )
    if event["user_id"] != anonymous_id:
        problems.append(
            "alias: the event is stored under the known id, expected under the anonymous id"
            " that holds the assignment"
        )
    return problems


def _targeting_problems(sdk: str, payload: dict) -> list[str]:
    """What is wrong with the smoke's ``targeted`` report (see ``ATTRIBUTE_SDKS``)."""
    if sdk not in ATTRIBUTE_SDKS:
        return [f"targeted reported but {sdk} is not in ATTRIBUTE_SDKS"] if "targeted" in payload else []
    targeted = payload.get("targeted")
    if not isinstance(targeted, dict):
        return [f"targeted missing ({sdk} is in ATTRIBUTE_SDKS)"]
    problems: list[str] = []
    if targeted.get("pro") is not True:
        problems.append(f"targeted.pro={targeted.get('pro')!r} (seeded rule plan=pro)")
    if targeted.get("free") is not False:
        problems.append(f"targeted.free={targeted.get('free')!r} (seeded rollout 0)")
    if targeted.get("reason") != "targeting_rule":
        problems.append(f"targeted.reason={targeted.get('reason')!r} (expected 'targeting_rule' for plan=pro)")
    return problems


def _exposure_problems(sdk: str, payload: dict, experiment_key: str) -> list[str]:
    """What is wrong with the smoke's ``exposure`` report (see ``EXPOSURE_SDKS``)."""
    if sdk not in EXPOSURE_SDKS:
        return [f"exposure reported but {sdk} is not in EXPOSURE_SDKS"] if "exposure" in payload else []
    exposure = payload.get("exposure")
    if not isinstance(exposure, dict):
        return [f"exposure missing ({sdk} is in EXPOSURE_SDKS)"]
    problems: list[str] = []
    calls = exposure.get("calls")
    if type(calls) is not int or calls != 1:  # JSON true and 1.0 are not a count of 1
        problems.append(f"exposure.calls={calls!r} (two assigns on one client; expected 1)")
    received = exposure.get("payload") if isinstance(exposure.get("payload"), dict) else {}
    if received.get("experiment_key") != experiment_key:
        found = received.get("experiment_key")
        problems.append(f"exposure.payload.experiment_key={found!r}, expected {experiment_key!r}")
    assigned = (payload.get("assign") or {}).get("variant_name")
    if received.get("variant_name") != assigned:
        found = received.get("variant_name")
        problems.append(f"exposure.payload.variant_name={found!r} != assign.variant_name={assigned!r}")
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


def _evaluate_request(api_url: str, key: str, flag_key: str) -> tuple[int, dict]:
    """``GET /feature-flags/evaluate/{flag_key}`` for one user with no attributes."""
    path = f"/api/v1/feature-flags/evaluate/{urllib.parse.quote(flag_key, safe='')}?user_id=contract-user"
    request = urllib.request.Request(f"{api_url}{path}", headers={"X-API-Key": key})
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, {}


def check_staging(
    api_url: str, plain_key: str, staging_key: str, staging_local_key: str, flag_key: str, targeted_key: str
) -> tuple[str, str]:
    """A key answers its own flag environment (#2011).

    The seed configures two flags in staging, each the other way round from
    production (``sdk_contract_flag`` off, the targeted one on at 100%). The
    staging keys must be answered staging's, by the evaluate route and by the
    ruleset, and the production key production's.
    """
    problems: list[str] = []
    for name, key, flag, expected in (
        ("production", plain_key, flag_key, "on"),
        ("staging", staging_key, flag_key, "off"),
        ("production", plain_key, targeted_key, "off"),
        ("staging", staging_key, targeted_key, "on"),
    ):
        status, answer = _evaluate_request(api_url, key, flag)
        got = ("on" if answer.get("enabled") else "off") if status == 200 else f"{status}"
        if got != expected:
            problems.append(f"evaluate {flag!r} with the {name} key: expected {expected}, got {got}")
    status, _headers, body = _ruleset_request(api_url, staging_local_key)
    if status != 200:
        problems.append(f"the staging sdk:ruleset key got {status} from {RULESET_PATH}, expected 200")
        return "FAIL", "; ".join(problems)
    entries = {entry.get("key"): entry for entry in json.loads(body).get("flags", [])}
    for flag, expected in ((flag_key, "off"), (targeted_key, "on")):
        entry = entries.get(flag) or {}
        got = "on" if entry.get("active") and entry.get("rollout_percentage") == 100 else "off"
        if flag not in entries or got != expected:
            problems.append(f"ruleset {flag!r} with the staging key: expected {expected}, got {got if entry else 'no entry'}")
    if problems:
        return "FAIL", "; ".join(problems)
    return "PASS", f"{flag_key} off and {targeted_key} on, by evaluate and by the ruleset; production the other way round"


def _api_key_get(api_url: str, key: str, path: str, query: dict[str, str]) -> tuple[int, object]:
    """``GET`` *path* with the API key and *query*: (status, the JSON body or None)."""
    url = f"{api_url}{path}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    request = urllib.request.Request(url, headers={"X-API-Key": key})
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, None


def _context(context: dict | None) -> dict[str, str]:
    return {} if context is None else {"context": json.dumps(context)}


def check_multiway(api_url: str, key: str, users: dict[str, str]) -> tuple[str, str]:
    """The seed's two multi-way flags, on the running stack (#2012).

    The evaluate route answers each of *users* (``{user: value}``, picked by
    the seed with the server's own split function) its value of the split,
    so as many distinct values as users, and a user with ``plan`` equals
    ``pro`` the rule's own value. ``/feature-flags/user/{id}`` stays boolean:
    the split flag ``true`` for each user, the rule flag ``true`` with plan=pro
    and ``false`` without.
    """
    evaluate = f"/api/v1/feature-flags/evaluate/{urllib.parse.quote(MULTIWAY_FLAG_KEY, safe='')}"
    problems: list[str] = []
    served: dict[str, object] = {}
    for user, expected in users.items():
        status, answer = _api_key_get(api_url, key, evaluate, {"user_id": user})
        answer = answer if isinstance(answer, dict) else {}
        served[user] = answer.get("value") if status == 200 else f"HTTP {status}"
        if status != 200 or answer.get("enabled") is not True or served[user] != expected:
            problems.append(
                f"evaluate {MULTIWAY_FLAG_KEY!r} for {user!r}: enabled {answer.get('enabled')!r},"
                f" value {served[user]!r} (expected {expected!r})"
            )
    distinct = len({json.dumps(value, sort_keys=True) for value in served.values()})
    if distinct != len(users):
        plural = "" if distinct == 1 else "s"
        problems.insert(0, f"{len(users)} users got {distinct} distinct value{plural}; expected {len(users)}")
    rule_path = f"/api/v1/feature-flags/evaluate/{urllib.parse.quote(RULE_VALUE_FLAG_KEY, safe='')}"
    status, answer = _api_key_get(api_url, key, rule_path, {"user_id": RULE_USER, **_context({"plan": "pro"})})
    answer = answer if isinstance(answer, dict) else {}
    if status != 200 or answer.get("reason") != "targeting_rule" or answer.get("value") != RULE_VALUE:
        problems.append(
            f"evaluate {RULE_VALUE_FLAG_KEY!r} with plan=pro: HTTP {status}, reason {answer.get('reason')!r},"
            f" value {answer.get('value')!r} (expected {RULE_VALUE!r} by the rule)"
        )
    bulk = [(user, None, {MULTIWAY_FLAG_KEY: True, RULE_VALUE_FLAG_KEY: False}) for user in users]
    bulk.append((RULE_USER, {"plan": "pro"}, {RULE_VALUE_FLAG_KEY: True}))
    for user, context, wanted in bulk:
        path = f"/api/v1/feature-flags/user/{urllib.parse.quote(user, safe='')}"
        status, flags = _api_key_get(api_url, key, path, _context(context))
        flags = flags if isinstance(flags, dict) else {}
        for flag, expected in wanted.items():
            got = flags.get(flag) if status == 200 else f"HTTP {status}"
            if type(got) is not bool or got is not expected:
                problems.append(
                    f"/feature-flags/user/{user} answered {flag!r}: {got!r}"
                    f" (expected {json.dumps(expected)}, a boolean)"
                )
    if problems:
        return "FAIL", "; ".join(problems)
    shown = ", ".join(f"{user} {value!r}" for user, value in served.items())
    return (
        "PASS",
        f"{len(users)} users, {distinct} distinct values ({shown}); the rule's own value {RULE_VALUE!r} for"
        " plan=pro; /feature-flags/user answers booleans",
    )


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


def _exposure_rows(user_id: str, experiment_key: str) -> int:
    """The exposure events the server stored for *user_id* in *experiment_key*."""
    sys.path.insert(0, str(REPO_ROOT))
    from backend.app.db.session import SessionLocal
    from backend.app.models import register_core_models
    from backend.app.models.event import Event
    from backend.app.models.experiment import Experiment
    from backend.app.services.event_matching import EXPOSURE_EVENT_TYPES

    register_core_models()
    db = SessionLocal()
    try:
        experiment = db.query(Experiment).filter(Experiment.key == experiment_key).first()
        if experiment is None:
            raise RuntimeError(f"experiment {experiment_key!r} is not in the database")
        return int(
            db.query(Event)
            .filter(
                Event.user_id == user_id,
                Event.experiment_id == experiment.id,
                Event.event_type.in_(EXPOSURE_EVENT_TYPES),
            )
            .count()
        )
    finally:
        db.close()


def documented_bootstrap(page_text: str) -> dict | None:
    """The JSON example of a bootstrap on the React page: the first ``json``
    fence whose object has ``userId``, ``flags`` and ``experiments``."""
    for block in page_text.split("```json\n")[1:]:
        try:
            value = json.loads(block.split("\n```", 1)[0])
        except ValueError:
            continue
        if isinstance(value, dict) and {"userId", "flags", "experiments"} <= set(value):
            return value
    return None


def _entry_fields(entries: object) -> list[list[str]]:
    """The sorted field names of each entry of a bootstrap's ``flags`` or ``experiments``."""
    if not isinstance(entries, dict):
        return []
    return [sorted(entry) for entry in entries.values() if isinstance(entry, dict)]


def bootstrap_problems(
    server: dict,
    client: dict,
    counts: tuple[int, int],
    user_id: str,
    flag_key: str,
    experiment_key: str,
    documented: dict | None,
) -> list[str]:
    """What is wrong with a bootstrap run: the two steps' reports, the exposure
    rows counted after each (*counts*), and the page's JSON example."""
    problems: list[str] = []
    bootstrap = server.get("bootstrap") if isinstance(server.get("bootstrap"), dict) else {}
    if server.get("step") != "server" or bootstrap.get("userId") != user_id:
        problems.append(f"the server step reported {server.get('step')!r} for {bootstrap.get('userId')!r}")
    flag = (bootstrap.get("flags") or {}).get(flag_key)
    if not isinstance(flag, dict) or flag.get("enabled") is not True:
        problems.append(f"bootstrap.flags[{flag_key!r}] is {flag!r} (seeded flag is 100% on)")
    assigned = (bootstrap.get("experiments") or {}).get(experiment_key)
    if not isinstance(assigned, dict) or assigned.get("variant_name") not in EXPECTED_VARIANTS:
        problems.append(f"bootstrap.experiments[{experiment_key!r}] is {assigned!r}")
    if server.get("requests_during_render") != 0:
        problems.append(f"the server render made {server.get('requests_during_render')!r} request(s), expected 0")
    after_server, after_client = counts
    if after_server != BOOTSTRAP_SERVER_EXPOSURES:
        problems.append(
            f"exposure rows after the server step: {after_server}, expected {BOOTSTRAP_SERVER_EXPOSURES}"
        )
    if after_client - after_server != BOOTSTRAP_CLIENT_EXPOSURES:
        problems.append(
            f"exposure rows added by the client step: {after_client - after_server},"
            f" expected {BOOTSTRAP_CLIENT_EXPOSURES}"
        )
    if isinstance(assigned, dict) and client.get("variant_name") != assigned.get("variant_name"):
        problems.append(
            f"the client step was assigned {client.get('variant_name')!r},"
            f" the bootstrap {assigned.get('variant_name')!r} (assignment is sticky)"
        )
    if documented is None:
        problems.append(f"{REACT_PAGE.relative_to(REPO_ROOT)} has no JSON example of a bootstrap")
    else:
        for part in ("flags", "experiments"):
            live_fields, doc_fields = _entry_fields(bootstrap.get(part)), _entry_fields(documented.get(part))
            if not live_fields or any(fields != live_fields[0] for fields in doc_fields + live_fields):
                problems.append(
                    f"the page's example {part} entries have the fields {doc_fields},"
                    f" the live server's {live_fields}: regenerate the example from the bootstrap printed here"
                )
        if sorted(documented) != sorted(bootstrap):
            problems.append(f"the page's example has the keys {sorted(documented)}, the live one {sorted(bootstrap)}")
    return problems


def run_react_bootstrap(env: dict[str, str], timeout: int) -> tuple[str, str]:
    """The React SDK's bootstrap, judged by what the server recorded (see BOOTSTRAP_SMOKE)."""
    import uuid

    user_id = f"bootstrap-{uuid.uuid4()}"
    experiment_key, flag_key = env["CONTRACT_EXPERIMENT_KEY"], env["CONTRACT_FLAG_KEY"]
    reports: dict[str, dict] = {}
    counts: list[int] = []
    for step in ("server", "client"):
        status, detail, payload = _run_command(
            BOOTSTRAP_SMOKE, dict(env, CONTRACT_USER_ID=user_id, CONTRACT_BOOTSTRAP_STEP=step), timeout
        )
        if status != "PASS":
            return status, f"{step} step: {detail}"
        reports[step] = payload
        try:
            counts.append(_exposure_rows(user_id, experiment_key))
        except Exception as exc:  # the backend package or its database is not reachable
            return "FAIL", f"cannot read the database for the exposure rows: {exc}"
    documented = documented_bootstrap(REACT_PAGE.read_text(encoding="utf-8"))
    problems = bootstrap_problems(
        reports["server"], reports["client"], (counts[0], counts[1]), user_id, flag_key, experiment_key, documented
    )
    live = json.dumps(reports["server"].get("bootstrap"), sort_keys=True)
    if problems:
        return "FAIL", "; ".join(problems) + f"; live bootstrap: {live}"
    return (
        "PASS",
        f"exposure rows {counts[0]} after the server render, +{counts[1] - counts[0]} after hydration;"
        f" the page's example has the live fields; live bootstrap: {live}",
    )


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


def _published_sdks():
    """tests/sdk-contract/published_sdks.py, imported by path (stdlib only)."""
    spec = importlib.util.spec_from_file_location("published_sdks", SDK_CONTRACT_DIR / "published_sdks.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("published_sdks", module)  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


def _api(api_url: str, method: str, path: str, token: str = "", body: dict | None = None) -> tuple[int, object]:
    """One API call with a user's bearer token; (status, parsed JSON body or None)."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(f"{api_url}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:  # noqa: S310
            status, payload = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, payload = exc.code, exc.read()
    try:
        return status, json.loads(payload) if payload else None
    except ValueError:
        return status, None


def _sign_in(api_url: str) -> str:
    email, password = TOGGLE_USER
    status, body = _api(api_url, "POST", "/api/v1/auth/login", body={"email": email, "password": password})
    if status != 200 or not isinstance(body, dict) or not body.get("access_token"):
        raise RuntimeError(f"POST /api/v1/auth/login as {email} answered {status}")
    return str(body["access_token"])


def _flag_ids(api_url: str, token: str, keys: tuple[str, ...]) -> dict[str, str]:
    ids = {}
    for key in keys:
        status, body = _api(api_url, "GET", f"/api/v1/feature-flags/?search={urllib.parse.quote(key)}", token)
        items = body.get("items", []) if isinstance(body, dict) else []
        found = [item["id"] for item in items if item.get("key") == key]
        if status != 200 or len(found) != 1:
            raise RuntimeError(f"flag {key!r} is not in GET /api/v1/feature-flags/ ({status}); run the seed")
        ids[key] = found[0]
    return ids


def _turn(api_url: str, token: str, ids: dict[str, str], on: bool) -> None:
    action = "activate" if on else "deactivate"
    for key, flag_id in ids.items():
        status, _ = _api(api_url, "POST", f"/api/v1/feature-flags/{flag_id}/{action}", token)
        if status != 200:
            raise RuntimeError(f"POST /api/v1/feature-flags/{{id}}/{action} for {key!r} answered {status}")


def _published_harness(language: str, root: Path, config: dict) -> list[str]:
    argument = json.dumps(config)
    if language == "js":
        return [shutil.which("node") or "node", str(SDK_CONTRACT_DIR / "harness" / "published_js_live.cjs"), str(root), argument]
    # -I -S: only the verified extraction on sys.path (see published_python_live.py).
    return [sys.executable, "-I", "-S", str(SDK_CONTRACT_DIR / "harness" / "published_python_live.py"), str(root), argument]


def _shown(answers: dict) -> str:
    return ", ".join(f"{key}: {json.dumps(answer, sort_keys=True)}" for key, answer in sorted(answers.items()))


def run_published_local(
    sdk: str,
    version: str,
    api_url: str,
    local_key: str,
    timeout: int,
    multiway: tuple[str, str] | None = None,
) -> tuple[str, str]:
    """A published release's own client, in local mode, against this server (G3).

    It must load the server's ruleset (its version is the one the route serves)
    and answer every value flag locally as on; after the runner turns them off
    through the API it must answer each ``inactive``, locally, within
    PUBLISHED_WAIT_SECONDS. The flags are turned back on whatever happens.

    With *multiway* (``(user, the value of the split the server serves the
    user)``), the row reads the seed's multi-way flag for that user instead
    (#2012): first answered by the server with that value, then, after the
    runner turns it off, ``inactive`` locally with the off value.
    """
    if multiway is None:
        flags, user_id = VALUE_FLAG_KEYS, f"published-{sdk}-{version}"
        expected_on, expected_off = PUBLISHED_ON, PUBLISHED_OFF
    else:
        flags, user_id = (MULTIWAY_FLAG_KEY,), multiway[0]
        expected_on = {**PUBLISHED_SERVER, "value": multiway[1]}
        expected_off = {**PUBLISHED_OFF, "value": MULTIWAY_OFF_VALUE}
    try:
        ps = _published_sdks()
        (artefact,) = [a for a in ps.load_pins() if a.language == sdk and a.version == version]
    except Exception as exc:  # a missing or ambiguous pin is a failure of this row
        return "FAIL", f"no single pin for {sdk} {version} in published-sdks.json: {exc}"
    work = Path(tempfile.mkdtemp(prefix=f"live-published-{sdk}-"))
    process = None
    token, ids, turned_off = "", {}, False
    try:
        extraction = work / "artefact"
        root = ps.extract(artefact, ps.fetch(artefact, work / "cache"), extraction)
        (work / "cwd").mkdir()
        config = {
            "api_url": api_url,
            "api_key": local_key,
            "flags": list(flags),
            "user_id": user_id,
            "refresh_seconds": PUBLISHED_REFRESH_SECONDS,
            "ready_seconds": 30,
            "wait_seconds": PUBLISHED_WAIT_SECONDS,
        }
        if multiway is not None:
            config["values"] = True
        process = subprocess.Popen(
            _published_harness(artefact.language, root, config),
            cwd=work / "cwd",
            env={"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8"},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        lines: "queue.Queue[str]" = queue.Queue()
        threading.Thread(target=lambda: [lines.put(line) for line in process.stdout], daemon=True).start()

        def report(phase: str) -> dict:
            try:
                line = lines.get(timeout=timeout)
            except queue.Empty:
                raise RuntimeError(f"no '{phase}' report within {timeout}s") from None
            payload = json.loads(line)
            if payload.get("phase") != phase:
                raise RuntimeError(f"expected the '{phase}' report, got {line[:200]}")
            return payload

        loaded = report("loaded")
        module = ps.check_identity(artefact, extraction, loaded)
        _status, headers, _body = _ruleset_request(api_url, local_key)
        served = headers.get("etag", "").strip('"')
        problems = []
        if not loaded["ready"].get("ok") or loaded["status"].get("version") != served:
            problems.append(
                f"it did not load the ruleset the server serves ({served}): ready {loaded['ready']}, "
                f"version {loaded['status'].get('version')!r}"
            )
        if any(answer != expected_on for answer in loaded["answers"].values()):
            problems.append(f"before the change it answered {_shown(loaded['answers'])}; expected each {expected_on}")
        if problems:
            return "FAIL", "; ".join(problems + loaded.get("errors", [])[:3])

        token = _sign_in(api_url)
        ids = _flag_ids(api_url, token, tuple(flags))
        turned_off = True
        _turn(api_url, token, ids, on=False)
        process.stdin.write("turned off\n")
        process.stdin.flush()
        after = report("after")
        if any(answer != expected_off for answer in after["answers"].values()):
            return "FAIL", (
                f"answer unchanged after the flags were turned off: after {after['seconds']:.1f}s it answered "
                f"{_shown(after['answers'])}; expected each {expected_off}"
            )
        if multiway is not None:
            return "PASS", (
                f"{artefact.digest[:19]}... at {module}: loaded ruleset {served[:12]}, {MULTIWAY_FLAG_KEY} answered "
                f"by the server for {user_id} ({multiway[1]!r}); turned off through the API, it answered inactive "
                f"locally after {after['seconds']:.1f}s"
            )
        return "PASS", (
            f"{artefact.digest[:19]}... at {module}: loaded ruleset {served[:12]}, the {len(ids)} value flags "
            f"answered locally; turned off through the API, each answered inactive locally after "
            f"{after['seconds']:.1f}s"
        )
    except Exception as exc:  # the row fails and says why; the next row still runs
        stderr = ""
        if process is not None and process.poll() is not None and process.stderr is not None:
            stderr = process.stderr.read()[-300:]
        return "FAIL", f"{type(exc).__name__}: {exc} {stderr}".strip()
    finally:
        if turned_off:
            try:
                _turn(api_url, token, ids, on=True)
            except Exception as exc:  # reported; the next run's seed turns them back on too
                print(f"could not turn {', '.join(flags)} back on: {exc}", file=sys.stderr)
        if process is not None:
            if process.poll() is None:
                try:
                    process.stdin.close()
                    process.wait(timeout=15)
                except Exception:
                    process.kill()
            process.wait()
        shutil.rmtree(work, ignore_errors=True)


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
    problems = validate(sdk, payload, env["CONTRACT_EXPERIMENT_KEY"])
    if not problems:
        problems = _stored_once_problems(sdk, payload)
        problems += _alias_stored_problems(sdk, payload, env["CONTRACT_EXPERIMENT_KEY"])
    if problems:
        return "FAIL", "; ".join(problems)
    return "PASS", f"{payload['assign']['variant_name']} in {detail}"


#: What the smokes get under the settings' former names: an address nothing
#: listens on and a key the server does not know.
DECOY_URL = "http://127.0.0.1:9"
DECOY_KEY = "former-name-decoy-key"


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
    # The smokes get the real values under the new names and decoys under the
    # former ones: a smoke that reads a former name first talks to a port
    # nothing listens on, with a key the server refuses, and fails.
    env.update(
        {
            "CONTROLVAR_API_URL": args.api_url,
            "CONTROLVAR_API_KEY": api_key,
            "EXPERIMENTLY_API_URL": DECOY_URL,
            "EXPERIMENTLY_API_KEY": DECOY_KEY,
        }
    )
    env.setdefault("CONTRACT_EXPERIMENT_KEY", "sdk_contract_ab")
    env.setdefault("CONTRACT_FLAG_KEY", "sdk_contract_flag")
    env.setdefault("CONTRACT_TARGETED_FLAG_KEY", TARGETED_FLAG_KEY)

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

    # And that a key answers its own environment, with the seed's two staging keys.
    staging_key, staging_local_key = (
        os.environ.get(name) or (path.read_text().strip() if path.exists() else "")
        for name, path in (
            ("CONTROLVAR_STAGING_API_KEY", STAGING_KEY_FILE),
            ("CONTROLVAR_LOCAL_STAGING_API_KEY", STAGING_LOCAL_KEY_FILE),
        )
    )
    if staging_key and staging_local_key:
        status, detail = check_staging(
            args.api_url,
            api_key,
            staging_key,
            staging_local_key,
            env["CONTRACT_FLAG_KEY"],
            env["CONTRACT_TARGETED_FLAG_KEY"],
        )
    else:
        status = "FAIL" if args.strict else "SKIP"
        detail = "no staging keys: run seed_sdk_contract.py"
    results.append(("ruleset (staging)", status, detail))
    print(f"{status:4} {'ruleset (staging)':<20} {detail}", flush=True)

    # And the seed's two multi-way flags (#2012), for the users the smokes are given.
    try:
        users = multiway_users()
    except Exception as exc:  # the seed module or the backend package does not import
        users = {}
        status, detail = "FAIL", f"cannot pick the seed's users of {MULTIWAY_FLAG_KEY}: {exc}"
    else:
        status, detail = check_multiway(args.api_url, api_key, users)
    results.append(("multiway (server)", status, detail))
    print(f"{status:4} {'multiway (server)':<20} {detail}", flush=True)
    env[MULTIWAY_USERS_ENV] = ",".join(users)

    # Every smoke also gets the sdk:ruleset key, under the new name only (the decoy under the
    # former one): the alias step needs a server-side key (ALIAS_SDKS), and a smoke that links ids
    # without being listed then reports it, and fails.
    smoke_env = dict(env, CONTROLVAR_LOCAL_API_KEY=local_key, EXPERIMENTLY_LOCAL_API_KEY=DECOY_KEY) if local_key else env

    for sdk in selected:
        command, tools = MANIFEST[sdk]
        missing = [t for t in tools if shutil.which(t) is None]
        if missing:
            results.append((sdk, "FAIL" if args.strict else "SKIP", f"missing toolchain: {', '.join(missing)}"))
            continue
        status, detail = run_one(sdk, command, smoke_env, args.timeout)
        results.append((sdk, status, detail))
        print(f"{status:4} {sdk:<20} {detail}", flush=True)
        if sdk == "react":
            # After the react row, which built sdk/react/dist (#1518).
            name = "react (bootstrap)"
            status, detail = run_react_bootstrap(env, args.timeout)
            results.append((name, status, detail))
            print(f"{status:4} {name:<20} {detail}", flush=True)

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
        local_env = dict(env, CONTROLVAR_LOCAL_API_KEY=local_key, EXPERIMENTLY_LOCAL_API_KEY=DECOY_KEY)
        status, detail = run_local(sdk, command, local_env, args.timeout, env["CONTRACT_FLAG_KEY"])
        results.append((name, status, detail))
        print(f"{status:4} {name:<20} {detail}", flush=True)

    # The published releases, last (they turn the seed's value flags, and its
    # multi-way flag, off and on).
    published = [(sdk, version, reads) for sdk in selected for (s, version, reads) in PUBLISHED_LOCAL if s == sdk]
    for sdk, version, reads in published:
        name = f"{sdk} {version} (published, {'multi-way' if reads == 'multiway' else 'local'})"
        tools = ("node",) if sdk == "js" else ()
        missing = [t for t in tools if shutil.which(t) is None]
        if missing:
            results.append((name, "FAIL" if args.strict else "SKIP", f"missing toolchain: {', '.join(missing)}"))
            continue
        if not local_key:
            results.append((name, "FAIL" if args.strict else "SKIP", "no sdk:ruleset key"))
            continue
        if reads == "multiway":
            if not users:
                results.append((name, "FAIL", "no users of the multi-way flag (see the multiway (server) row)"))
                continue
            user = next(iter(users))
            status, detail = run_published_local(
                sdk, version, args.api_url, local_key, args.timeout, multiway=(user, users[user])
            )
        else:
            status, detail = run_published_local(sdk, version, args.api_url, local_key, args.timeout)
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
