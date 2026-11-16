"""The page-runtime Playwright project (#1545): where it runs, on what, and what fails it.

The project tests sdk/visual, the visual experiments' browser script, in a real
browser. Three ways it could run nowhere, or run less than it lists, while the
job stays green -- each pinned here:

* A Playwright project runs in PR CI only where a workflow step names it, and
  a step that is deleted or skipped runs nothing; so Browser E2E must pack the
  script, run ``--project=page-runtime`` with a JSON report, and compare that
  report with the project's listing.
* Playwright exits 0 when a test is skipped or passes only on its retry; so
  scripts/check_page_runtime_run.py fails the run unless every listed test
  passed first time (its refusals are exercised below).
* A spec that imports ``test`` from ``@playwright/test`` instead of the launch
  fixture skips the fixture's command-line check, with no error; so every
  ``*.runtime.spec.ts`` must take ``test`` from the launch fixture or from a
  fixture that extends it.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
PR_QA_GATE = WORKFLOWS / "pr-qa-gate.yml"
CONFIG = REPO_ROOT / "frontend" / "playwright.config.ts"
SPEC_DIR = REPO_ROOT / "frontend" / "tests" / "e2e"
LAUNCH_FIXTURE = SPEC_DIR / "fixtures" / "launch.fixture.ts"
CHECK = REPO_ROOT / "scripts" / "check_page_runtime_run.py"

LIST_JSON = '"$RUNNER_TEMP/page-runtime-list.json"'
RUN_JSON = '"$RUNNER_TEMP/page-runtime-run.json"'


def _browser_e2e_steps() -> list[dict]:
    workflow = yaml.safe_load(PR_QA_GATE.read_text(encoding="utf-8"))
    return workflow["jobs"]["browser-e2e"]["steps"]


def _commands(step: dict) -> list[str]:
    """The step's shell lines, stripped, with comment lines and blanks dropped,
    and backslash continuations joined into one line."""
    joined = re.sub(r"[ \t]*\\\n\s*", " ", step.get("run", ""))
    return [
        line.strip()
        for line in joined.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _one_step(steps: list[dict], needle: str, what: str) -> int:
    hits = [
        i for i, step in enumerate(steps) if any(needle in c for c in _commands(step))
    ]
    assert hits, f"no browser-e2e step {what} (no command contains {needle!r})"
    assert len(hits) == 1, f"{len(hits)} browser-e2e steps {what}"
    return hits[0]


def _named(steps: list[dict], name: str) -> int:
    hits = [i for i, step in enumerate(steps) if step.get("name") == name]
    assert len(hits) == 1, f"browser-e2e has {len(hits)} steps named {name!r}"
    return hits[0]


def test_browser_e2e_packs_the_script_after_the_journeys() -> None:
    steps = _browser_e2e_steps()
    pack = _one_step(steps, "npm pack", "packs sdk/visual")
    commands = _commands(steps[pack])
    wanted = [
        "cd sdk/visual",
        "npm ci --no-audit --no-fund",
        "npm run build",
        'npm pack --pack-destination "$out"',
        'tar -xzf "${tarballs[0]}" -C "$out"',
        'test -f "$out/package/dist/controlvar-visual.js"',
        'echo "VISUAL_PACKAGE_DIR=$out/package" >> "$GITHUB_ENV"',
    ]
    positions = [commands.index(w) if w in commands else -1 for w in wanted]
    assert -1 not in positions, (
        f"the pack step lacks {[w for w, p in zip(wanted, positions) if p < 0]}"
    )
    assert positions == sorted(positions), (
        f"the pack step's commands are out of order: {commands}"
    )
    assert pack > _named(steps, "Run the browser journeys")


def test_browser_e2e_runs_the_project_with_a_listing_and_a_json_report() -> None:
    steps = _browser_e2e_steps()
    pack = _one_step(steps, "npm pack", "packs sdk/visual")
    run = _one_step(
        steps,
        "npx playwright test --project=page-runtime --reporter=",
        "runs --project=page-runtime",
    )
    commands = _commands(steps[run])
    assert (
        f"PLAYWRIGHT_JSON_OUTPUT_NAME={LIST_JSON} "
        "npx playwright test --project=page-runtime --list --reporter=json"
    ) in commands, commands
    assert (
        f"PLAYWRIGHT_JSON_OUTPUT_NAME={RUN_JSON} "
        "npx playwright test --project=page-runtime --reporter=list,json"
    ) in commands, commands
    assert commands[0] == "set -euo pipefail", commands
    assert run > pack, "the page-runtime run comes before the script is packed"


def test_browser_e2e_builds_the_framework_fixtures_before_the_run() -> None:
    """The App Router site's static export is built where the specs read it,
    after the packed script and before the run."""
    steps = _browser_e2e_steps()
    pack = _one_step(steps, "npm pack", "packs sdk/visual")
    build = _one_step(
        steps, "node tests/e2e/runtime/fixtures/build.mjs", "builds the fixtures"
    )
    run = _one_step(
        steps,
        "npx playwright test --project=page-runtime --reporter=",
        "runs --project=page-runtime",
    )
    assert _commands(steps[build]) == [
        "cd frontend && node tests/e2e/runtime/fixtures/build.mjs"
    ]
    assert pack < build < run, (pack, build, run)
    script = (SPEC_DIR / "runtime" / "fixtures" / "build.mjs").read_text(
        encoding="utf-8"
    )
    assert 'export const FIXTURES = ["app-router"];' in script
    for name in ("app-router",):
        assert (SPEC_DIR / "runtime" / "fixtures" / name / "next.config.mjs").is_file()


def test_the_counts_are_checked_after_the_run() -> None:
    steps = _browser_e2e_steps()
    run = _one_step(
        steps,
        "npx playwright test --project=page-runtime --reporter=",
        "runs --project=page-runtime",
    )
    check = _one_step(
        steps, "scripts/check_page_runtime_run.py", "checks the page-runtime counts"
    )
    assert _commands(steps[check]) == [
        f"python3 scripts/check_page_runtime_run.py {LIST_JSON} {RUN_JSON} frontend/tests/e2e"
    ]
    assert check > run, "the counts are checked before the run"


def test_no_page_runtime_step_can_be_skipped_or_ignored() -> None:
    steps = _browser_e2e_steps()
    for needle in (
        "npm pack",
        "node tests/e2e/runtime/fixtures/build.mjs",
        "--project=page-runtime",
        "scripts/check_page_runtime_run.py",
    ):
        step = steps[
            [i for i, s in enumerate(steps) if any(needle in c for c in _commands(s))][
                0
            ]
        ]
        assert "if" not in step, f"{step['name']!r} has an if:"
        assert "continue-on-error" not in step, f"{step['name']!r} may fail and pass"


def test_only_browser_e2e_runs_the_project() -> None:
    """playwright.config.ts says Browser E2E runs it and no other step does."""
    found = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "--project=page-runtime" in line:
                found.append((path.name, number))
    steps = _browser_e2e_steps()
    run = _one_step(
        steps,
        "npx playwright test --project=page-runtime --reporter=",
        "runs --project=page-runtime",
    )
    in_job = sum(c.count("--project=page-runtime") for c in _commands(steps[run]))
    assert {name for name, _ in found} == {"pr-qa-gate.yml"}, found
    assert len(found) == in_job == 2, found


def test_the_project_runs_runtime_specs_under_the_launch_override() -> None:
    text = CONFIG.read_text(encoding="utf-8")
    assert 'import { launchOptions } from "./tests/e2e/launch";' in text
    block = re.search(r'name: "page-runtime",\s*\n(?P<body>(?:\s+[^\n]*\n){2})', text)
    assert block, "playwright.config.ts has no page-runtime project"
    assert [line.strip() for line in block["body"].splitlines()] == [
        r"testMatch: /\.runtime\.spec\.ts$/,",
        'use: { ...devices["Desktop Chrome"], launchOptions },',
    ]
    extended = re.search(r'name: "extended",(?P<body>.*?)\n    \},', text, re.S)
    assert extended, "playwright.config.ts has no extended project"
    assert r"/\.runtime\.spec\.ts$/," in extended["body"], (
        "extended does not ignore the runtime specs, so they would run there "
        "too, under the default launch"
    )


# ---------------------------------------------------------------------------
# Every runtime spec takes `test` from the launch fixture
# ---------------------------------------------------------------------------
IMPORT = re.compile(
    r"""^\s*import\s+(?P<clause>[^;]*?)\s+from\s+["'](?P<module>[^"']+)["']""", re.M
)
PLAYWRIGHT = re.compile(r"^@?playwright(?:/test)?(?:/|$)")


def _imports(text: str) -> list[tuple[str, str, str]]:
    """``(module, imported, local)`` for each value binding a file imports.

    A type-only import (``import type …`` or ``type X`` in braces) binds no
    value and is left out. A default import is reported as ``default``, a
    namespace import as ``*``."""
    found = []
    for match in IMPORT.finditer(text):
        clause, module = match["clause"].strip(), match["module"]
        if clause.startswith("type "):
            continue
        braces = re.search(r"\{(?P<names>[^}]*)\}", clause, re.S)
        outside = re.sub(r"\{[^}]*\}", "", clause).strip().strip(",").strip()
        if outside.startswith("*"):
            found.append((module, "*", outside.split()[-1]))
        elif outside:
            found.append((module, "default", outside))
        for name in braces["names"].split(",") if braces else []:
            name = name.strip()
            if not name or name.startswith("type "):
                continue
            imported, _, local = name.partition(" as ")
            found.append((module, imported.strip(), (local or imported).strip()))
    return found


def _resolve(importer: Path, module: str) -> Path | None:
    if not module.startswith("."):
        return None
    target = (importer.parent / module).resolve()
    candidate = target.with_name(target.name + ".ts")
    return candidate if candidate.is_file() else None


def _extends_launch_fixture(path: Path, launch_fixture: Path, depth: int = 0) -> bool:
    """True when ``path`` is the launch fixture, or exports a ``test`` made by
    ``.extend`` on a ``test`` it imports from a file that is one."""
    if path == launch_fixture.resolve():
        return True
    if depth > 3:
        return False
    text = path.read_text(encoding="utf-8")
    for module, imported, local in _imports(text):
        target = _resolve(path, module)
        if (
            imported == "test"
            and target
            and _extends_launch_fixture(target, launch_fixture, depth + 1)
        ):
            if re.search(
                rf"\bexport\s+const\s+test\s*=\s*{re.escape(local)}\s*\.extend\b", text
            ):
                return True
    return False


def runtime_spec_problems(spec_dir: Path, launch_fixture: Path) -> list[str]:
    """Every way a ``*.runtime.spec.ts`` under ``spec_dir`` gets ``test`` from
    somewhere other than ``launch_fixture``; empty when none does."""
    problems = []
    specs = sorted(spec_dir.rglob("*.runtime.spec.ts"))
    if not specs:
        return [f"no *.runtime.spec.ts under {spec_dir}"]
    for spec in specs:
        name = spec.relative_to(spec_dir).as_posix()
        imports = _imports(spec.read_text(encoding="utf-8"))
        for module, imported, _ in imports:
            if PLAYWRIGHT.match(module) and imported in {"test", "default", "*"}:
                problems.append(
                    f"{name} imports test from {module}, which skips the launch fixture's check"
                )
        sources = [
            (module, _resolve(spec, module))
            for module, imported, local in imports
            if imported == "test" and local == "test"
        ]
        if not any(
            target and _extends_launch_fixture(target, launch_fixture)
            for _, target in sources
        ):
            problems.append(
                f"{name} does not import test from the launch fixture or a fixture that "
                f"extends it (it imports test from {[m for m, _ in sources]})"
            )
    return problems


def test_the_sdk_contract_job_runs_the_visual_vector_test() -> None:
    """V7's CI half: SDK Contract Tests runs the script against every vector,
    with nothing that could let a failure through."""
    workflow = yaml.safe_load(PR_QA_GATE.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["sdk-contracts"]["steps"]
    hits = [s for s in steps if "__tests__/validate.test.ts" in s.get("run", "")]
    assert len(hits) == 1, (
        f"sdk-contracts has {len(hits)} steps running the visual vector test"
    )
    step = hits[0]
    assert step.get("working-directory") == "sdk/visual"
    assert _commands(step) == [
        "npm ci --no-audit --no-fund",
        "npx jest --ci __tests__/validate.test.ts",
    ]
    assert "if" not in step and not step.get("continue-on-error")
    assert "validate.test.ts" in {
        p.name for p in (REPO_ROOT / "sdk" / "visual" / "__tests__").iterdir()
    }


def test_every_runtime_spec_imports_test_from_the_launch_fixture() -> None:
    assert LAUNCH_FIXTURE.is_file()
    assert "export const test = base.extend" in LAUNCH_FIXTURE.read_text(
        encoding="utf-8"
    )
    assert runtime_spec_problems(SPEC_DIR, LAUNCH_FIXTURE) == []


def _plant(
    tmp_path: Path, spec_text: str, fixture_text: str | None = None
) -> list[str]:
    """A copy of the e2e fixtures beside one planted spec, checked."""
    root = tmp_path / "e2e"
    (root / "fixtures").mkdir(parents=True)
    (root / "fixtures" / "launch.fixture.ts").write_text(
        LAUNCH_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (root / "runtime").mkdir()
    (root / "runtime" / "runtime.fixture.ts").write_text(
        fixture_text
        if fixture_text is not None
        else (SPEC_DIR / "runtime" / "runtime.fixture.ts").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (root / "runtime" / "planted.runtime.spec.ts").write_text(
        spec_text, encoding="utf-8"
    )
    return runtime_spec_problems(root, root / "fixtures" / "launch.fixture.ts")


@pytest.mark.parametrize(
    ("spec", "fixture", "expected"),
    [
        ('import { expect, test } from "./runtime.fixture";\n', None, []),
        ('import { test } from "../fixtures/launch.fixture";\n', None, []),
        (
            'import { test, expect } from "@playwright/test";\n',
            None,
            [
                "imports test from @playwright/test",
                "does not import test from the launch fixture",
            ],
        ),
        (
            'import { expect } from "./runtime.fixture";\nimport test from "@playwright/test";\n',
            None,
            [
                "imports test from @playwright/test",
                "does not import test from the launch fixture",
            ],
        ),
        (
            'import { test } from "./runtime.fixture";\nimport * as pw from "@playwright/test";\n',
            None,
            ["imports test from @playwright/test"],
        ),
        (
            'import { test } from "./runtime.fixture";\n',
            'import { test } from "@playwright/test";\nexport { test };\n',
            ["does not import test from the launch fixture"],
        ),
    ],
    ids=[
        "through-runtime-fixture",
        "launch-fixture",
        "named",
        "default",
        "namespace",
        "fixture-not-extending",
    ],
)
def test_a_spec_that_skips_the_launch_fixture_is_named(
    tmp_path: Path, spec: str, fixture: str | None, expected: list[str]
) -> None:
    problems = _plant(tmp_path, spec, fixture)
    assert len(problems) == len(expected), problems
    for fragment, problem in zip(expected, problems):
        assert (
            problem.startswith("runtime/planted.runtime.spec.ts ")
            and fragment in problem
        )


def test_a_type_import_from_playwright_is_allowed(tmp_path: Path) -> None:
    spec = (
        'import type { Page } from "@playwright/test";\n'
        'import { type Route, expect } from "@playwright/test";\n'
        'import { test } from "./runtime.fixture";\n'
    )
    assert _plant(tmp_path, spec) == []


# ---------------------------------------------------------------------------
# scripts/check_page_runtime_run.py
# ---------------------------------------------------------------------------
def _listing(files: dict[str, int]) -> dict:
    return {
        "suites": [
            {
                "title": name,
                "file": name,
                "specs": [
                    {
                        "title": f"t{i}",
                        "file": name,
                        "tests": [{"projectName": "page-runtime", "status": "skipped"}],
                    }
                    for i in range(count)
                ],
                "suites": [],
            }
            for name, count in files.items()
        ],
        "errors": [],
    }


def _run_check(
    tmp_path: Path, on_disk: list[str], listing: dict, stats: dict, errors=()
):
    spec_dir = tmp_path / "e2e"
    for name in on_disk:
        (spec_dir / name).parent.mkdir(parents=True, exist_ok=True)
        (spec_dir / name).write_text("// spec\n", encoding="utf-8")
    spec_dir.mkdir(exist_ok=True)
    (tmp_path / "list.json").write_text(json.dumps(listing), encoding="utf-8")
    run = {"suites": [], "errors": list(errors), "stats": stats}
    (tmp_path / "run.json").write_text(json.dumps(run), encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            str(CHECK),
            str(tmp_path / "list.json"),
            str(tmp_path / "run.json"),
            str(spec_dir),
        ],
        capture_output=True,
        text=True,
    )


CLEAN = {"expected": 2, "unexpected": 0, "flaky": 0, "skipped": 0}


def test_the_check_accepts_a_complete_clean_run(tmp_path: Path) -> None:
    result = _run_check(
        tmp_path,
        ["a.runtime.spec.ts", "d/b.runtime.spec.ts"],
        _listing({"a.runtime.spec.ts": 1, "d/b.runtime.spec.ts": 1}),
        CLEAN,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 test(s) listed, each ran and passed first time" in result.stdout


@pytest.mark.parametrize(
    ("on_disk", "files", "stats", "errors", "message"),
    [
        (
            ["a.runtime.spec.ts"],
            {"a.runtime.spec.ts": 2},
            {**CLEAN, "expected": 1, "skipped": 1},
            (),
            "skipped 1 != 0",
        ),
        (
            ["a.runtime.spec.ts"],
            {"a.runtime.spec.ts": 2},
            {**CLEAN, "expected": 1, "flaky": 1},
            (),
            "flaky 1 != 0",
        ),
        (
            ["a.runtime.spec.ts"],
            {"a.runtime.spec.ts": 2},
            {**CLEAN, "expected": 1, "unexpected": 1},
            (),
            "unexpected 1 != 0",
        ),
        (
            ["a.runtime.spec.ts"],
            {"a.runtime.spec.ts": 2},
            {**CLEAN, "expected": 1},
            (),
            "expected 1 != 2",
        ),
        (
            ["a.runtime.spec.ts"],
            {"a.runtime.spec.ts": 2},
            {"expected": 2},
            (),
            "skipped None != 0",
        ),
        (
            ["a.runtime.spec.ts"],
            {"a.runtime.spec.ts": 2},
            CLEAN,
            ({"message": "boom"},),
            "the run reported errors",
        ),
        (
            ["a.runtime.spec.ts"],
            {},
            {**CLEAN, "expected": 0},
            (),
            "the page-runtime listing holds no test",
        ),
        (
            ["a.runtime.spec.ts", "b.runtime.spec.ts"],
            {"a.runtime.spec.ts": 2},
            CLEAN,
            (),
            "not listed ['b.runtime.spec.ts']",
        ),
        (
            ["a.runtime.spec.ts"],
            {"a.runtime.spec.ts": 1, "c.spec.ts": 1},
            CLEAN,
            (),
            "not a runtime spec ['c.spec.ts']",
        ),
    ],
    ids=[
        "skipped",
        "flaky",
        "unexpected",
        "fewer",
        "no-stats",
        "errors",
        "empty",
        "unlisted",
        "foreign",
    ],
)
def test_the_check_refuses(tmp_path, on_disk, files, stats, errors, message) -> None:
    result = _run_check(tmp_path, on_disk, _listing(files), stats, errors)
    assert result.returncode == 1, result.stdout + result.stderr
    assert message in result.stdout, result.stdout


def test_the_check_counts_only_page_runtime_tests(tmp_path: Path) -> None:
    listing = _listing({"a.runtime.spec.ts": 1})
    listing["suites"][0]["specs"][0]["tests"].append({"projectName": "extended"})
    result = _run_check(
        tmp_path, ["a.runtime.spec.ts"], listing, {**CLEAN, "expected": 1}
    )
    assert result.returncode == 0, result.stdout


def test_the_check_refuses_an_unreadable_report(tmp_path: Path) -> None:
    (tmp_path / "list.json").write_text("{", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(CHECK),
            str(tmp_path / "list.json"),
            str(tmp_path / "list.json"),
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "cannot read a Playwright JSON report" in result.stdout
