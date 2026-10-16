"""The release scans each image for stored credentials before it is pushed (#748).

``.github/workflows/release.yml`` builds every image into the runner's Docker,
scans it with trivy's secret scanner -- the whole image with trivy's default
rules, then this project's own payload with every allow rule off -- and only
then builds it again with ``push: true``, after which it checks the pushed
layers are the scanned ones. Each property below is one way that gate could
pass for the wrong reason, and every one was measured against trivy 0.74.0:

* without ``--exit-code 1`` trivy exits 0 on findings;
* ``--severity`` drops MEDIUM credentials;
* without ``--image-src docker`` a missing local tag is fetched and scanned
  from the registry -- on a re-run, the previous image;
* a ``trivy.yaml``, ``trivy-secret.yaml`` or ``.trivyignore`` in the working
  directory is read silently (a ``skip-dirs`` there removed ``/app``);
* a misspelled allow-rule ID, or a missing ``--secret-config`` file, falls back
  to the default rules without a word, and the defaults skip the dashboard's
  entire payload (``/usr/share``);
* a scan placed after the push, or a status swallowed by the summary step,
  reports a finding about an image that is already public.

These parse YAML and text and call nothing -- no git, docker, trivy or npm --
so they run in the ordinary unit job and in ``scripts/core_build.sh``'s copy,
which has no ``.git``. The two helper scripts are run with this interpreter.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
TRIVY_DIR = ROOT / ".github" / "trivy"
RENDER = ROOT / "scripts" / "render_secret_findings.py"
LAYERS = ROOT / "scripts" / "check_scanned_layers.py"

pytestmark = pytest.mark.unit

#: Every built-in allow rule in trivy 0.74.0, spelled as
#: pkg/fanal/secret/builtin-allow-rules.go spells it.
TRIVY_ALLOW_RULE_IDS = {
    "dist-info",
    "tests",
    "examples",
    "vendor",
    "usr-dirs",
    "locale-dir",
    "markdown",
    "node.js",
    "golang",
    "python",
    "rubygems",
    "wordpress",
    "anaconda-log",
}

TRIVY_VERSION = "0.74.0"
TRIVY_SHA256 = "2ae6fe3ee734b7fdf11335663e18c75ea12dccc76062f09f164a3b0f8be4371a"

#: The image jobs, what the load build tags, and the paths copied out of the
#: image for the payload scan.
IMAGE_JOBS = {
    "api-images": ("release-scan/api:", ["/app"]),
    "web-images": ("release-scan/web:", ["/usr/share/nginx/html", "/etc/nginx"]),
}

#: The flags every trivy invocation must carry, with their values.
REQUIRED_FLAGS = {
    "--scanners": "secret",
    "--exit-code": "1",
    "--format": "json",
    "--timeout": "15m",
    "--config": "$TRIVY_CONF/trivy.yaml",
    "--ignorefile": "$TRIVY_CONF/trivyignore",
}

#: Flags that narrow what is reported or read. None may appear.
FORBIDDEN_FLAGS = (
    "--severity",
    "--skip-dirs",
    "--skip-files",
    "--ignore-policy",
    "--ignored-licenses",
    "--secret-config-skip",
    "--file-patterns",
    "--ignore-unfixed",
)


def load_workflow(text: str | None = None) -> dict:
    return yaml.safe_load(RELEASE.read_text(encoding="utf-8") if text is None else text)


def is_build_push(step: dict) -> bool:
    return str(step.get("uses", "")).startswith("docker/build-push-action@")


def trivy_commands(run: str) -> list[list[str]]:
    """Each ``"$TRIVY" ...`` command in a run script, as argv (continuations joined)."""
    joined = run.replace("\\\n", " ")
    commands = []
    for line in joined.splitlines():
        stripped = line.strip()
        if re.match(r'^("\$TRIVY"|\$TRIVY|trivy)\s', stripped):
            commands.append(shlex.split(stripped, posix=True, comments=False))
    return commands


def flag_values(argv: list[str]) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for index in range(len(argv)):
        arg = argv[index]
        if arg.startswith("--"):
            name, _, inline = arg.partition("=")
            value = inline or (argv[index + 1] if index + 1 < len(argv) else "")
            values.setdefault(name, []).append(value)
    return values


# --------------------------------------------------------------------------
# The structural checks, as functions of the parsed workflow, so the tamper
# tests below can run them over a broken copy and watch them fail.
# --------------------------------------------------------------------------


def check_install(workflow: dict) -> list[str]:
    problems = []
    env = workflow.get("env") or {}
    if str(env.get("TRIVY_VERSION")) != TRIVY_VERSION:
        problems.append(
            f"TRIVY_VERSION is {env.get('TRIVY_VERSION')!r}, not {TRIVY_VERSION}"
        )
    if str(env.get("TRIVY_SHA256")) != TRIVY_SHA256:
        problems.append("TRIVY_SHA256 is not the 0.74.0 Linux-64bit tarball's")
    for name, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            uses = str(step.get("uses", ""))
            run = str(step.get("run", ""))
            if "trivy" in uses.lower():
                problems.append(
                    f"{name}: trivy comes from an action ({uses}), not the pinned binary"
                )
            if re.search(r"apt(-get)?\s+install[^\n]*trivy", run):
                problems.append(f"{name}: trivy installed with apt")
    return problems


def check_job(workflow: dict, job_name: str) -> list[str]:
    """Every property of one image job's scan, as a list of problems."""
    tag_prefix, payload_paths = IMAGE_JOBS[job_name]
    steps = workflow["jobs"][job_name]["steps"]
    problems: list[str] = []

    def index_of(pred, what: str) -> int:
        hits = [i for i in range(len(steps)) if pred(steps[i])]
        if len(hits) != 1:
            problems.append(
                f"{job_name}: expected exactly one {what}, found {len(hits)}"
            )
            return -1
        return hits[0]

    install = index_of(
        lambda s: (
            "sha256sum -c" in str(s.get("run", "")) and "trivy" in str(s.get("run", ""))
        ),
        "trivy install",
    )
    load = index_of(
        lambda s: is_build_push(s) and (s.get("with") or {}).get("load") is True,
        "load build",
    )
    push = index_of(
        lambda s: is_build_push(s) and (s.get("with") or {}).get("push") is True,
        "push build",
    )
    image_scan = index_of(
        lambda s: any(
            c[1:2] == ["image"] for c in trivy_commands(str(s.get("run", "")))
        ),
        "trivy image step",
    )
    payload_scan = index_of(
        lambda s: any(c[1:2] == ["fs"] for c in trivy_commands(str(s.get("run", "")))),
        "trivy fs step",
    )
    identity = index_of(
        lambda s: "check_scanned_layers.py" in str(s.get("run", "")), "identity step"
    )
    if -1 in (install, load, push, image_scan, payload_scan, identity):
        return problems

    # Order: install, load build, both scans, push, identity -- and the
    # identity check before anything signs or attests the pushed digest.
    if not install < load < image_scan < push:
        problems.append(
            f"{job_name}: the image scan is not between the load build and the push"
        )
    if not load < payload_scan < push:
        problems.append(
            f"{job_name}: the payload scan is not between the load build and the push"
        )
    if not push < identity:
        problems.append(f"{job_name}: the identity check is not after the push")
    for i in range(push + 1, identity):
        step = steps[i]
        run = str(step.get("run", "")) + str(step.get("uses", ""))
        if "cosign" in run or "sbom" in run.lower():
            problems.append(
                f"{job_name}: {step.get('name')} uses the pushed digest before the identity check"
            )

    load_with = steps[load].get("with") or {}
    push_with = steps[push].get("with") or {}
    if "id" in steps[load]:
        problems.append(
            f"{job_name}: the load build has an id ({steps[load]['id']}); steps.build must be the push"
        )
    if steps[push].get("id") != "build":
        problems.append(f"{job_name}: the push build is not `id: build`")
    if load_with.get("push") is not False:
        problems.append(f"{job_name}: the load build does not say push: false")
    for key in ("context", "file", "target", "build-args", "platforms"):
        if load_with.get(key) != push_with.get(key):
            problems.append(
                f"{job_name}: the load build's {key} differs from the push build's"
            )
    writers = [
        s.get("name")
        for s in steps
        if is_build_push(s) and "cache-to" in (s.get("with") or {})
    ]
    if writers != [steps[push].get("name")]:
        problems.append(
            f"{job_name}: cache-to is written by {writers}, not the push build alone"
        )
    tag = str(load_with.get("tags", ""))
    if not tag.startswith(tag_prefix):
        problems.append(
            f"{job_name}: the load build tags {tag!r}, expected {tag_prefix}<profile>"
        )

    for i, kind, config in (
        (image_scan, "image", "secret-default.yaml"),
        (payload_scan, "fs", "secret-payload.yaml"),
    ):
        step = steps[i]
        run = str(step.get("run", ""))
        env = step.get("env") or {}
        where = f"{job_name}: {step.get('name')}"
        if step.get("continue-on-error"):
            problems.append(f"{where}: continue-on-error")
        if not str(env.get("TRIVY_CONF", "")).endswith("/.github/trivy"):
            problems.append(f"{where}: TRIVY_CONF is not the workflow's .github/trivy")
        if env.get("SCAN_IMAGE") != tag:
            problems.append(
                f"{where}: scans {env.get('SCAN_IMAGE')!r}, not the load build's {tag!r}"
            )
        if not run.lstrip().startswith("set -euo pipefail"):
            problems.append(f"{where}: does not start with set -euo pipefail")
        if 'cd "$(mktemp -d "$RUNNER_TEMP/' not in run:
            problems.append(
                f"{where}: trivy does not run from an empty directory under $RUNNER_TEMP"
            )
        commands = [c for c in trivy_commands(run) if c[1:2] == [kind]]
        if len(commands) != 1:
            problems.append(
                f"{where}: expected one trivy {kind} command, found {len(commands)}"
            )
            continue
        argv = commands[0]
        flags = flag_values(argv)
        for flag, value in REQUIRED_FLAGS.items():
            if flags.get(flag) != [value]:
                problems.append(
                    f"{where}: {flag} is {flags.get(flag)}, expected [{value!r}]"
                )
        if flags.get("--secret-config") != [f"$TRIVY_CONF/{config}"]:
            problems.append(
                f"{where}: --secret-config is {flags.get('--secret-config')}, expected {config}"
            )
        output = flags.get("--output") or [""]
        if output != ["$report"] or not re.search(
            r'^\s*report="\$RUNNER_TEMP/[^"/]+\.json"$', run, re.M
        ):
            problems.append(
                f"{where}: the JSON does not go to --output under $RUNNER_TEMP"
            )
        for flag in FORBIDDEN_FLAGS:
            if flag in flags:
                problems.append(f"{where}: {flag} narrows what is reported")
        if kind == "image":
            if flags.get("--image-src") != ["docker"]:
                problems.append(
                    f"{where}: --image-src is {flags.get('--image-src')}, expected ['docker']"
                )
            if argv[-3:] != ["$SCAN_IMAGE", "||", "status=$?"]:
                problems.append(
                    f"{where}: trivy's status is not kept with `|| status=$?`"
                )
        else:
            if argv[-3:] != ["$payload", "||", "status=$?"]:
                problems.append(
                    f"{where}: trivy's status is not kept with `|| status=$?`"
                )

        # The renderer, and the status kept: trivy's exit code wins, then the
        # renderer's; nothing swallows either.
        if "|| true" in run or "|| :" in run or "set +e" in run:
            problems.append(f"{where}: an exit status is swallowed")
        if (
            'render_secret_findings.py" "$report"' not in run
            or "|| render=$?" not in run
        ):
            problems.append(
                f"{where}: the report is not rendered by render_secret_findings.py"
            )
        tail = [line.strip() for line in run.strip().splitlines()[-2:]]
        if tail != [
            'if [ "$status" -ne 0 ]; then exit "$status"; fi',
            'exit "$render"',
        ]:
            problems.append(
                f"{where}: does not end by exiting with trivy's status, then the renderer's"
            )
        # The report is written by trivy, read by the renderer, and touched
        # by nothing else: any other line naming it could print it.
        allowed_report_lines = (
            r'report="\$RUNNER_TEMP/[^"/]+\.json"',
            r'rm -r?f( "\$payload")? "\$report"',
            r'--exit-code 1 --format json --output "\$report" --timeout 15m \\',
            r'python3 "\$GITHUB_WORKSPACE/scripts/render_secret_findings\.py" "\$report" --label "\$LABEL" \|\| render=\$\?',
        )
        for line in run.splitlines():
            if "report" in line and not line.lstrip().startswith("#"):
                if not any(
                    re.fullmatch(pattern, line.strip())
                    for pattern in allowed_report_lines
                ):
                    problems.append(
                        f"{where}: an unexpected line touches the JSON report: {line.strip()}"
                    )

        if kind == "fs":
            if re.search(r"\bdocker\s+(run|start|exec)\b", run):
                problems.append(
                    f"{where}: the image is run; it must only be created and copied from"
                )
            if 'cid="$(docker create "$SCAN_IMAGE")"' not in run:
                problems.append(f"{where}: no `docker create` of the scanned image")
            copied = re.findall(r'docker cp "\$cid:([^"]+)"', run)
            if copied != payload_paths:
                problems.append(f"{where}: copies {copied}, expected {payload_paths}")
            if "-type f -print -quit" not in run or "exit 1" not in run:
                problems.append(
                    f"{where}: an empty extracted directory does not fail the step"
                )

    # The report is never uploaded.
    for step in steps:
        if str(step.get("uses", "")).startswith("actions/upload-artifact"):
            path = str((step.get("with") or {}).get("path", ""))
            if any(
                word in path
                for word in (
                    "trivy",
                    "RUNNER_TEMP",
                    "runner.temp",
                    "payload",
                    "image.json",
                )
            ):
                problems.append(f"{job_name}: {step.get('name')} uploads {path}")
    return problems


