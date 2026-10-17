"""The PR-time credential scans print only path, rule, severity and line (#772).

``security-scan.yml`` writes each credential scan's trivy JSON report to a file
under ``$RUNNER_TEMP`` and hands it to ``scripts/render_secret_findings.py
--stage ci``, the way ``release.yml`` does. These tests

* pin the renderer's release wording byte for byte, as it printed before
  ``--stage`` existed, and pin the ci wording the same way;
* run each scan step's real ``run:`` script under bash, in a working directory
  the test builds itself, with a fake ``$TRIVY`` whose report carries a value
  planted at run time in every field, and a ``trivy`` first on PATH that
  fails, so a step that calls the unpinned binary cannot pass.

They call no git, docker or trivy, so they run in the unit job and in
``scripts/core_build.sh``'s copy, which has no ``.git`` and no ``modules/``.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "security-scan.yml"
RENDER = REPO_ROOT / "scripts" / "render_secret_findings.py"

#: Generated per run, so nothing in the repository can match it by accident.
PLANTED = "planted" + secrets.token_hex(12)

TRIVY_VERSION = "0.74.0"

IMAGES = [
    "experimently-api:scan-core",
    "experimently-api:scan-full",
    "experimently-web:scan-core",
    "experimently-web:scan-full",
]

BACKEND_STEP = "Scan the backend tree for credentials"
MODULES_STEP = "Scan the module tree for credentials"
MODULES_VULN_STEP = "Scan the module requirements for known advisories"
IMAGE_STEP = "Scan the built images for credentials"
IMAGE_VULN_STEP = "Scan the built images for known advisories"

#: The row every planted finding renders to.
ROW = "app/zz_plant.py\tgithub-pat\tCRITICAL\t2"
CVE = "CVE-2099-0001"


# -- the renderer's wording, byte for byte ---------------------------------------

RELEASE_LABEL = "controlvar:core-1.2.3 (whole image)"
CI_LABEL = "backend/ (source tree)"

#: Captured from scripts/render_secret_findings.py before ``--stage`` existed.
RELEASE_FINDINGS_STDOUT = (
    "2 finding(s) in controlvar:core-1.2.3 (whole image) (trivy 0.74.0, secret scanner):\n"
    "Target\tRuleID\tSeverity\tStartLine\n"
    "app/backend/x_test.py\tgithub-pat\tCRITICAL\t3\n"
    "usr/share/a|b.js\tgithub-pat\tCRITICAL\t7\n"
    "::error title=Credential found in image::controlvar:core-1.2.3 (whole image) "
    "was not pushed: 2 finding(s). See the job summary.\n"
)
RELEASE_FINDINGS_SUMMARY = (
    "**Credential scan: 2 finding(s) in controlvar:core-1.2.3 (whole image)** "
    "(trivy 0.74.0, secret scanner). Not pushed.\n"
    "\n"
    "| Target | RuleID | Severity | StartLine |\n"
    "|---|---|---|---|\n"
    "| app/backend/x_test.py | github-pat | CRITICAL | 3 |\n"
    "| usr/share/a\\|b.js | github-pat | CRITICAL | 7 |\n"
    "\n"
)
RELEASE_CLEAN_STDOUT = (
    "Credential scan: no findings in controlvar:core-1.2.3 (whole image) "
    "(trivy 0.74.0, secret scanner), scanned before push.\n"
)
RELEASE_CLEAN_SUMMARY = RELEASE_CLEAN_STDOUT + "\n"

CI_FINDINGS_STDOUT = (
    "2 finding(s) in backend/ (source tree) (trivy 0.74.0, secret scanner):\n"
    "Target\tRuleID\tSeverity\tStartLine\n"
    "app/backend/x_test.py\tgithub-pat\tCRITICAL\t3\n"
    "usr/share/a|b.js\tgithub-pat\tCRITICAL\t7\n"
    "::error title=Credential pattern found::backend/ (source tree): 2 finding(s). "
    "The job summary lists path, rule, severity and line; the matched text is not "
    "printed.\n"
)
CI_FINDINGS_SUMMARY = (
    "**Credential scan: 2 finding(s) in backend/ (source tree)** "
    "(trivy 0.74.0, secret scanner). Remove the value from the file at that line.\n"
    "\n"
    "| Target | RuleID | Severity | StartLine |\n"
    "|---|---|---|---|\n"
    "| app/backend/x_test.py | github-pat | CRITICAL | 3 |\n"
    "| usr/share/a\\|b.js | github-pat | CRITICAL | 7 |\n"
    "\n"
)
CI_CLEAN_STDOUT = (
    "Credential scan: no findings in backend/ (source tree) "
    "(trivy 0.74.0, secret scanner).\n"
)
CI_CLEAN_SUMMARY = CI_CLEAN_STDOUT + "\n"


def _report(results: Optional[list]) -> dict:
    data: Dict[str, Any] = {
        "SchemaVersion": 2,
        "Trivy": {"Version": TRIVY_VERSION},
        "ArtifactName": PLANTED,
        "ArtifactType": "filesystem",
        "Metadata": {"ImageConfig": {"config": {"Env": [f"TOKEN={PLANTED}"]}}},
    }
    if results is not None:
        data["Results"] = results
    return data


def _secret(line: int) -> dict:
    return {
        "RuleID": "github-pat",
        "Category": "GitHub",
        "Severity": "CRITICAL",
        "Title": f"title {PLANTED}",
        "StartLine": line,
        "EndLine": line,
        "Code": {
            "Lines": [
                {"Number": line - 1, "Content": f"note = '{PLANTED}'"},
                {"Number": line, "Content": f"token = '{PLANTED}'"},
            ]
        },
        "Match": f"token = '{PLANTED}'",
    }


GOLDEN_FINDINGS = _report(
    [
        {"Target": "app/backend/x_test.py", "Class": "secret", "Secrets": [_secret(3)]},
        {"Target": "usr/share/a|b.js", "Class": "secret", "Secrets": [_secret(7)]},
    ]
)
GOLDEN_CLEAN = _report([])


def _render(
    tmp_path: Path, payload: dict, label: str, stage: List[str], script: Path = RENDER
) -> tuple:
    report = tmp_path / "report.json"
    report.write_text(json.dumps(payload), encoding="utf-8")
    summary = tmp_path / "summary.md"
    summary.unlink(missing_ok=True)
    result = subprocess.run(
        [sys.executable, str(script), str(report), "--label", label, *stage],
        capture_output=True,
        text=True,
        check=False,
        env={"GITHUB_STEP_SUMMARY": str(summary), "PATH": "/usr/bin:/bin"},
    )
    return result, summary.read_text(encoding="utf-8") if summary.exists() else ""


class TestTheRendererWording:
    @pytest.mark.parametrize(
        "stage", [[], ["--stage", "release"]], ids=["default", "release"]
    )
    def test_the_release_wording_is_unchanged(self, tmp_path, stage):
        result, summary = _render(tmp_path, GOLDEN_FINDINGS, RELEASE_LABEL, stage)
        assert result.returncode == 1
        assert (result.stdout, result.stderr, summary) == (
            RELEASE_FINDINGS_STDOUT,
            "",
            RELEASE_FINDINGS_SUMMARY,
        )
        result, summary = _render(tmp_path, GOLDEN_CLEAN, RELEASE_LABEL, stage)
        assert result.returncode == 0
        assert (result.stdout, result.stderr, summary) == (
            RELEASE_CLEAN_STDOUT,
            "",
            RELEASE_CLEAN_SUMMARY,
        )

    def test_the_ci_wording(self, tmp_path):
        result, summary = _render(
            tmp_path, GOLDEN_FINDINGS, CI_LABEL, ["--stage", "ci"]
        )
        assert result.returncode == 1
        assert (result.stdout, result.stderr, summary) == (
            CI_FINDINGS_STDOUT,
            "",
            CI_FINDINGS_SUMMARY,
        )
        result, summary = _render(tmp_path, GOLDEN_CLEAN, CI_LABEL, ["--stage", "ci"])
        assert result.returncode == 0
        assert (result.stdout, result.stderr, summary) == (
            CI_CLEAN_STDOUT,
            "",
            CI_CLEAN_SUMMARY,
        )

    def test_the_ci_wording_names_no_image_and_no_push(self):
        """The ci wording also runs on source trees, pushes to main and the
        weekly schedule, where nothing is an image and nothing is pushed."""
        for text in (
            CI_FINDINGS_STDOUT,
            CI_FINDINGS_SUMMARY,
            CI_CLEAN_STDOUT,
            CI_CLEAN_SUMMARY,
        ):
            assert not re.search(r"image|push", text, flags=re.I), text

    def test_an_unknown_stage_is_refused(self, tmp_path):
        result, _ = _render(tmp_path, GOLDEN_CLEAN, CI_LABEL, ["--stage", "pr"])
        assert result.returncode == 2

    def test_an_edited_release_wording_fails_the_comparison(self, tmp_path):
        """A positive control for the golden text: one changed word shows."""
        edited = tmp_path / "render_secret_findings.py"
        source = RENDER.read_text(encoding="utf-8")
        assert source.count("Not pushed.") == 1
        edited.write_text(
            source.replace("Not pushed.", "Not published."), encoding="utf-8"
        )
        _, summary = _render(
            tmp_path, GOLDEN_FINDINGS, RELEASE_LABEL, [], script=edited
        )
        assert summary != RELEASE_FINDINGS_SUMMARY


# -- the scan steps, run against a fake trivy ------------------------------------

#: The fake trivy. A scenario keyed by scan target says what it exits with and
#: what it reports: "secret" (one finding, the planted value in every field),
#: "vuln" (one advisory), "clean", or "none" (no report written at all). A
#: table is printed as the whole result, every field included.
FAKE_TRIVY = """\
#!{python}
import json, os, sys
from pathlib import Path

