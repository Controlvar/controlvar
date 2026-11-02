"""sdk-release.yml's NuGet jobs, `nuget-build` and `nuget`.

`nuget-build` runs the .NET SDK's tests on every leg, packs the one project
the release publishes, checks the package's contents and its README, and
checks that a consumer restores it from the package alone. It holds no
token. `nuget` holds the `sdk-release` environment and the OIDC token, has no
checkout, checks the package against the build job's digest and the tag,
exchanges the token for a short-lived API key with NuGet/login (pinned to a
commit) and pushes the package, with no --skip-duplicate.

The steps' scripts run here the way the runner runs them, through
test_sdk_release_wiring.py's `_run_step`, on synthetic packages and with a
fake `dotnet`. Nothing here runs dotnet or reaches a registry: the steps that
need the real toolchain run in CI (sdk-unit-tests.yml) and in DR-N.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

from backend.tests.smoke.test_sdk_release_wiring import (
    INSTALLS_OR_TESTS,
    ROOT,
    WORKFLOW,
    _outputs,
    _run_of,
    _run_step,
    _step,
    _workflow,
    _write_executable,
    ecosystems,
)

pytestmark = pytest.mark.smoke

DOTNET = ROOT / "sdk" / "dotnet"
PROJECT = DOTNET / "src" / "Controlvar" / "Controlvar.csproj"
TESTS = DOTNET / "tests" / "Controlvar.Tests" / "Controlvar.Tests.csproj"
NETSTANDARD_TESTS = (
    DOTNET
    / "tests"
    / "Controlvar.Tests.NetStandard"
    / "Controlvar.Tests.NetStandard.csproj"
)
NAME = "Controlvar.SDK"
VERSION = "0.1.0"
FILE = f"{NAME}.{VERSION}.nupkg"
SHA = "3d81f2763181d5a8f11b319a22a36ea3cd4fe5b3"
NUGET_LOGIN = "NuGet/login@8d196754b4036150537f80ac539e15c2f1028841 # v1.2.0"
PUSH = 'dotnet nuget push "$NUPKG" --api-key "$NUGET_API_KEY" --source https://api.nuget.org/v3/index.json'
PINNED_SDK = "10.0.403"


def _load(name: str):
    spec = spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _labels(job: str) -> list[str]:
    return [s.get("name") or s.get("uses") for s in _workflow()["jobs"][job]["steps"]]


def _job_text(job: str) -> str:
    """The workflow's own lines for one job, comments included."""
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    start = lines.index(f"  {job}:")
    end = next(
        (
            i
            for i in range(start + 1, len(lines))
            if re.match(r"^  [a-z][\w-]*:$", lines[i])
        ),
        len(lines),
    )
    return "\n".join(lines[start:end])


# --------------------------------------------------------------------------
# The jobs' shape
# --------------------------------------------------------------------------

NUGET_BUILD_STEPS = [
    "actions/checkout@v7",
    "actions/setup-dotnet@v6",
    "Run the SDK's tests",
    "Pack",
    "Check the package",
    "Check the packed README",
    "A consumer restores the package alone",
    "Record the package",
    "Upload the checked package",
]

#: The `nuget` job, step by step. Nothing else may be added to it.
NUGET_STEPS = [
    "Download the checked package",
    "Check the package's name and digest",
    "Check the packed nuspec against the tag",
    "actions/setup-dotnet@a98b56852c35b8e3190ac28c8c2271da59106c68",
    "The .NET SDK is the pinned version",
    "The NuGet user is set",
    "Exchange the OIDC token for a NuGet API key",
    "Push",
]


def test_the_nuget_build_and_publish_jobs_are_a_pair() -> None:
    """Same condition; the publish job needs the build; the build job holds
    no token and no environment and runs its steps in this order."""
    jobs = _workflow()["jobs"]
    build, publish = jobs["nuget-build"], jobs["nuget"]
    condition = "needs.resolve.outputs.ecosystem == 'nuget'"
    assert build.get("if") == condition
    assert publish.get("if") == condition
    assert build.get("needs") == "resolve"
    assert set(publish.get("needs") or []) == {"resolve", "nuget-build"}
    assert build.get("permissions") == {"contents": "read"}
    assert "environment" not in build and "env" not in build
    assert build.get("outputs") == {
        "file": "${{ steps.record.outputs.file }}",
        "digest": "${{ steps.record.outputs.digest }}",
    }
    assert _labels("nuget-build") == NUGET_BUILD_STEPS
    setup = build["steps"][1]
    assert setup.get("with") == {"dotnet-version": "8.0.x\n10.0.x\n"}

    assert publish.get("name") == "publish to NuGet"
    assert publish.get("environment") == {
        "name": "sdk-release",
        "url": "https://www.nuget.org/packages/${{ needs.resolve.outputs.name }}",
    }
    assert publish.get("permissions") == {"contents": "read", "id-token": "write"}
    assert publish.get("env") == {"NUGET_USER": "${{ vars.NUGET_USER }}"}