def check_trivy_files(
    payload_text: str, default_text: str, config_text: str, ignore_text: str
) -> list[str]:
    problems = []
    payload = yaml.safe_load(payload_text)
    if not isinstance(payload, dict) or set(payload) != {
        "disable-allow-rules",
        "skip-patterns",
    }:
        problems.append(
            f"secret-payload.yaml keys are {sorted(payload) if isinstance(payload, dict) else payload}"
        )
    else:
        rules = payload["disable-allow-rules"]
        if (
            not isinstance(rules, list)
            or set(rules) != TRIVY_ALLOW_RULE_IDS
            or len(rules) != len(TRIVY_ALLOW_RULE_IDS)
        ):
            missing = TRIVY_ALLOW_RULE_IDS - set(rules or [])
            extra = set(rules or []) - TRIVY_ALLOW_RULE_IDS
            problems.append(
                f"secret-payload.yaml disables the wrong rules: missing {sorted(missing)}, unknown {sorted(extra)}"
            )
        if payload["skip-patterns"] != []:
            problems.append(
                f"secret-payload.yaml skip-patterns is {payload['skip-patterns']!r}, not []"
            )
    default = yaml.safe_load(default_text)
    if default != {"disable-allow-rules": []}:
        problems.append(
            f"secret-default.yaml is {default!r}; it must be trivy's defaults, stated"
        )
    if yaml.safe_load(config_text) is not None:
        problems.append(
            "trivy.yaml sets options; every option belongs on the command line"
        )
    live = [
        line
        for line in ignore_text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if live:
        problems.append(
            f"trivyignore ignores {len(live)} entries; there is no allow-list"
        )
    return problems


def trivy_file_texts() -> tuple[str, str, str, str]:
    return tuple(
        (TRIVY_DIR / name).read_text(encoding="utf-8")
        for name in (
            "secret-payload.yaml",
            "secret-default.yaml",
            "trivy.yaml",
            "trivyignore",
        )
    )


# --------------------------------------------------------------------------
# The gate as it stands.
# --------------------------------------------------------------------------


def test_trivy_is_the_pinned_checksummed_binary():
    assert check_install(load_workflow()) == []


@pytest.mark.parametrize("job_name", sorted(IMAGE_JOBS))
def test_each_image_is_scanned_before_it_is_pushed(job_name):
    assert check_job(load_workflow(), job_name) == []


def test_every_job_that_pushes_an_image_is_covered():
    """A new image job is not silently outside the gate."""
    workflow = load_workflow()
    pushing = {
        name
        for name, job in workflow["jobs"].items()
        for step in job.get("steps", [])
        if is_build_push(step) and (step.get("with") or {}).get("push") is True
    }
    assert pushing == set(IMAGE_JOBS)


def test_the_trivy_configuration_is_the_workflows_own():
    assert check_trivy_files(*trivy_file_texts()) == []


def test_the_repository_root_carries_no_trivy_configuration():
    """trivy reads these from its working directory. The scans run from an
    empty one and name their own, but a root file would apply to anyone who
    reproduces a scan from a checkout."""
    present = [
        name
        for name in (
            "trivy.yaml",
            "trivy-secret.yaml",
            ".trivyignore",
            ".trivyignore.yaml",
        )
        if (ROOT / name).exists()
    ]
    assert present == []


# --------------------------------------------------------------------------
# Tampers: each defect the gate exists to refuse, planted in a copy.
# --------------------------------------------------------------------------


def tampered(old: str, new: str, count: int = -1) -> dict:
    text = RELEASE.read_text(encoding="utf-8")
    assert old in text, f"tamper anchor not found: {old!r}"
    return load_workflow(text.replace(old, new, count))


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("--exit-code 1 --format json", "--format json", "--exit-code is None"),
        (
            "--exit-code 1 --format json",
            "--exit-code 1 --severity HIGH,CRITICAL --format json",
            "--severity narrows",
        ),
        (
            "image --image-src docker --scanners secret",
            "image --scanners secret",
            "--image-src is None",
        ),
        ('"$SCAN_IMAGE" || status=$?', '"$SCAN_IMAGE" || true', "status is swallowed"),
        ('"$payload" || status=$?', '"$payload" || true', "status is swallowed"),
        (
            'if [ "$status" -ne 0 ]; then exit "$status"; fi\n',
            "",
            "trivy's status, then the renderer's",
        ),
        ("--timeout 15m", "", "--timeout is None"),
        ('--ignorefile "$TRIVY_CONF/trivyignore" \\\n', "", "--ignorefile is None"),
        ("secret-payload.yaml", "secret-default.yaml", "expected secret-payload.yaml"),
        (
            'docker cp "$cid:/etc/nginx" "$payload/nginx"\n',
            "",
            "copies ['/usr/share/nginx/html']",
        ),
        ("-type f -print -quit", "-print -quit", "empty extracted directory"),
        (
            "GIT_COMMIT=${{ github.sha }}\n          provenance: false",
            "GIT_COMMIT=tampered\n          provenance: false",
            "build-args differs",
        ),
        (
            "- name: Build for the scan\n        uses:",
            "- name: Build for the scan\n        id: build\n        uses:",
            "the load build has an id",
        ),
        ('cd "$(mktemp -d "$RUNNER_TEMP/trivy-cwd.XXXXXX")"\n', "", "empty directory"),
        ('--output "$report"', "--output scan.json", "--output under $RUNNER_TEMP"),
    ],
)
def test_a_planted_defect_turns_the_gate_red(old, new, expected):
    workflow = tampered(old, new)
    problems = check_job(workflow, "api-images") + check_job(workflow, "web-images")
    assert problems, f"tamper {old!r} -> {new!r} went unnoticed"
    if expected:
        assert any(expected in p for p in problems), problems


