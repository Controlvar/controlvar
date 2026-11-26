"""The persona days' job and project, pinned per pull request (no browser).

The days run nightly only, in ``nightly-qa.yml``'s ``persona-days`` job, so
what keeps their output out of the public log is pinned here, where a pull
request meets it:

* the job uploads nothing, sends both Playwright commands' output to files,
  sets ``PERSONAS=1``, and leaves the verdict to ``tests/personas/check_run.py``
  with both exit statuses; the run step is the job's last, runs exactly the
  reviewed commands, and no other step names the files it writes;
* with ``PERSONAS=1`` the config's only reporter is the JSON report to a file,
  the ``personas`` project exists only then, and ``extended`` ignores the
  persona specs whatever the environment;
* every ``test()`` and ``test.step()`` in a persona spec is titled with a
  string literal, so the titles the checker binds to the cards are the ones
  in the source.

Reads two files and the persona specs: no database, no network, no docs/.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

import pytest

from backend.tests.unit.infrastructure.test_docs_only_gate import _load

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
NIGHTLY = REPO_ROOT / ".github" / "workflows" / "nightly-qa.yml"
CONFIG = REPO_ROOT / "frontend" / "playwright.config.ts"
SPECS = REPO_ROOT / "frontend" / "tests" / "e2e" / "personas"
JOB = "persona-days"
RUN_STEP = "Run the persona days"
MAX_MINUTES = 45

CHECK_RUN = [
    "python",
    "tests/personas/check_run.py",
    "--list",
    "$out/list.json",
    "--list-exit",
    "$list_exit",
    "--report",
    "$out/report.json",
    "--run-exit",
    "$run_exit",
    "--oracle-log",
    "$out/oracle-log.jsonl",
    "--details",
    "$out/details.txt",
    "--backend-log",
    "backend.log",
]


def _job() -> dict[str, Any]:
    return _load(NIGHTLY)["jobs"][JOB]


def _run_step() -> dict[str, Any]:
    (step,) = [s for s in _job()["steps"] if s.get("name") == RUN_STEP]
    return step


#: The run step's commands, exactly (whitespace collapsed): anything added to
#: it, a `grep -h` of a file included, is refused until this list says so.
RUN_SCRIPT = [
    "set +e",
    'out="$RUNNER_TEMP/personas"',
    'mkdir -p "$out"',
    "cd frontend || exit 1",
    "npx playwright test --list --project=personas --reporter=json"
    ' > "$out/list.json" 2> "$out/list.log"',
    "list_exit=$?",
    'PERSONAS_REPORT="$out/report.json" PERSONAS_ORACLE_LOG="$out/oracle-log.jsonl"'
    ' npx playwright test --project=personas > "$out/playwright.log" 2>&1',
    "run_exit=$?",
    "cd .. || exit 1",
    " ".join(f'"{t}"' if t.startswith("$") else t for t in CHECK_RUN),
]

#: A step other than the run step that names the files the days write.
_READS_PERSONA_FILES = re.compile(
    r"(RUNNER_TEMP|runner\.temp\s*\}\})/personas|persona-results"
)


def _commands(script: str) -> list[str]:
    """The script's commands, one per line, continuations joined."""
    return [
        " ".join(c.split())
        for c in script.replace("\\\n", " ").splitlines()
        if c.strip()
    ]