state = Path(os.environ["FAKE_STATE"])
argv = sys.argv[1:]
with open(state / "calls.log", "a", encoding="utf-8") as log:
    log.write(json.dumps(argv) + "\\n")
scenario = json.loads((state / "scenario.json").read_text())
case = scenario.get(argv[-1], {{"exit": 0, "report": "clean"}})


def flag(name):
    return argv[argv.index(name) + 1] if name in argv else None


scanners = (flag("--scanners") or "vuln,secret").split(",")
planted = os.environ["FAKE_PLANTED"]
results = []
if case["report"] == "secret" and "secret" in scanners:
    results.append({{
        "Target": "app/zz_plant.py", "Class": "secret",
        "Secrets": [{{
            "RuleID": "github-pat", "Category": "GitHub", "Severity": "CRITICAL",
            "Title": "GitHub Personal Access Token", "StartLine": 2, "EndLine": 2,
            "Code": {{"Lines": [
                {{"Number": 1, "Content": "note = '" + planted + "'"}},
                {{"Number": 2, "Content": "token = '" + planted + "'"}},
                {{"Number": 3, "Content": "other = '" + planted + "'"}},
            ]}},
            "Match": "token = '" + planted + "'",
        }}],
    }})
if case["report"] == "vuln" and "vuln" in scanners:
    results.append({{
        "Target": "requirements.txt", "Class": "lang-pkgs", "Type": "pip",
        "Vulnerabilities": [{{
            "VulnerabilityID": "{cve}", "PkgName": "examplepkg",
            "InstalledVersion": "1.0.0", "FixedVersion": "1.0.1",
            "Severity": "HIGH",
        }}],
    }})