def test_moving_the_scan_after_the_push_turns_the_gate_red():
    workflow = load_workflow()
    steps = workflow["jobs"]["api-images"]["steps"]
    scan = next(
        s for s in steps if s.get("name") == "Scan the image for stored credentials"
    )
    steps.remove(scan)
    push = next(i for i in range(len(steps)) if steps[i].get("id") == "build")
    steps.insert(push + 1, scan)
    assert any(
        "not between the load build and the push" in p
        for p in check_job(workflow, "api-images")
    )


def test_a_cache_write_on_the_load_build_turns_the_gate_red():
    workflow = load_workflow()
    steps = workflow["jobs"]["web-images"]["steps"]
    load = next(s for s in steps if s.get("name") == "Build for the scan")
    load["with"]["cache-to"] = "type=gha,scope=x,mode=max"
    assert any("cache-to is written by" in p for p in check_job(workflow, "web-images"))


def test_a_pinned_action_or_an_unpinned_version_turns_the_gate_red():
    workflow = load_workflow()
    workflow["env"]["TRIVY_VERSION"] = "0.69.4"
    workflow["jobs"]["api-images"]["steps"].insert(
        0, {"uses": "aquasecurity/trivy-action@0.30.0"}
    )
    problems = check_install(workflow)
    assert any("TRIVY_VERSION" in p for p in problems) and any(
        "action" in p for p in problems
    )


