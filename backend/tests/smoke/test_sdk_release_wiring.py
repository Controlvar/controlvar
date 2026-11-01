"""Every SDK in the tree is accounted for by the release plumbing.

``scripts/check_sdk_version.py``'s ``ECOSYSTEMS`` map and
``.github/workflows/sdk-release.yml`` both enumerate SDKs by name.  A
hand-typed list of names beside a directory that grows is the repository's most
frequent defect: a list that was complete when it was written, silently
incomplete a month later, and green the whole time because nothing compares it
to the thing it lists.

So compare it.  A new ``sdk/<name>/`` fails these tests until someone has said
how it is published -- including "not yet", which is a real answer that the
gate then enforces by refusing its tags.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
SDK_DIR = ROOT / "sdk"
WORKFLOW = ROOT / ".github" / "workflows" / "sdk-release.yml"

pytestmark = pytest.mark.smoke


def sdk_directories() -> set[str]:
    """Every SDK in the tree.

    A directory under ``sdk/`` holding a manifest or source, not the shared
    files that sit beside them (``sdk/LICENSE`` is a file, and anything
    starting with a dot or an underscore is tooling).
    """
    return {
        entry.name
        for entry in SDK_DIR.iterdir()
        if entry.is_dir() and not entry.name.startswith((".", "_"))
    }


def _check_sdk_version():
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location(
        "check_sdk_version", ROOT / "scripts" / "check_sdk_version.py"
    )
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ecosystems() -> dict[str, str]:
    return dict(_check_sdk_version().ECOSYSTEMS)


def manifest_version(sdk: str) -> str:
    """The version sdk/<sdk>'s own manifest carries in this tree, read by the
    function `resolve`'s gate uses, so no test here pins an SDK's version."""
    return _check_sdk_version().read_manifest_version(sdk)[0]


def test_every_sdk_directory_has_an_ecosystem() -> None:
    directories = sdk_directories()
    mapped = set(ecosystems())
    unmapped = directories - mapped
    assert not unmapped, (
        f"sdk/{{{','.join(sorted(unmapped))}}} exist but "
        "scripts/check_sdk_version.py does not say how they are published. "
        "Add them to ECOSYSTEMS -- 'unwired' is a valid answer and refuses "
        "their tags until an account exists."
    )


def test_no_ecosystem_entry_without_a_directory() -> None:
    """The other direction: a name left behind after a rename publishes nothing."""
    stale = set(ecosystems()) - sdk_directories()
    assert not stale, (
        f"ECOSYSTEMS names {sorted(stale)}, which are not directories under sdk/"
    )


@pytest.mark.parametrize("sdk", sorted(sdk_directories()))
def test_wired_sdks_report_a_version_and_unwired_ones_refuse(sdk: str) -> None:
    """Each SDK behaves the way its ecosystem says it should.

    ``pypi``/``npm`` must yield a version from their own manifest; ``tag-only``
    (Go) needs no manifest; ``unwired`` must *fail*, so that tagging one is a
    loud refusal rather than a workflow that quietly publishes nothing.
    """
    ecosystem = ecosystems()[sdk]
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_sdk_version.py"), sdk],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if ecosystem == "unwired":
        assert result.returncode != 0, (
            f"sdk/{sdk} is marked unwired but the gate accepted it: {result.stdout}"
        )
        assert "no publishing identity" in result.stderr
    else:
        assert result.returncode == 0, (
            f"sdk/{sdk} ({ecosystem}) failed its own check: {result.stderr}"
        )


#: The SDKs whose tag the gate compares with a manifest: every ecosystem but
#: `tag-only` (no manifest) and `unwired` (refused whatever the version), so a
#: new ecosystem is in the parametrisation below without anyone adding it.
COMPARED_SDKS = sorted(
    name for name, eco in ecosystems().items() if eco not in {"tag-only", "unwired"}
)


def test_the_compared_sdks_include_every_wired_registry() -> None:
    """The parametrisation is read from ECOSYSTEMS, so this is what it holds."""
    assert {"pypi", "npm", "maven", "nuget"} <= {
        ecosystems()[sdk] for sdk in COMPARED_SDKS
    }
    assert {"java", "dotnet"} <= set(COMPARED_SDKS)


@pytest.mark.parametrize("sdk", COMPARED_SDKS)
def test_a_mismatched_tag_is_refused(sdk: str) -> None:
    """The gate's whole job: a tag that does not match the manifest.

    It once listed pypi and npm by name, so wiring java to maven and dotnet
    to nuget would have left those readers' mismatches untested."""
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "check_sdk_version.py"),
            sdk,
            "--expect",
            "99.99.99",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, (
        f"sdk/{sdk} accepted a tag of 99.99.99: {result.stdout}"
    )


def test_every_ecosystem_has_a_job_that_runs_for_it() -> None:
    """Each ecosystem in ECOSYSTEMS is reachable by a job in sdk-release.yml.

    The first version of this test searched the workflow *text* for ``sdk/js``
    and friends. That passed on the header comments alone: the jobs are gated
    on the resolved ``ecosystem``, not on SDK names, so deleting a comment
    broke it and deleting the whole ``npm:`` job did not. It was also a bare
    substring search, which ``sdk/openfeature-python`` satisfied for
    ``sdk/openfeature``. Two ways to pass for the wrong reason in six lines --
    the most frequent defect class in this repository: a check that passes
    for the wrong reason.

    So parse the YAML and read the conditions the jobs actually carry. If an
    SDK resolves to an ecosystem no job selects, its tag passes the version
    gate and then finds nothing to run: a green workflow that published
    nothing.
    """
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    conditions = " ".join(str(job.get("if", "")) for job in workflow["jobs"].values())

    # 'unwired' is the one ecosystem with no job on purpose: check_sdk_version.py
    # refuses those tags in `resolve`, before any publish job is reached.
    needed = {eco for eco in ecosystems().values() if eco != "unwired"}
    missing = {eco for eco in needed if f"'{eco}'" not in conditions}
    assert not missing, (
        f"no job in sdk-release.yml runs for ecosystem(s) {sorted(missing)}; "
        f"the job conditions are: {conditions!r}"
    )


def test_unwired_sdks_have_no_publish_job() -> None:
    """The other direction: 'unwired' must not be selectable by any job.

    A job gated on ``ecosystem == 'unwired'`` would publish an SDK whose
    registry account does not exist, which is the state `resolve` refuses
    outright.
    """
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for name, job in workflow["jobs"].items():
        assert "'unwired'" not in str(job.get("if", "")), (
            f"job {name!r} selects the 'unwired' ecosystem"
        )


def test_the_resolve_job_runs_the_version_gate() -> None:
    """`resolve` must actually call check_sdk_version.py.

    Everything else here assumes the workflow refuses a mismatched tag. It
    does that by running the script; a `resolve` that stopped would leave the
    version check to nothing at all.
    """
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["resolve"]["steps"]
    commands = " ".join(str(step.get("run", "")) for step in steps)
    assert "scripts/check_sdk_version.py" in commands
    assert "--expect" in commands, (
        "resolve runs check_sdk_version.py but without --expect, so it never "
        "compares the tag to the manifest"
    )


# --------------------------------------------------------------------------
# #687: the npm jobs publish the tarball that was checked, with registry
# ranges. `npm-build` pins, packs and checks it; `npm` publishes that file.
# --------------------------------------------------------------------------

PACKED_TGZ = "${{ steps.pack.outputs.tgz }}"
DOWNLOADED_TGZ = "${{ github.workspace }}/package/${{ needs.npm-build.outputs.file }}"


def _npm_build_steps() -> list[dict]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["npm-build"]["steps"]


def _npm_publish_steps() -> list[dict]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["npm"]["steps"]


def _index(steps: list[dict], what: str, predicate, job: str = "npm-build") -> int:
    found = [i for i in range(len(steps)) if predicate(steps[i])]
    assert len(found) == 1, (
        f"expected exactly one {job} step that {what}, found {len(found)}: "
        f"{[steps[i].get('name') for i in found]}"
    )
    return found[0]


