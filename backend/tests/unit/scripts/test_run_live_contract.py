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


def good(runner, sdk: str, experiment_key: str = "sdk_contract_ab") -> dict:
    """A passing report from *sdk*'s smoke: GOOD, with EXPOSURE only if the
    runner lists *sdk* in EXPOSURE_SDKS (an unlisted SDK may not report one)."""
    report = {"sdk": sdk, **GOOD}
    if sdk in runner.EXPOSURE_SDKS:
        hook = {**EXPOSURE["payload"], "experiment_key": experiment_key}
        report["exposure"] = {**EXPOSURE, "payload": hook}
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
    monkeypatch.setattr(module, "backend_healthy", lambda api_url: True)
    # Every name each setting is read under, so a developer's shell cannot
    # stand in for the value a test sets.
    for name in (
        *(n for names in SETTINGS.values() for n in names[:2]),
        "CONTRACT_EXPERIMENT_KEY",
        "CONTRACT_FLAG_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CONTROLVAR_API_KEY", "contract-key")
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
    report = {"sdk": sdk, **GOOD}
    if exposure is not None:
        report["exposure"] = exposure
    assert runner.validate(sdk, report) == [problem]


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
        and node.targets[0].id in ("EXPERIMENT_KEY", "FLAG_KEY")
    }
    assert constants == {
        "EXPERIMENT_KEY": "sdk_contract_ab",
        "FLAG_KEY": "sdk_contract_flag",
    }
    monkeypatch.setattr(runner.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    _, ran = run_main(monkeypatch, runner, "--sdk", "go")
    env = ran[0][1]
    assert (env["CONTRACT_EXPERIMENT_KEY"], env["CONTRACT_FLAG_KEY"]) == (
        constants["EXPERIMENT_KEY"],
        constants["FLAG_KEY"],
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
        lambda sdk, version, api_url, key, timeout: (
            order.append(("published", sdk, version, key)) or ("PASS", "ok")
        ),
    )
    status, _ = run_main(
        monkeypatch, runner, "--sdk", "python", "--sdk", "js", "--strict"
    )
    assert status == 0
    assert order == [
        ("local", "python"),
        ("local", "js"),
        ("published", "python", "0.2.0", "scoped"),
        ("published", "js", "0.2.0", "scoped"),
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
    assert runner.PUBLISHED_LOCAL == {"python": "0.2.0", "js": "0.2.0"}
    assert set(runner.PUBLISHED_LOCAL.items()) <= pinned
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


def _fake_published(monkeypatch, runner, tmp_path, loaded, after):
    """``run_published_local`` with everything around it replaced: the pins,
    the download, the API and the harness. Returns the flag changes made."""
    from types import SimpleNamespace

    artefact = SimpleNamespace(
        language="python", version="0.2.0", digest="sha256:" + "0" * 64
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


def _report(phase, answer, version="v1", ready=True):
    return {
        "phase": phase,
        "seconds": 5.0,
        "ready": {"ok": ready},
        "status": {"ready": ready, "version": version},
        "answers": {
            key: dict(answer)
            for key in (
                "sdk_contract_value_string",
                "sdk_contract_value_number",
                "sdk_contract_value_json",
            )
        },
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