@pytest.mark.parametrize("rule", sorted(TRIVY_ALLOW_RULE_IDS))
def test_dropping_any_disabled_rule_turns_the_gate_red(rule):
    payload, default, config, ignore = trivy_file_texts()
    broken = re.sub(rf"^  - {re.escape(rule)}\n", "", payload, flags=re.M)
    assert broken != payload
    assert any(rule in p for p in check_trivy_files(broken, default, config, ignore))


@pytest.mark.parametrize(
    ("which", "old", "new"),
    [
        ("payload", "skip-patterns: []", "skip-patterns: ['*.svg']"),
        ("payload", "skip-patterns: []", ""),
        ("payload", "  - golang\n", "  - golang\n  - go-lang\n"),
        ("default", "disable-allow-rules: []", "disable-allow-rules: [tests]"),
        ("config", "# Deliberately empty.", "skip-dirs: [app]\n# Deliberately empty."),
        ("ignore", "# Deliberately empty.", "github-pat\n# Deliberately empty."),
    ],
)
def test_a_widened_trivy_configuration_turns_the_gate_red(which, old, new):
    texts = dict(
        zip(("payload", "default", "config", "ignore"), trivy_file_texts(), strict=True)
    )
    assert old in texts[which]
    texts[which] = texts[which].replace(old, new)
    assert check_trivy_files(
        texts["payload"], texts["default"], texts["config"], texts["ignore"]
    )