def job_problems(job: dict[str, Any]) -> list[str]:
    """Why this job could put a test's title in the public log, or no verdict."""
    problems = []
    timeout = job.get("timeout-minutes")
    if not isinstance(timeout, int) or not 0 < timeout <= MAX_MINUTES:
        problems.append(f"timeout-minutes is {timeout!r}, not 1 to {MAX_MINUTES}")
    for step in job.get("steps", []):
        uses = step.get("uses", "")
        if uses.startswith("actions/upload-artifact"):
            problems.append(f"step {step.get('name', uses)!r} uploads an artefact")
        if step.get("name") != RUN_STEP and _READS_PERSONA_FILES.search(repr(step)):
            problems.append(f"step {step.get('name', uses)!r} reads the persona files")
    steps = [s for s in job.get("steps", []) if s.get("name") == RUN_STEP]
    if len(steps) != 1:
        return [*problems, f"no single step named {RUN_STEP!r}"]
    if (job.get("steps") or [{}])[-1].get("name") != RUN_STEP:
        problems.append("the run step is not the job's last step")
    step = steps[0]
    if (step.get("env") or {}).get("PERSONAS") != "1":
        problems.append("the run step does not set PERSONAS=1")
    commands = _commands(step.get("run", ""))
    playwright = [c for c in commands if "npx playwright" in c]
    if len(playwright) != 2:
        problems.append(f"{len(playwright)} Playwright commands, not 2 (list and run)")
    for command in playwright:
        if not re.search(r'(?<!2)> "\$out/[a-z.-]+"', command):
            problems.append(f"stdout not redirected to a file: {command}")
        if not re.search(r'2>&1|2> "\$out/[a-z.-]+"', command):
            problems.append(f"stderr not redirected to a file: {command}")
    for command in commands:
        if command not in RUN_SCRIPT:
            problems.append(f"a command outside the reviewed script: {command}")
    if commands != RUN_SCRIPT and not any("outside" in p for p in problems):
        problems.append("the run step's commands are not the reviewed script's")
    if not commands or shlex.split(commands[-1]) != CHECK_RUN:
        problems.append(
            "the step does not end with check_run.py over both exit statuses"
        )
    if commands[:1] != ["set +e"]:
        problems.append(
            "the step does not start with `set +e`: a red run skips the checker"
        )
    return problems


def test_the_job_keeps_every_title_off_the_log_and_leaves_the_verdict_to_the_checker():
    problems = job_problems(_job())
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize(
    ("plant", "expected"),
    [
        (
            lambda j: j["steps"].append(
                {"name": "Upload report", "uses": "actions/upload-artifact@v7"}
            ),
            "step 'Upload report' uploads an artefact",
        ),
        (
            lambda j: _plant_run(
                j, lambda r: r.replace(' > "$out/playwright.log" 2>&1', "")
            ),
            "stdout not redirected to a file",
        ),
        (
            lambda j: _plant_run(j, lambda r: r.replace(' 2> "$out/list.log"', "")),
            "stderr not redirected to a file",
        ),
        (
            lambda j: _plant_run(j, lambda r: r + 'cat "$out/details.txt"\n'),
            'a command outside the reviewed script: cat "$out/details.txt"',
        ),
        (
            lambda j: _plant_run(j, lambda r: r + 'grep -h . "$out/playwright.log"\n'),
            'a command outside the reviewed script: grep -h . "$out/playwright.log"',
        ),
        (
            lambda j: j["steps"].append(
                {
                    "name": "Show the run",
                    "if": "failure()",
                    "run": 'cat "$RUNNER_TEMP/personas/playwright.log"',
                }
            ),
            "step 'Show the run' reads the persona files",
        ),
        (
            lambda j: j["steps"].append({"name": "Done", "run": "true"}),
            "the run step is not the job's last step",
        ),
        (
            lambda j: j["steps"][-1].get("env", {}).pop("PERSONAS", None),
            "the run step does not set PERSONAS=1",
        ),
        (lambda j: j.update({"timeout-minutes": 60}), "timeout-minutes is 60"),
        (lambda j: _plant_run(j, lambda r: r.replace("set +e\n", "")), "set +e"),
    ],
)
def test_each_way_a_title_could_reach_the_log_is_refused(plant, expected):
    """The real job with one thing broken: refused, naming that thing."""
    job = _load(NIGHTLY)["jobs"][JOB]
    job["steps"] = [s for s in job["steps"] if s.get("name") != RUN_STEP] + [
        _run_step()
    ]
    plant(job)
    problems = job_problems(job)
    assert any(expected in p for p in problems), problems


