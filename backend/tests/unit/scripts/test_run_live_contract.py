"""tests/sdk-contract/live/run_live_contract.py: what it runs and what it requires.

The SDK pages and the SDK guide say a "verified live" SDK is one whose
``contract_smoke`` entry point this runner ran against a backend seeded with
``backend/scripts/seed_sdk_contract.py``, and that ``--sdk <name> --strict``
runs one SDK and fails on a missing toolchain. These import the runner by path
and drive ``main`` with its network calls and subprocesses replaced: no
backend, no toolchain, nothing started.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
RUNNER = REPO_ROOT / "tests" / "sdk-contract" / "live" / "run_live_contract.py"
SEED = REPO_ROOT / "backend" / "scripts" / "seed_sdk_contract.py"
#: Each setting the runner reads: its name, its name before the rename, and
#: what it falls back to (the key files below hold the fallback keys).
SETTINGS = {
    "url": ("CONTROLVAR_API_URL", "EXPERIMENTLY_API_URL", "http://localhost:8000"),
    "key": ("CONTROLVAR_API_KEY", "EXPERIMENTLY_API_KEY", "file-key"),
    "local": (
        "CONTROLVAR_LOCAL_API_KEY",
        "EXPERIMENTLY_LOCAL_API_KEY",
        "file-local-key",
    ),
}
#: The staging environment's two keys (#2011), each under its one name.
STAGING_KEYS = {
    "CONTROLVAR_STAGING_API_KEY": "staging-key",
    "CONTROLVAR_LOCAL_STAGING_API_KEY": "staging-local-key",
}
#: Three distinct values per setting: neither is a default, and neither is the
#: 127.0.0.1:8000 CI sets.
NEW = {"url": "http://127.0.0.1:9", "key": "new-key", "local": "new-local-key"}
OLD = {"url": "http://127.0.0.1:7", "key": "old-key", "local": "old-local-key"}
GOOD = {
    "assign": {"variant_name": "control", "is_control": True, "sticky": True},
    "flag": {"enabled": True},
    "track": {"ok": True},
    "fanout": {"ok": True},
}
#: What the smoke of an SDK in EXPOSURE_SDKS adds: one hook call, for the
#: contract experiment and the variant GOOD was assigned.
EXPOSURE = {
    "calls": 1,
    "payload": {
        "experiment_key": "sdk_contract_ab",
        "variant_name": "control",
        "variant_id": "v-control",
        "user_id": "smoke-user",
    },
}
#: What the smoke of an SDK in ATTRIBUTE_SDKS adds (#1520): the seed's targeted
#: flag on for plan=pro through its rule, off for plan=free.
TARGETED = {"pro": True, "free": False, "reason": "targeting_rule"}
#: What the smoke of an SDK in EVENT_ID_SDKS adds (#1543, C4): the user and
#: event name the database is asked about, the one id, and two sends.
EVENT_ID = {
    "user_id": "smoke-user",
    "event_name": "contract_dedup_1",
    "id": "contract-1",
    "sends": 2,
}
#: What the smoke of an SDK in VALUE_SDKS adds (#1810): what its string,
#: number and json getters returned for the seed's three value flags, each on
#: at 100%, so each is its on value.
VALUES = {"string": "on-copy", "number": 10, "json": {"color": "blue"}}
#: What the smoke of an SDK in ALIAS_SDKS adds (#449): the two ids it linked,
#: the two answers to the same link, the variant before and after, and the
#: key-less event it sent under the known id.
ALIAS = {
    "anonymous_id": "smoke-anon-1",
    "user_id": "smoke-known-1",
    "created": [True, False],
    "variant_before": "control",
    "variant_after": "control",
    "event_name": "contract_alias_1",
}
#: What the database holds after a passing alias step: one assignment row
#: among the two ids, the anonymous id's, and the event stored under it,
#: tagged with the experiment and that row's variant.
ALIAS_STORED = {
    "assignments": ["smoke-anon-1"],
    "events": [{"user_id": "smoke-anon-1", "tagged": True, "variant": "control"}],
}
PR_QA_GATE = REPO_ROOT / ".github" / "workflows" / "pr-qa-gate.yml"
PLATFORM = REPO_ROOT / ".github" / "workflows" / "_platform.yml"


def multiway(runner) -> dict:
    """What the smoke of an SDK in MULTIWAY_SDKS adds (#2012): each of the
    seed's three users of its split flag with the value the seed picked it for,
    and the rule's own value."""
    return {"users": dict(runner.multiway_users()), "rule": "pro-copy"}


def good(runner, sdk: str, experiment_key: str = "sdk_contract_ab") -> dict:
    """A passing report from *sdk*'s smoke: GOOD, with EXPOSURE only if the
    runner lists *sdk* in EXPOSURE_SDKS, TARGETED only if it lists *sdk* in
    ATTRIBUTE_SDKS, EVENT_ID only if in EVENT_ID_SDKS, VALUES only if in
    VALUE_SDKS, the multi-way report only if in MULTIWAY_SDKS and ALIAS only
    if in ALIAS_SDKS (an unlisted SDK may report none of them)."""
    report = {"sdk": sdk, **GOOD}
    if sdk in runner.EXPOSURE_SDKS:
        hook = {**EXPOSURE["payload"], "experiment_key": experiment_key}
        report["exposure"] = {**EXPOSURE, "payload": hook}
    if sdk in runner.ATTRIBUTE_SDKS:
        report["targeted"] = dict(TARGETED)
    if sdk in runner.EVENT_ID_SDKS:
        report["event_id"] = dict(EVENT_ID)
    if sdk in runner.VALUE_SDKS:
        report["values"] = json.loads(json.dumps(VALUES))
    if sdk in runner.MULTIWAY_SDKS:
        report["multiway"] = multiway(runner)
    if sdk in runner.ALIAS_SDKS:
        report["alias"] = json.loads(json.dumps(ALIAS))
    return report


@pytest.fixture
def runner(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "run_live_contract_under_test", RUNNER
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "KEY_FILE", tmp_path / "no-key")
    monkeypatch.setattr(module, "LOCAL_KEY_FILE", tmp_path / "no-local-key")
    monkeypatch.setattr(module, "STAGING_KEY_FILE", tmp_path / "no-staging-key")
    monkeypatch.setattr(
        module, "STAGING_LOCAL_KEY_FILE", tmp_path / "no-staging-local-key"
    )
    monkeypatch.setattr(module, "backend_healthy", lambda api_url: True)
    # Every name each setting is read under, so a developer's shell cannot
    # stand in for the value a test sets.
    for name in (
        *(n for names in SETTINGS.values() for n in names[:2]),
        *STAGING_KEYS,
        "CONTRACT_EXPERIMENT_KEY",
        "CONTRACT_FLAG_KEY",
        "CONTRACT_MULTIWAY_USERS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CONTROLVAR_API_KEY", "contract-key")
    # The staging row passes, unless a test says otherwise.
    for name, value in STAGING_KEYS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(module, "check_staging", lambda *args: ("PASS", "ok"))
    # And so does the multi-way row (#2012).
    monkeypatch.setattr(module, "check_multiway", lambda *args: ("PASS", "ok"))
    # No database here: the one event sent twice is one stored row, unless a
    # test says otherwise.
    monkeypatch.setattr(module, "_event_rows", lambda user_id, event_name: 1)
    # Nor here: the alias step stored what it should, unless a test says otherwise.
    monkeypatch.setattr(
        module,
        "_alias_rows",
        lambda anonymous_id, user_id, event_name, experiment_key: json.loads(
            json.dumps(ALIAS_STORED)
        ),
    )
    return module


def run_main(
    monkeypatch, runner, *args: str
) -> Tuple[int, List[Tuple[str, Dict[str, str]]]]:
    """main() with every smoke command answered GOOD, and what it ran."""
    ran: List[Tuple[str, Dict[str, str]]] = []

    def fake_run(command, env, timeout):
        ran.append((command, env))
        sdk = next(name for name, (cmd, _) in runner.MANIFEST.items() if cmd == command)
        return "PASS", "0.1s", good(runner, sdk, env["CONTRACT_EXPERIMENT_KEY"])

    monkeypatch.setattr(runner, "_run_command", fake_run)
    monkeypatch.setattr(sys, "argv", ["run_live_contract.py", *args])
    return runner.main(), ran


def test_each_sdk_runs_its_own_contract_smoke(runner):
    assert (
        runner.MANIFEST["python"][0] == "python sdk/python/examples/contract_smoke.py"
    )
    assert runner.MANIFEST["openfeature-python"][0] == (
        "python sdk/openfeature-python/examples/contract_smoke.py"
    )
    assert runner.MANIFEST["js"][0] == (
        "cd sdk/js && npm run build --silent && node examples/contract_smoke.mjs"
    )
    assert runner.MANIFEST["go"][0] == "cd sdk/go && go run ./examples/contract_smoke"
    assert runner.MANIFEST["java"][0] == "bash sdk/java/examples/contract_smoke.sh"
    assert runner.LOCAL_MANIFEST["js"][0].endswith("node examples/local_eval_smoke.mjs")


def test_sdk_runs_that_sdk_against_the_seeds_fixtures(monkeypatch, runner):
    monkeypatch.setattr(runner.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    status, ran = run_main(monkeypatch, runner, "--sdk", "python", "--strict")
    assert [command for command, _ in ran] == [
        "python sdk/python/examples/contract_smoke.py"
    ]
    env = ran[0][1]
    assert (env["CONTRACT_EXPERIMENT_KEY"], env["CONTRACT_FLAG_KEY"]) == (
        "sdk_contract_ab",
        "sdk_contract_flag",
    )
    assert env["CONTROLVAR_API_KEY"] == "contract-key"
    # --strict: no sdk:ruleset key is a failure, so the run fails as a whole.
    assert status == 1


def test_strict_fails_on_a_missing_toolchain_and_the_default_skips_it(
    monkeypatch, runner, capsys
):
    monkeypatch.setattr(runner.shutil, "which", lambda tool: None)
    monkeypatch.setenv("EXPERIMENTLY_LOCAL_API_KEY", "scoped")
    monkeypatch.setattr(runner, "check_ruleset", lambda *args: ("PASS", "ok"))
    assert run_main(monkeypatch, runner, "--sdk", "go")[0] == 0
    assert "SKIP go" in " ".join(capsys.readouterr().out.split())
    assert run_main(monkeypatch, runner, "--sdk", "go", "--strict")[0] == 1
    assert "missing toolchain: go" in capsys.readouterr().out


def test_a_smoke_must_report_a_sticky_assignment_an_enabled_flag_and_both_tracks(
    runner,
):
    # Every SDK the runner knows, with the report its smoke gives: an SDK that
    # joins EXPOSURE_SDKS is checked here with its exposure block.
    for sdk in runner.MANIFEST:
        report = good(runner, sdk)
        assert runner.validate(sdk, report) == [], sdk
        for change, problem in (
            (
                {"assign": {**GOOD["assign"], "sticky": False}},
                "assignment was not sticky",
            ),
            (
                {"flag": {"enabled": False}},
                "flag.enabled=False (seeded flag is 100% on)",
            ),
            ({"track": {"ok": False}}, "track failed"),
            ({"fanout": {"ok": False}}, "fan-out track failed"),
        ):
            assert runner.validate(sdk, {**report, **change}) == [problem], sdk


def test_a_listed_sdk_passes_with_one_exposure_and_an_unlisted_one_without_any(
    monkeypatch, runner
):
    monkeypatch.setattr(runner, "EXPOSURE_SDKS", frozenset({"js"}))
    listed, unlisted = good(runner, "js"), good(runner, "python")
    assert listed["exposure"] == EXPOSURE and "exposure" not in unlisted
    assert runner.validate("js", listed) == []
    assert runner.validate("python", unlisted) == []


@pytest.mark.parametrize(
    ("sdk", "exposure", "problem"),
    [
        pytest.param(
            "js",
            {**EXPOSURE, "calls": 0},
            "exposure.calls=0 (two assigns on one client; expected 1)",
            id="calls-0",
        ),
        pytest.param(
            "js",
            {**EXPOSURE, "calls": 2},
            "exposure.calls=2 (two assigns on one client; expected 1)",
            id="calls-2",
        ),
        pytest.param(
            "js",
            None,
            "exposure missing (js is in EXPOSURE_SDKS)",
            id="missing",
        ),
        pytest.param(
            "js",
            {
                **EXPOSURE,
                "payload": {**EXPOSURE["payload"], "variant_name": "treatment"},
            },
            "exposure.payload.variant_name='treatment' != assign.variant_name='control'",
            id="wrong-variant",
        ),
        pytest.param(
            "python",
            EXPOSURE,
            "exposure reported but python is not in EXPOSURE_SDKS",
            id="unlisted",
        ),
        pytest.param(
            "js",
            {**EXPOSURE, "calls": True},
            "exposure.calls=True (two assigns on one client; expected 1)",
            id="calls-true",
        ),
        pytest.param(
            "js",
            {**EXPOSURE, "payload": {**EXPOSURE["payload"], "experiment_key": "other"}},
            "exposure.payload.experiment_key='other', expected 'sdk_contract_ab'",
            id="wrong-experiment",
        ),
    ],
)
def test_each_wrong_exposure_report_gives_exactly_its_problem(
    monkeypatch, runner, sdk, exposure, problem
):
    """The gate's own planted reports, with "js" listed and "python" not."""
    monkeypatch.setattr(runner, "EXPOSURE_SDKS", frozenset({"js"}))
    monkeypatch.setattr(runner, "ATTRIBUTE_SDKS", frozenset())  # exposure alone here
    monkeypatch.setattr(runner, "EVENT_ID_SDKS", frozenset())  # exposure alone here
    monkeypatch.setattr(runner, "VALUE_SDKS", frozenset())  # exposure alone here
    monkeypatch.setattr(runner, "MULTIWAY_SDKS", frozenset())  # exposure alone here
    monkeypatch.setattr(runner, "ALIAS_SDKS", frozenset())  # exposure alone here
    report = {"sdk": sdk, **GOOD}
    if exposure is not None:
        report["exposure"] = exposure
    assert runner.validate(sdk, report) == [problem]


def test_java_js_python_react_react_native_dotnet_and_android_are_the_sdks_that_send_one_event_twice_with_one_id(
    runner,
):
    assert runner.EVENT_ID_SDKS == frozenset(
        {"java", "js", "python", "react", "react-native", "dotnet", "android"}
    )
    for sdk in sorted(runner.EVENT_ID_SDKS):
        assert good(runner, sdk)["event_id"] == EVENT_ID
        assert runner.validate(sdk, good(runner, sdk)) == []


@pytest.mark.parametrize(
    ("sdk", "report", "problems"),
    [
        pytest.param(
            "java", None, ["event_id missing (java is in EVENT_ID_SDKS)"], id="missing"
        ),
        pytest.param(
            "java",
            {**EVENT_ID, "sends": 1},
            ["event_id.sends=1 (the same event is to be sent twice)"],
            id="sent-once",
        ),
        pytest.param(
            "java",
            {**EVENT_ID, "sends": True},
            ["event_id.sends=True (the same event is to be sent twice)"],
            id="sends-true",
        ),
        pytest.param(
            "java",
            {**EVENT_ID, "id": ""},
            ["event_id.id='' (expected a non-empty string)"],
            id="no-id",
        ),
        pytest.param(
            "go",
            EVENT_ID,
            ["event_id reported but go is not in EVENT_ID_SDKS"],
            id="unlisted",
        ),
    ],
)
def test_each_wrong_event_id_report_gives_exactly_its_problems(
    monkeypatch, runner, sdk, report, problems
):
    """The gate's own planted reports, with "java" listed and "go" not."""
    monkeypatch.setattr(runner, "EVENT_ID_SDKS", frozenset({"java"}))
    payload = {**good(runner, sdk)}
    payload.pop("event_id", None)
    if report is not None:
        payload["event_id"] = report
    assert runner.validate(sdk, payload) == problems


def test_the_event_sent_twice_must_be_one_stored_row(monkeypatch, runner):
    asked = []

    def rows(user_id, event_name):
        asked.append((user_id, event_name))
        return 1

    monkeypatch.setattr(runner, "_event_rows", rows)
    monkeypatch.setattr(
        runner, "_run_command", lambda c, e, t: ("PASS", "0.1s", good(runner, "java"))
    )
    env = {"CONTRACT_EXPERIMENT_KEY": "sdk_contract_ab"}
    assert runner.run_one("java", "cmd", env, 5) == ("PASS", "control in 0.1s")
    assert asked == [("smoke-user", "contract_dedup_1")]


def test_two_stored_rows_for_the_one_event_fail_the_sdk(monkeypatch, runner):
    """The planted defect: a fresh id per send, or a server that does not dedupe."""
    monkeypatch.setattr(runner, "_event_rows", lambda user_id, event_name: 2)
    monkeypatch.setattr(
        runner, "_run_command", lambda c, e, t: ("PASS", "0.1s", good(runner, "java"))
    )
    env = {"CONTRACT_EXPERIMENT_KEY": "sdk_contract_ab"}
    status, detail = runner.run_one("java", "cmd", env, 5)
    assert status == "FAIL"
    assert "left 2 stored row(s)" in detail and "expected 1" in detail


def test_no_stored_row_or_no_database_fails_the_sdk(monkeypatch, runner):
    monkeypatch.setattr(
        runner, "_run_command", lambda c, e, t: ("PASS", "0.1s", good(runner, "java"))
    )
    env = {"CONTRACT_EXPERIMENT_KEY": "sdk_contract_ab"}
    monkeypatch.setattr(runner, "_event_rows", lambda user_id, event_name: 0)
    assert runner.run_one("java", "cmd", env, 5)[0] == "FAIL"

    def unreachable(user_id, event_name):
        raise RuntimeError("no database")

    monkeypatch.setattr(runner, "_event_rows", unreachable)
    status, detail = runner.run_one("java", "cmd", env, 5)
    assert status == "FAIL" and "cannot count the stored events: no database" in detail


def test_an_sdk_that_is_not_listed_is_not_asked_of_the_database(monkeypatch, runner):
    def never(user_id, event_name):
        raise AssertionError("the database was read for an unlisted SDK")

    monkeypatch.setattr(runner, "_event_rows", never)
    monkeypatch.setattr(
        runner, "_run_command", lambda c, e, t: ("PASS", "0.1s", good(runner, "go"))
    )
    env = {"CONTRACT_EXPERIMENT_KEY": "sdk_contract_ab"}
    assert runner.run_one("go", "cmd", env, 5)[0] == "PASS"


# ---------------------------------------------------------------------------
# Two ids of one person linked with alias (#449)
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_js_and_python_are_the_sdks_whose_smokes_link_two_ids(runner):
    assert runner.ALIAS_SDKS == frozenset({"js", "python"})
    for sdk in sorted(runner.ALIAS_SDKS):
        assert good(runner, sdk)["alias"] == ALIAS
        assert runner.validate(sdk, good(runner, sdk)) == [], sdk


@pytest.mark.regression
@pytest.mark.parametrize(
    ("sdk", "report", "problems"),
    [
        pytest.param(
            "js", None, ["alias missing (js is in ALIAS_SDKS)"], id="listed-but-silent"
        ),
        pytest.param(
            "go",
            ALIAS,
            ["alias reported but go is not in ALIAS_SDKS"],
            id="unlisted-but-reporting",
        ),
        pytest.param(
            "python",
            {**ALIAS, "created": [True, True]},
            [
                "alias.created=[True, True] (expected [true, false]: stored, then "
                "already stored)"
            ],
            id="stored-twice",
        ),
        pytest.param(
            "python",
            {**ALIAS, "created": [1, 0]},
            [
                "alias.created=[1, 0] (expected [true, false]: stored, then already "
                "stored)"
            ],
            id="created-not-booleans",
        ),
        pytest.param(
            "js",
            {**ALIAS, "user_id": "smoke-anon-1"},
            [
                "alias.user_id is alias.anonymous_id (two different ids are to be linked)"
            ],
            id="one-id",
        ),
        pytest.param(
            "js",
            {**ALIAS, "event_name": ""},
            ["alias.event_name='' (expected a non-empty string)"],
            id="no-event",
        ),
        pytest.param(
            "js",
            {**ALIAS, "variant_before": "blue", "variant_after": "blue"},
            ["alias.variant_before='blue' (expected control or treatment)"],
            id="unknown-variant",
        ),
        pytest.param(
            "python",
            {**ALIAS, "variant_after": "treatment"},
            [
                "alias.variant_after='treatment' != alias.variant_before='control' "
                "(the known id is to be answered the anonymous id's variant)"
            ],
            id="known-id-reassigned",
        ),
    ],
)
def test_each_wrong_alias_report_gives_exactly_its_problems(
    runner, sdk, report, problems
):
    """The gate's own planted reports, checked both ways: js and python listed, go not."""
    payload = good(runner, sdk)
    payload.pop("alias", None)
    if report is not None:
        payload["alias"] = report
    assert runner.validate(sdk, payload) == problems


@pytest.mark.regression
def test_the_alias_step_is_judged_by_what_the_database_holds(monkeypatch, runner):
    asked = []

    def rows(anonymous_id, user_id, event_name, experiment_key):
        asked.append((anonymous_id, user_id, event_name, experiment_key))
        return json.loads(json.dumps(ALIAS_STORED))

    monkeypatch.setattr(runner, "_alias_rows", rows)
    monkeypatch.setattr(
        runner,
        "_run_command",
        lambda c, e, t: ("PASS", "0.1s", good(runner, "js", "another_ab")),
    )
    env = {"CONTRACT_EXPERIMENT_KEY": "another_ab"}
    assert runner.run_one("js", "cmd", env, 5) == ("PASS", "control in 0.1s")
    assert asked == [
        ("smoke-anon-1", "smoke-known-1", "contract_alias_1", "another_ab")
    ]


@pytest.mark.regression
@pytest.mark.parametrize(
    ("stored", "problems"),
    [
        pytest.param(
            {**ALIAS_STORED, "assignments": ["smoke-anon-1", "smoke-known-1"]},
            [
                "alias: the assignment rows among the two ids are held by "
                "['smoke-anon-1', 'smoke-known-1'], expected one, held by the "
                "anonymous id"
            ],
            id="the-known-id-got-a-second-row",
        ),
        pytest.param(
            {**ALIAS_STORED, "assignments": []},
            [
                "alias: the assignment rows among the two ids are held by [], "
                "expected one, held by the anonymous id"
            ],
            id="no-row",
        ),
        pytest.param(
            {
                **ALIAS_STORED,
                "events": [
                    {"user_id": "smoke-known-1", "tagged": False, "variant": None}
                ],
            },
            ["alias: 0 tagged events (experiment_id NULL)"],
            id="the-sdk-skipped-the-copy",
        ),
        pytest.param(
            {
                **ALIAS_STORED,
                "events": [
                    {"user_id": "smoke-known-1", "tagged": True, "variant": None}
                ],
            },
            [
                "alias: event variant NULL",
                "alias: the event is stored under the known id, expected under the "
                "anonymous id that holds the assignment",
            ],
            id="the-server-skipped-resolution-at-receipt",
        ),
        pytest.param(
            {
                **ALIAS_STORED,
                "events": [
                    {"user_id": "smoke-anon-1", "tagged": True, "variant": "treatment"}
                ],
            },
            [
                "alias: the event counts for 'treatment', the anonymous id holds "
                "'control'"
            ],
            id="another-variant",
        ),
        pytest.param(
            {**ALIAS_STORED, "events": ALIAS_STORED["events"] * 2},
            ["alias: 2 stored row(s) for the key-less event, expected 1"],
            id="stored-twice",
        ),
        pytest.param(
            {**ALIAS_STORED, "events": []},
            ["alias: 0 stored row(s) for the key-less event, expected 1"],
            id="not-stored",
        ),
    ],
)
def test_each_wrong_stored_alias_state_fails_the_sdk_with_its_problem(
    monkeypatch, runner, stored, problems
):
    monkeypatch.setattr(
        runner, "_alias_rows", lambda *args: json.loads(json.dumps(stored))
    )
    monkeypatch.setattr(
        runner, "_run_command", lambda c, e, t: ("PASS", "0.1s", good(runner, "python"))
    )
    env = {"CONTRACT_EXPERIMENT_KEY": "sdk_contract_ab"}
    assert runner.run_one("python", "cmd", env, 5) == ("FAIL", "; ".join(problems))


@pytest.mark.regression
def test_no_database_fails_an_sdk_that_links_two_ids(monkeypatch, runner):
    def unreachable(*args):
        raise RuntimeError("no database")

    monkeypatch.setattr(runner, "_alias_rows", unreachable)
    monkeypatch.setattr(
        runner, "_run_command", lambda c, e, t: ("PASS", "0.1s", good(runner, "js"))
    )
    env = {"CONTRACT_EXPERIMENT_KEY": "sdk_contract_ab"}
    assert runner.run_one("js", "cmd", env, 5) == (
        "FAIL",
        "cannot read the stored alias rows: no database",
    )


@pytest.mark.regression
def test_an_sdk_that_links_no_ids_is_not_asked_of_the_alias_rows(monkeypatch, runner):
    def never(*args):
        raise AssertionError("the alias rows were read for an unlisted SDK")

    monkeypatch.setattr(runner, "_alias_rows", never)
    monkeypatch.setattr(
        runner, "_run_command", lambda c, e, t: ("PASS", "0.1s", good(runner, "java"))
    )
    env = {"CONTRACT_EXPERIMENT_KEY": "sdk_contract_ab"}
    assert runner.run_one("java", "cmd", env, 5)[0] == "PASS"


@pytest.mark.regression
def test_the_smokes_are_given_the_sdk_ruleset_key_and_a_decoy_under_its_former_name(
    monkeypatch, runner
):
    """The alias step needs a server-side key, so every smoke row gets the seed's
    sdk:ruleset key as CONTROLVAR_LOCAL_API_KEY (and the decoy under the former
    name): a smoke that links ids without being listed then reports it, and fails."""
    monkeypatch.setenv("CONTROLVAR_LOCAL_API_KEY", "scoped")
    monkeypatch.setattr(runner.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(runner, "check_ruleset", lambda *args: ("PASS", "ok"))
    monkeypatch.setattr(runner, "run_local", lambda *args: ("PASS", "ok"))
    monkeypatch.setattr(runner, "run_published_local", lambda *args: ("PASS", "ok"))
    status, ran = run_main(monkeypatch, runner, "--sdk", "js", "--sdk", "go")
    assert status == 0
    assert [command for command, _ in ran] == [
        runner.MANIFEST["js"][0],
        runner.MANIFEST["go"][0],
    ]
    for _, env in ran:
        assert env["CONTROLVAR_LOCAL_API_KEY"] == "scoped"
        assert env["EXPERIMENTLY_LOCAL_API_KEY"] == runner.DECOY_KEY != "scoped"
        assert env["CONTROLVAR_API_KEY"] == "contract-key"


@pytest.mark.regression
def test_every_alias_sdk_runs_in_the_sdk_live_contract_job(runner):
    """CI runs every SDK in ALIAS_SDKS with --strict (the attribute test above
    pins --strict), so an SDK that links ids cannot pass as SKIP."""
    import yaml

    job = yaml.safe_load(PR_QA_GATE.read_text(encoding="utf-8"))["jobs"][
        "sdk-live-contract"
    ]
    assert set(runner.ALIAS_SDKS) <= set(job["with"]["sdks"].split())


def test_js_react_native_java_dotnet_and_android_must_show_their_attributes_reach_flag_evaluation(
    runner,
):
    """#1520, with java (#1718), dotnet (#1719) and android (#2017): the five
    SDKs whose smokes report the targeted flag, and each passes with exactly
    TARGETED."""
    assert runner.ATTRIBUTE_SDKS == frozenset(
        {"js", "react-native", "java", "dotnet", "android"}
    )
    assert runner.TARGETED_FLAG_KEY == "sdk_contract_targeted"
    for sdk in sorted(runner.ATTRIBUTE_SDKS):
        report = good(runner, sdk)
        assert report["targeted"] == TARGETED
        assert runner.validate(sdk, report) == [], sdk


@pytest.mark.parametrize(
    ("sdk", "targeted", "problems"),
    [
        pytest.param(
            "react-native",
            {**TARGETED, "pro": False, "reason": "rollout"},
            [
                "targeted.pro=False (seeded rule plan=pro)",
                "targeted.reason='rollout' (expected 'targeting_rule' for plan=pro)",
            ],
            id="context-dropped",
        ),
        pytest.param(
            "android",
            {**TARGETED, "pro": False, "reason": "rollout"},
            [
                "targeted.pro=False (seeded rule plan=pro)",
                "targeted.reason='rollout' (expected 'targeting_rule' for plan=pro)",
            ],
            id="android-context-dropped",
        ),
        pytest.param(
            "android",
            None,
            ["targeted missing (android is in ATTRIBUTE_SDKS)"],
            id="android-missing",
        ),
        pytest.param(
            "js",
            {**TARGETED, "free": True},
            ["targeted.free=True (seeded rollout 0)"],
            id="free-on",
        ),
        pytest.param(
            "react-native",
            {**TARGETED, "reason": None},
            ["targeted.reason=None (expected 'targeting_rule' for plan=pro)"],
            id="no-reason",
        ),
        pytest.param(
            "react-native",
            {**TARGETED, "pro": 1},
            ["targeted.pro=1 (seeded rule plan=pro)"],
            id="pro-not-a-bool",
        ),
        pytest.param(
            "react-native",
            None,
            ["targeted missing (react-native is in ATTRIBUTE_SDKS)"],
            id="missing",
        ),
        pytest.param(
            "python",
            TARGETED,
            ["targeted reported but python is not in ATTRIBUTE_SDKS"],
            id="unlisted",
        ),
    ],
)
def test_each_wrong_targeted_report_gives_exactly_its_problems(
    runner, sdk, targeted, problems
):
    """The gate's own planted reports. ``context-dropped`` is what an SDK that
    sends no attributes reports: the server answers the rollout, 0%."""
    report = good(runner, sdk)
    report.pop("targeted", None)
    if targeted is not None:
        report["targeted"] = targeted
    assert runner.validate(sdk, report) == problems


def test_a_smoke_that_drops_the_attributes_fails_its_row(monkeypatch, runner, capsys):
    """main() on react-native whose smoke reports what a dropped context gets."""
    dropped = {
        **good(runner, "react-native"),
        "targeted": {"pro": False, "free": False, "reason": "rollout"},
    }
    monkeypatch.setattr(
        runner, "_run_command", lambda command, env, timeout: ("PASS", "0.1s", dropped)
    )
    monkeypatch.setattr(runner.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(sys, "argv", ["run_live_contract.py", "--sdk", "react-native"])
    assert runner.main() == 1
    out = " ".join(capsys.readouterr().out.split())
    assert "FAIL react-native targeted.pro=False (seeded rule plan=pro)" in out


def test_every_attribute_sdk_runs_in_ci_where_a_skip_is_a_failure(
    monkeypatch, runner, capsys
):
    """The SDK Live Contract job names every SDK in ATTRIBUTE_SDKS and runs the
    runner with --strict, under which a missing toolchain fails the row: an
    SDK that cannot run cannot pass as SKIP."""
    import yaml

    job = yaml.safe_load(PR_QA_GATE.read_text(encoding="utf-8"))["jobs"][
        "sdk-live-contract"
    ]
    assert set(runner.ATTRIBUTE_SDKS) <= set(job["with"]["sdks"].split())
    platform = PLATFORM.read_text(encoding="utf-8")
    assert (
        "python tests/sdk-contract/live/run_live_contract.py --strict $args" in platform
    )

    monkeypatch.setattr(runner.shutil, "which", lambda tool: None)
    monkeypatch.setattr(runner, "check_ruleset", lambda *args: ("PASS", "ok"))
    monkeypatch.setenv("CONTROLVAR_LOCAL_API_KEY", "scoped")
    status, ran = run_main(monkeypatch, runner, "--sdk", "react-native", "--strict")
    assert (status, ran) == (1, [])
    out = " ".join(capsys.readouterr().out.split())
    assert "FAIL react-native missing toolchain: node, npx" in out


def test_the_sdks_whose_smokes_report_the_flag_values(runner):
    """#1810: the SDKs whose smokes read the seed's three value flags with their
    typed getters (React: what useFlagValue reads), and each passes with
    exactly VALUES."""
    assert runner.VALUE_SDKS == frozenset(
        {"js", "react", "react-native", "java", "dotnet"}
    )
    for sdk in sorted(runner.VALUE_SDKS):
        report = good(runner, sdk)
        assert report["values"] == VALUES
        assert runner.validate(sdk, report) == [], sdk


def test_the_values_a_smoke_must_report_are_the_seeds_on_values(runner):
    """The runner judges against the seed's own on values (each flag is on at
    100% while the SDK rows run; the published rows, which turn them off, run
    last)."""
    from backend.scripts import seed_sdk_contract as seed

    on_values = {kind: on for kind, (_, on, _) in seed.VALUE_FLAGS.items()}
    assert runner.VALUE_EXPECTED == on_values == VALUES


@pytest.mark.parametrize(
    ("sdk", "values", "problems"),
    [
        pytest.param(
            "js",
            {**VALUES, "string": "off-copy"},
            ["values.string='off-copy' (expected 'on-copy', the seed's on value)"],
            id="off-value",
        ),
        pytest.param(
            "js",
            {**VALUES, "number": -1},
            ["values.number=-1 (expected 10, the seed's on value)"],
            id="the-default",
        ),
        pytest.param(
            "js",
            {**VALUES, "number": "10"},
            ["values.number='10' (expected 10, the seed's on value)"],
            id="number-as-text",
        ),
        pytest.param(
            "js",
            {**VALUES, "number": True},
            ["values.number=True (expected 10, the seed's on value)"],
            id="number-true",
        ),
        pytest.param(
            "js",
            {**VALUES, "json": ["fallback"]},
            [
                "values.json=['fallback'] (expected {'color': 'blue'}, "
                "the seed's on value)"
            ],
            id="json-off-value",
        ),
        pytest.param(
            "js",
            {"string": "on-copy", "number": 10},
            ["values.json=None (expected {'color': 'blue'}, the seed's on value)"],
            id="json-missing",
        ),
        pytest.param("js", {**VALUES, "number": 10.0}, [], id="a-double-is-10"),
        # The Java smoke's getNumberValue returns a double, which it writes as 10.0.
        pytest.param("java", {**VALUES, "number": 10.0}, [], id="java-writes-10.0"),
        pytest.param(
            "java",
            {**VALUES, "string": "smoke-default"},
            ["values.string='smoke-default' (expected 'on-copy', the seed's on value)"],
            id="java-the-default",
        ),
        # The .NET smoke's GetNumberValueAsync returns a double too.
        pytest.param("dotnet", {**VALUES, "number": 10.0}, [], id="dotnet-a-double"),
        pytest.param(
            "dotnet",
            {**VALUES, "json": ["smoke-default"]},
            [
                "values.json=['smoke-default'] (expected {'color': 'blue'}, "
                "the seed's on value)"
            ],
            id="dotnet-the-default",
        ),
        pytest.param(
            "js", None, ["values missing (js is in VALUE_SDKS)"], id="missing"
        ),
        pytest.param(
            "python",
            VALUES,
            ["values reported but python is not in VALUE_SDKS"],
            id="unlisted",
        ),
    ],
)
def test_each_wrong_values_report_gives_exactly_its_problems(
    runner, sdk, values, problems
):
    """The gate's own planted reports. ``off-value`` is what an SDK that reads
    the off value for an on user reports; ``the-default`` one whose getter fell
    back."""
    report = good(runner, sdk)
    report.pop("values", None)
    if values is not None:
        report["values"] = values
    assert runner.validate(sdk, report) == problems


def test_a_smoke_that_reports_the_off_value_fails_its_row(monkeypatch, runner, capsys):
    """main() on js whose smoke reports the string flag's off value."""
    wrong = {**good(runner, "js"), "values": {**VALUES, "string": "off-copy"}}
    monkeypatch.setattr(
        runner, "_run_command", lambda command, env, timeout: ("PASS", "0.1s", wrong)
    )
    monkeypatch.setattr(runner.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(runner, "check_ruleset", lambda *args: ("PASS", "ok"))
    monkeypatch.setattr(runner, "LOCAL_MANIFEST", {})
    monkeypatch.setattr(runner, "PUBLISHED_LOCAL", {})
    monkeypatch.setattr(sys, "argv", ["run_live_contract.py", "--sdk", "js"])
    assert runner.main() == 1
    out = " ".join(capsys.readouterr().out.split())
    assert (
        "FAIL js values.string='off-copy' (expected 'on-copy', the seed's on value)"
        in out
    )


def test_every_value_sdk_runs_in_the_sdk_live_contract_job(runner):
    """CI runs every SDK in VALUE_SDKS with --strict (the attribute test above
    pins --strict), so a value SDK cannot pass as SKIP."""
    import yaml

    job = yaml.safe_load(PR_QA_GATE.read_text(encoding="utf-8"))["jobs"][
        "sdk-live-contract"
    ]
    assert set(runner.VALUE_SDKS) <= set(job["with"]["sdks"].split())


def test_the_seeds_targeted_flag_is_on_only_through_its_plan_rule(runner):
    """The flag the targeted reports are judged against: ACTIVE at 0%, one rule
    plan equals pro at 100%; put back to exactly that when it already exists."""
    from types import SimpleNamespace

    from backend.app.core.targeting_adapter import (
        expand_context,
        match_targeting_rule,
        normalise_targeting_rules,
    )
    from backend.app.models import register_core_models
    from backend.app.models.feature_flag import FeatureFlagStatus
    from backend.scripts import seed_sdk_contract as seed

    register_core_models()
    assert seed.TARGETED_FLAG_KEY == runner.TARGETED_FLAG_KEY
    rules = normalise_targeting_rules(seed.TARGETED_FLAG_RULES)
    matched = match_targeting_rule(rules, expand_context({"plan": "pro"}))
    assert matched is not None and matched.rollout_percentage == 100
    assert match_targeting_rule(rules, expand_context({"plan": "free"})) is None
    assert match_targeting_rule(rules, expand_context({})) is None

    class Db:
        def __init__(self, existing):
            self.existing, self.added = existing, []

        def query(self, model):
            return self

        def filter(self, *criteria):
            return self

        def first(self):
            return self.existing

        def add(self, obj):
            self.added.append(obj)

        def commit(self):
            pass

        def refresh(self, obj):
            pass

    admin = SimpleNamespace(id="00000000-0000-0000-0000-000000000001")
    created = seed.seed_targeted_flag(Db(None), admin)
    edited = SimpleNamespace(
        id="f",
        status=FeatureFlagStatus.INACTIVE,
        rollout_percentage=50,
        targeting_rules={},
    )
    restored = seed.seed_targeted_flag(Db(edited), admin)
    for flag in (created, restored):
        assert flag.status == FeatureFlagStatus.ACTIVE
        assert flag.rollout_percentage == 0
        assert flag.targeting_rules == seed.TARGETED_FLAG_RULES
    assert created.key == "sdk_contract_targeted"


def test_a_run_checks_the_exposure_against_the_experiment_it_gave_the_smoke(
    monkeypatch, runner, capsys
):
    """main() hands validate() the CONTRACT_EXPERIMENT_KEY the smoke was given,
    not the default."""
    monkeypatch.setattr(runner, "EXPOSURE_SDKS", frozenset({"go"}))
    monkeypatch.setenv("CONTRACT_EXPERIMENT_KEY", "another_ab")
    monkeypatch.setattr(runner.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    status, ran = run_main(monkeypatch, runner, "--sdk", "go")
    assert ran[0][1]["CONTRACT_EXPERIMENT_KEY"] == "another_ab"
    assert "PASS go" in " ".join(capsys.readouterr().out.split())
    assert status == 0


def test_the_seed_creates_the_fixtures_the_runner_defaults_to(monkeypatch, runner):
    constants = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in ast.parse(SEED.read_text(encoding="utf-8")).body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in ("EXPERIMENT_KEY", "FLAG_KEY", "TARGETED_FLAG_KEY")
    }
    assert constants == {
        "EXPERIMENT_KEY": "sdk_contract_ab",
        "FLAG_KEY": "sdk_contract_flag",
        "TARGETED_FLAG_KEY": "sdk_contract_targeted",
    }
    monkeypatch.setattr(runner.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    _, ran = run_main(monkeypatch, runner, "--sdk", "go")
    env = ran[0][1]
    assert (
        env["CONTRACT_EXPERIMENT_KEY"],
        env["CONTRACT_FLAG_KEY"],
        env["CONTRACT_TARGETED_FLAG_KEY"],
    ) == (
        constants["EXPERIMENT_KEY"],
        constants["FLAG_KEY"],
        constants["TARGETED_FLAG_KEY"],
    )


def _what_main_read(monkeypatch, runner, tmp_path, env):
    """The URL, key and sdk:ruleset key main() went on to use, given *env*."""
    for name, old, _ in SETTINGS.values():
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(old, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    (tmp_path / "key").write_text("file-key\n")
    (tmp_path / "local").write_text("file-local-key\n")
    monkeypatch.setattr(runner, "KEY_FILE", tmp_path / "key")
    monkeypatch.setattr(runner, "LOCAL_KEY_FILE", tmp_path / "local")
    read = {}

    def healthy(api_url):
        read["url"] = api_url
        return True

    def ruleset(api_url, plain_key, scoped_key, flag_key):
        read["key"], read["local"] = plain_key, scoped_key
        return "PASS", "ok"

    monkeypatch.setattr(runner, "backend_healthy", healthy)
    monkeypatch.setattr(runner, "check_ruleset", ruleset)
    monkeypatch.setattr(runner.shutil, "which", lambda tool: None)
    monkeypatch.setattr(sys, "argv", ["run_live_contract.py", "--sdk", "go"])
    runner.main()
    return read


@pytest.mark.parametrize("setting", sorted(SETTINGS))
def test_each_setting_is_read_under_its_name_then_its_former_one(
    monkeypatch, runner, tmp_path, setting
):
    name, old, fallback = SETTINGS[setting]
    read = lambda env: _what_main_read(monkeypatch, runner, tmp_path, env)[setting]  # noqa: E731
    assert read({name: NEW[setting]}) == NEW[setting]
    assert read({old: OLD[setting]}) == OLD[setting]
    both = read({name: NEW[setting], old: OLD[setting]})
    assert both == NEW[setting] and both != OLD[setting]
    assert read({}) == fallback


def test_the_smokes_are_given_each_setting_under_both_names(monkeypatch, runner):
    """The smokes read the new names first. The runner gives them the real
    values under the new names and decoys under the former ones (an address
    nothing listens on, a key the server refuses), so a smoke that reads a
    former name first fails instead of passing on the same value."""
    monkeypatch.setenv("CONTROLVAR_LOCAL_API_KEY", "scoped")
    monkeypatch.setattr(runner.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(runner, "check_ruleset", lambda *args: ("PASS", "ok"))
    local = []
    monkeypatch.setattr(
        runner,
        "run_local",
        lambda sdk, command, env, timeout, flag: local.append(env) or ("PASS", "ok"),
    )
    monkeypatch.setattr(
        runner, "run_published_local", lambda *args: ("PASS", "not run here")
    )
    _, ran = run_main(monkeypatch, runner, "--sdk", "js")
    env = ran[0][1]
    for setting in ("url", "key"):
        name, old, _ = SETTINGS[setting]
        assert env[name] and env[old] and env[name] != env[old], setting
    assert env["CONTROLVAR_API_KEY"] == "contract-key"
    assert env["EXPERIMENTLY_API_URL"] == runner.DECOY_URL == "http://127.0.0.1:9"
    assert env["EXPERIMENTLY_API_KEY"] == runner.DECOY_KEY != "contract-key"
    name, old, _ = SETTINGS["local"]
    assert local and local[0][name] == "scoped"
    assert local[0][old] == runner.DECOY_KEY != "scoped"


# ---------------------------------------------------------------------------
# A key answers its own flag environment (#2011)
# ---------------------------------------------------------------------------

#: What the server answers, by key: the two flags, production's and staging's.
SERVED = {
    "contract-key": {"sdk_contract_flag": True, "sdk_contract_targeted": False},
    "staging-key": {"sdk_contract_flag": False, "sdk_contract_targeted": True},
}


def _staging(module, monkeypatch, served, ruleset_key="staging-key"):
    """The runner's own ``check_staging`` against a server that serves *served*,
    and the ruleset of *ruleset_key*'s environment."""

    def evaluate(api_url, key, flag_key):
        return 200, {"enabled": served[key][flag_key]}

    def ruleset(api_url, key, etag=""):
        assert key == "staging-local-key"
        flags = [
            {"key": flag, "active": on, **({"rollout_percentage": 100} if on else {})}
            for flag, on in served[ruleset_key].items()
        ]
        return 200, {}, json.dumps({"flags": flags}).encode()

    monkeypatch.setattr(module, "_evaluate_request", evaluate)
    monkeypatch.setattr(module, "_ruleset_request", ruleset)
    return module.check_staging(
        "http://127.0.0.1:9",
        "contract-key",
        "staging-key",
        "staging-local-key",
        "sdk_contract_flag",
        "sdk_contract_targeted",
    )


@pytest.fixture
def staging_runner(monkeypatch):
    """The runner with its own ``check_staging``."""
    spec = importlib.util.spec_from_file_location(
        "run_live_contract_staging_under_test", RUNNER
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_staging_keys_answered_stagings_pass(staging_runner, monkeypatch):
    status, detail = _staging(staging_runner, monkeypatch, SERVED)
    assert status == "PASS", detail


@pytest.mark.regression
def test_staging_keys_answered_productions_fail_the_row(staging_runner, monkeypatch):
    """What a seed that names no environment makes: two more production keys."""
    served = {**SERVED, "staging-key": SERVED["contract-key"]}
    status, detail = _staging(staging_runner, monkeypatch, served)
    assert status == "FAIL"
    assert detail == (
        "evaluate 'sdk_contract_flag' with the staging key: expected off, got on; "
        "evaluate 'sdk_contract_targeted' with the staging key: expected on, got "
        "off; ruleset 'sdk_contract_flag' with the staging key: expected off, got "
        "on; ruleset 'sdk_contract_targeted' with the staging key: expected on, got "
        "off"
    )


def test_a_production_key_answered_stagings_fails_the_row(staging_runner, monkeypatch):
    served = {**SERVED, "contract-key": SERVED["staging-key"]}
    status, detail = _staging(staging_runner, monkeypatch, served)
    assert status == "FAIL"
    assert (
        "evaluate 'sdk_contract_flag' with the production key: expected on, got off"
        in detail
    )


def test_a_ruleset_of_the_other_environment_fails_the_row(staging_runner, monkeypatch):
    status, detail = _staging(
        staging_runner, monkeypatch, SERVED, ruleset_key="contract-key"
    )
    assert status == "FAIL"
    assert detail.startswith(
        "ruleset 'sdk_contract_flag' with the staging key: expected off, got on"
    )


def test_the_row_is_run_with_the_seeds_staging_keys_before_the_sdks(
    monkeypatch, runner, capsys
):
    asked = []
    monkeypatch.setattr(
        runner, "check_staging", lambda *args: asked.append(args) or ("FAIL", "planted")
    )
    monkeypatch.setattr(runner.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    status, ran = run_main(monkeypatch, runner, "--sdk", "go")
    assert asked == [
        (
            "http://localhost:8000",
            "contract-key",
            "staging-key",
            "staging-local-key",
            "sdk_contract_flag",
            "sdk_contract_targeted",
        )
    ]
    assert status == 1
    out = " ".join(capsys.readouterr().out.split())
    assert "FAIL ruleset (staging) planted" in out
    assert out.index("ruleset (staging)") < out.index("PASS go")
    # No SDK row is given a staging key.
    assert not {"staging-key", "staging-local-key"} & {
        value for name, value in ran[0][1].items() if name not in STAGING_KEYS
    }


@pytest.mark.parametrize("missing", sorted(STAGING_KEYS))
def test_a_missing_staging_key_fails_under_strict_and_is_skipped_otherwise(
    monkeypatch, runner, capsys, missing
):
    monkeypatch.delenv(missing)
    monkeypatch.setattr(
        runner, "check_staging", lambda *args: pytest.fail("must not run")
    )
    monkeypatch.setattr(runner.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    assert run_main(monkeypatch, runner, "--sdk", "go")[0] == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "SKIP ruleset (staging) no staging keys: run seed_sdk_contract.py" in out
    assert run_main(monkeypatch, runner, "--sdk", "go", "--strict")[0] == 1
    out = " ".join(capsys.readouterr().out.split())
    assert "FAIL ruleset (staging) no staging keys" in out


def test_the_seed_configures_staging_the_other_way_round_from_production(runner):
    """What the row judges: the flag that is on at 100% in production is off in
    staging, and the one at 0% is on at 100%, so either key answering the other
    environment is seen on both."""
    from backend.scripts import seed_sdk_contract as seed

    assert seed.STAGING_SETTINGS == {
        seed.FLAG_KEY: (False, 0),
        seed.TARGETED_FLAG_KEY: (True, 100),
    }
    for seeded, read in (
        (seed.KEY_FILE, RUNNER.parent / ".api_key"),
        (seed.STAGING_KEY_FILE, RUNNER.parent / ".api_key_staging"),
        (seed.STAGING_LOCAL_KEY_FILE, RUNNER.parent / ".api_key_local_staging"),
    ):
        assert seeded == read


# ---------------------------------------------------------------------------
# The published releases against the live server (#1497, G3)
# ---------------------------------------------------------------------------


def test_the_published_releases_run_last_with_the_scoped_key(monkeypatch, runner):
    monkeypatch.setenv("CONTROLVAR_LOCAL_API_KEY", "scoped")
    monkeypatch.setattr(runner.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(runner, "check_ruleset", lambda *args: ("PASS", "ok"))
    order = []
    monkeypatch.setattr(
        runner,
        "run_local",
        lambda sdk, command, env, timeout, flag: (
            order.append(("local", sdk)) or ("PASS", "ok")
        ),
    )
    monkeypatch.setattr(
        runner,
        "run_published_local",
        lambda sdk, version, api_url, key, timeout, multiway=None: (
            order.append(("published", sdk, version, key, multiway)) or ("PASS", "ok")
        ),
    )
    status, _ = run_main(
        monkeypatch, runner, "--sdk", "python", "--sdk", "js", "--strict"
    )
    assert status == 0
    (first_user, first_value), *_ = runner.multiway_users().items()
    assert order == [
        ("local", "python"),
        ("local", "js"),
        ("published", "python", "0.2.0", "scoped", None),
        ("published", "python", "0.3.0", "scoped", (first_user, first_value)),
        ("published", "js", "0.2.0", "scoped", None),
    ]


def test_a_published_row_fails_under_strict_without_node_or_a_key(
    monkeypatch, runner, capsys
):
    monkeypatch.setattr(runner, "check_ruleset", lambda *args: ("PASS", "ok"))
    monkeypatch.setattr(runner, "run_local", lambda *args: ("PASS", "ok"))
    monkeypatch.setattr(
        runner, "run_published_local", lambda *args: pytest.fail("must not run")
    )
    monkeypatch.setattr(
        runner.shutil, "which", lambda tool: None if tool == "node" else tool
    )
    monkeypatch.setenv("CONTROLVAR_LOCAL_API_KEY", "scoped")
    assert run_main(monkeypatch, runner, "--sdk", "js", "--strict")[0] == 1
    out = " ".join(capsys.readouterr().out.split())
    assert "FAIL js 0.2.0 (published, local) missing toolchain: node" in out
    monkeypatch.delenv("CONTROLVAR_LOCAL_API_KEY")
    assert run_main(monkeypatch, runner, "--sdk", "python", "--strict")[0] == 1
    out = " ".join(capsys.readouterr().out.split())
    assert "FAIL python 0.2.0 (published, local) no sdk:ruleset key" in out


def test_the_published_rows_use_the_pins_the_seeds_flags_and_its_developer(runner):
    from backend.app.core.flag_values import BOOLEAN, VALUE_TYPES
    from backend.app.models.user import UserRole
    from backend.scripts import seed_sdk_contract as seed
    from backend.scripts.seed_demo_data import DEMO_USERS

    pinned = {(a.language, a.version) for a in runner._published_sdks().load_pins()}
    assert runner.PUBLISHED_LOCAL == (
        ("python", "0.2.0", "values"),
        ("js", "0.2.0", "values"),
        ("python", "0.3.0", "multiway"),
    )
    assert {(sdk, version) for sdk, version, _ in runner.PUBLISHED_LOCAL} <= pinned
    # The multi-way row needs the value in each answer, which only the Python
    # harness reports.
    assert {sdk for sdk, _, reads in runner.PUBLISHED_LOCAL if reads == "multiway"} == {
        "python"
    }
    assert runner.MULTIWAY_OFF_VALUE == seed.VALUE_FLAGS["string"][2]
    assert set(seed.VALUE_FLAGS) == set(VALUE_TYPES) - {BOOLEAN}
    assert runner.VALUE_FLAG_KEYS == tuple(
        key for key, _, _ in seed.VALUE_FLAGS.values()
    )
    (developer,) = [u for u in DEMO_USERS if u["email"] == runner.TOGGLE_USER[0]]
    assert developer["password"] == runner.TOGGLE_USER[1]
    assert developer["role"] == UserRole.DEVELOPER and not developer["is_superuser"]


#: A stand-in for a published release's live harness: it prints the "loaded"
#: report, waits for the runner's line, and prints the "after" report.
FAKE_HARNESS = """
import json, sys
reports = json.load(open(sys.argv[1]))
print(json.dumps(reports[0]), flush=True)
sys.stdin.readline()
print(json.dumps(reports[1]), flush=True)
"""


def _fake_published(monkeypatch, runner, tmp_path, loaded, after, version="0.2.0"):
    """``run_published_local`` with everything around it replaced: the pins,
    the download, the API and the harness. Returns the flag changes made."""
    from types import SimpleNamespace

    artefact = SimpleNamespace(
        language="python", version=version, digest="sha256:" + "0" * 64
    )
    monkeypatch.setattr(
        runner,
        "_published_sdks",
        lambda: SimpleNamespace(
            load_pins=lambda: [artefact],
            fetch=lambda a, cache: b"",
            extract=lambda a, data, dest: dest.mkdir(parents=True) or dest,
            check_identity=lambda a, extraction, report: "controlvar/local.py",
        ),
    )
    script = tmp_path / "harness.py"
    script.write_text(FAKE_HARNESS)
    reports = tmp_path / "reports.json"
    reports.write_text(json.dumps([loaded, after]))
    monkeypatch.setattr(
        runner,
        "_published_harness",
        lambda language, root, config: [sys.executable, str(script), str(reports)],
    )
    monkeypatch.setattr(
        runner, "_ruleset_request", lambda url, key: (200, {"etag": '"v1"'}, b"")
    )
    monkeypatch.setattr(runner, "_sign_in", lambda url: "token")
    monkeypatch.setattr(
        runner, "_flag_ids", lambda url, token, keys: {k: f"id-{k}" for k in keys}
    )
    turned = []
    monkeypatch.setattr(
        runner, "_turn", lambda url, token, ids, on: turned.append((sorted(ids), on))
    )
    return turned


def _report(
    phase,
    answer,
    version="v1",
    ready=True,
    keys=(
        "sdk_contract_value_string",
        "sdk_contract_value_number",
        "sdk_contract_value_json",
    ),
):
    return {
        "phase": phase,
        "seconds": 5.0,
        "ready": {"ok": ready},
        "status": {"ready": ready, "version": version},
        "answers": {key: dict(answer) for key in keys},
        "errors": [],
    }


def test_a_published_release_that_receives_the_change_passes(
    monkeypatch, runner, tmp_path
):
    turned = _fake_published(
        monkeypatch,
        runner,
        tmp_path,
        _report("loaded", runner.PUBLISHED_ON),
        _report("after", runner.PUBLISHED_OFF),
    )
    status, detail = runner.run_published_local("python", "0.2.0", "http://x", "k", 30)
    assert status == "PASS", detail
    assert "each answered inactive locally after 5.0s" in detail
    keys = sorted(runner.VALUE_FLAG_KEYS)
    assert turned == [(keys, False), (keys, True)]


def test_an_answer_that_does_not_change_fails_and_the_flags_go_back_on(
    monkeypatch, runner, tmp_path
):
    """The QA plan's G3 failure: the release kept answering on."""
    turned = _fake_published(
        monkeypatch,
        runner,
        tmp_path,
        _report("loaded", runner.PUBLISHED_ON),
        _report("after", runner.PUBLISHED_ON),
    )
    status, detail = runner.run_published_local("python", "0.2.0", "http://x", "k", 30)
    assert status == "FAIL"
    assert "answer unchanged after the flags were turned off" in detail
    assert [on for _, on in turned] == [False, True]


def test_a_release_that_cannot_read_the_ruleset_fails_before_any_change(
    monkeypatch, runner, tmp_path
):
    """A refused document: the release answers from the server, and no flag
    is touched."""
    server = {**runner.PUBLISHED_ON, "source": "server"}
    turned = _fake_published(
        monkeypatch,
        runner,
        tmp_path,
        _report("loaded", server, version=None, ready=False),
        _report("after", server, version=None, ready=False),
    )
    status, detail = runner.run_published_local("python", "0.2.0", "http://x", "k", 30)
    assert status == "FAIL"
    assert "did not load the ruleset the server serves (v1)" in detail
    assert '"source": "server"' in detail
    assert turned == []


# ---------------------------------------------------------------------------
# The seed's multi-way flags (#2012)
# ---------------------------------------------------------------------------

MULTIWAY_KEYS = ("sdk_contract_multiway",)


def test_the_published_multiway_row_is_answered_by_the_server_then_turned_off(
    monkeypatch, runner, tmp_path
):
    """0.3.0 defers the remote entry to the server, which serves the user its
    value of the split; turned off, it answers inactive locally with the off
    value."""
    turned = _fake_published(
        monkeypatch,
        runner,
        tmp_path,
        _report(
            "loaded",
            {**runner.PUBLISHED_SERVER, "value": "green"},
            keys=MULTIWAY_KEYS,
        ),
        _report(
            "after",
            {**runner.PUBLISHED_OFF, "value": "off-copy"},
            keys=MULTIWAY_KEYS,
        ),
        version="0.3.0",
    )
    status, detail = runner.run_published_local(
        "python", "0.3.0", "http://x", "k", 30, multiway=("mw-user-1", "green")
    )
    assert status == "PASS", detail
    assert (
        "sdk_contract_multiway answered by the server for mw-user-1 ('green')" in detail
    )
    assert turned == [
        (["sdk_contract_multiway"], False),
        (["sdk_contract_multiway"], True),
    ]


@pytest.mark.regression
def test_a_release_that_answers_the_multiway_flag_locally_fails(
    monkeypatch, runner, tmp_path
):
    """The ruleset's plant: a local entry for the flag with a split. The release
    answers every user the on value, locally, and the row fails before any
    change."""
    local = {**runner.PUBLISHED_ON, "value": "on-copy"}
    turned = _fake_published(
        monkeypatch,
        runner,
        tmp_path,
        _report("loaded", local, keys=MULTIWAY_KEYS),
        _report("after", local, keys=MULTIWAY_KEYS),
        version="0.3.0",
    )
    status, detail = runner.run_published_local(
        "python", "0.3.0", "http://x", "k", 30, multiway=("mw-user-1", "green")
    )
    assert status == "FAIL"
    assert detail == (
        'before the change it answered sdk_contract_multiway: {"enabled": true, '
        '"reason": "rollout", "source": "local", "value": "on-copy"}; expected each '
        "{'enabled': True, 'reason': 'rollout', 'source': 'server', 'value': 'green'}"
    )
    assert turned == []


def test_a_multiway_answer_the_off_switch_does_not_reach_fails(
    monkeypatch, runner, tmp_path
):
    """The cached server answer kept after the flag is off."""
    server = {**runner.PUBLISHED_SERVER, "value": "green"}
    turned = _fake_published(
        monkeypatch,
        runner,
        tmp_path,
        _report("loaded", server, keys=MULTIWAY_KEYS),
        _report("after", server, keys=MULTIWAY_KEYS),
        version="0.3.0",
    )
    status, detail = runner.run_published_local(
        "python", "0.3.0", "http://x", "k", 30, multiway=("mw-user-1", "green")
    )
    assert status == "FAIL"
    assert "answer unchanged after the flags were turned off" in detail
    assert "'value': 'off-copy'" in detail
    assert [on for _, on in turned] == [False, True]


def test_the_multiway_sdks_are_the_value_sdks(runner):
    """Both ways: an SDK with typed getters reads the multi-way flags too, and
    no SDK without them is asked to."""
    assert runner.MULTIWAY_SDKS == runner.VALUE_SDKS
    assert runner.MULTIWAY_SDKS == frozenset(
        {"js", "react", "react-native", "java", "dotnet"}
    )
    for sdk in sorted(runner.MULTIWAY_SDKS):
        assert runner.validate(sdk, good(runner, sdk)) == [], sdk


def test_the_runner_reads_the_seeds_multiway_flags(runner):
    from backend.scripts import seed_sdk_contract as seed

    assert runner.MULTIWAY_FLAG_KEY == seed.MULTIWAY_FLAG_KEY == "sdk_contract_multiway"
    assert (
        runner.RULE_VALUE_FLAG_KEY
        == seed.RULE_VALUE_FLAG_KEY
        == "sdk_contract_rule_value"
    )
    assert runner.RULE_VALUE == seed.RULE_VALUE == seed.RULE_VALUE_FLAG_RULES["value"]
    assert runner.multiway_users() == seed.multiway_users()


def test_the_seeds_users_get_three_distinct_values_by_the_servers_split(runner):
    """Picked with the server's own function, one user per value of the split:
    never typed by hand."""
    from backend.app.core.flag_values import split_bucket, split_value
    from backend.scripts import seed_sdk_contract as seed

    users = runner.multiway_users()
    assert len(users) == 3
    assert sorted(users.values()) == sorted(e["value"] for e in seed.MULTIWAY_SPLIT)
    for user, value in users.items():
        assert (
            split_value(
                seed.MULTIWAY_SPLIT, split_bucket(user, "sdk_contract_multiway")
            )
            == value
        )
    assert [e["weight"] for e in seed.MULTIWAY_SPLIT] == [34, 33, 33]


@pytest.mark.parametrize(
    ("sdk", "change", "problems"),
    [
        pytest.param(
            "java",
            {"user": "mw-user-1", "value": "smoke-default"},
            [
                "multiway.users['mw-user-1']='smoke-default' (expected 'green', "
                "the split's value for the user)"
            ],
            id="java-the-default",
        ),
        pytest.param(
            "js",
            {"user": "mw-user-0", "value": "on-copy"},
            [
                "multiway.users['mw-user-0']='on-copy' (expected 'red', "
                "the split's value for the user)"
            ],
            id="the-on-value",
        ),
        pytest.param(
            "dotnet",
            {"rule": "on-copy"},
            ["multiway.rule='on-copy' (expected 'pro-copy', the rule's own value)"],
            id="rule-on-value",
        ),
        pytest.param(
            "react",
            {"rule": "smoke-default"},
            [
                "multiway.rule='smoke-default' (expected 'pro-copy', the rule's own "
                "value)"
            ],
            id="rule-the-default",
        ),
        pytest.param(
            "react-native",
            {"users": {}},
            [
                "multiway.users has [], expected the users "
                "['mw-user-0', 'mw-user-1', 'mw-user-2']"
            ],
            id="no-users-read",
        ),
        pytest.param(
            "react-native",
            None,
            ["multiway missing (react-native is in MULTIWAY_SDKS)"],
            id="missing",
        ),
        pytest.param(
            "python",
            {},
            ["multiway reported but python is not in MULTIWAY_SDKS"],
            id="unlisted",
        ),
    ],
)
def test_each_wrong_multiway_report_gives_exactly_its_problems(
    runner, sdk, change, problems
):
    """The gate's own planted reports. ``java-the-default`` is a getter that
    fell back; ``the-on-value`` an SDK, or a server, that serves the on value
    in place of the split's."""
    assert runner.multiway_users() == {
        "mw-user-0": "red",
        "mw-user-1": "green",
        "mw-user-2": "blue",
    }
    report = good(runner, sdk)
    report.pop("multiway", None)
    if change is not None:
        planted = multiway(runner)
        if "user" in change:
            planted["users"][change["user"]] = change["value"]
        planted.update({k: v for k, v in change.items() if k in ("users", "rule")})
        report["multiway"] = planted
    assert runner.validate(sdk, report) == problems


@pytest.mark.regression
def test_a_smoke_that_reads_its_default_for_a_multiway_user_fails_its_row(
    monkeypatch, runner, capsys
):
    """main() on java whose string getter fell back for one user."""
    planted = multiway(runner)
    planted["users"]["mw-user-1"] = "smoke-default"
    wrong = {**good(runner, "java"), "multiway": planted}
    monkeypatch.setattr(
        runner, "_run_command", lambda command, env, timeout: ("PASS", "0.1s", wrong)
    )
    monkeypatch.setattr(runner.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(sys, "argv", ["run_live_contract.py", "--sdk", "java"])
    assert runner.main() == 1
    out = " ".join(capsys.readouterr().out.split())
    assert (
        "FAIL java multiway.users['mw-user-1']='smoke-default' (expected 'green', "
        "the split's value for the user)" in out
    )


def test_the_smokes_are_given_the_seeds_multiway_users(monkeypatch, runner, capsys):
    """CONTRACT_MULTIWAY_USERS, comma-separated, in the seed's order; the
    multiway (server) row runs once, after the staging row and before the
    SDKs, with the plain key and the same users."""
    asked = []
    monkeypatch.setattr(
        runner, "check_multiway", lambda *args: asked.append(args) or ("PASS", "ok")
    )
    monkeypatch.setattr(runner.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    status, ran = run_main(monkeypatch, runner, "--sdk", "js", "--sdk", "go")
    users = runner.multiway_users()
    assert asked == [("http://localhost:8000", "contract-key", users)]
    for _, env in ran:
        assert env["CONTRACT_MULTIWAY_USERS"] == ",".join(users)
        assert env["CONTRACT_MULTIWAY_USERS"] == "mw-user-0,mw-user-1,mw-user-2"
    out = " ".join(capsys.readouterr().out.split())
    assert out.index("ruleset (staging)") < out.index("PASS multiway (server) ok")
    assert out.index("multiway (server)") < out.index("PASS js")


def test_every_multiway_sdk_runs_in_the_sdk_live_contract_job(runner):
    """CI runs every SDK in MULTIWAY_SDKS with --strict (the attribute test
    above pins --strict), so a multi-way SDK cannot pass as SKIP."""
    import yaml

    job = yaml.safe_load(PR_QA_GATE.read_text(encoding="utf-8"))["jobs"][
        "sdk-live-contract"
    ]
    assert set(runner.MULTIWAY_SDKS) <= set(job["with"]["sdks"].split())


#: Each multi-way SDK's smoke, as MANIFEST runs it.
MULTIWAY_SMOKES = {
    "js": "sdk/js/examples/contract_smoke.mjs",
    "react": "sdk/react/examples/contract_smoke.mjs",
    "react-native": "sdk/react-native/examples/contract_smoke.test.ts",
    "java": "sdk/java/core/src/main/java/com/controlvar/sdk/examples/ContractSmoke.java",
    "dotnet": "sdk/dotnet/examples/ContractSmoke/Program.cs",
}


def test_each_multiway_smoke_reads_the_users_and_both_flags(runner):
    """A static floor under the live rows (the .NET one has no toolchain in
    the unit job): each smoke reads the users it is given and both flags, and
    reports them."""
    assert set(MULTIWAY_SMOKES) == set(runner.MULTIWAY_SDKS)
    for sdk, path in MULTIWAY_SMOKES.items():
        source = (REPO_ROOT / path).read_text(encoding="utf-8")
        for needle in (
            "CONTRACT_MULTIWAY_USERS",
            "'sdk_contract_multiway'"
            if path.endswith(("mjs", "ts"))
            else '"sdk_contract_multiway"',
            "sdk_contract_rule_value",
            "multiway",
        ):
            assert needle in source, (sdk, needle)


#: What the server answers in check_multiway's tests: (path, query) -> body.
def _served(runner, value_for, rule=("targeting_rule", "pro-copy"), bulk=True):
    users = runner.multiway_users()

    def get(api_url, key, path, query):
        assert key == "contract-key"
        if path == "/api/v1/feature-flags/evaluate/sdk_contract_multiway":
            return 200, {"enabled": True, "value": value_for(query["user_id"], users)}
        if path == "/api/v1/feature-flags/evaluate/sdk_contract_rule_value":
            assert json.loads(query["context"]) == {"plan": "pro"}
            return 200, {"enabled": True, "reason": rule[0], "value": rule[1]}
        user = path.rsplit("/", 1)[1]
        pro = "context" in query
        answer = {
            "sdk_contract_multiway": True if bulk else "red",
            "sdk_contract_rule_value": pro,
        }
        assert user == runner.RULE_USER if pro else user in users
        return 200, answer

    return get


@pytest.fixture
def multiway_runner(monkeypatch):
    """The runner with its own ``check_multiway``."""
    spec = importlib.util.spec_from_file_location(
        "run_live_contract_multiway_under_test", RUNNER
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_multiway_server_row_passes_on_the_splits_values(
    multiway_runner, monkeypatch
):
    runner = multiway_runner
    monkeypatch.setattr(
        runner, "_api_key_get", _served(runner, lambda user, users: users[user])
    )
    status, detail = runner.check_multiway(
        "http://x", "contract-key", runner.multiway_users()
    )
    assert status == "PASS", detail
    assert detail.startswith("3 users, 3 distinct values (mw-user-0 'red', ")


@pytest.mark.regression
def test_a_server_that_serves_the_on_value_to_all_fails_the_row(
    multiway_runner, monkeypatch
):
    """The plan's plant for the server row."""
    runner = multiway_runner
    monkeypatch.setattr(
        runner, "_api_key_get", _served(runner, lambda user, users: "on-copy")
    )
    status, detail = runner.check_multiway(
        "http://x", "contract-key", runner.multiway_users()
    )
    assert status == "FAIL"
    assert detail.startswith("3 users got 1 distinct value; expected 3; ")
    assert (
        "evaluate 'sdk_contract_multiway' for 'mw-user-1': enabled True, value 'on-copy' (expected 'green')"
        in detail
    )


def test_a_rule_that_serves_the_on_value_fails_the_row(multiway_runner, monkeypatch):
    runner = multiway_runner
    monkeypatch.setattr(
        runner,
        "_api_key_get",
        _served(
            runner, lambda user, users: users[user], rule=("targeting_rule", "on-copy")
        ),
    )
    status, detail = runner.check_multiway(
        "http://x", "contract-key", runner.multiway_users()
    )
    assert status == "FAIL"
    assert detail == (
        "evaluate 'sdk_contract_rule_value' with plan=pro: HTTP 200, reason "
        "'targeting_rule', value 'on-copy' (expected 'pro-copy' by the rule)"
    )


def test_a_bulk_answer_that_is_not_a_boolean_fails_the_row(
    multiway_runner, monkeypatch
):
    """/feature-flags/user/{id} stays boolean."""
    runner = multiway_runner
    monkeypatch.setattr(
        runner,
        "_api_key_get",
        _served(runner, lambda user, users: users[user], bulk=False),
    )
    status, detail = runner.check_multiway(
        "http://x", "contract-key", runner.multiway_users()
    )
    assert status == "FAIL"
    assert (
        "/feature-flags/user/mw-user-0 answered 'sdk_contract_multiway': 'red' "
        "(expected true, a boolean)" in detail
    )


def test_the_seeds_multiway_flags_are_put_back(runner):
    """The split flag on at 100% with its split, the rule value flag at 0% with
    the targeted flag's rule and its own value; an edited one is put back."""
    from types import SimpleNamespace

    from backend.app.models import register_core_models
    from backend.app.models.feature_flag import FeatureFlagStatus
    from backend.scripts import seed_sdk_contract as seed

    register_core_models()

    class Db:
        """The flags *existing* holds, by key; what is added is kept."""

        def __init__(self, existing):
            self.existing, self.key, self.added = existing, None, []

        def query(self, model):
            return self

        def filter(self, criterion):
            self.key = criterion.right.value  # FeatureFlag.key == <key>
            return self

        def first(self):
            return self.existing.get(self.key)

        def add(self, obj):
            self.added.append(obj)

        def commit(self):
            pass

        def refresh(self, obj):
            pass

    admin = SimpleNamespace(id="00000000-0000-0000-0000-000000000001")
    fresh = Db({})
    created = seed.seed_multiway_flags(fresh, admin)
    assert fresh.added == created
    edited = [
        SimpleNamespace(
            key=key,
            id=key,
            status=FeatureFlagStatus.INACTIVE,
            value_type="boolean",
            on_value=None,
            off_value=None,
            rollout_percentage=50,
            targeting_rules={},
            split=[{"value": "x", "weight": 100}],
        )
        for key in (seed.MULTIWAY_FLAG_KEY, seed.RULE_VALUE_FLAG_KEY)
    ]
    kept = Db({flag.key: flag for flag in edited})
    restored = seed.seed_multiway_flags(kept, admin)
    assert restored == edited and kept.added == []
    for split, rule in (created, restored):
        for flag in (split, rule):
            assert flag.status == FeatureFlagStatus.ACTIVE
            assert (flag.value_type, flag.on_value, flag.off_value) == (
                "string",
                "on-copy",
                "off-copy",
            )
        assert split.key == "sdk_contract_multiway"
        assert (split.rollout_percentage, split.targeting_rules) == (100, {})
        assert split.split == seed.MULTIWAY_SPLIT
        assert rule.key == "sdk_contract_rule_value"
        assert (rule.rollout_percentage, rule.split) == (0, None)
        assert rule.targeting_rules == {**seed.TARGETED_FLAG_RULES, "value": "pro-copy"}


# ---------------------------------------------------------------------------
# The React SDK's bootstrap (#1518)
# ---------------------------------------------------------------------------

#: A bootstrap as the live server builds it for the contract seed.
LIVE_BOOTSTRAP = {
    "userId": "bootstrap-user",
    "flags": {
        "sdk_contract_flag": {
            "key": "sdk_contract_flag",
            "enabled": True,
            "value": True,
            "value_type": "boolean",
            "config": None,
            "reason": "rollout",
        }
    },
    "experiments": {
        "sdk_contract_ab": {
            "experiment_key": "sdk_contract_ab",
            "user_id": "bootstrap-user",
            "variant_id": "v-control",
            "variant_name": "control",
            "is_control": True,
            "configuration": None,
            "assigned": True,
            "reason": "assigned",
        }
    },
}
#: The page's example: other keys and values, the live fields.
DOCUMENTED = {
    "userId": "9f1c",
    "flags": {
        "new_search": {
            **LIVE_BOOTSTRAP["flags"]["sdk_contract_flag"],
            "key": "new_search",
        }
    },
    "experiments": {
        "checkout_flow": {
            **LIVE_BOOTSTRAP["experiments"]["sdk_contract_ab"],
            "experiment_key": "checkout_flow",
        }
    },
}


def _server_report(user_id: str = "bootstrap-user") -> dict:
    bootstrap = json.loads(
        json.dumps(LIVE_BOOTSTRAP).replace("bootstrap-user", user_id)
    )
    return {
        "sdk": "react",
        "step": "server",
        "bootstrap": bootstrap,
        "html": "<span>on|control|ready</span>",
        "requests_during_render": 0,
    }


def _problems(runner, counts, server=None, client=None, documented=DOCUMENTED):
    return runner.bootstrap_problems(
        server or _server_report(),
        client or {"sdk": "react", "step": "client", "variant_name": "control"},
        counts,
        "bootstrap-user",
        "sdk_contract_flag",
        "sdk_contract_ab",
        documented,
    )


def test_a_bootstrap_run_passes_with_one_exposure_row_per_step(runner):
    assert _problems(runner, (1, 2)) == []


def test_each_wrong_exposure_count_gives_exactly_its_problem(runner):
    """5i's planted defect, in memory: a server step that assigns twice stores
    two rows; a client step that does not assign stores none."""
    assert _problems(runner, (2, 3)) == [
        "exposure rows after the server step: 2, expected 1"
    ]
    assert _problems(runner, (1, 1)) == [
        "exposure rows added by the client step: 0, expected 1"
    ]
    assert _problems(runner, (0, 1)) == [
        "exposure rows after the server step: 0, expected 1"
    ]


def test_a_client_step_that_adds_two_rows_gives_exactly_its_problem(runner):
    """The count is exact, not a lower bound: a client step that records two
    exposures is as wrong as one that records none (#1653)."""
    assert _problems(runner, (1, 3)) == [
        "exposure rows added by the client step: 2, expected 1"
    ]


def test_a_client_assigned_another_variant_than_the_bootstrap_fails(runner):
    client = {"sdk": "react", "step": "client", "variant_name": "treatment"}
    assert _problems(runner, (1, 2), client=client) == [
        "the client step was assigned 'treatment', the bootstrap 'control'"
        " (assignment is sticky)"
    ]


def test_a_flag_that_is_not_on_in_the_bootstrap_fails(runner):
    server = _server_report()
    server["bootstrap"]["flags"]["sdk_contract_flag"]["enabled"] = False
    (problem,) = _problems(runner, (1, 2), server=server)
    assert problem.startswith("bootstrap.flags['sdk_contract_flag'] is {")
    assert problem.endswith("(seeded flag is 100% on)")
    server["bootstrap"]["flags"] = {}
    # (The page-example comparison also notices the empty flags; only the first
    # problem is this check's.)
    assert _problems(runner, (1, 2), server=server)[0] == (
        "bootstrap.flags['sdk_contract_flag'] is None (seeded flag is 100% on)"
    )


def test_a_page_example_with_other_top_level_keys_than_the_live_bootstrap_fails(
    runner,
):
    extra = {**DOCUMENTED, "extra": 1}
    assert _problems(runner, (1, 2), documented=extra) == [
        "the page's example has the keys ['experiments', 'extra', 'flags', 'userId'],"
        " the live one ['experiments', 'flags', 'userId']"
    ]
    short = {k: v for k, v in DOCUMENTED.items() if k != "experiments"}
    (problem,) = [p for p in _problems(runner, (1, 2), documented=short) if "keys" in p]
    assert problem.startswith("the page's example has the keys ['flags', 'userId']")


def test_a_server_render_that_made_a_request_fails(runner):
    server = {**_server_report(), "requests_during_render": 1}
    assert _problems(runner, (1, 2), server=server) == [
        "the server render made 1 request(s), expected 0"
    ]


def test_the_pages_example_must_have_the_live_fields(runner):
    old_body = json.loads(json.dumps(DOCUMENTED))
    del old_body["flags"]["new_search"]["value_type"]
    problems = _problems(runner, (1, 2), documented=old_body)
    assert len(problems) == 1
    assert problems[0].startswith("the page's example flags entries have the fields")
    assert "regenerate the example" in problems[0]
    (missing,) = _problems(runner, (1, 2), documented=None)
    assert missing.endswith("/react.md has no JSON example of a bootstrap")


def test_the_pages_example_is_the_json_fence_holding_a_bootstrap(runner):
    page = (
        'text\n```json\n{"not": "it"}\n```\nmore\n```json\n'
        + json.dumps(DOCUMENTED, indent=2)
        + "\n```\n"
    )
    assert runner.documented_bootstrap(page) == DOCUMENTED
    assert runner.documented_bootstrap("```json\n{broken\n```\n") is None


def test_react_runs_its_bootstrap_after_its_row_for_one_new_user(
    monkeypatch, runner, tmp_path, capsys
):
    """main() --sdk react: the react smoke, then the bootstrap's server and
    client steps for one user, the rows read after each."""
    page = tmp_path / "react.md"
    page.write_text("```json\n" + json.dumps(DOCUMENTED) + "\n```\n", encoding="utf-8")
    monkeypatch.setattr(runner, "REACT_PAGE", page)
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    ran: List[Tuple[str, Dict[str, str]]] = []
    rows = iter([1, 2])

    def fake_run(command, env, timeout):
        ran.append((command, env))
        if command == runner.BOOTSTRAP_SMOKE:
            step = env["CONTRACT_BOOTSTRAP_STEP"]
            if step == "server":
                return "PASS", "0.1s", _server_report(env["CONTRACT_USER_ID"])
            return (
                "PASS",
                "0.1s",
                {"sdk": "react", "step": "client", "variant_name": "control"},
            )
        return "PASS", "0.1s", good(runner, "react")

    read: List[Tuple[str, str]] = []
    monkeypatch.setattr(runner, "_run_command", fake_run)
    monkeypatch.setattr(
        runner,
        "_exposure_rows",
        lambda user, key: read.append((user, key)) or next(rows),
    )
    monkeypatch.setattr(runner.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(sys, "argv", ["run_live_contract.py", "--sdk", "react"])
    assert runner.main() == 0
    assert [command for command, _ in ran] == [
        runner.MANIFEST["react"][0],
        runner.BOOTSTRAP_SMOKE,
        runner.BOOTSTRAP_SMOKE,
    ]
    steps = [
        (env["CONTRACT_BOOTSTRAP_STEP"], env["CONTRACT_USER_ID"]) for _, env in ran[1:]
    ]
    assert [step for step, _ in steps] == ["server", "client"]
    user = steps[0][1]
    assert user.startswith("bootstrap-") and steps[1][1] == user
    assert read == [(user, "sdk_contract_ab"), (user, "sdk_contract_ab")]
    out = " ".join(capsys.readouterr().out.split())
    assert (
        "PASS react (bootstrap) exposure rows 1 after the server render, +1 after hydration"
        in out
    )
    assert '"value_type": "boolean"' in out