def _run_of(step: dict) -> str:
    return str(step.get("run", ""))


@pytest.mark.regression
def test_the_npm_job_pins_checks_and_publishes_the_checked_tarball() -> None:
    """#687: pin -> npm view -> pack -> check the packed package.json ->
    consumer install -> record -> upload, in that order in `npm-build`; then
    `npm`, which needs `npm-build`, publishes that same tarball.

    On the old job none of these steps existed and ``npm publish`` packed the
    source manifest, ``file:../js`` and all.
    """
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = _npm_build_steps()
    publish_steps = _npm_publish_steps()

    pin = _index(
        steps,
        "runs the pin script on the SDK directory",
        lambda s: (
            'scripts/pin_npm_local_deps.py "sdk/' in _run_of(s)
            and "--rewrites-file" in _run_of(s)
        ),
    )
    view = _index(steps, "runs npm view", lambda s: "npm view" in _run_of(s))
    pack = _index(
        steps,
        "packs",
        lambda s: "npm pack" in _run_of(s) and "--dry-run" not in _run_of(s),
    )
    check = _index(
        steps,
        "checks the packed package.json",
        lambda s: "--check-only" in _run_of(s),
    )
    consumer = _index(
        steps, "runs npm ls --all", lambda s: "npm ls --all" in _run_of(s)
    )
    record = _index(steps, "records the tarball", lambda s: s.get("id") == "record")
    upload = _index(
        steps,
        "uploads the tarball",
        lambda s: str(s.get("uses", "")).startswith("actions/upload-artifact@"),
    )
    publish = _index(
        publish_steps,
        "runs npm publish",
        lambda s: "npm publish" in _run_of(s),
        job="npm",
    )

    order = [pin, view, pack, check, consumer, record, upload]
    assert order == sorted(order), (
        "npm-build must run, in order: pin, npm view, npm pack, --check-only, "
        f"the consumer install, record, upload; indices were {order}"
    )
    # The publish runs in a later job, which waits for the build job.
    assert "npm-build" in workflow["jobs"]["npm"].get("needs", [])
    assert not any("npm publish" in _run_of(s) for s in steps), (
        "npm-build publishes nothing"
    )
    assert not any("npm pack" in _run_of(s) for s in publish_steps), (
        "the npm job publishes the downloaded tarball, not a fresh pack"
    )

    # The pin step feeds the npm view step's condition and its list.
    assert steps[pin].get("id") == "pin"
    assert "rewritten=" in _run_of(steps[pin])
    assert steps[view].get("if") == "steps.pin.outputs.rewritten != '0'"
    assert '"$RUNNER_TEMP/rewrites.txt"' in _run_of(steps[view])
    assert "|| true" not in _run_of(steps[view])
    assert "continue-on-error" not in steps[view]

    # The check reads package/package.json out of the packed tarball itself.
    assert steps[pack].get("id") == "pack"
    assert "tgz=" in _run_of(steps[pack])
    check_run = _run_of(steps[check])
    assert 'tar -xzf "$TGZ"' in check_run and "package/package.json" in check_run
    assert '--check-only "$out/package/package.json"' in check_run

    # The consumer installs that tarball alone, runs no lifecycle scripts, and
    # takes peers at the versions in the SDK's lock.
    consumer_run = _run_of(steps[consumer])
    assert "--ignore-scripts" in consumer_run
    assert (
        'npm install --ignore-scripts --no-audit --no-fund "$TGZ" $peers'
        in consumer_run
    )
    assert "--print-peer-pins" in consumer_run

    # The checked, recorded and uploaded file is the packed one ...
    for i in (check, consumer, record):
        assert (steps[i].get("env") or {}).get("TGZ") == PACKED_TGZ, steps[i].get(
            "name"
        )
    assert steps[upload]["with"]["path"] == PACKED_TGZ
    assert workflow["jobs"]["npm-build"]["outputs"]["file"] == (
        "${{ steps.record.outputs.file }}"
    )

    # ... and the publish is of that same file, by the name the build job
    # recorded, not a fresh pack of the directory.
    publish_commands = [
        line.strip()
        for line in _run_of(publish_steps[publish]).splitlines()
        if "npm publish" in line
    ]
    assert publish_commands, _run_of(publish_steps[publish])
    for command in publish_commands:
        assert command.split(") ", 1)[-1].startswith('npm publish "$TGZ"'), command
    assert (publish_steps[publish].get("env") or {}).get("TGZ") == DOWNLOADED_TGZ

    for i in (pin, view, pack, check, consumer, record, upload):
        assert "continue-on-error" not in steps[i], steps[i].get("name")
    for step in publish_steps:
        assert "continue-on-error" not in step, step.get("name") or step.get("uses")


# --------------------------------------------------------------------------
# #775: every npm SDK names this repository, so provenance can be attached
# --------------------------------------------------------------------------

# The form npm itself writes: `npm publish` rewrites any other form, with a warning
# (it did for @controlvar/js-sdk 0.1.0).
REPOSITORY_URL = "git+https://github.com/Controlvar/controlvar.git"


def npm_sdks() -> list[str]:
    return sorted(name for name, eco in ecosystems().items() if eco == "npm")


def test_the_npm_list_is_not_empty() -> None:
    """The parametrised test below is vacuous if no SDK resolves to npm."""
    assert npm_sdks(), "ECOSYSTEMS names no npm SDK"


@pytest.mark.regression
@pytest.mark.parametrize("sdk", npm_sdks())
def test_every_npm_sdk_names_its_repository(sdk: str) -> None:
    """#775: ``npm publish --provenance`` compares ``repository.url`` with the
    repository that ran the workflow, and refuses a package that names none.

    sdk/edge and sdk/react carried no ``repository`` at all, so their first
    publish would have failed at the very last step. The URL is compared
    exactly, in one form for every package, and ``directory`` must be the
    package's own folder.
    """
    import json

    manifest = json.loads((SDK_DIR / sdk / "package.json").read_text(encoding="utf-8"))
    repository = manifest.get("repository")
    assert isinstance(repository, dict), (
        f"sdk/{sdk}/package.json has no repository object: {repository!r}"
    )
    assert repository.get("type") == "git", repository
    assert repository.get("url") == REPOSITORY_URL, (
        f"sdk/{sdk}/package.json repository.url is {repository.get('url')!r}, "
        f"expected {REPOSITORY_URL!r}"
    )
    assert repository.get("directory") == f"sdk/{sdk}", (
        f"sdk/{sdk}/package.json repository.directory is "
        f"{repository.get('directory')!r}, expected 'sdk/{sdk}'"
    )


# --------------------------------------------------------------------------
# #773: a job that installs or tests cannot request an OIDC token, and the
# publish job checks the package it uploads against the tag
# --------------------------------------------------------------------------
#
# Exact sets, so that no split can be undone without a test going red:
#
#   UNSPLIT_TOKEN_JOBS  jobs that install or test while holding
#                       `id-token: write`. None.
#   PUBLISH_JOBS        jobs that hold the OIDC token and publish only.
#   ENVIRONMENT_JOBS    jobs in the `sdk-release` environment: the OIDC ones
#                       and `maven`, which holds no token (Maven Central has
#                       no trusted publishing) but reads the environment's
#                       secrets. Its own set, so that splitting it from the
#                       token set cannot drop `maven` from the checks that
#                       loop over the environment.

UNSPLIT_TOKEN_JOBS: set[str] = set()
PUBLISH_JOBS = {"pypi", "npm", "nuget"}
ENVIRONMENT_JOBS = {"pypi", "npm", "maven", "nuget"}
RELEASE_ENVIRONMENT = "sdk-release"