def _plant_run(job: dict[str, Any], change) -> None:
    step = job["steps"][-1]
    step["run"] = change(step["run"])


def test_the_job_carries_what_every_day_needs():
    """The SDK app's day needs this checkout's JavaScript SDK built and a port
    for its page; every day needs the API and the dashboard where the
    Playwright config looks for them. Later days add no workflow change."""
    job = _job()
    names = [s.get("name") for s in job["steps"]]
    assert "Build the JavaScript SDK" in names
    sdk = next(s for s in job["steps"] if s.get("name") == "Build the JavaScript SDK")
    assert "cd sdk/js && npm ci" in sdk["run"] and "npm run build" in sdk["run"]
    env = job["env"]
    assert env["PLAYWRIGHT_API_URL"] == "http://localhost:8000"
    assert env["PLAYWRIGHT_PORT"] == "3100"
    assert env["PERSONAS_SDK_PORT"].isdigit()
    assert env["PERSONAS_SDK_PORT"] not in {"3100", "8000"}
    assert not job.get("needs"), "the days wait for no other job"


# ---------------------------------------------------------- playwright.config.ts


#: The personas project, spread into `projects` only when PERSONAS=1.
_ONLY_ON_REQUEST = re.compile(
    r'\n    \.\.\.\(PERSONAS\n      \? \[\n          \{\n            name: "personas",\n'
    r"            testMatch: /\\.persona\\.spec\\.ts\$/,\n"
)


def config_problems(text: str) -> list[str]:
    problems = []
    if '? [["json", { outputFile: PERSONAS_REPORT }]]' not in text:
        problems.append(
            "PERSONAS=1's reporter is not exactly one JSON report to a file"
        )
    if text.count('name: "personas"') != 1 or not _ONLY_ON_REQUEST.search(text):
        problems.append("the personas project is not present only under PERSONAS=1")
    ignore = re.search(r'name: "extended",.*?testIgnore: \[(.*?)\]', text, re.S)
    if not ignore or r"/\.persona\.spec\.ts$/," not in ignore.group(1):
        problems.append("extended does not ignore the persona specs")
    return problems


def test_the_config_lists_personas_only_on_request_and_reports_to_a_file_only():
    problems = config_problems(CONFIG.read_text("utf-8"))
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        (
            '? [["json", { outputFile: PERSONAS_REPORT }]]',
            '? [["json", { outputFile: PERSONAS_REPORT }], ["list"]]',
            "one JSON report to a file",
        ),
        (r"        /\.persona\.spec\.ts$/," "\n", "", "extended does not ignore"),
        (
            "    ...(PERSONAS\n",
            "    ...(true\n",
            "only under",
        ),
    ],
)
def test_each_config_change_that_would_print_or_list_the_days_is_refused(
    old, new, expected
):
    text = CONFIG.read_text("utf-8")
    assert old in text
    problems = config_problems(text.replace(old, new))
    assert any(expected in p for p in problems), problems


# ------------------------------------------------------------- literal titles

_CALL = re.compile(
    r"(?<![\w.$])test(?P<kind>\.(?:step|describe(?:\.serial)?|only|skip|fixme|fail))?\s*\("
)
_LITERAL = re.compile(r"""\s*(?:"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')\s*,""")


def title_problems(text: str, name: str) -> list[str]:
    """Each test() or test.step() whose title is not one string literal."""
    problems = []
    for call in _CALL.finditer(text):
        if not _LITERAL.match(text, call.end()):
            line = text.count("\n", 0, call.start()) + 1
            what = f"test{call.group('kind') or ''}"
            problems.append(f"{name}:{line}: {what} title is not a string literal")
    return problems


def _specs() -> list[Path]:
    specs = sorted(SPECS.glob("*.persona.spec.ts"))
    assert specs, "no persona spec: the scan would pass on nothing"
    return specs