# --------------------------------------------------------------------------
# The renderer: path, rule, severity and line only; fails closed.
# --------------------------------------------------------------------------

#: Not a credential: a value no rule matches, standing in for one so the test
#: can assert it never reaches the output.
PLANTED = "planted-value-that-must-not-be-printed"


def report(results: list | None) -> dict:
    data: dict = {
        "SchemaVersion": 2,
        "Trivy": {"Version": TRIVY_VERSION},
        "ArtifactName": "x",
        "ArtifactType": "container_image",
    }
    data["Metadata"] = {"ImageConfig": {"config": {"Env": [f"TOKEN={PLANTED}"]}}}
    if results is not None:
        data["Results"] = results
    return data


def finding(target: str = "app/backend/x_test.py") -> dict:
    return {
        "Target": target,
        "Class": "secret",
        "Secrets": [
            {
                "RuleID": "github-pat",
                "Category": "GitHub",
                "Severity": "CRITICAL",
                "Title": "GitHub Personal Access Token",
                "StartLine": 3,
                "EndLine": 3,
                "Code": {"Lines": [{"Number": 2, "Content": f"before = '{PLANTED}'"}]},
                "Match": f"token = {PLANTED}",
                "Offset": 10,
            }
        ],
    }


def render(
    tmp_path: Path, payload: object | None, raw: str | None = None
) -> subprocess.CompletedProcess:
    path = tmp_path / "report.json"
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
    elif payload is not None:
        path.write_text(json.dumps(payload), encoding="utf-8")
    summary = tmp_path / "summary.md"
    return subprocess.run(
        [
            sys.executable,
            str(RENDER),
            str(path),
            "--label",
            "controlvar:core-1.2.3 (whole image)",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={"GITHUB_STEP_SUMMARY": str(summary), "PATH": "/usr/bin:/bin"},
    )


def test_the_renderer_prints_only_the_whitelisted_fields(tmp_path):
    result = render(
        tmp_path, report([finding(), finding("usr/share/nginx/html/_next/a.js")])
    )
    assert result.returncode == 1
    out = (
        result.stdout
        + result.stderr
        + (tmp_path / "summary.md").read_text(encoding="utf-8")
    )
    assert PLANTED not in out
    assert "app/backend/x_test.py\tgithub-pat\tCRITICAL\t3" in result.stdout
    assert "usr/share/nginx/html/_next/a.js\tgithub-pat\tCRITICAL\t3" in result.stdout
    assert "::error title=Credential found in image::" in result.stdout
    assert "2 finding(s)" in result.stdout


def test_the_renderer_passes_a_clean_report(tmp_path):
    for results in (None, [], [{"Target": "x", "Class": "secret"}]):
        result = render(tmp_path, report(results))
        assert result.returncode == 0, result.stdout
        assert "no findings" in result.stdout and TRIVY_VERSION in result.stdout


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "{not json",
        "[]",
        '{"Results": []}',
        '{"SchemaVersion": 2, "Results": {}}',
    ],
    ids=["missing", "empty", "unparsable", "list", "no-schema", "results-not-list"],
)
def test_the_renderer_fails_closed(tmp_path, raw):
    result = render(tmp_path, None, raw=raw)
    assert result.returncode == 2, (result.stdout, result.stderr)