#: The secrets each job reads, exactly. Only `maven` reads any: npm, PyPI and
#: NuGet publish by OIDC, with no long-lived token.
SECRETS_BY_JOB = {
    "maven": {
        "CENTRAL_TOKEN_USERNAME",
        "CENTRAL_TOKEN_PASSWORD",
        "GPG_PRIVATE_KEY",
        "GPG_PASSPHRASE",
    },
}
#: The configuration variables each job reads, exactly. NUGET_USER is the
#: nuget.org account the trusted publishing policy belongs to.
VARIABLES_BY_JOB = {
    "maven": {"MAVEN_GPG_FINGERPRINT"},
    "nuget": {"NUGET_USER"},
}

#: A step whose `run:` installs packages, runs a test suite or builds one.
INSTALLS_OR_TESTS = re.compile(
    r"\bnpm\s+(ci|install|test|run)\b"
    r"|\bpip3?\s+install\b|-m\s+pip\s+install\b"
    r"|\bpytest\b|-m\s+build\b|\btwine\b"
    r"|\bmvnw?\b"
    r"|\bdotnet\s+(restore|build|test|pack|add|new|run)\b"
)
SECRET = re.compile(r"\$\{\{[^}]*?\bsecrets\.([A-Za-z_][A-Za-z0-9_]*)")
VARIABLE = re.compile(r"\$\{\{[^}]*?\bvars\.([A-Za-z_][A-Za-z0-9_]*)")
EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")
PYPA_PIN = re.compile(r"pypa/gh-action-pypi-publish@([0-9a-f]{40})")


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _effective_permissions(workflow: dict, job: dict):
    """A job with no `permissions:` block inherits the workflow's."""
    if "permissions" in job:
        return job["permissions"]
    return workflow.get("permissions")


def _can_request_oidc(permissions) -> bool:
    if permissions == "write-all":
        return True
    return isinstance(permissions, dict) and permissions.get("id-token") == "write"


def _installs_or_tests(job: dict) -> bool:
    return any(INSTALLS_OR_TESTS.search(_run_of(step)) for step in job.get("steps", []))


def _step(job: str, name: str) -> dict:
    steps = [s for s in _workflow()["jobs"][job]["steps"] if s.get("name") == name]
    assert len(steps) == 1, (
        f"expected one {job!r} step named {name!r}, found {len(steps)}"
    )
    return steps[0]


@pytest.mark.regression
def test_no_job_that_installs_or_tests_can_request_an_oidc_token() -> None:
    """B1. Effective permissions, so a workflow-level grant is seen too.

    On the old file both `pypi` and `npm` installed, tested and held
    `id-token: write`. The set is exact, and empty: it fails if either split
    is undone.
    """
    workflow = _workflow()
    holding = {
        name
        for name, job in workflow["jobs"].items()
        if _installs_or_tests(job)
        and _can_request_oidc(_effective_permissions(workflow, job))
    }
    assert holding == UNSPLIT_TOKEN_JOBS, (
        f"jobs that install or test and can request an OIDC token: "
        f"{sorted(holding)}; expected exactly {sorted(UNSPLIT_TOKEN_JOBS)}"
    )


def _environment_jobs(workflow: dict) -> set[str]:
    """The jobs that name an environment, read from the workflow itself."""
    found = set()
    for name, job in workflow["jobs"].items():
        environment = job.get("environment")
        if isinstance(environment, dict):
            environment = environment.get("name")
        if environment is not None:
            assert environment == RELEASE_ENVIRONMENT, (name, environment)
            found.add(name)
    return found


def test_the_release_environment_and_the_token_sit_on_the_publish_jobs_only() -> None:
    """B3. The approval belongs to the jobs that publish or upload, and the
    OIDC token to the ones that publish by it. The two sets differ by `maven`
    alone, which uploads with the environment's secrets."""
    workflow = _workflow()
    with_environment = _environment_jobs(workflow)
    with_token = {
        name
        for name, job in workflow["jobs"].items()
        if _can_request_oidc(_effective_permissions(workflow, job))
    }
    expected_environment = ENVIRONMENT_JOBS | UNSPLIT_TOKEN_JOBS
    expected_token = PUBLISH_JOBS | UNSPLIT_TOKEN_JOBS
    assert with_environment == expected_environment, (
        f"jobs in the {RELEASE_ENVIRONMENT!r} environment: {sorted(with_environment)}; "
        f"expected exactly {sorted(expected_environment)}"
    )
    assert with_token == expected_token, (
        f"jobs that can request an OIDC token: {sorted(with_token)}; "
        f"expected exactly {sorted(expected_token)}"
    )
    assert ENVIRONMENT_JOBS - PUBLISH_JOBS == {"maven"}


def test_no_publish_job_run_block_contains_an_expression() -> None:
    """C3. In a job that holds the environment, values reach a script
    through `env:` only. The loop is over the jobs the workflow puts in the
    environment, not over the OIDC set, so `maven`, which holds the
    environment and no token, is checked too."""
    workflow = _workflow()
    jobs = workflow["jobs"]
    environment_jobs = _environment_jobs(workflow)
    assert ENVIRONMENT_JOBS <= environment_jobs, sorted(
        ENVIRONMENT_JOBS - environment_jobs
    )
    found = [
        (name, step.get("name") or step.get("uses"))
        for name in sorted(environment_jobs)
        for step in jobs[name]["steps"]
        if "${{" in _run_of(step)
    ]
    assert not found, f"a publish-job run: block contains ${{{{ }}}}: {found}"


def _references(pattern: re.Pattern, node) -> set[str]:
    """Every name `pattern` captures anywhere in a parsed YAML node."""
    if isinstance(node, dict):
        return set().union(
            *(
                _references(pattern, key) | _references(pattern, value)
                for key, value in node.items()
            )
        )
    if isinstance(node, list):
        return set().union(*(_references(pattern, item) for item in node))
    return set(pattern.findall(str(node)))


def _by_job(pattern: re.Pattern) -> dict[str, set[str]]:
    jobs = _workflow()["jobs"]
    found = {name: _references(pattern, job) for name, job in jobs.items()}
    return {name: names for name, names in found.items() if names}


def test_each_job_reads_exactly_its_own_secrets() -> None:
    """Only `maven` reads a secret, and exactly the Central token and the
    signing key: npm, PyPI and NuGet publish by OIDC, with no long-lived
    token, and no build job, which runs tests and third-party plugins, holds
    a credential. Only `maven` and `nuget` read a variable, each its own.
    Nothing outside the jobs (a workflow-level `env:`) names either, every
    expression that reads one names it (no `toJSON(secrets)`, no
    `secrets['X']`), the secrets reach `maven` through step `env:` only, and
    a variable reaches its job through `env:` only (the job's or a step's),
    never through `with:` or a `run:` line."""
    workflow = _workflow()
    assert _by_job(SECRET) == SECRETS_BY_JOB
    assert _by_job(VARIABLE) == VARIABLES_BY_JOB
    outside = {key: value for key, value in workflow.items() if key != "jobs"}
    assert not _references(SECRET, outside), _references(SECRET, outside)
    assert not _references(VARIABLE, outside), _references(VARIABLE, outside)

    text = WORKFLOW.read_text(encoding="utf-8")
    assert "secrets: inherit" not in text
    for expression in EXPRESSION.findall(text):
        for context in ("secrets", "vars"):
            if re.search(rf"\b{context}\b", expression):
                assert re.fullmatch(rf"{context}\.[A-Z][A-Z0-9_]*", expression), (
                    f"${{{{ {expression} }}}} reads {context} other than by one name"
                )

    for name in set(SECRETS_BY_JOB) | set(VARIABLES_BY_JOB):
        job = workflow["jobs"][name]
        job_level = {key: value for key, value in job.items() if key != "steps"}
        assert not _references(SECRET, job_level), name
        outside_env = {
            key: value for key, value in job.items() if key not in ("env", "steps")
        }
        assert not _references(VARIABLE, outside_env), name
        for step in job["steps"]:
            label = step.get("name") or step.get("uses")
            outside_env = {key: value for key, value in step.items() if key != "env"}
            assert not _references(SECRET, outside_env), (name, label)
            assert not _references(VARIABLE, outside_env), (name, label)