def test_the_nuget_publish_job_runs_only_its_checks_the_login_and_the_push() -> None:
    """No checkout and nothing that installs, tests or builds."""
    steps = _workflow()["jobs"]["nuget"]["steps"]
    assert _labels("nuget") == NUGET_STEPS
    for step in steps:
        assert not str(step.get("uses", "")).startswith("actions/checkout@"), step
        assert not INSTALLS_OR_TESTS.search(_run_of(step)), step.get("name")
        assert "continue-on-error" not in step, step.get("name") or step.get("uses")
        assert "working-directory" not in step, step.get("name")
    download = steps[0]
    assert download.get("with") == {
        "name": "nuget-package",
        "path": "${{ github.workspace }}/package",
    }


@pytest.mark.regression
def test_every_action_in_the_nuget_job_is_pinned_to_a_commit() -> None:
    """V28. The job holds the OIDC token and CI never runs it, so each action
    is a full commit SHA with its version beside it, and NuGet/login is the
    commit its v1.2.0 tag names."""
    uses = [line.strip() for line in _job_text("nuget").splitlines() if "uses:" in line]
    assert len(uses) == 3, uses
    for line in uses:
        assert re.fullmatch(
            r"(?:- )?uses: [\w.-]+/[\w.-]+@[0-9a-f]{40} # v\d+\.\d+\.\d+", line
        ), line
    assert f"uses: {NUGET_LOGIN}" in uses
    login = _step("nuget", "Exchange the OIDC token for a NuGet API key")
    assert login.get("id") == "login"
    assert login.get("with") == {"user": "${{ env.NUGET_USER }}"}
    assert set(login) == {"name", "id", "uses", "with"}


@pytest.mark.regression
def test_a_duplicate_push_fails() -> None:
    """V29. `--skip-duplicate` would turn a version nuget.org already has into
    a green run that published nothing; it appears on no line of the
    workflow but a comment, and the push command is exactly this."""
    found = [
        line
        for line in WORKFLOW.read_text(encoding="utf-8").splitlines()
        if "skip-duplicate" in line and not line.lstrip().startswith("#")
    ]
    assert not found, found
    push = _step("nuget", "Push")
    lines = [
        line.strip()
        for line in _run_of(push).splitlines()
        if "dotnet nuget push" in line
    ]
    assert lines == [PUSH]
    assert push.get("env") == {
        "NUPKG": "${{ github.workspace }}/package/${{ needs.nuget-build.outputs.file }}",
        "NUGET_API_KEY": "${{ steps.login.outputs.NUGET_API_KEY }}",
    }


def test_the_push_masks_the_key_before_it_uses_it(tmp_path: Path) -> None:
    """The push step under bash with a `dotnet` that records its arguments:
    the mask is the first thing it prints, and dotnet gets the downloaded
    file, the key and nuget.org's index, nothing else."""
    calls = tmp_path / "dotnet-calls"
    _write_executable(
        tmp_path / "bin" / "dotnet", f'printf \'%s\\n\' "$@" >> "{calls}"\n'
    )
    (tmp_path / "runner_temp" / "push").mkdir(parents=True)
    workspace = tmp_path / "workspace"
    key = "oy2-temporary-key-value"
    result = _run_step(
        _step("nuget", "Push"),
        {
            "github.workspace": str(workspace),
            "needs.nuget-build.outputs.file": FILE,
            "steps.login.outputs.NUGET_API_KEY": key,
        },
        tmp_path,
        tmp_path,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines()[0] == f"::add-mask::{key}"
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "nuget",
        "push",
        f"{workspace}/package/{FILE}",
        "--api-key",
        key,
        "--source",
        "https://api.nuget.org/v3/index.json",
    ]