def test_the_renderer_keeps_control_characters_out_of_the_log(tmp_path):
    result = render(tmp_path, report([finding("a\n::warning::b")]))
    assert result.returncode == 1
    assert "\n::warning::" not in result.stdout


# --------------------------------------------------------------------------
# The identity check: the pushed layers are the scanned layers.
# --------------------------------------------------------------------------

LAYER_A = "sha256:" + "a" * 64
LAYER_B = "sha256:" + "b" * 64
LAYER_C = "sha256:" + "c" * 64


def layers(
    tmp_path: Path, loaded: object, pushed: object
) -> subprocess.CompletedProcess:
    (tmp_path / "loaded.json").write_text(json.dumps(loaded), encoding="utf-8")
    (tmp_path / "pushed.json").write_text(json.dumps(pushed), encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            str(LAYERS),
            "--loaded",
            str(tmp_path / "loaded.json"),
            "--pushed",
            str(tmp_path / "pushed.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def inspect(*diff_ids: str) -> list:
    return [{"Id": "sha256:x", "RootFS": {"Type": "layers", "Layers": list(diff_ids)}}]


def config(*diff_ids: str) -> dict:
    return {
        "architecture": "amd64",
        "os": "linux",
        "rootfs": {"type": "layers", "diff_ids": list(diff_ids)},
    }


def test_identical_layers_pass(tmp_path):
    assert (
        layers(tmp_path, inspect(LAYER_A, LAYER_B), config(LAYER_A, LAYER_B)).returncode
        == 0
    )
    assert (
        layers(tmp_path, inspect(LAYER_A), {"linux/amd64": config(LAYER_A)}).returncode
        == 0
    )


@pytest.mark.parametrize(
    "pushed",
    [
        config(LAYER_A, LAYER_C),
        config(LAYER_A),
        config(LAYER_A, LAYER_B, LAYER_C),
        config(LAYER_B, LAYER_A),
    ],
    ids=["changed", "fewer", "more", "reordered"],
)
def test_different_layers_fail(tmp_path, pushed):
    result = layers(tmp_path, inspect(LAYER_A, LAYER_B), pushed)
    assert result.returncode == 1
    assert (
        "::error title=Pushed image differs from the scanned image::" in result.stdout
    )


@pytest.mark.parametrize(
    ("loaded", "pushed"),
    [
        (inspect(), config()),
        ([], config(LAYER_A)),
        (inspect(LAYER_A) * 2, config(LAYER_A)),
        (
            inspect(LAYER_A),
            {"linux/amd64": config(LAYER_A), "linux/arm64": config(LAYER_A)},
        ),
        (inspect(LAYER_A), {"config": {}}),
        (inspect("not-a-digest"), config("not-a-digest")),
    ],
    ids=[
        "empty",
        "no-image",
        "two-images",
        "two-platforms",
        "no-rootfs",
        "not-digests",
    ],
)
def test_unreadable_layer_lists_fail_closed(tmp_path, loaded, pushed):
    assert layers(tmp_path, loaded, pushed).returncode == 2