def test_the_pypi_build_and_publish_jobs_are_a_pair() -> None:
    """B11. Same condition; the publish job needs the build; the build job
    keeps every check it had and cannot request a token."""
    workflow = _workflow()
    build = workflow["jobs"]["pypi-build"]
    publish = workflow["jobs"]["pypi"]
    condition = "needs.resolve.outputs.ecosystem == 'pypi'"
    assert build.get("if") == condition
    assert publish.get("if") == condition
    assert build.get("needs") == "resolve"
    assert set(publish.get("needs") or []) == {"resolve", "pypi-build"}
    assert build.get("permissions") == {"contents": "read"}
    assert "environment" not in build
    assert publish.get("name") == "publish to PyPI"

    names = [s.get("name") or s.get("uses") for s in build["steps"]]
    order = [
        "Run the SDK's tests",
        "Build",
        "Check the distributions",
        "Built version matches the tag",
        "Record the distributions",
        "Upload the checked distributions",
    ]
    indices = [names.index(n) for n in order]
    assert indices == sorted(indices), f"pypi-build steps out of order: {names}"
    assert "twine check dist/*" in _run_of(build["steps"][names.index(order[2])])


def test_the_pypi_publish_job_runs_only_the_download_the_check_and_the_upload() -> None:
    """B2. Exactly three steps; no checkout, no Python set-up, no install."""
    publish = _workflow()["jobs"]["pypi"]
    steps = publish["steps"]
    assert [s.get("name") for s in steps[:2]] == [
        "Download the checked distributions",
        "Check the distributions against the tag",
    ], [s.get("name") or s.get("uses") for s in steps]
    assert len(steps) == 3, [s.get("name") or s.get("uses") for s in steps]

    download, check, upload = steps
    assert download.get("uses") == "actions/download-artifact@v8"
    assert download.get("with") == {
        "name": "pypi-dist",
        "path": "${{ github.workspace }}/dist",
    }
    assert "run" not in download

    assert "uses" not in check
    assert "working-directory" not in check
    assert _run_of(check).startswith("set -euo pipefail\npython3 - <<'PY'\n")
    assert not INSTALLS_OR_TESTS.search(_run_of(check))

    assert PYPA_PIN.fullmatch(str(upload.get("uses"))), upload.get("uses")
    assert upload.get("with") == {"packages-dir": "dist/"}
    assert set(upload) == {"uses", "with"}


def test_the_pypa_action_is_pinned_to_a_commit_with_its_version() -> None:
    """The publish action runs beside the token: a full commit SHA, not a
    branch, with the version it is in a comment for the reader."""
    lines = [
        line
        for line in WORKFLOW.read_text(encoding="utf-8").splitlines()
        if "pypa/gh-action-pypi-publish@" in line and not line.lstrip().startswith("#")
    ]
    assert len(lines) == 1, lines
    assert re.search(
        r"uses: pypa/gh-action-pypi-publish@[0-9a-f]{40} # v\d+\.\d+\.\d+$", lines[0]
    ), lines[0]


#: (job, action): actions pinned by full commit SHA, with the version in a
#: comment for the reader, like the pypi job's publish action.
PINNED_ACTIONS = [
    ("pypi", "pypa/gh-action-pypi-publish"),
    ("maven", "actions/download-artifact"),
    ("nuget", "actions/download-artifact"),
    ("nuget", "actions/setup-dotnet"),
    ("nuget", "NuGet/login"),
]


@pytest.mark.parametrize(("job", "action"), PINNED_ACTIONS)
def test_these_actions_are_pinned_to_a_commit_with_their_version(
    job: str, action: str
) -> None:
    uses = [
        str(step.get("uses"))
        for step in _workflow()["jobs"][job]["steps"]
        if str(step.get("uses", "")).startswith(f"{action}@")
    ]
    assert len(uses) == 1, (job, uses)
    assert re.fullmatch(rf"{re.escape(action)}@[0-9a-f]{{40}}", uses[0]), uses[0]
    # Two jobs may pin the same commit (maven and nuget both download with
    # actions/download-artifact): every line that names it carries the version.
    lines = [
        line
        for line in WORKFLOW.read_text(encoding="utf-8").splitlines()
        if f"uses: {uses[0]}" in line and not line.lstrip().startswith("#")
    ]
    assert lines, (job, uses[0])
    for line in lines:
        assert re.search(rf"uses: {re.escape(uses[0])} # v\d+\.\d+\.\d+$", line), line


def test_the_publish_job_compares_with_the_digest_the_build_job_recorded() -> None:
    """B4. The expected digest comes from the build job's output, not from a
    file that travels in the same artifact."""
    workflow = _workflow()
    build = workflow["jobs"]["pypi-build"]
    assert build.get("outputs") == {"digest": "${{ steps.record.outputs.digest }}"}
    record = _step("pypi-build", "Record the distributions")
    assert record.get("id") == "record"
    assert "digest=" in _run_of(record)

    check = _step("pypi", "Check the distributions against the tag")
    assert check.get("env") == {
        "NAME": "${{ needs.resolve.outputs.name }}",
        "VERSION": "${{ needs.resolve.outputs.version }}",
        "DIGEST": "${{ needs.pypi-build.outputs.digest }}",
    }
    script = _run_of(check)
    assert 'os.environ["DIGEST"]' in script
    assert "if digest != expected_digest:" in script


def test_the_distributions_are_uploaded_once_and_never_overwritten() -> None:
    """B5. An empty upload fails the build job; an existing artifact of the
    same name is not replaced."""
    upload = _step("pypi-build", "Upload the checked distributions")
    assert upload.get("uses") == "actions/upload-artifact@v7"
    assert upload.get("with") == {
        "name": "pypi-dist",
        "path": "sdk/${{ needs.resolve.outputs.sdk }}/dist/",
        "if-no-files-found": "error",
        "overwrite": False,
    }


# The steps' scripts, run the way the runner runs them: bash -eo pipefail,
# with `env:` resolved from the values given and `python3`/`python` this
# interpreter. A copy of the idea in test_dashboard_deploy_wiring.py's Runner,
# cut down to what these steps use, rather than an import of it: that one
# fakes aws and curl, which nothing here calls.