report = {{
    "SchemaVersion": 2, "Trivy": {{"Version": "{version}"}},
    "ArtifactName": argv[-1], "Results": results,
}}
if (flag("--format") or "table") == "json":
    if case["report"] != "none":
        Path(flag("--output")).write_text(json.dumps(report))
else:
    print(json.dumps(results))
sys.exit(case["exit"])
"""

#: First on PATH: a step that runs the bare word gets this, not a scanner.
TRIVY_SHIM = """\
#!/bin/sh
printf 'unpinned trivy called\\n' >&2
exit 97
"""


class Run(NamedTuple):
    code: int
    out: str
    summary: str
    calls: List[List[str]]

    @property
    def everything(self) -> str:
        return self.out + self.summary


def _steps() -> Dict[str, str]:
    job = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"][
        "container-security"
    ]
    return {
        s["name"]: str(s["run"]) for s in job["steps"] if "run" in s and "name" in s
    }


class Harness:
    """A workflow step's ``run:`` script, run the way the runner runs it."""

    def __init__(self, tmp_path: Path, modules: bool = True):
        self.root = tmp_path
        self.work = tmp_path / "work"
        (self.work / "backend").mkdir(parents=True)
        (self.work / "scripts").mkdir()
        shutil.copy(RENDER, self.work / "scripts" / RENDER.name)
        if modules:
            (self.work / "modules").mkdir()
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        (self.bin / "trivy").write_text(TRIVY_SHIM)
        (self.bin / "trivy").chmod(0o755)
        (self.bin / "python3").symlink_to(sys.executable)
        self.fake = tmp_path / "fake-trivy"
        self.fake.write_text(
            FAKE_TRIVY.format(python=sys.executable, cve=CVE, version=TRIVY_VERSION)
        )
        self.fake.chmod(0o755)
        self.state = tmp_path / "state"
        self.state.mkdir()
        self.temp = tmp_path / "runner-temp"
        self.temp.mkdir()
        self.summary = tmp_path / "summary.md"

    def run(self, script: str, scenario: Dict[str, Dict[str, Any]]) -> Run:
        (self.state / "scenario.json").write_text(json.dumps(scenario))
        (self.state / "calls.log").write_text("")
        self.summary.write_text("")
        for stale in self.temp.iterdir():
            stale.unlink()
        result = subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
            cwd=self.work,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
            env={
                "PATH": f"{self.bin}:/usr/bin:/bin",
                "HOME": str(self.root),
                "TRIVY": str(self.fake),
                "RUNNER_TEMP": str(self.temp),
                "GITHUB_WORKSPACE": str(self.work),
                "GITHUB_STEP_SUMMARY": str(self.summary),
                "FAKE_STATE": str(self.state),
                "FAKE_PLANTED": PLANTED,
            },
        )
        calls = [
            json.loads(line)
            for line in (self.state / "calls.log").read_text().splitlines()
        ]
        return Run(
            result.returncode,
            result.stdout + result.stderr,
            self.summary.read_text(),
            calls,
        )