def test_every_persona_spec_titles_its_tests_and_steps_with_literals():
    problems = []
    for spec in _specs():
        problems += title_problems(spec.read_text("utf-8"), spec.name)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("test.step(`U-DEV day ${n}: start`, async () => {});", "test.step title"),
        ("test.step(title, async () => {});", "test.step title"),
        ('test.step("U-DEV day " + n, async () => {});', "test.step title"),
        ("test(`U-DEV: the day`, async () => {});", "test title"),
        ("test.describe(name, () => {});", "test.describe title"),
    ],
)
def test_a_title_that_is_not_a_literal_is_refused(source, expected):
    assert title_problems(f"\n{source}\n", "x.persona.spec.ts") == [
        f"x.persona.spec.ts:2: {expected} is not a string literal"
    ]


def test_a_literal_title_and_other_test_methods_are_accepted():
    source = (
        'test("U-DEV: the day", async () => {\n'
        "  await test.step('U-DEV day 1: it', async () => {});\n"
        "});\n"
        "test.extend({});\ntest.setTimeout(1);\nconst x = mytest(a);\n"
    )
    assert title_problems(source, "x") == []


# ------------------------------------------------------------------ sign-ins

ORACLES = REPO_ROOT / "frontend" / "tests" / "e2e" / "oracles"
PACER = "signin.ts"
#: A way to sign in other than oracles/signin.ts: the role fixtures (their
#: pages sign in on their own and wait out a 429), the login page object, or
#: the login path, spelled out or imported. A URL built some other way is
#: still caught at run time: check_run.py holds the API's own count of
#: sign-ins (backend.log) to the ones signin.ts logged.
_UNPACED_SIGN_IN = re.compile(
    r"auth\.fixture|\b(?:adminPage|developerPage|analystPage|viewerPage)\b"
    r"|\b(?:loginAsRole|loginAs|LoginPage|submitResilient|LOGIN_PATH)\b|auth/login"
)


def sign_in_problems(text: str, name: str) -> list[str]:
    return [
        f"{name}:{text.count(chr(10), 0, m.start()) + 1}: signs in without "
        f"oracles/{PACER} (unpaced; a 429 must fail, not be waited out)"
        for m in _UNPACED_SIGN_IN.finditer(text)
    ]


def test_the_days_sign_in_only_through_the_pacer():
    files = sorted(SPECS.rglob("*.ts")) + sorted(
        p for p in ORACLES.glob("*.ts") if p.name != PACER
    )
    assert files and (ORACLES / PACER).is_file()
    problems = []
    for path in files:
        problems += sign_in_problems(path.read_text("utf-8"), path.name)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize(
    "source",
    [
        "await login.submitResilient(email, password);",
        'await loginAs(page, "analyst");',
        "const login = new LoginPage(page);",
        "await request.post(`${API_URL}/api/v1/auth/login`, { data });",
        'await page.request.fetch("/api/v1/auth/login", { method: "POST" });',
        'import { test } from "../fixtures/auth.fixture";',
        'test("U-DEV: a day", async ({ analystPage }) => {});',
        "await page.request.post(`${API_URL}${LOGIN_PATH}`, { data });",
        'await page.evaluate(() => fetch("/api/v1/auth/login", { method: "POST" }));',
    ],
)
def test_a_sign_in_around_the_pacer_is_refused(source):
    assert sign_in_problems(f"\n{source}\n", "x.persona.spec.ts") == [
        "x.persona.spec.ts:2: signs in without oracles/signin.ts "
        "(unpaced; a 429 must fail, not be waited out)"
    ]


def test_an_allow_list_names_the_sign_in_by_the_pacer_s_constant():
    assert sign_in_problems('[SIGN_IN_REQUEST, "signing in"],', "x") == []
    assert sign_in_problems('["POST /api/v1/auth/login", "signing in"],', "x")