def test_the_push_refuses_an_empty_key(tmp_path: Path) -> None:
    calls = tmp_path / "dotnet-calls"
    _write_executable(
        tmp_path / "bin" / "dotnet", f'printf \'%s\\n\' "$@" >> "{calls}"\n'
    )
    result = _run_step(
        _step("nuget", "Push"),
        {
            "github.workspace": str(tmp_path / "workspace"),
            "needs.nuget-build.outputs.file": FILE,
            "steps.login.outputs.NUGET_API_KEY": "",
        },
        tmp_path,
        tmp_path,
    )
    assert result.returncode == 1, result.stdout
    assert "the login step produced no API key" in result.stdout
    assert not calls.exists()


@pytest.mark.parametrize(("user", "ok"), [("controlvar", True), ("", False)])
def test_the_publish_job_refuses_an_empty_nuget_user(
    user: str, ok: bool, tmp_path: Path
) -> None:
    result = _run_step(
        _step("nuget", "The NuGet user is set"),
        {},
        tmp_path,
        tmp_path,
        runner_env={"NUGET_USER": user},
    )
    assert (result.returncode == 0) is ok, result.stdout + result.stderr
    if not ok:
        assert "NUGET_USER is empty in the sdk-release environment" in result.stdout


@pytest.mark.parametrize(
    ("version", "ok"),
    [(PINNED_SDK, True), ("10.0.500", False), ("8.0.400", False), ("", False)],
)
def test_the_publish_job_checks_the_dotnet_sdk(
    version: str, ok: bool, tmp_path: Path
) -> None:
    """The SDK set up is the one the check pins, in a directory whose
    global.json refuses any other."""
    setup = _workflow()["jobs"]["nuget"]["steps"][3]
    assert setup.get("with") == {"dotnet-version": PINNED_SDK}
    step = _step("nuget", "The .NET SDK is the pinned version")
    assert _run_of(step).count(PINNED_SDK) == 3
    _write_executable(tmp_path / "bin" / "dotnet", f"printf '%s\\n' '{version}'\n")
    result = _run_step(step, {}, tmp_path, tmp_path)
    assert (result.returncode == 0) is ok, result.stdout + result.stderr
    global_json = tmp_path / "runner_temp" / "push" / "global.json"
    assert '"rollForward": "disable"' in global_json.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# V24: the package's contents (nuget-build's "Check the package")
# --------------------------------------------------------------------------


def _nuspec(
    *,
    id_: str = NAME,
    version: str = VERSION,
    commit: str = SHA,
    licence: str = '<license type="expression">MIT</license>',
    readme: str = "<readme>README.md</readme>",
    project_url: str = "https://github.com/Controlvar/controlvar/tree/main/sdk/dotnet#readme",
    groups: str = (
        '<group targetFramework="net8.0" />'
        '<group targetFramework=".NETStandard2.1">'
        '<dependency id="System.Text.Json" version="8.0.6" exclude="Build,Analyzers" />'
        "</group>"
    ),
) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://schemas.microsoft.com/packaging/2013/05/nuspec.xsd"><metadata>'
        f"<id>{id_}</id><version>{version}</version><authors>Controlvar</authors>{licence}"
        f"{readme}<projectUrl>{project_url}</projectUrl><description>d</description>"
        f'<repository type="git" url="https://github.com/Controlvar/controlvar" commit="{commit}" />'
        f"<dependencies>{groups}</dependencies></metadata></package>"
    )


ENTRIES = {
    "_rels/.rels": b"rels",
    "[Content_Types].xml": b"types",
    "README.md": b"# Controlvar.SDK\n",
    "lib/net8.0/Controlvar.dll": b"net8",
    "lib/net8.0/Controlvar.xml": b"<doc/>",
    "lib/netstandard2.1/Controlvar.dll": b"ns21",
    "lib/netstandard2.1/Controlvar.xml": b"<doc/>",
    "package/services/metadata/core-properties/nuget.psmdcp": b"props",
}