def _common(run: Run, what: str) -> List[str]:
    problems = []
    if PLANTED in run.everything:
        problems.append(f"{what}: the planted value reached the log or the summary")
    if "unpinned trivy called" in run.out:
        problems.append(f"{what}: a bare trivy ran instead of $TRIVY")
    return problems


def check_single(harness: Harness, script: str, target: str, label: str) -> List[str]:
    """One credential scan of one tree: path, rule, severity and line only;
    trivy's status first, then the renderer's; a missing report fails."""
    problems = []
    cases = [
        ("findings", {"exit": 1, "report": "secret"}, 1),
        ("trivy failed on a clean report", {"exit": 2, "report": "clean"}, 2),
        ("findings, trivy exited 0", {"exit": 0, "report": "secret"}, 1),
        ("no report, trivy exited 0", {"exit": 0, "report": "none"}, 2),
        ("clean", {"exit": 0, "report": "clean"}, 0),
    ]
    for what, case, expected in cases:
        run = harness.run(script, {target: case})
        problems += _common(run, what)
        if run.code != expected:
            problems.append(f"{what}: exit {run.code}, expected {expected}")
        if len(run.calls) != 1:
            problems.append(f"{what}: {len(run.calls)} trivy calls, expected 1")
        if case["report"] == "secret":
            if ROW not in run.out or label not in run.out:
                problems.append(f"{what}: the row or the label is not printed")
            if "| app/zz_plant.py | github-pat | CRITICAL | 2 |" not in run.summary:
                problems.append(f"{what}: the row is not in the job summary")
        if what == "clean" and f"no findings in {label}" not in run.out:
            problems.append(f"{what}: no clean line for {label}")
    return problems


