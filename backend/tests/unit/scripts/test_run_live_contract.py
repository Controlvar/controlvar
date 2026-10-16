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
        return "PASS", "0.1s", {"sdk": sdk, **GOOD}

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
    assert env["EXPERIMENTLY_API_KEY"] == "contract-key"
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
    assert runner.validate("python", {"sdk": "python", **GOOD}) == []
    for change, problem in (
        ({"assign": {**GOOD["assign"], "sticky": False}}, "assignment was not sticky"),
        ({"flag": {"enabled": False}}, "flag.enabled=False (seeded flag is 100% on)"),
        ({"track": {"ok": False}}, "track failed"),
        ({"fanout": {"ok": False}}, "fan-out track failed"),
    ):
        assert runner.validate("python", {"sdk": "python", **GOOD, **change}) == [
            problem
        ]


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
    """The smokes under sdk/ read the names before the rename until they move
    to the new ones: the runner hands them both, equal."""
    monkeypatch.setenv("CONTROLVAR_LOCAL_API_KEY", "scoped")
    monkeypatch.setattr(runner.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(runner, "check_ruleset", lambda *args: ("PASS", "ok"))
    local = []
    monkeypatch.setattr(
        runner,
        "run_local",
        lambda sdk, command, env, timeout, flag: local.append(env) or ("PASS", "ok"),
    )
    _, ran = run_main(monkeypatch, runner, "--sdk", "js")
    env = ran[0][1]
    for setting in ("url", "key"):
        name, old, _ = SETTINGS[setting]
        assert env[name] == env[old] and env[name], setting
    assert env["CONTROLVAR_API_KEY"] == "contract-key"
    name, old, _ = SETTINGS["local"]
    assert local and local[0][name] == local[0][old] == "scoped"