def _package(
    path: Path, nuspec: str | None = None, entries: dict | None = None
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{NAME}.nuspec", nuspec if nuspec is not None else _nuspec())
        for name, data in (ENTRIES if entries is None else entries).items():
            archive.writestr(name, data)
    return path


def _check_package(tmp_path: Path, package: Path, version: str = VERSION):
    return _run_step(
        _step("nuget-build", "Check the package"),
        {
            "steps.pack.outputs.nupkg": str(package),
            "needs.resolve.outputs.name": NAME,
            "needs.resolve.outputs.version": version,
        },
        tmp_path,
        ROOT,
        runner_env={"GITHUB_SHA": SHA},
    )


def test_the_package_check_accepts_what_dotnet_pack_writes(tmp_path: Path) -> None:
    """The layout of a real `dotnet pack` of this project (.NET 10 SDK)."""
    result = _check_package(tmp_path, _package(tmp_path / FILE))
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{FILE}: {NAME} {VERSION}, commit {SHA}" in result.stdout


#: (case, nuspec changes, entries, file name, the refusal).
PACKAGE_REFUSALS = [
    (
        "another id",
        {"id_": "Controlvar.Sdk2"},
        None,
        FILE,
        "the nuspec's id is 'Controlvar.Sdk2'",
    ),
    (
        "another version",
        {"version": "0.1.1"},
        None,
        FILE,
        "the nuspec's version is '0.1.1'",
    ),
    (
        "a file named for another version",
        {},
        None,
        f"{NAME}.0.1.1.nupkg",
        f"not {FILE}",
    ),
    (
        "an extra target framework",
        {},
        {**ENTRIES, "lib/net9.0/Controlvar.dll": b"net9"},
        FILE,
        "unexpected entry: lib/net9.0/Controlvar.dll",
    ),
    (
        "a missing build",
        {},
        {k: v for k, v in ENTRIES.items() if not k.startswith("lib/netstandard2.1/")},
        FILE,
        "the package is missing lib/netstandard2.1/Controlvar.dll",
    ),
    (
        "no System.Text.Json for netstandard2.1",
        {
            "groups": '<group targetFramework="net8.0" /><group targetFramework=".NETStandard2.1" />'
        },
        None,
        FILE,
        "the nuspec's dependencies are",
    ),
    (
        "a dependency on net8.0",
        {
            "groups": (
                '<group targetFramework="net8.0"><dependency id="System.Text.Json" version="8.0.6" /></group>'
                '<group targetFramework=".NETStandard2.1">'
                '<dependency id="System.Text.Json" version="8.0.6" /></group>'
            )
        },
        None,
        FILE,
        "the nuspec's dependencies are",
    ),
    (
        "another commit",
        {"commit": "0" * 40},
        None,
        FILE,
        f"the nuspec's repository commit is '{'0' * 40}', not this run's {SHA}",
    ),
    ("no README", {"readme": ""}, None, FILE, "the nuspec's readme is None"),
    (
        "another licence",
        {"licence": '<license type="expression">Apache-2.0</license>'},
        None,
        FILE,
        "the nuspec's licence is not the expression MIT",
    ),
    (
        "a stray file",
        {},
        {**ENTRIES, "tools/install.ps1": b"x"},
        FILE,
        "unexpected entry: tools/install.ps1",
    ),
    (
        "symbols in the package",
        {},
        {**ENTRIES, "lib/net8.0/Controlvar.pdb": b"pdb"},
        FILE,
        "unexpected entry: lib/net8.0/Controlvar.pdb",
    ),
]


@pytest.mark.parametrize(
    ("case", "changes", "entries", "file", "refusal"),
    PACKAGE_REFUSALS,
    ids=[c[0] for c in PACKAGE_REFUSALS],
)
def test_the_package_check_refuses(
    case: str, changes: dict, entries, file: str, refusal: str, tmp_path: Path
) -> None:
    package = _package(tmp_path / "pack" / file, _nuspec(**changes), entries)
    result = _check_package(tmp_path, package)
    assert result.returncode == 1, (case, result.stdout, result.stderr)
    assert refusal in result.stdout, (case, result.stdout)


def _csproj_properties(path: Path) -> dict[str, str]:
    root = ET.parse(path).getroot()
    return {
        child.tag: (child.text or "").strip()
        for group in root.iter("PropertyGroup")
        for child in group
    }


def test_the_package_check_expects_what_the_project_declares() -> None:
    """FRAMEWORKS and the two links in the check are the project's own: its
    target frameworks, each with the packages it references there, and its
    PackageProjectUrl and RepositoryUrl. A target added to the project (or a
    dependency bumped by Dependabot) fails here, on the pull request, not at
    the release."""
    step = _step("nuget-build", "Check the package")
    env = step["env"]
    properties = _csproj_properties(PROJECT)
    frameworks = properties["TargetFrameworks"].split(";")
    assert frameworks == ["netstandard2.1", "net8.0"], frameworks

    root = ET.parse(PROJECT).getroot()
    dependencies: dict[str, list[str]] = {f: [] for f in frameworks}
    for group in root.iter("ItemGroup"):
        condition = group.get("Condition", "")
        match = re.fullmatch(r"'\$\(TargetFramework\)' == '([\w.]+)'", condition)
        targets = [match.group(1)] if match else frameworks
        assert match or not condition, condition
        for reference in group.iter("PackageReference"):
            for target in targets:
                dependencies[target].append(
                    f"{reference.get('Include')}@{reference.get('Version')}"
                )
    declared = sorted(
        f"{f}:{','.join(sorted(deps))}" if deps else f
        for f, deps in dependencies.items()
    )
    assert sorted(env["FRAMEWORKS"].split()) == declared
    assert env["PROJECT_URL"] == properties["PackageProjectUrl"]
    assert env["REPOSITORY_URL"] == properties["RepositoryUrl"]
    assert properties["PackageReadmeFile"] == "README.md"
    assert properties["PublishRepositoryUrl"] == "true"
    assert properties["DebugType"] == "embedded"


def test_the_release_packs_the_project_the_version_gate_reads() -> None:
    """One project, the one check_sdk_version.py reads for the tag."""
    gate = _load("check_sdk_version")
    assert gate.NUGET_PROJECTS == {"dotnet": "src/Controlvar/Controlvar.csproj"}
    assert sorted(s for s, e in ecosystems().items() if e == "nuget") == ["dotnet"]
    pack = _run_of(_step("nuget-build", "Pack"))
    assert (
        "dotnet pack sdk/dotnet/src/Controlvar/Controlvar.csproj -c Release --nologo "
        '-o "$dest" -p:ContinuousIntegrationBuild=true'
    ) in pack


# --------------------------------------------------------------------------
# V23: check_sdk_version.py reads the project
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "edit", "refusal"),
    [
        (
            "a second <Version>",
            lambda t: t.replace(
                "</PropertyGroup>", "<Version>9.9.9</Version></PropertyGroup>", 1
            ),
            "must set <Version> exactly once",
        ),
        (
            "no <PackageId>",
            lambda t: re.sub(r"<PackageId>[^<]*</PackageId>", "", t),
            "must set <PackageId> exactly once",
        ),
        (
            "a <VersionSuffix>",
            lambda t: t.replace(
                "</PropertyGroup>",
                "<VersionSuffix>rc1</VersionSuffix></PropertyGroup>",
                1,
            ),
            "sets <VersionSuffix>",
        ),
        (
            "a conditional <Version>",
            lambda t: t.replace("<Version>", "<Version Condition=\"'$(X)' == ''\">"),
            "sets <Version> under a Condition",
        ),
    ],
)
def test_the_version_gate_refuses_an_ambiguous_project(
    case: str, edit, refusal: str, tmp_path: Path
) -> None:
    gate = _load("check_sdk_version")
    project = tmp_path / "sdk" / "dotnet" / "src" / "Controlvar" / "Controlvar.csproj"
    project.parent.mkdir(parents=True)
    text = PROJECT.read_text(encoding="utf-8")
    edited = edit(text)
    assert edited != text, case
    project.write_text(edited, encoding="utf-8")
    with pytest.raises(SystemExit) as raised:
        gate.read_nuget_version("dotnet", tmp_path / "sdk" / "dotnet")
    assert refusal in str(raised.value), (case, raised.value)