def check_image_loop(harness: Harness, script: str) -> List[str]:
    """The credential loop over four images: one image with findings, or one
    with no report, fails the step; every image is scanned either way."""
    problems = []
    clean = {"exit": 0, "report": "clean"}
    findings = {"exit": 1, "report": "secret"}
    cases = [
        ("findings on image 2 of 4", {IMAGES[1]: findings}, True),
        (
            "no report from image 3, trivy exited 0",
            {IMAGES[2]: {"exit": 0, "report": "none"}},
            True,
        ),
        (
            "findings on image 2, trivy exited 0",
            {IMAGES[1]: {"exit": 0, "report": "secret"}},
            True,
        ),
        ("all four clean", {}, False),
    ]
    for what, scenario, fails in cases:
        run = harness.run(script, {**dict.fromkeys(IMAGES, clean), **scenario})
        problems += _common(run, what)
        if (run.code != 0) != fails:
            expected = "non-zero" if fails else "0"
            problems.append(f"{what}: exit {run.code}, expected {expected}")
        if [call[-1] for call in run.calls] != IMAGES:
            problems.append(f"{what}: scanned {[c[-1] for c in run.calls]}")
        has_row = any(case["report"] == "secret" for case in scenario.values())
        if has_row and f"{IMAGES[1]} (image)" not in run.out.split(ROW)[0]:
            problems.append(f"{what}: the row for image 2 is not printed")
    return problems


def check_vuln(harness: Harness, script: str, targets: List[str]) -> List[str]:
    """An advisory scan still names the advisory, and fails on it."""
    problems = []
    clean = {"exit": 0, "report": "clean"}
    run = harness.run(
        script,
        {**dict.fromkeys(targets, clean), targets[-1]: {"exit": 1, "report": "vuln"}},
    )
    problems += _common(run, "advisory")
    if run.code == 0:
        problems.append("advisory: the step passed")
    if CVE not in run.out:
        problems.append("advisory: the advisory id is not in the log")
    run = harness.run(script, dict.fromkeys(targets, clean))
    if run.code != 0:
        problems.append(f"clean: exit {run.code}")
    return problems


@pytest.fixture
def harness(tmp_path):
    return Harness(tmp_path)


class TestTheScanSteps:
    def test_the_harness_finds_every_scan_step(self):
        assert {
            BACKEND_STEP,
            MODULES_STEP,
            MODULES_VULN_STEP,
            IMAGE_STEP,
            IMAGE_VULN_STEP,
        } <= set(_steps())

    @pytest.mark.regression
    def test_the_backend_scan(self, harness):
        assert check_single(harness, _steps()[BACKEND_STEP], "backend/", CI_LABEL) == []

    @pytest.mark.regression
    def test_the_modules_scan(self, harness):
        assert (
            check_single(
                harness, _steps()[MODULES_STEP], "modules/", "modules/ (source tree)"
            )
            == []
        )

    def test_the_modules_scans_pass_a_core_checkout(self, tmp_path):
        core = Harness(tmp_path, modules=False)
        for name in (MODULES_STEP, MODULES_VULN_STEP):
            run = core.run(
                _steps()[name], {"modules/": {"exit": 1, "report": "secret"}}
            )
            assert (run.code, run.calls) == (0, []), (name, run)
            assert "core checkout: no modules/ to scan" in run.out

    @pytest.mark.regression
    def test_the_image_scan(self, harness):
        assert check_image_loop(harness, _steps()[IMAGE_STEP]) == []

    def test_the_modules_advisory_scan_names_the_advisory(self, harness):
        assert check_vuln(harness, _steps()[MODULES_VULN_STEP], ["modules/"]) == []

    def test_the_image_advisory_scan_names_the_advisory(self, harness):
        assert check_vuln(harness, _steps()[IMAGE_VULN_STEP], IMAGES) == []