def _run_step(
    step: dict,
    values: dict[str, str],
    tmp_path: Path,
    cwd: Path,
    runner_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """`runner_env` is what the runner itself sets beside the step's `env:`
    (`GITHUB_SHA`, say)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name in ("python3", "python"):
        if not (bin_dir / name).exists():
            (bin_dir / name).symlink_to(sys.executable)
    output = tmp_path / "github_output"
    output.touch()
    runner_temp = tmp_path / "runner_temp"
    runner_temp.mkdir(exist_ok=True)

    def resolve(match: re.Match) -> str:
        expression = match.group(1)
        assert expression in values, f"no value for ${{{{ {expression} }}}}"
        return values[expression]

    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "GITHUB_OUTPUT": str(output),
        "GITHUB_WORKSPACE": str(tmp_path / "workspace"),
        "RUNNER_TEMP": str(runner_temp),
        "HOME": str(tmp_path),
        **(runner_env or {}),
    }
    for key, value in (step.get("env") or {}).items():
        env[key] = EXPRESSION.sub(resolve, str(value))
    script = tmp_path / "step.sh"
    script.write_text(_run_of(step), encoding="utf-8")
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _outputs(tmp_path: Path) -> dict[str, str]:
    lines = (tmp_path / "github_output").read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


def _dist_files(project: str, version: str) -> dict[str, bytes]:
    stem = re.sub(r"[-_.]+", "_", project).lower()
    return {
        f"{stem}-{version}-py3-none-any.whl": f"wheel {project} {version}".encode(),
        f"{stem}-{version}.tar.gz": f"sdist {project} {version}".encode(),
    }


def _record(tmp_path: Path, files: dict[str, bytes]) -> str:
    """Run the build job's record step over `files`; return its digest."""
    sdk = tmp_path / "sdk"
    (sdk / "dist").mkdir(parents=True)
    for name, data in files.items():
        (sdk / "dist" / name).write_bytes(data)
    result = _run_step(
        _step("pypi-build", "Record the distributions"), {}, tmp_path, sdk
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return _outputs(tmp_path)["digest"]


def _check(
    tmp_path: Path, files: dict[str, bytes], name: str, version: str, digest: str
) -> subprocess.CompletedProcess:
    """Run the publish job's check over `files` in $GITHUB_WORKSPACE/dist."""
    dist = tmp_path / "workspace" / "dist"
    dist.mkdir(parents=True)
    for filename, data in files.items():
        (dist / filename).write_bytes(data)
    return _run_step(
        _step("pypi", "Check the distributions against the tag"),
        {
            "needs.resolve.outputs.name": name,
            "needs.resolve.outputs.version": version,
            "needs.pypi-build.outputs.digest": digest,
        },
        tmp_path,
        tmp_path / "workspace",
    )


@pytest.mark.parametrize("project", ["controlvar", "controlvar-openfeature"])
def test_the_check_accepts_what_the_build_job_recorded(
    project: str, tmp_path: Path
) -> None:
    files = _dist_files(project, "0.1.0")
    digest = _record(tmp_path, files)
    result = _check(tmp_path, files, project, "0.1.0", digest)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"2 file(s) for {project} 0.1.0" in result.stdout


SIBLING_WHEEL = "controlvar_openfeature-0.1.0-py3-none-any.whl"


#: Each case is checked twice over: the build job recording a digest of the
#: changed set (so the digest agrees and only the name, version and file
#: checks stand between it and the upload), and the original set.
REFUSALS = [
    (
        "a same-version wheel of the sibling project",
        lambda f: {**f, SIBLING_WHEEL: b"sibling"},
        f"{SIBLING_WHEEL} is for 'controlvar_openfeature', not 'controlvar'",
    ),
    (
        "a same-version sdist of the sibling project",
        lambda f: {**f, "controlvar_openfeature-0.1.0.tar.gz": b"sibling"},
        "is for 'controlvar_openfeature', not 'controlvar'",
    ),
    (
        "a wheel at another version",
        lambda f: {**f, "controlvar-0.0.9-py3-none-any.whl": b"old"},
        "is version '0.0.9', not '0.1.0'",
    ),
    (
        "a stray file",
        lambda f: {**f, "notes.txt": b"x"},
        "notes.txt is neither a wheel nor an sdist",
    ),
    (
        "a digest file beside the distributions",
        lambda f: {**f, "SHA256SUMS": b"x"},
        "SHA256SUMS is neither a wheel nor an sdist",
    ),
]


@pytest.mark.parametrize("recorded", ["the changed set", "the original set"])
@pytest.mark.parametrize(
    ("case", "change", "refusal"),
    REFUSALS,
    ids=[case for case, _, _ in REFUSALS],
)
def test_the_check_refuses(
    case: str, change, refusal: str, recorded: str, tmp_path: Path
) -> None:
    """Condition 4: each of these must stop the upload, whichever set the
    build job's digest describes."""
    files = _dist_files("controlvar", "0.1.0")
    changed = change(files)
    digest = _record(
        tmp_path / "build", changed if recorded == "the changed set" else files
    )
    result = _check(tmp_path / "publish", changed, "controlvar", "0.1.0", digest)
    assert result.returncode == 1, (case, result.stdout, result.stderr)
    assert refusal in result.stdout, (case, result.stdout)


def test_the_check_refuses_a_byte_changed_after_the_digest_was_recorded(
    tmp_path: Path,
) -> None:
    """B4: the harness flips one byte of the wheel between the two jobs."""
    files = _dist_files("controlvar", "0.1.0")
    digest = _record(tmp_path / "build", files)
    flipped = {
        name: (data[:-1] + bytes([data[-1] ^ 1]) if name.endswith(".whl") else data)
        for name, data in files.items()
    }
    result = _check(tmp_path / "publish", flipped, "controlvar", "0.1.0", digest)
    assert result.returncode == 1, result.stdout
    assert "the downloaded files have digest" in result.stdout, result.stdout
    assert f"the build job recorded {digest}" in result.stdout, result.stdout


def test_the_check_refuses_an_empty_download(tmp_path: Path) -> None:
    """B5: no files is a refusal, not a vacuous pass."""
    digest = _record(tmp_path / "build", _dist_files("controlvar", "0.1.0"))
    result = _check(tmp_path / "publish", {}, "controlvar", "0.1.0", digest)
    assert result.returncode == 1, result.stdout
    assert "nothing was downloaded into" in result.stdout, result.stdout


def test_the_check_refuses_a_missing_digest(tmp_path: Path) -> None:
    files = _dist_files("controlvar", "0.1.0")
    result = _check(tmp_path, files, "controlvar", "0.1.0", "")
    assert result.returncode == 1, result.stdout
    assert "the build job recorded no usable digest" in result.stdout


def test_the_check_compares_the_whole_normalised_name(tmp_path: Path) -> None:
    """`Controlvar.OpenFeature` and `controlvar_openfeature` are one
    project under PEP 503; `controlvar` is a different one."""
    files = _dist_files("controlvar-openfeature", "0.1.0")
    digest = _record(tmp_path, files)
    result = _check(tmp_path, files, "Controlvar.OpenFeature", "0.1.0", digest)
    assert result.returncode == 0, result.stdout + result.stderr

    other = tmp_path / "other"
    other.mkdir()
    result = _check(other, files, "controlvar", "0.1.0", digest)
    assert result.returncode == 1, result.stdout


@pytest.mark.parametrize(
    ("tag", "wheel_version", "ok"),
    [
        ("0.1.0", "0.1.0", True),
        ("1.0.0rc1", "1.0.0rc1", True),
        ("1.0.0-rc.1", "1.0.0rc1", False),
    ],
)
def test_the_build_job_refuses_a_version_not_in_its_pep_440_spelling(
    tag: str, wheel_version: str, ok: bool, tmp_path: Path
) -> None:
    """The publish job compares file names with the tag as strings, so the
    build job refuses a tag it would only refuse after approval."""
    sdk = tmp_path / "sdk"
    (sdk / "dist").mkdir(parents=True)
    (sdk / "dist" / f"controlvar-{wheel_version}-py3-none-any.whl").write_bytes(b"w")
    result = _run_step(
        _step("pypi-build", "Built version matches the tag"),
        {"needs.resolve.outputs.version": tag},
        tmp_path,
        sdk,
    )
    assert (result.returncode == 0) is ok, result.stdout + result.stderr


# --------------------------------------------------------------------------
# #773 and #774, npm: `npm-build` tests and packs with no token; `npm` checks
# the tarball it downloaded against the build job's digest and the tag, then
# publishes it, a prerelease under `next` and a stable version with no --tag
# --------------------------------------------------------------------------

NPM_NAME = "@controlvar/js-sdk"
NPM_FILE = "controlvar-js-sdk-0.1.0.tgz"


def test_the_npm_build_and_publish_jobs_are_a_pair() -> None:
    """Same condition; the publish job needs the build; the build job cannot
    request a token and uses Node 22's own npm, as sdk-unit-tests.yml does."""
    workflow = _workflow()
    build = workflow["jobs"]["npm-build"]
    publish = workflow["jobs"]["npm"]
    condition = "needs.resolve.outputs.ecosystem == 'npm'"
    assert build.get("if") == condition
    assert publish.get("if") == condition
    assert build.get("needs") == "resolve"
    assert set(publish.get("needs") or []) == {"resolve", "npm-build"}
    assert build.get("permissions") == {"contents": "read"}
    assert "environment" not in build
    assert publish.get("name") == "publish to npm"
    assert build.get("outputs") == {
        "file": "${{ steps.record.outputs.file }}",
        "digest": "${{ steps.record.outputs.digest }}",
    }

    setup = [
        s
        for s in build["steps"]
        if str(s.get("uses", "")).startswith("actions/setup-node@")
    ]
    assert len(setup) == 1 and setup[0].get("with") == {"node-version": "22"}, setup
    global_install = re.compile(r"\bnpm\s+(install|i)\b[^\n]*(\s-g\b|--global)")
    found = [s.get("name") for s in build["steps"] if global_install.search(_run_of(s))]
    assert not found, f"npm-build installs a global npm: {found}"


#: The npm publish job, step by step. Nothing else may be added to it.
NPM_PUBLISH_STEPS = [
    "Download the checked tarball",
    "Check the tarball's name and digest",
    "Check the packed package.json against the tag",
    "actions/setup-node@v7",
    "Node and npm are the pinned versions",
    "Publish",
]


def test_the_npm_publish_job_runs_only_its_checks_and_the_publish() -> None:
    """B2. No checkout and no install: the download, the two checks, Node,
    the version check and the publish, in that order."""
    steps = _workflow()["jobs"]["npm"]["steps"]
    assert [s.get("name") or s.get("uses") for s in steps] == NPM_PUBLISH_STEPS

    download, name_check, manifest_check, setup, versions, publish = steps
    assert download.get("uses") == "actions/download-artifact@v8"
    assert download.get("with") == {
        "name": "npm-tarball",
        "path": "${{ github.workspace }}/package",
    }
    assert "run" not in download
    assert set(setup) == {"uses", "with"}
    for step in (name_check, manifest_check, versions, publish):
        assert "uses" not in step, step.get("name")
        assert "working-directory" not in step, step.get("name")
        assert not INSTALLS_OR_TESTS.search(_run_of(step)), step.get("name")
    assert _run_of(name_check).startswith("set -euo pipefail\npython3 - <<'PY'\n")


def test_the_npm_publish_job_sets_no_registry_configuration() -> None:
    """setup-node gets no `registry-url`, so no .npmrc is written for the
    publish, and no package-manager cache. No `NPM_CONFIG_*` or
    `NODE_AUTH_TOKEN` at job or step level."""
    job = _workflow()["jobs"]["npm"]
    setup = [
        s
        for s in job["steps"]
        if str(s.get("uses", "")).startswith("actions/setup-node@")
    ]
    assert len(setup) == 1, setup
    assert setup[0].get("with") == {
        "node-version": "24.21.0",
        "package-manager-cache": False,
    }

    forbidden = re.compile(r"npm_config_|node_auth_token|npmrc|registry", re.IGNORECASE)
    places = [("job env", key) for key in (job.get("env") or {})]
    for step in job["steps"]:
        label = step.get("name") or step.get("uses")
        places += [(f"{label} env", key) for key in (step.get("env") or {})]
        places += [(f"{label} with", key) for key in (step.get("with") or {})]
        places += [(f"{label} run", line) for line in _run_of(step).splitlines()]
    found = [(where, what) for where, what in places if forbidden.search(str(what))]
    assert not found, f"the npm publish job sets registry configuration: {found}"


def test_the_npm_tarball_is_compared_with_the_digest_the_build_job_recorded() -> None:
    """B4. The expected digest and file name come from the build job's
    outputs; the name and version come from `resolve`."""
    record = _step("npm-build", "Record the tarball")
    assert record.get("id") == "record"
    assert record.get("env") == {"TGZ": PACKED_TGZ}
    assert "file=" in _run_of(record) and "digest=" in _run_of(record)

    name_check = _step("npm", "Check the tarball's name and digest")
    assert name_check.get("env") == {
        "FILE": "${{ needs.npm-build.outputs.file }}",
        "DIGEST": "${{ needs.npm-build.outputs.digest }}",
    }
    assert 'os.environ["DIGEST"]' in _run_of(name_check)
    assert "if digest != expected:" in _run_of(name_check)

    manifest_check = _step("npm", "Check the packed package.json against the tag")
    assert manifest_check.get("env") == {
        "NAME": "${{ needs.resolve.outputs.name }}",
        "VERSION": "${{ needs.resolve.outputs.version }}",
        "FILE": "${{ needs.npm-build.outputs.file }}",
    }


def test_the_npm_tarball_is_uploaded_once_and_never_overwritten() -> None:
    """B5."""
    upload = _step("npm-build", "Upload the checked tarball")
    assert upload.get("uses") == "actions/upload-artifact@v7"
    assert upload.get("with") == {
        "name": "npm-tarball",
        "path": PACKED_TGZ,
        "if-no-files-found": "error",
        "overwrite": False,
    }


PUBLISH_CASE = """set -euo pipefail
case "$PRERELEASE" in
  true) npm publish "$TGZ" --provenance --access public --tag next ;;
  false) npm publish "$TGZ" --provenance --access public ;;
  *)
"""


@pytest.mark.regression
def test_a_prerelease_publishes_under_next_and_a_stable_version_passes_no_tag() -> None:
    """B9, #774. On the old file every version was published with no --tag.

    The stable branch is exactly `npm publish "$TGZ" --provenance --access
    public`, `--tag` appears once, as `--tag next` on the prerelease branch,
    and any other value fails."""
    publish = _step("npm", "Publish")
    assert publish.get("env") == {
        "TGZ": DOWNLOADED_TGZ,
        "PRERELEASE": "${{ needs.resolve.outputs.prerelease }}",
    }
    script = _run_of(publish)
    assert script.startswith(PUBLISH_CASE), script
    assert script.count("--tag") == 1, script
    assert script.count("npm publish") == 2, script
    rest = script[len(PUBLISH_CASE) :].splitlines()
    assert rest[-2:] == ["    ;;", "esac"], rest
    assert rest[-3] == "    exit 1", rest


def _write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\n" + body, encoding="utf-8")
    path.chmod(0o755)


PUBLISHED = ["--provenance", "--access", "public"]


@pytest.mark.parametrize(
    ("prerelease", "arguments"),
    [
        ("true", [*PUBLISHED, "--tag", "next"]),
        ("false", PUBLISHED),
        ("", None),
        ("TRUE", None),
        ("next", None),
    ],
)
def test_the_publish_step_passes_the_tag_its_branch_names(
    prerelease: str, arguments: list[str] | None, tmp_path: Path
) -> None:
    """The publish step under bash, with an `npm` that records its arguments,
    one per line. This shows which arguments the step passes; it says nothing
    about how npm treats them."""
    calls = tmp_path / "npm-calls"
    _write_executable(tmp_path / "bin" / "npm", f'printf \'%s\\n\' "$@" >> "{calls}"\n')
    workspace = tmp_path / "workspace"
    result = _run_step(
        _step("npm", "Publish"),
        {
            "github.workspace": str(workspace),
            "needs.npm-build.outputs.file": NPM_FILE,
            "needs.resolve.outputs.prerelease": prerelease,
        },
        tmp_path,
        tmp_path,
    )
    recorded = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    if arguments is None:
        assert result.returncode == 1, result.stdout
        assert "neither a prerelease nor stable" in result.stdout
        assert recorded == []
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert recorded == ["publish", f"{workspace}/package/{NPM_FILE}", *arguments]


@pytest.mark.parametrize(
    ("version", "prerelease"),
    [
        ("0.1.0", "false"),
        ("1.0.0", "false"),
        ("1.0.0+build.5", "false"),
        ("1.0.0+build-5", "false"),
        ("1.0.0-rc.1", "true"),
        ("1.0.0-0", "true"),
        ("2.0.0-beta.2+build.7", "true"),
    ],
)
def test_resolve_classifies_a_prerelease_by_its_version(
    version: str, prerelease: str, tmp_path: Path
) -> None:
    """B8. A `-` once `+build` is removed is a prerelease."""
    step = _step("resolve", "Classify the version")
    assert step.get("env") == {"VERSION": "${{ steps.parse.outputs.version }}"}
    result = _run_step(
        step, {"steps.parse.outputs.version": version}, tmp_path, tmp_path
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _outputs(tmp_path) == {"prerelease": prerelease}


def test_a_hyphen_in_the_sdk_name_does_not_make_a_prerelease(tmp_path: Path) -> None:
    """B8. `sdk/react-native/v<its version>` through resolve's real parse
    step, then the classifier: stable."""
    workflow = _workflow()
    assert workflow["jobs"]["resolve"]["outputs"]["prerelease"] == (
        "${{ steps.classify.outputs.prerelease }}"
    )
    import json

    version = json.loads(
        (SDK_DIR / "react-native" / "package.json").read_text(encoding="utf-8")
    )["version"]
    assert "-" not in version, "pick a stable sdk/react-native version for this test"
    (tmp_path / "parse").mkdir()
    (tmp_path / "classify").mkdir()
    parse = _run_step(
        _step("resolve", "Parse and check"),
        {
            "github.ref_name": f"sdk/react-native/v{version}",
            "github.ref_type": "tag",
            "inputs.sdk": "",
            "inputs.version": "",
        },
        tmp_path / "parse",
        ROOT,
    )
    assert parse.returncode == 0, parse.stdout + parse.stderr
    parsed = _outputs(tmp_path / "parse")
    assert parsed["sdk"] == "react-native" and parsed["version"] == version, parsed

    classify = _run_step(
        _step("resolve", "Classify the version"),
        {"steps.parse.outputs.version": parsed["version"]},
        tmp_path / "classify",
        tmp_path,
    )
    assert classify.returncode == 0, classify.stdout + classify.stderr
    assert _outputs(tmp_path / "classify") == {"prerelease": "false"}


# --------------------------------------------------------------------------
# #1498 (PP-8): a dispatch at a release tag releases that tag and nothing
# else. The `sdk-release` environment admits a run by its ref, so `resolve` is
# what ties the SDK and version a dispatch builds to the tag it runs at.
# --------------------------------------------------------------------------

#: Read from the manifests, so P's version bumps never touch these tests.
JS_VERSION = manifest_version("js")
REACT_VERSION = manifest_version("react")
OPENFEATURE_VERSION = manifest_version("openfeature")
OPENFEATURE_PYTHON_VERSION = manifest_version("openfeature-python")


def _parse(
    tmp_path: Path, ref_type: str, ref_name: str, sdk: str, version: str
) -> subprocess.CompletedProcess:
    """`resolve`'s real parse step, at this tree, with the run's ref and the
    dispatch's inputs (both empty on a tag push)."""
    return _run_step(
        _step("resolve", "Parse and check"),
        {
            "github.ref_name": ref_name,
            "github.ref_type": ref_type,
            "inputs.sdk": sdk,
            "inputs.version": version,
        },
        tmp_path,
        ROOT,
    )


#: (case, tag, inputs.sdk, inputs.version, the tag's SDK, the tag's version).
#: Every case after the first names an SDK at a version its own manifest
#: carries (go has none to compare), so without the comparison `resolve`
#: accepts it and releases the inputs' SDK from the tag's commit.
DISPATCH_REFUSALS = [
    ("java at react's tag", "sdk/react/v0.1.0", "java", "0.1.0", "react", "0.1.0"),
    (
        "another SDK at its own version, at react's tag",
        f"sdk/react/v{REACT_VERSION}",
        "js",
        JS_VERSION,
        "react",
        REACT_VERSION,
    ),
    (
        "an SDK whose name begins the tag's",
        f"sdk/openfeature-python/v{OPENFEATURE_VERSION}",
        "openfeature",
        OPENFEATURE_VERSION,
        "openfeature-python",
        OPENFEATURE_VERSION,
    ),
    (
        "the tag's SDK at another version",
        "sdk/js/v99.99.99",
        "js",
        JS_VERSION,
        "js",
        "99.99.99",
    ),
    (
        "go, which has no manifest version, at another version",
        "sdk/go/v99.99.99",
        "go",
        "0.1.0",
        "go",
        "99.99.99",
    ),
]


@pytest.mark.regression
@pytest.mark.parametrize(
    ("case", "tag", "sdk", "version", "tag_sdk", "tag_version"),
    DISPATCH_REFUSALS,
    ids=[case[0] for case in DISPATCH_REFUSALS],
)
def test_a_dispatch_at_a_release_tag_must_name_that_tags_sdk_and_version(
    case: str,
    tag: str,
    sdk: str,
    version: str,
    tag_sdk: str,
    tag_version: str,
    tmp_path: Path,
) -> None:
    """PP-8. On the old step the inputs won over the tag: `gh workflow run
    sdk-release.yml --ref sdk/react/v0.1.0 -f sdk=java -f version=0.1.0`
    resolved to java, and every case after the first resolved to the inputs'
    SDK and exited 0. Now each is refused before the gate reads a manifest,
    with a message naming the tag's SDK and version and the inputs', and
    `resolve` outputs nothing."""
    result = _parse(tmp_path, "tag", tag, sdk, version)
    assert result.returncode == 1, (case, result.stdout, result.stderr)
    assert (
        f"::error::this run is at the tag {tag} (sdk {tag_sdk}, version "
        f"{tag_version}), but its inputs ask for sdk {sdk}, version {version}. "
        "A dispatch at a release tag must name that tag's own SDK and version"
    ) in result.stdout, (case, result.stdout, result.stderr)
    assert _outputs(tmp_path) == {}, case


#: (case, ref type, ref name, inputs.sdk, inputs.version, released SDK, version)
DISPATCH_ACCEPTED = [
    (
        "a dispatch naming its tag's SDK and version",
        "tag",
        f"sdk/js/v{JS_VERSION}",
        "js",
        JS_VERSION,
        "js",
        JS_VERSION,
    ),
    (
        "the same, with the version spelt as the tag spells it",
        "tag",
        f"sdk/js/v{JS_VERSION}",
        "js",
        f"v{JS_VERSION}",
        "js",
        JS_VERSION,
    ),
    (
        "a hyphenated SDK's dispatch at its own tag",
        "tag",
        f"sdk/openfeature-python/v{OPENFEATURE_PYTHON_VERSION}",
        "openfeature-python",
        OPENFEATURE_PYTHON_VERSION,
        "openfeature-python",
        OPENFEATURE_PYTHON_VERSION,
    ),
    (
        "a dispatch from a branch, as before",
        "branch",
        "main",
        "js",
        JS_VERSION,
        "js",
        JS_VERSION,
    ),
    (
        "a tag push, as before",
        "tag",
        f"sdk/js/v{JS_VERSION}",
        "",
        "",
        "js",
        JS_VERSION,
    ),
]


@pytest.mark.parametrize(
    ("case", "ref_type", "ref_name", "sdk", "version", "released", "at"),
    DISPATCH_ACCEPTED,
    ids=[case[0] for case in DISPATCH_ACCEPTED],
)
def test_resolve_accepts_a_dispatch_at_its_own_tag_a_branch_dispatch_and_a_tag_push(
    case: str,
    ref_type: str,
    ref_name: str,
    sdk: str,
    version: str,
    released: str,
    at: str,
    tmp_path: Path,
) -> None:
    result = _parse(tmp_path, ref_type, ref_name, sdk, version)
    assert result.returncode == 0, (case, result.stdout, result.stderr)
    outputs = _outputs(tmp_path)
    assert (outputs["sdk"], outputs["version"]) == (released, at), (case, outputs)
    assert outputs["ecosystem"] == ecosystems()[released], (case, outputs)


def test_the_tag_check_gates_every_job_of_the_release() -> None:
    """The step the tests above run is `resolve`'s parse step, bound to the
    run's real ref and the dispatch's inputs through `env:` only; neither it
    nor `resolve` can be skipped or allowed to fail; its outputs are the ones
    `resolve` passes on; and every other job needs `resolve`."""
    jobs = _workflow()["jobs"]
    resolve = jobs["resolve"]
    parse = _step("resolve", "Parse and check")
    assert parse.get("id") == "parse"
    assert parse.get("env") == {
        "REF_NAME": "${{ github.ref_name }}",
        "REF_TYPE": "${{ github.ref_type }}",
        "INPUT_SDK": "${{ inputs.sdk }}",
        "INPUT_VERSION": "${{ inputs.version }}",
    }
    assert "${{" not in _run_of(parse)
    for key in ("if", "continue-on-error"):
        assert key not in resolve, key
        assert key not in parse, key
    for key in ("sdk", "version", "ecosystem", "name"):
        assert resolve["outputs"][key] == f"${{{{ steps.parse.outputs.{key} }}}}"

    unguarded = []
    for name, job in jobs.items():
        if name == "resolve":
            continue
        needs = job.get("needs") or []
        if "resolve" not in ([needs] if isinstance(needs, str) else needs):
            unguarded.append(name)
    assert not unguarded, f"jobs that do not need resolve: {unguarded}"


def _tarball(path: Path, manifest: dict) -> bytes:
    """A gzipped tar holding `package/package.json`, the way npm packs."""
    import io
    import json
    import tarfile

    data = json.dumps(manifest).encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("package/package.json")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buffer.getvalue())
    return buffer.getvalue()


def _npm_record(tmp_path: Path, manifest: dict, filename: str = NPM_FILE) -> dict:
    """Run `npm-build`'s record step over a packed tarball; return its outputs."""
    tgz = tmp_path / "pack" / filename
    _tarball(tgz, manifest)
    result = _run_step(
        _step("npm-build", "Record the tarball"),
        {"steps.pack.outputs.tgz": str(tgz)},
        tmp_path,
        tmp_path,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return _outputs(tmp_path)


def _download(tmp_path: Path, files: dict[str, bytes]) -> None:
    folder = tmp_path / "workspace" / "package"
    folder.mkdir(parents=True)
    for name, data in files.items():
        (folder / name).write_bytes(data)


def _npm_name_check(tmp_path: Path, file: str, digest: str):
    return _run_step(
        _step("npm", "Check the tarball's name and digest"),
        {
            "needs.npm-build.outputs.file": file,
            "needs.npm-build.outputs.digest": digest,
        },
        tmp_path,
        tmp_path,
    )


def _npm_manifest_check(tmp_path: Path, name: str, version: str, file: str):
    return _run_step(
        _step("npm", "Check the packed package.json against the tag"),
        {
            "needs.resolve.outputs.name": name,
            "needs.resolve.outputs.version": version,
            "needs.npm-build.outputs.file": file,
        },
        tmp_path,
        tmp_path,
    )


def test_the_npm_checks_accept_what_the_build_job_recorded(tmp_path: Path) -> None:
    """The round trip: record in `npm-build`, both checks in `npm`."""
    manifest = {"name": NPM_NAME, "version": "0.1.0"}
    recorded = _npm_record(tmp_path / "build", manifest)
    assert recorded["file"] == NPM_FILE
    data = (tmp_path / "build" / "pack" / NPM_FILE).read_bytes()

    publish = tmp_path / "publish"
    _download(publish, {NPM_FILE: data})
    result = _npm_name_check(publish, recorded["file"], recorded["digest"])
    assert result.returncode == 0, result.stdout + result.stderr
    result = _npm_manifest_check(publish, NPM_NAME, "0.1.0", recorded["file"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{NPM_NAME} 0.1.0" in result.stdout


NPM_NAME_REFUSALS = [
    (
        "a byte changed after the digest was recorded",
        "flipped",
        NPM_FILE,
        "has digest",
    ),
    ("a second file beside the tarball", "extra", NPM_FILE, "expected exactly"),
    ("nothing downloaded", "none", NPM_FILE, "expected exactly"),
    (
        "another file name than the one recorded",
        "same",
        "other-0.1.0.tgz",
        "expected exactly",
    ),
    ("a name that is a path", "same", "../x.tgz", "no usable file name"),
    ("a name that is not a .tgz", "same", "x.tar", "no usable file name"),
    ("a name with a space", "same", "a b.tgz", "no usable file name"),
    ("a name with a substitution", "same", "$(id).tgz", "no usable file name"),
    ("an empty name", "same", "", "no usable file name"),
    ("no digest", "same", NPM_FILE, "no usable digest"),
]


@pytest.mark.parametrize(
    ("case", "files", "file", "refusal"),
    NPM_NAME_REFUSALS,
    ids=[case for case, _, _, _ in NPM_NAME_REFUSALS],
)
def test_the_npm_name_and_digest_check_refuses(
    case: str, files: str, file: str, refusal: str, tmp_path: Path
) -> None:
    """B4, B5, B6."""
    recorded = _npm_record(tmp_path / "build", {"name": NPM_NAME, "version": "0.1.0"})
    data = (tmp_path / "build" / "pack" / NPM_FILE).read_bytes()
    downloaded = {
        "same": {NPM_FILE: data},
        "flipped": {NPM_FILE: data[:-1] + bytes([data[-1] ^ 1])},
        "extra": {NPM_FILE: data, "notes.txt": b"x"},
        "none": {},
    }[files]
    publish = tmp_path / "publish"
    _download(publish, downloaded)
    digest = "" if case == "no digest" else recorded["digest"]
    result = _npm_name_check(publish, file, digest)
    assert result.returncode == 1, (case, result.stdout, result.stderr)
    assert refusal in result.stdout, (case, result.stdout)


#: Each case is a tarball whose digest the build job recorded, so only the
#: check of the packed package.json against `resolve` stands before publish.
NPM_MANIFEST_REFUSALS = [
    (
        "a prerelease that sets a top-level tag",
        {"name": NPM_NAME, "version": "0.3.0-rc.2", "tag": "latest"},
        "0.3.0-rc.2",
        "the packed package.json sets 'tag'",
    ),
    (
        "a stable version that sets publishConfig",
        {"name": NPM_NAME, "version": "0.1.0", "publishConfig": {"tag": "latest"}},
        "0.1.0",
        "the packed package.json sets 'publishConfig'",
    ),
    (
        "another SDK's name",
        {"name": "@controlvar/openfeature-provider", "version": "0.1.0"},
        "0.1.0",
        f"the tarball is '@controlvar/openfeature-provider', not '{NPM_NAME}'",
    ),
    (
        "another version",
        {"name": NPM_NAME, "version": "0.0.9"},
        "0.1.0",
        "the tarball is version '0.0.9', not '0.1.0'",
    ),
    (
        "no name",
        {"version": "0.1.0"},
        "0.1.0",
        f"the tarball is None, not '{NPM_NAME}'",
    ),
]


@pytest.mark.parametrize(
    ("case", "manifest", "version", "refusal"),
    NPM_MANIFEST_REFUSALS,
    ids=[case for case, _, _, _ in NPM_MANIFEST_REFUSALS],
)
def test_the_npm_manifest_check_refuses(
    case: str, manifest: dict, version: str, refusal: str, tmp_path: Path
) -> None:
    """The publish job checks the package it uploads against the tag, and
    refuses a manifest that sets publish configuration."""
    recorded = _npm_record(tmp_path / "build", manifest)
    data = (tmp_path / "build" / "pack" / NPM_FILE).read_bytes()
    publish = tmp_path / "publish"
    _download(publish, {NPM_FILE: data})

    result = _npm_name_check(publish, recorded["file"], recorded["digest"])
    assert result.returncode == 0, result.stdout + result.stderr
    result = _npm_manifest_check(publish, NPM_NAME, version, recorded["file"])
    assert result.returncode == 1, (case, result.stdout, result.stderr)
    assert refusal in result.stdout, (case, result.stdout)


@pytest.mark.parametrize(
    ("node", "npm", "ok"),
    [
        ("v24.21.0", "11.19.0", True),
        ("v22.14.0", "11.19.0", False),
        ("v24.21.0", "11.5.0", False),
        ("v24.21.0", "", False),
    ],
)
def test_the_npm_publish_job_checks_node_and_npm(
    node: str, npm: str, ok: bool, tmp_path: Path
) -> None:
    for tool, version in (("node", node), ("npm", npm)):
        _write_executable(tmp_path / "bin" / tool, f"printf '%s\\n' '{version}'\n")
    result = _run_step(
        _step("npm", "Node and npm are the pinned versions"), {}, tmp_path, tmp_path
    )
    assert (result.returncode == 0) is ok, result.stdout + result.stderr