def test_the_version_gate_reads_the_project() -> None:
    gate = _load("check_sdk_version")
    version, name = gate.read_nuget_version("dotnet", DOTNET)
    assert name == NAME
    assert version == _csproj_properties(PROJECT)["Version"]
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "check_sdk_version.py"),
            "dotnet",
            "--print-name",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert (result.returncode, result.stdout.strip()) == (0, NAME), result.stderr


# --------------------------------------------------------------------------
# The publish job's checks of the downloaded package
# --------------------------------------------------------------------------


def _record(tmp_path: Path, package: Path) -> dict[str, str]:
    result = _run_step(
        _step("nuget-build", "Record the package"),
        {"steps.pack.outputs.nupkg": str(package)},
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


def _name_check(tmp_path: Path, file: str, digest: str):
    return _run_step(
        _step("nuget", "Check the package's name and digest"),
        {
            "needs.nuget-build.outputs.file": file,
            "needs.nuget-build.outputs.digest": digest,
        },
        tmp_path,
        tmp_path,
    )


def _nuspec_check(tmp_path: Path, name: str, version: str, file: str):
    return _run_step(
        _step("nuget", "Check the packed nuspec against the tag"),
        {
            "needs.resolve.outputs.name": name,
            "needs.resolve.outputs.version": version,
            "needs.nuget-build.outputs.file": file,
        },
        tmp_path,
        tmp_path,
    )


def test_the_publish_checks_accept_what_the_build_job_recorded(tmp_path: Path) -> None:
    package = _package(tmp_path / "build" / "pack" / FILE)
    recorded = _record(tmp_path / "build", package)
    assert recorded["file"] == FILE
    assert recorded["digest"] == hashlib.sha256(package.read_bytes()).hexdigest()
    publish = tmp_path / "publish"
    _download(publish, {FILE: package.read_bytes()})
    result = _name_check(publish, recorded["file"], recorded["digest"])
    assert result.returncode == 0, result.stdout + result.stderr
    result = _nuspec_check(publish, NAME, VERSION, recorded["file"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{NAME} {VERSION}" in result.stdout


NAME_REFUSALS = [
    ("a byte changed after the digest was recorded", "flipped", FILE, "has digest"),
    ("a second file beside the package", "extra", FILE, "expected exactly"),
    ("nothing downloaded", "none", FILE, "expected exactly"),
    (
        "another file name than the one recorded",
        "same",
        "Other.0.1.0.nupkg",
        "expected exactly",
    ),
    ("a name that is a path", "same", "../x.nupkg", "no usable file name"),
    ("a name that is not a .nupkg", "same", "x.snupkg", "no usable file name"),
    ("a name with a substitution", "same", "$(id).nupkg", "no usable file name"),
    ("no digest", "same", FILE, "no usable digest"),
]


@pytest.mark.parametrize(
    ("case", "files", "file", "refusal"),
    NAME_REFUSALS,
    ids=[c[0] for c in NAME_REFUSALS],
)
def test_the_name_and_digest_check_refuses(
    case: str, files: str, file: str, refusal: str, tmp_path: Path
) -> None:
    package = _package(tmp_path / "build" / "pack" / FILE)
    recorded = _record(tmp_path / "build", package)
    data = package.read_bytes()
    downloaded = {
        "same": {FILE: data},
        "flipped": {FILE: data[:-1] + bytes([data[-1] ^ 1])},
        "extra": {FILE: data, "notes.txt": b"x"},
        "none": {},
    }[files]
    publish = tmp_path / "publish"
    _download(publish, downloaded)
    digest = "" if case == "no digest" else recorded["digest"]
    result = _name_check(publish, file, digest)
    assert result.returncode == 1, (case, result.stdout, result.stderr)
    assert refusal in result.stdout, (case, result.stdout)


@pytest.mark.parametrize(
    ("case", "nuspec", "name", "version", "refusal"),
    [
        (
            "another id",
            _nuspec(id_="Other.SDK"),
            NAME,
            VERSION,
            "the package is 'Other.SDK', not",
        ),
        (
            "another version",
            _nuspec(version="0.0.9"),
            NAME,
            VERSION,
            "the package is version '0.0.9'",
        ),
        (
            "the tag resolved no name",
            _nuspec(),
            "",
            VERSION,
            "the tag resolved no name",
        ),
    ],
)
def test_the_nuspec_check_refuses(
    case: str, nuspec: str, name: str, version: str, refusal: str, tmp_path: Path
) -> None:
    package = _package(tmp_path / "workspace" / "package" / FILE, nuspec)
    assert package.is_file()
    result = _nuspec_check(tmp_path, name, version, FILE)
    assert result.returncode == 1, (case, result.stdout)
    assert refusal in result.stdout, (case, result.stdout)


# --------------------------------------------------------------------------
# V25: the test legs
# --------------------------------------------------------------------------


def _trx(
    path: Path,
    assembly: str,
    framework: str,
    total: int,
    passed: int,
    outcome: str = "Completed",
) -> None:
    storage = f"/w/sdk/dotnet/tests/{assembly}/bin/release/{framework}/{assembly}.dll"
    path.parent.mkdir(parents=True, exist_ok=True)
    tests = "".join(
        f'<UnitTest name="t{i}" storage="{storage}" id="{i}" />'
        for i in range(max(total, 1))
    )
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<TestRun xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010">'
        f"<TestDefinitions>{tests}</TestDefinitions>"
        f'<ResultSummary outcome="{outcome}"><Counters total="{total}" executed="{total}" '
        f'passed="{passed}" failed="{total - passed}" error="0" timeout="0" aborted="0" />'
        "</ResultSummary></TestRun>",
        encoding="utf-8",
    )


LEGS = [
    ("controlvar.tests", "net8.0"),
    ("controlvar.tests", "net10.0"),
    ("controlvar.tests.netstandard", "net8.0"),
]


def _legs(
    tmp_path: Path, totals=(94, 94, 94), passed=None, legs=LEGS
) -> subprocess.CompletedProcess:
    for index, ((assembly, framework), total) in enumerate(zip(legs, totals)):
        done = total if passed is None else passed[index]
        _trx(tmp_path / f"run_{index}.trx", assembly, framework, total, done)
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "check_dotnet_test_legs.py"),
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
    )