#: The tag list of the credential loop, as the workflow spells it.
LOOP_HEAD = re.compile(r"for image in ([^;]+); do", flags=re.S)


def _pipe_fed(script: str) -> str:
    """The same loop, fed by a pipe: it runs in a subshell, so ``failed=1``
    is lost when it ends."""
    tags = " ".join(LOOP_HEAD.search(script).group(1).replace("\\\n", " ").split())
    pipe = f"printf '%s\\n' {tags} | while read -r image; do"
    out, count = LOOP_HEAD.subn(lambda _: pipe, script)
    assert count == 1
    return out


def _replace_once(script: str, old: str, new: str) -> str:
    assert script.count(old) == 1, old
    return script.replace(old, new)


class TestAPlantedDefectTurnsTheHarnessRed:
    """Each check above, run over a broken copy of a step, must fail."""

    def test_a_pipe_fed_loop(self, harness):
        problems = check_image_loop(harness, _pipe_fed(_steps()[IMAGE_STEP]))
        assert any("findings on image 2 of 4: exit 0" in p for p in problems), problems
        assert any(
            "no report from image 3, trivy exited 0: exit 0" in p for p in problems
        )

    def test_the_table_command(self, harness):
        old = (
            '"$TRIVY" fs --skip-version-check --scanners secret --severity HIGH,CRITICAL'
            " --exit-code 1 --format table backend/\n"
        )
        problems = check_single(harness, old, "backend/", CI_LABEL)
        assert any("the planted value reached" in p for p in problems), problems

    def test_the_renderer_status_winning(self, harness):
        script = _replace_once(
            _steps()[BACKEND_STEP],
            'if [ "$status" -ne 0 ]; then exit "$status"; fi\n',
            "",
        )
        problems = check_single(harness, script, "backend/", CI_LABEL)
        assert any("trivy failed on a clean report: exit 0" in p for p in problems), (
            problems
        )

    def test_a_bare_trivy(self, harness):
        script = _replace_once(_steps()[BACKEND_STEP], '"$TRIVY" fs', "trivy fs")
        problems = check_single(harness, script, "backend/", CI_LABEL)
        assert any("a bare trivy ran" in p for p in problems), problems

    def test_a_loop_that_keeps_only_trivy_s_status(self, harness):
        script = _replace_once(
            _steps()[IMAGE_STEP],
            'if [ "$status" -ne 0 ] || [ "$render" -ne 0 ]; then failed=1; fi',
            'if [ "$status" -ne 0 ]; then failed=1; fi',
        )
        problems = check_image_loop(harness, script)
        assert any("no report from image 3" in p for p in problems), problems

    def test_an_advisory_scan_through_the_renderer(self, harness):
        script = _replace_once(
            _steps()[MODULES_VULN_STEP],
            "--format table modules/",
            '--format json --output "$RUNNER_TEMP/v.json" modules/ || true\n'
            '    python3 "$GITHUB_WORKSPACE/scripts/render_secret_findings.py"'
            ' "$RUNNER_TEMP/v.json" --stage ci --label modules/',
        )
        problems = check_vuln(harness, script, ["modules/"])
        assert "advisory: the advisory id is not in the log" in problems, problems