def test_the_legs_check_accepts_three_equal_runs(tmp_path: Path) -> None:
    result = _legs(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "controlvar.tests.netstandard on net8.0: 94 of 94 passed" in result.stdout


@pytest.mark.parametrize(
    ("case", "kwargs", "refusal"),
    [
        (
            "net8.0 dropped from the tests",
            {"legs": LEGS[1:], "totals": (94, 94)},
            "missing [('controlvar.tests', 'net8.0')]",
        ),
        (
            "the netstandard leg dropped",
            {"legs": LEGS[:2], "totals": (94, 94)},
            "missing [('controlvar.tests.netstandard', 'net8.0')]",
        ),
        (
            "a leg that ran fewer tests",
            {"totals": (94, 94, 60)},
            "the legs ran different numbers of tests",
        ),
        ("legs that ran nothing", {"totals": (0, 0, 0)}, "0 of 0 tests passed"),
        ("a failed test", {"passed": (94, 93, 94)}, "'failed': 1"),
        (
            "an unknown leg",
            {"legs": [*LEGS, ("controlvar.tests", "net9.0")], "totals": (94,) * 4},
            "legs this check does not know",
        ),
        ("a leg twice", {"legs": [*LEGS, LEGS[0]], "totals": (94,) * 4}, "ran twice"),
    ],
)
def test_the_legs_check_refuses(
    case: str, kwargs: dict, refusal: str, tmp_path: Path
) -> None:
    result = _legs(tmp_path, **kwargs)
    assert result.returncode == 1, (case, result.stdout)
    assert refusal in result.stderr, (case, result.stderr)


def test_the_legs_check_refuses_an_empty_directory(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "check_dotnet_test_legs.py"),
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1 and "no .trx file" in result.stderr


def test_the_legs_are_the_test_projects() -> None:
    """LEGS is what the two test projects run: Controlvar.Tests on each of its
    target frameworks, and Controlvar.Tests.NetStandard on its one, against
    the SDK's netstandard2.1 build (PP-11). The SDK targets exactly
    netstandard2.1 and net8.0 (P-M12: net6.0 removed)."""
    legs = _load("check_dotnet_test_legs").LEGS
    tests = _csproj_properties(TESTS)["TargetFrameworks"].split(";")
    netstandard = _csproj_properties(NETSTANDARD_TESTS)
    assert sorted(legs) == sorted(
        [("controlvar.tests", f) for f in tests]
        + [("controlvar.tests.netstandard", netstandard["TargetFramework"])]
    )
    assert tests == ["net8.0", "net10.0"]
    assert "CONTROLVAR_NETSTANDARD_LEG" in netstandard["DefineConstants"]
    references = [
        (r.get("Include"), r.get("SetTargetFramework"))
        for r in ET.parse(NETSTANDARD_TESTS).getroot().iter("ProjectReference")
    ]
    assert references == [
        ("../../src/Controlvar/Controlvar.csproj", "TargetFramework=netstandard2.1")
    ]
    assert _csproj_properties(PROJECT)["TargetFrameworks"] == "netstandard2.1;net8.0"
    solution = (DOTNET / "Controlvar.sln").read_text(encoding="utf-8")
    assert (
        r"tests\Controlvar.Tests.NetStandard\Controlvar.Tests.NetStandard.csproj"
        in solution
    )


def test_both_workflows_check_the_legs_after_the_tests() -> None:
    """nuget-build and the SDK Unit Tests dotnet leg run every test with a TRX
    logger, then the legs check over that directory, with nothing between."""
    build = _run_of(_step("nuget-build", "Run the SDK's tests")).splitlines()
    assert build[1:] == [
        'dotnet test sdk/dotnet/Controlvar.sln -c Release --nologo --logger trx --results-directory "$RUNNER_TEMP/trx"',
        'python3 scripts/check_dotnet_test_legs.py "$RUNNER_TEMP/trx"',
    ]
    unit = (ROOT / ".github" / "workflows" / "sdk-unit-tests.yml").read_text(
        encoding="utf-8"
    )
    lines = [line.strip() for line in unit.splitlines()]
    test = lines.index(
        'dotnet test sdk/dotnet/Controlvar.sln --nologo --verbosity minimal --logger trx --results-directory "$RUNNER_TEMP/trx"'
    )
    assert (
        lines[test + 1]
        == 'python3 scripts/check_dotnet_test_legs.py "$RUNNER_TEMP/trx"'
    )


# --------------------------------------------------------------------------
# The packed README gate (PP-1, EMP-P-3): every SDK whose build job runs it
# is classified
# --------------------------------------------------------------------------


def test_every_sdk_whose_build_runs_the_readme_gate_has_rules() -> None:
    """The gate refuses an SDK it has no rules for, so a build job that runs
    it for an unclassified SDK would fail at the release. Each ecosystem whose
    build job runs the gate has every one of its SDKs in the gate's rules."""
    gate = _load("check_packaged_readme")
    jobs = _workflow()["jobs"]
    gated = set()
    for job in jobs.values():
        if any(
            "scripts/check_packaged_readme.py" in _run_of(s)
            for s in job.get("steps", [])
        ):
            match = re.fullmatch(
                r"needs\.resolve\.outputs\.ecosystem == '(\w+)'", job.get("if", "")
            )
            assert match, job.get("if")
            gated.add(match.group(1))
    assert "nuget" in gated
    sdks = {sdk for sdk, eco in ecosystems().items() if eco in gated}
    assert sdks <= set(gate.I_README), sorted(sdks - set(gate.I_README))


def test_the_readme_gate_runs_on_the_packed_file_before_it_is_recorded() -> None:
    labels = _labels("nuget-build")
    gate = _step("nuget-build", "Check the packed README")
    assert gate.get("env") == {
        "SDK": "${{ needs.resolve.outputs.sdk }}",
        "NUPKG": "${{ steps.pack.outputs.nupkg }}",
    }
    assert (
        _run_of(gate)
        == 'python3 scripts/check_packaged_readme.py --sdk "$SDK" "$NUPKG"'
    )
    assert (
        labels.index("Pack")
        < labels.index("Check the packed README")
        < labels.index("Record the package")
    )
    assert "continue-on-error" not in gate


def test_the_consumer_starts_empty_and_takes_the_package_from_the_packed_directory() -> (
    None
):
    """V26. The consumer's package cache and directory must not exist
    beforehand, its NuGet.Config maps the package to the packed directory
    alone, and the restored file must be the packed one."""
    run = _run_of(_step("nuget-build", "A consumer restores the package alone"))
    assert 'export NUGET_PACKAGES="$RUNNER_TEMP/consumer-packages"' in run
    assert "the consumer must start empty" in run
    assert "<clear />" in run and "packageSourceMapping" in run
    assert 'dotnet add package "$NAME" --version "$VERSION"' in run
    assert "that is not the packed one" in run
    assert "<TargetFrameworks>net8.0;netstandard2.1</TargetFrameworks>" in run
