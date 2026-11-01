"""sdk-release.yml's Maven Central jobs, `maven-build` and `maven`.

`maven-build` tests the Java SDK, builds the Central bundle with no credential,
checks it and resolves the starter from it alone. `maven` holds the
`sdk-release` environment and its secrets, checks the bundle against the build
job's digest and the tag, signs it, uploads it to the Central Portal for
validation only (USER_MANAGED) and waits for Central's verdict. Nothing in the
workflow can publish to Central: that is a person's click in the Portal.

The steps' scripts run here the way the runner runs them, through
test_sdk_release_wiring.py's `_run_step`, with a fake `curl` and, for the key
steps, a real `gpg` and throwaway keys. On CI a missing gpg fails these tests
rather than skipping them.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import warnings
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from backend.tests.smoke.test_sdk_release_wiring import (
    INSTALLS_OR_TESTS,
    ROOT,
    SECRET,
    VARIABLE,
    WORKFLOW,
    _outputs,
    _references,
    _run_of,
    _run_step,
    _step,
    _workflow,
    _write_executable,
)

pytestmark = pytest.mark.smoke

JAVA = ROOT / "sdk" / "java"
POM = {"m": "http://maven.apache.org/POM/4.0.0"}
NAME = "com.controlvar:controlvar-sdk-parent"
VERSION = "0.1.0"
BUNDLE = "central-bundle.zip"
UUID = "28570f16-da32-4c14-bd2e-c1acc0782365"

BUILD_CHECK = ("maven-build", "Check the bundle")
PUBLISH_CHECK = ("maven", "Check the bundle against the tag")
CONSUMER = ("maven-build", "A consumer resolves the starter from the bundle alone")


# --------------------------------------------------------------------------
# The jobs' shape
# --------------------------------------------------------------------------

MAVEN_BUILD_STEPS = [
    "actions/checkout@v7",
    "actions/setup-java@v6",
    "Run the SDK's tests",
    "No Central credentials in this job",
    "Build the Central bundle",
    "Check the bundle",
    "Each module's published links are the parent's",
    "A consumer resolves the starter from the bundle alone",
    "Record the bundle",
    "Upload the checked bundle",
]

#: The `maven` job, step by step. Nothing else may be added to it.
MAVEN_STEPS = [
    "Download the checked bundle",
    "Check the bundle against the tag",
    "Import the signing key",
    "The key is on keyserver.ubuntu.com",
    "Sign each file",
    "Verify every signature",
    "Zip the signed bundle",
    "Upload for validation",
    "Wait for Central's validation",
]


def _names(steps: list[dict]) -> list[str]:
    return [s.get("name") or s.get("uses") for s in steps]


def test_the_maven_build_and_publish_jobs_are_a_pair() -> None:
    """Same condition; `maven` needs the build; the build job reads no secret,
    cannot request a token and holds no environment; `maven` holds the
    environment and no token."""
    jobs = _workflow()["jobs"]
    build, publish = jobs["maven-build"], jobs["maven"]
    condition = "needs.resolve.outputs.ecosystem == 'maven'"
    assert build.get("if") == condition
    assert publish.get("if") == condition
    assert build.get("needs") == "resolve"
    assert set(publish.get("needs") or []) == {"resolve", "maven-build"}
    assert build.get("permissions") == {"contents": "read"}
    assert publish.get("permissions") == {"contents": "read"}
    assert "environment" not in build
    assert publish.get("environment") == {
        "name": "sdk-release",
        "url": "https://central.sonatype.com/publishing/deployments",
    }
    assert build.get("outputs") == {"digest": "${{ steps.record.outputs.digest }}"}
    assert _names(build["steps"]) == MAVEN_BUILD_STEPS

    setup = build["steps"][1]
    assert setup.get("with") == {"distribution": "temurin", "java-version": "17"}, (
        "Java 17 as sdk-unit-tests.yml, and no dependency cache"
    )
    for step in build["steps"] + publish["steps"]:
        assert "continue-on-error" not in step, step.get("name") or step.get("uses")
        assert "if" not in step, step.get("name") or step.get("uses")


def test_the_maven_job_runs_only_its_checks_the_signing_and_the_upload() -> None:
    """No checkout, no Java, no build tool beside the secrets."""
    steps = _workflow()["jobs"]["maven"]["steps"]
    assert _names(steps) == MAVEN_STEPS
    download = steps[0]
    # Pinned by full commit SHA; test_sdk_release_wiring.py's
    # test_these_actions_are_pinned_to_a_commit_with_their_version holds the
    # form, so a Dependabot bump of the pin does not have to edit this.
    assert str(download.get("uses")).startswith("actions/download-artifact@")
    assert download.get("with") == {
        "name": "maven-bundle",
        "path": "${{ github.workspace }}/bundle",
    }
    for step in steps[1:]:
        assert "uses" not in step, step.get("name")
        assert "working-directory" not in step, step.get("name")
        assert not INSTALLS_OR_TESTS.search(_run_of(step)), step.get("name")


def test_the_bundle_is_uploaded_once_and_never_overwritten() -> None:
    upload = _step("maven-build", "Upload the checked bundle")
    assert upload.get("uses") == "actions/upload-artifact@v7"
    assert upload.get("with") == {
        "name": "maven-bundle",
        "path": f"sdk/java/target/central-publishing/{BUNDLE}",
        "if-no-files-found": "error",
        "overwrite": False,
    }


#: The secrets and variables each `maven` step reads, exactly: each credential
#: is in the environment of the steps that use it and no other.
SECRETS_BY_STEP = {
    "Import the signing key": {"GPG_PRIVATE_KEY", "GPG_PASSPHRASE"},
    "Sign each file": {"GPG_PASSPHRASE"},
    "Upload for validation": {"CENTRAL_TOKEN_USERNAME", "CENTRAL_TOKEN_PASSWORD"},
    "Wait for Central's validation": {
        "CENTRAL_TOKEN_USERNAME",
        "CENTRAL_TOKEN_PASSWORD",
    },
}
VARIABLES_BY_STEP = {"Import the signing key": {"MAVEN_GPG_FINGERPRINT"}}


def test_each_secret_reaches_only_the_steps_that_use_it() -> None:
    steps = _workflow()["jobs"]["maven"]["steps"]
    secrets = {
        s["name"]: _references(SECRET, s.get("env") or {}) for s in steps if "name" in s
    }
    variables = {
        s["name"]: _references(VARIABLE, s.get("env") or {})
        for s in steps
        if "name" in s
    }
    assert {k: v for k, v in secrets.items() if v} == SECRETS_BY_STEP
    assert {k: v for k, v in variables.items() if v} == VARIABLES_BY_STEP
    for step in steps:
        env = step.get("env") or {}
        for key, value in env.items():
            for name in _references(SECRET, value):
                assert key == name, (step.get("name"), key, value)


#: Nothing in the workflow may publish to Central, or print a credential. The
#: Portal's publish and drop calls live under publisher/deployment/; the
#: others are the ways to make Central publish by itself, or to print what a
#: step sends.
FORBIDDEN = {
    "the Portal's publish or drop call": re.compile(r"publisher/deployment/"),
    "automatic publishing": re.compile(r"AUTOMATIC"),
    "the plugin's autoPublish": re.compile(r"autoPublish", re.IGNORECASE),
    "the plugin's waitUntil": re.compile(r"waitUntil", re.IGNORECASE),
    "curl's --verbose": re.compile(r"--verbose\b"),
    "curl's --trace": re.compile(r"--trace"),
    "a curl short-option cluster with -v": re.compile(
        r"\bcurl\b[^\n]{0,400}?\s-[A-Za-z]{0,10}v[A-Za-z]{0,10}(?=\s)"
    ),
    "shell tracing": re.compile(r"\bset\s+-[a-z]{0,10}x|xtrace|\bbash\s+-[a-z]{0,10}x"),
}


@pytest.mark.parametrize("what", sorted(FORBIDDEN))
def test_nothing_in_the_workflow_can_publish_or_print_a_credential(what: str) -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    found = [line.strip() for line in text.splitlines() if FORBIDDEN[what].search(line)]
    assert not found, f"sdk-release.yml has {what}: {found}"


@pytest.mark.parametrize(
    "line",
    [
        "publisher/deployment/{id}",
        "publishingType=AUTOMATIC",
        "-DautoPublish=true",
        "<waitUntil>published</waitUntil>",
        "curl --verbose https://x",
        "curl --trace-ascii - https://x",
        "curl -sSv https://x",
        "curl -v https://x",
        "set -x",
        "set -euxo pipefail",
        "set -o xtrace",
    ],
)
def test_each_forbidden_pattern_finds_what_it_exists_for(line: str) -> None:
    assert any(p.search(line) for p in FORBIDDEN.values()), line


def test_the_only_central_calls_are_the_upload_for_validation_and_its_status() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    calls = sorted(set(re.findall(r"https://central\.sonatype\.com/[^\s\"',)]*", text)))
    assert calls == [
        "https://central.sonatype.com/api/v1/publisher/status?id=$DEPLOYMENT",
        "https://central.sonatype.com/api/v1/publisher/upload?publishingType=USER_MANAGED&name=controlvar-sdk-$VERSION",
        "https://central.sonatype.com/publishing/deployments",
    ], calls


def _central_profile() -> ET.Element:
    root = ET.parse(JAVA / "pom.xml").getroot()
    profiles = [
        p
        for p in root.findall("m:profiles/m:profile", POM)
        if p.findtext("m:id", namespaces=POM) == "central"
    ]
    assert len(profiles) == 1, "sdk/java/pom.xml has no single `central` profile"
    return profiles[0]


def test_the_central_profile_fixes_skip_upload_in_the_pom() -> None:
    """A value in the POM is not overridden by `-DskipUpload=false`, so the
    plugin can only ever write the bundle. And nothing in the POM asks it to
    publish."""
    plugins = [
        p
        for p in _central_profile().findall("m:build/m:plugins/m:plugin", POM)
        if p.findtext("m:artifactId", namespaces=POM)
        == "central-publishing-maven-plugin"
    ]
    assert len(plugins) == 1
    plugin = plugins[0]
    assert plugin.findtext("m:groupId", namespaces=POM) == "org.sonatype.central"
    assert plugin.findtext("m:extensions", namespaces=POM) == "true"
    configuration = plugin.find("m:configuration", POM)
    assert configuration is not None
    assert [(child.tag.split("}")[-1], child.text) for child in configuration] == [
        ("skipUpload", "true")
    ]
    assert "<skipUpload>true</skipUpload>" in (JAVA / "pom.xml").read_text(
        encoding="utf-8"
    )
    for pom in (
        JAVA / "pom.xml",
        JAVA / "core" / "pom.xml",
        JAVA / "spring-boot-starter" / "pom.xml",
    ):
        text = pom.read_text(encoding="utf-8")
        for word in ("autoPublish", "waitUntil", "publishingType", "AUTOMATIC"):
            assert word not in text, (pom, word)


def test_the_build_job_deploys_with_the_central_profile_and_proves_the_upload_was_skipped() -> (
    None
):
    run = _run_of(_step("maven-build", "Build the Central bundle"))
    assert "./mvnw -B -ntp -Pcentral -DskipTests deploy 2>&1 | tee" in run
    assert "-DskipUpload" not in run
    assert (
        'grep -F "Skipping the upload of the bundle at user\'s request (skipUpload)"'
        in run
    )


SETTINGS = """<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0">
  <servers>
    <server>
      <id>{server}</id>
      <username>${{env.GITHUB_ACTOR}}</username>
      <password>${{env.GITHUB_TOKEN}}</password>
    </server>
  </servers>
</settings>
"""


@pytest.mark.parametrize(
    ("settings", "ok"),
    [
        (None, True),
        (SETTINGS.format(server="github"), True),
        (SETTINGS.format(server="central"), False),
    ],
    ids=["no settings.xml", "setup-java's github server", "a central server"],
)
def test_the_build_job_refuses_central_credentials(
    settings: str | None, ok: bool, tmp_path: Path
) -> None:
    """PP-10: the build job has no credential for the server the plugin
    would upload with, so an upload it attempted could not authenticate."""
    if settings is not None:
        (tmp_path / ".m2").mkdir()
        (tmp_path / ".m2" / "settings.xml").write_text(settings, encoding="utf-8")
    result = _run_step(
        _step("maven-build", "No Central credentials in this job"),
        {},
        tmp_path,
        tmp_path,
    )
    assert (result.returncode == 0) is ok, result.stdout + result.stderr
    if not ok:
        assert "holds credentials for the server 'central'" in result.stdout


def _pom_artifacts() -> list[str]:
    """groupId:artifactId:packaging of the parent, then of each module."""
    parent = ET.parse(JAVA / "pom.xml").getroot()
    group = parent.findtext("m:groupId", namespaces=POM)
    found = [f"{group}:{parent.findtext('m:artifactId', namespaces=POM)}:pom"]
    for module in parent.findall("m:modules/m:module", POM):
        child = ET.parse(JAVA / module.text.strip() / "pom.xml").getroot()
        found.append(
            ":".join(
                (
                    child.findtext("m:groupId", namespaces=POM) or group,
                    child.findtext("m:artifactId", namespaces=POM),
                    child.findtext("m:packaging", namespaces=POM) or "jar",
                )
            )
        )
    return found


def test_the_bundle_checks_expect_every_artefact_the_poms_build() -> None:
    """V14. The two bundle checks and the consumer check read a constant
    list; a module added to sdk/java/pom.xml without it fails here, not at
    the release."""
    expected = _pom_artifacts()
    assert expected == [
        "com.controlvar:controlvar-sdk-parent:pom",
        "com.controlvar:controlvar-sdk:jar",
        "com.controlvar:controlvar-spring-boot-starter:jar",
    ]
    for job, name in (BUILD_CHECK, PUBLISH_CHECK, CONSUMER):
        assert _step(job, name)["env"]["ARTIFACTS"].split() == expected, (job, name)
    assert NAME == ":".join(expected[0].split(":")[:2])


def test_the_consumer_starts_from_an_empty_local_repository() -> None:
    """V15. `deploy` installs the artefacts into ~/.m2 first, so a consumer
    reading ~/.m2 would pass with a bundle that has no parent POM. And run in
    sdk/java, Maven reads the parent POM from the checkout (its reactor), not
    from the bundle: the first local run of this step resolved the starter
    and core with no parent POM in its repository. So it runs in an empty
    directory under $RUNNER_TEMP, with no `working-directory` and no POM."""
    step = _step("maven-build", "A consumer resolves the starter from the bundle alone")
    run = _run_of(step)
    assert "working-directory" not in step
    assert 'local_repo="$RUNNER_TEMP/consumer-m2"' in run
    assert 'consumer="$RUNNER_TEMP/consumer"' in run
    assert 'if [ -e "$local_repo" ] || [ -e "$consumer" ]; then' in run
    assert (
        'mkdir "$consumer"\ncd "$consumer"\n"$GITHUB_WORKSPACE/sdk/java/mvnw" ' in run
    )
    assert '-Dmaven.repo.local="$local_repo"' in run
    assert '-DremoteRepositories="bundle::default::file://$repo"' in run
    assert 'repo="$RUNNER_TEMP/bundle-repo"' in run
    assert "maven-dependency-plugin:3.11.0:get" in run
    assert " -N " not in run and "./mvnw" not in run


# --------------------------------------------------------------------------
# The bundle checks, run on synthetic bundles
# --------------------------------------------------------------------------

SUFFIXES = ("md5", "sha1", "sha256", "sha512")
ARTIFACTS = (
    ("controlvar-sdk-parent", "pom"),
    ("controlvar-sdk", "jar"),
    ("controlvar-spring-boot-starter", "jar"),
)


def _jar(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as jar:
        for name, data in entries.items():
            jar.writestr(name, data)
    return buffer.getvalue()


def _primary(version: str = VERSION) -> dict[str, bytes]:
    files = {}
    for artifact, packaging in ARTIFACTS:
        base = f"com/controlvar/{artifact}/{version}/{artifact}-{version}"
        files[f"{base}.pom"] = f"<project>{artifact}</project>".encode()
        if packaging == "jar":
            files[f"{base}.jar"] = _jar(
                {"com/controlvar/sdk/Client.class": artifact.encode()}
            )
            files[f"{base}-sources.jar"] = _jar(
                {"com/controlvar/sdk/Client.java": b"class Client {}"}
            )
            files[f"{base}-javadoc.jar"] = _jar({"index.html": artifact.encode()})
    return files


def _entries(primary: dict[str, bytes]) -> list[tuple[str, bytes]]:
    entries = []
    for name, data in sorted(primary.items()):
        entries.append((name, data))
        for suffix in SUFFIXES:
            entries.append(
                (f"{name}.{suffix}", hashlib.new(suffix, data).hexdigest().encode())
            )
    return entries


def _zip(entries: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # a duplicate name is one of the cases
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, data in entries:
                archive.writestr(name, data)
    return buffer.getvalue()


CORE = f"com/controlvar/controlvar-sdk/{VERSION}/controlvar-sdk-{VERSION}"


def _replace(entries, name, data):
    return [(n, data if n == name else d) for n, d in entries]


#: (case, change to the good entries, the refusal both checks print).
REFUSALS = [
    (
        "a missing javadoc jar",
        lambda e: [x for x in e if x[0] != f"{CORE}-javadoc.jar"],
        f"the bundle is missing {CORE}-javadoc.jar",
    ),
    (
        "a stray README",
        lambda e: [*e, ("README.md", b"x")],
        "the bundle holds an unexpected entry: README.md",
    ),
    (
        "a checksum that does not match",
        lambda e: _replace(e, f"{CORE}.jar.sha1", b"0" * 40),
        f"{CORE}.jar.sha1 does not match {CORE}.jar",
    ),
    (
        "an empty checksum",
        lambda e: _replace(e, f"{CORE}.pom.md5", b""),
        f"{CORE}.pom.md5 does not match {CORE}.pom",
    ),
    (
        "a name that climbs out of the bundle",
        lambda e: [*e, ("../x", b"x")],
        "the bundle holds an unexpected entry: ../x",
    ),
    (
        "an absolute name",
        lambda e: [*e, ("/etc/x", b"x")],
        "the bundle holds an unexpected entry: /etc/x",
    ),
    (
        "a signature already in the build's bundle",
        lambda e: [*e, (f"{CORE}.jar.asc", b"sig")],
        f"the bundle holds an unexpected entry: {CORE}.jar.asc",
    ),
    (
        "another version beside the tag's",
        lambda e: [
            *e,
            ("com/controlvar/controlvar-sdk/0.1.1/controlvar-sdk-0.1.1.pom", b"x"),
        ],
        "unexpected entry: com/controlvar/controlvar-sdk/0.1.1/controlvar-sdk-0.1.1.pom",
    ),
    (
        "_remote.repositories from a local install",
        lambda e: [
            *e,
            (f"com/controlvar/controlvar-sdk/{VERSION}/_remote.repositories", b"x"),
        ],
        f"unexpected entry: com/controlvar/controlvar-sdk/{VERSION}/_remote.repositories",
    ),
    (
        "maven-metadata-local.xml from a local install",
        lambda e: [
            *e,
            ("com/controlvar/controlvar-sdk/maven-metadata-local.xml", b"x"),
        ],
        "unexpected entry: com/controlvar/controlvar-sdk/maven-metadata-local.xml",
    ),
    (
        "maven-metadata.xml",
        lambda e: [*e, ("com/controlvar/controlvar-sdk/maven-metadata.xml", b"x")],
        "unexpected entry: com/controlvar/controlvar-sdk/maven-metadata.xml",
    ),
    (
        "a directory entry",
        lambda e: [*e, ("com/controlvar/", b"")],
        "the bundle holds an unexpected entry: com/controlvar/",
    ),
    (
        "an entry twice",
        lambda e: [*e, (f"{CORE}.pom", b"<project>other</project>")],
        "the bundle holds an entry twice",
    ),
]


def _build_check(tmp_path: Path, data: bytes, version: str = VERSION, name: str = NAME):
    target = tmp_path / "ws" / "sdk" / "java" / "target" / "central-publishing"
    target.mkdir(parents=True)
    (target / BUNDLE).write_bytes(data)
    return _run_step(
        _step(*BUILD_CHECK),
        {"needs.resolve.outputs.name": name, "needs.resolve.outputs.version": version},
        tmp_path,
        tmp_path / "ws",
    )


def _record(tmp_path: Path, data: bytes) -> str:
    target = tmp_path / "ws" / "sdk" / "java" / "target" / "central-publishing"
    target.mkdir(parents=True)
    (target / BUNDLE).write_bytes(data)
    result = _run_step(
        _step("maven-build", "Record the bundle"), {}, tmp_path, tmp_path / "ws"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return _outputs(tmp_path)["digest"]


def _publish_check(
    tmp_path: Path,
    files: dict[str, bytes],
    digest: str,
    version: str = VERSION,
    name: str = NAME,
):
    folder = tmp_path / "workspace" / "bundle"
    folder.mkdir(parents=True)
    for filename, data in files.items():
        (folder / filename).write_bytes(data)
    return _run_step(
        _step(*PUBLISH_CHECK),
        {
            "needs.resolve.outputs.name": name,
            "needs.resolve.outputs.version": version,
            "needs.maven-build.outputs.digest": digest,
        },
        tmp_path,
        tmp_path,
    )


def test_both_checks_accept_the_bundle_and_write_it_out(tmp_path: Path) -> None:
    good = _entries(_primary())
    assert len(good) == 45
    data = _zip(good)

    built = _build_check(tmp_path / "build", data)
    assert built.returncode == 0, built.stdout + built.stderr
    assert "45 entries: 9 files, each with md5, sha1, sha256 and sha512" in built.stdout
    repo = tmp_path / "build" / "runner_temp" / "bundle-repo"
    assert sorted(
        p.relative_to(repo).as_posix() for p in repo.rglob("*") if p.is_file()
    ) == sorted(n for n, _ in good)

    digest = _record(tmp_path / "record", data)
    assert digest == hashlib.sha256(data).hexdigest()
    checked = _publish_check(tmp_path / "publish", {BUNDLE: data}, digest)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert (
        f"45 entries for {NAME} {VERSION}, digest {digest}; 9 files to sign"
        in checked.stdout
    )
    temp = tmp_path / "publish" / "runner_temp"
    listed = (temp / "primary-files.txt").read_text(encoding="utf-8").split()
    assert sorted(listed) == sorted(_primary()) and len(listed) == 9
    for name, data_ in good:
        assert (temp / "bundle" / name).read_bytes() == data_


@pytest.mark.regression
@pytest.mark.parametrize(
    ("case", "change", "refusal"), REFUSALS, ids=[case for case, _, _ in REFUSALS]
)
def test_both_checks_refuse(case: str, change, refusal: str, tmp_path: Path) -> None:
    """V13, PP-10: each changed bundle is refused by the build job's check,
    and by the publish job's even when the build job recorded its digest."""
    data = _zip(change(_entries(_primary())))
    built = _build_check(tmp_path / "build", data)
    assert built.returncode == 1, (case, built.stdout, built.stderr)
    assert refusal in built.stdout, (case, built.stdout)
    assert not (tmp_path / "build" / "runner_temp" / "bundle-repo").exists(), case

    digest = hashlib.sha256(data).hexdigest()
    checked = _publish_check(tmp_path / "publish", {BUNDLE: data}, digest)
    assert checked.returncode == 1, (case, checked.stdout, checked.stderr)
    assert refusal in checked.stdout, (case, checked.stdout)
    assert not (tmp_path / "publish" / "runner_temp" / "bundle").exists(), case


@pytest.mark.parametrize(
    "jar", [f"{CORE}.jar", f"{CORE}-sources.jar", f"{CORE}-javadoc.jar"]
)
def test_the_build_check_refuses_the_examples_package_in_a_jar(
    jar: str, tmp_path: Path
) -> None:
    primary = _primary()
    primary[jar] = _jar({"com/controlvar/sdk/examples/ContractSmoke.class": b"x"})
    result = _build_check(tmp_path, _zip(_entries(primary)))
    assert result.returncode == 1, result.stdout
    assert (
        f"{jar} carries com/controlvar/sdk/examples, which is not API" in result.stdout
    )


@pytest.mark.parametrize(
    ("case", "version", "name", "refusal"),
    [
        ("an unusable version", "../0.1.0", NAME, "the tag resolved no usable version"),
        (
            "another version",
            "0.1.1",
            NAME,
            "the bundle is missing com/controlvar/controlvar-sdk/0.1.1/",
        ),
        (
            "another name",
            VERSION,
            "com.controlvar:controlvar-sdk",
            "the tag resolved 'com.controlvar:controlvar-sdk'; the bundle's parent is",
        ),
    ],
)
def test_both_checks_compare_with_the_tag(
    case: str, version: str, name: str, refusal: str, tmp_path: Path
) -> None:
    data = _zip(_entries(_primary()))
    built = _build_check(tmp_path / "build", data, version=version, name=name)
    assert built.returncode == 1, (case, built.stdout)
    assert refusal in built.stdout, (case, built.stdout)
    checked = _publish_check(
        tmp_path / "publish",
        {BUNDLE: data},
        hashlib.sha256(data).hexdigest(),
        version=version,
        name=name,
    )
    assert checked.returncode == 1, (case, checked.stdout)
    assert refusal in checked.stdout, (case, checked.stdout)


@pytest.mark.parametrize(
    ("case", "files", "digest", "refusal"),
    [
        (
            "a byte flipped after the digest",
            "flipped",
            "recorded",
            "central-bundle.zip has digest",
        ),
        ("a second file", "extra", "recorded", "expected exactly central-bundle.zip"),
        (
            "nothing downloaded",
            "none",
            "recorded",
            "expected exactly central-bundle.zip",
        ),
        (
            "another file name",
            "renamed",
            "recorded",
            "expected exactly central-bundle.zip",
        ),
        ("no digest", "same", "", "the build job recorded no usable digest"),
    ],
)
def test_the_publish_check_refuses_what_the_build_job_did_not_record(
    case: str, files: str, digest: str, refusal: str, tmp_path: Path
) -> None:
    data = _zip(_entries(_primary()))
    recorded = hashlib.sha256(data).hexdigest()
    downloaded = {
        "same": {BUNDLE: data},
        "flipped": {BUNDLE: data[:-1] + bytes([data[-1] ^ 1])},
        "extra": {BUNDLE: data, "notes.txt": b"x"},
        "none": {},
        "renamed": {"bundle.zip": data},
    }[files]
    result = _publish_check(
        tmp_path, downloaded, recorded if digest == "recorded" else digest
    )
    assert result.returncode == 1, (case, result.stdout)
    assert refusal in result.stdout, (case, result.stdout)


#: What a consumer of the starter resolves from the bundle: the three POMs and
#: the two jars.
RESOLVED = [
    f"com/controlvar/{artifact}/{VERSION}/{artifact}-{VERSION}.{extension}"
    for artifact, packaging in ARTIFACTS
    for extension in (("pom", "jar") if packaging == "jar" else ("pom",))
]


def _consumer(tmp_path: Path, bundle: dict[str, bytes], drop=(), change=()):
    """The consumer step, with an `mvnw` that "resolves" by copying the
    bundle's repository into the local one, then drops or changes files."""
    for name, data in bundle.items():
        path = tmp_path / "runner_temp" / "bundle-repo" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    script = (
        'local=""\n'
        'for arg in "$@"; do\n'
        '  case "$arg" in -Dmaven.repo.local=*) local="${arg#-Dmaven.repo.local=}" ;; esac\n'
        "done\n"
        'mkdir -p "$local"\n'
        'cp -R "$RUNNER_TEMP/bundle-repo/." "$local/"\n'
    )
    script += "".join(f'rm -f "$local/{name}"\n' for name in drop)
    script += "".join(f'printf x >> "$local/{name}"\n' for name in change)
    _write_executable(tmp_path / "workspace" / "sdk" / "java" / "mvnw", script)
    return _run_step(
        _step(*CONSUMER), {"needs.resolve.outputs.version": VERSION}, tmp_path, tmp_path
    )


def test_the_consumer_check_accepts_the_five_files_from_the_bundle(
    tmp_path: Path,
) -> None:
    assert len(RESOLVED) == 5
    primary = _primary()
    result = _consumer(tmp_path, {name: primary[name] for name in RESOLVED})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "5 files resolved from the bundle, each equal to it" in result.stdout


CORE_POM = f"com/controlvar/controlvar-sdk/{VERSION}/controlvar-sdk-{VERSION}.pom"
STARTER_JAR = (
    f"com/controlvar/controlvar-spring-boot-starter/{VERSION}/"
    f"controlvar-spring-boot-starter-{VERSION}.jar"
)
PARENT_POM = f"com/controlvar/controlvar-sdk-parent/{VERSION}/controlvar-sdk-parent-{VERSION}.pom"


@pytest.mark.parametrize(
    ("case", "missing", "drop", "change", "refusal"),
    [
        (
            "a module dropped from the bundle",
            ("controlvar-sdk/",),
            (),
            (),
            f"the bundle has no {CORE_POM}",
        ),
        (
            "a file the consumer did not resolve",
            (),
            (STARTER_JAR,),
            (),
            f"the consumer did not resolve {STARTER_JAR}",
        ),
        (
            "a file that is not the bundle's",
            (),
            (),
            (PARENT_POM,),
            f"the consumer resolved a {PARENT_POM} that is not the bundle's",
        ),
    ],
)
def test_the_consumer_check_expects_every_artefact_it_names(
    case: str, missing, drop, change, refusal: str, tmp_path: Path
) -> None:
    """The list, not the bundle, says what to expect: a module missing from
    both the bundle and the consumer is refused. The first version walked
    the bundle's own files, so it could not notice one that was not there."""
    primary = _primary()
    bundle = {
        name: primary[name]
        for name in RESOLVED
        if not any(f"/{part}" in f"/{name}" for part in missing)
    }
    result = _consumer(tmp_path, bundle, drop=drop, change=change)
    assert result.returncode == 1, (case, result.stdout, result.stderr)
    assert refusal in result.stdout, (case, result.stdout)


# --------------------------------------------------------------------------
# The fake curl
# --------------------------------------------------------------------------


def _fake_curl(
    bin_dir: Path, record: Path, replies: list[bytes], codes: list[int] | None = None
) -> None:
    """A `curl` that records each call's arguments (one per line) and, after
    `-K -`, its stdin; writes the n-th reply to the `--output` file; and exits
    with the n-th code (the last reply and code repeat)."""
    record.mkdir(parents=True, exist_ok=True)
    for index, reply in enumerate(replies, start=1):
        (record / f"reply.{index}").write_bytes(reply)
    for index, code in enumerate(codes or [0], start=1):
        (record / f"code.{index}").write_text(str(code), encoding="utf-8")
    last_reply, last_code = len(replies), len(codes or [0])
    _write_executable(
        bin_dir / "curl",
        f"""dir='{record}'
n=$(( $(cat "$dir/count" 2>/dev/null || echo 0) + 1 ))
printf '%s' "$n" > "$dir/count"
printf '%s\\n' "$@" > "$dir/argv.$n"
out=""
prev=""
for arg in "$@"; do
  case "$prev" in
    --output|-o) out="$arg" ;;
    -K) if [ "$arg" = "-" ]; then cat > "$dir/stdin.$n"; fi ;;
  esac
  prev="$arg"
done
reply="$dir/reply.$n"; [ -f "$reply" ] || reply="$dir/reply.{last_reply}"
code="$dir/code.$n"; [ -f "$code" ] || code="$dir/code.{last_code}"
if [ -n "$out" ] && [ -f "$reply" ]; then cat "$reply" > "$out"; fi
exit "$(cat "$code")"
""",
    )


def _calls(record: Path) -> int:
    count = record / "count"
    return int(count.read_text(encoding="utf-8")) if count.exists() else 0


# --------------------------------------------------------------------------
# PP-5: the Central bearer value is one line, masked before any use, and
# reaches curl on stdin
# --------------------------------------------------------------------------

#: A 100-byte "user:password": base64 of it is 136 characters, longer than the
#: 76 at which GNU base64 (ubuntu-latest's) wraps a line.
USERNAME = "cv-token-user-Kq7Zt2Wm9Rb4Xc"
PASSWORD = "Pw_" + "".join(
    str(i % 10) if i % 3 == 0 else chr(ord("a") + (i * 7) % 26) for i in range(68)
)
assert len(f"{USERNAME}:{PASSWORD}") == 100
BEARER = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()


def _bearer_step(which: str, tmp_path: Path, replies: list[bytes]):
    record = tmp_path / "curl"
    _fake_curl(tmp_path / "bin", record, replies)
    temp = tmp_path / "runner_temp"
    temp.mkdir(parents=True, exist_ok=True)
    (temp / "primary-files.txt").write_text(
        "".join(f"{name}\n" for name in sorted(_primary())), encoding="utf-8"
    )
    values = {
        "secrets.CENTRAL_TOKEN_USERNAME": USERNAME,
        "secrets.CENTRAL_TOKEN_PASSWORD": PASSWORD,
        "needs.resolve.outputs.version": VERSION,
        "steps.upload.outputs.deployment": UUID,
    }
    step = _step("maven", which)
    if which == "Wait for Central's validation":
        step = {**step, "env": {**step["env"], "POLL_SECONDS": "0"}}
    return _run_step(step, values, tmp_path, tmp_path), record


def _fragments(secret: str, size: int = 12) -> set[str]:
    return {secret[i : i + size] for i in range(len(secret) - size + 1)}


BEARER_STEPS = ["Upload for validation", "Wait for Central's validation"]


def test_the_bearer_steps_are_the_ones_that_read_the_token() -> None:
    assert BEARER_STEPS == [
        name
        for name, secrets in SECRETS_BY_STEP.items()
        if "CENTRAL_TOKEN_PASSWORD" in secrets
    ]


@pytest.mark.parametrize("which", BEARER_STEPS)
def test_the_bearer_is_masked_before_any_line_uses_it(which: str) -> None:
    """V21, read from the script: the value is computed from the two
    secrets, masked on the next line, and only then used. The run tests
    below cannot see a mask moved after the upload, which prints nothing."""
    lines = [line.strip() for line in _run_of(_step("maven", which)).splitlines()]
    computed = [i for i, line in enumerate(lines) if line.startswith('bearer="$(')]
    assert len(computed) == 1, lines
    assert "base64.b64encode(" in lines[computed[0]]
    assert lines[computed[0] + 1] == "printf '::add-mask::%s\\n' \"$bearer\""
    used = [
        i
        for i, line in enumerate(lines)
        if "$bearer" in line and i not in (computed[0], computed[0] + 1)
    ]
    assert used and min(used) > computed[0] + 1, lines
    for i in used:
        assert lines[i].startswith(
            'if ! printf \'header = "Authorization: Bearer %s"\\n\' "$bearer" | curl '
        ), lines[i]
        assert " -K - " in lines[i], lines[i]


@pytest.mark.regression
@pytest.mark.parametrize(
    ("which", "replies"),
    [
        ("Upload for validation", [UUID.encode()]),
        ("Wait for Central's validation", [b'{"deploymentState": "VALIDATED"}']),
    ],
)
def test_the_bearer_is_one_masked_line_and_never_an_argument(
    which: str, replies: list[bytes], tmp_path: Path
) -> None:
    """PP-5. With a 100-byte credential pair: the step's first output line
    masks the whole bearer value, on one line; no other line of its output
    holds a fragment of the value or of either credential; curl receives it
    on stdin as one config line, never in its arguments. GNU base64 wraps
    the value at 76 columns, so on ubuntu-latest a step that used it would
    print the rest of the value unmasked on the second line."""
    result, record = _bearer_step(which, tmp_path, replies)
    assert result.returncode == 0, result.stdout + result.stderr

    lines = result.stdout.splitlines()
    assert lines[0] == f"::add-mask::{BEARER}", lines[0]
    rest = "\n".join(lines[1:]) + "\n" + result.stderr
    leaked = sorted(
        fragment
        for secret in (BEARER, USERNAME, PASSWORD)
        for fragment in _fragments(secret)
        if fragment in rest
    )
    assert not leaked, f"unmasked fragments in the step's output: {leaked[:3]}"

    assert _calls(record) >= 1
    for n in range(1, _calls(record) + 1):
        argv = (record / f"argv.{n}").read_text(encoding="utf-8")
        assert not any(
            f in argv for s in (BEARER, USERNAME, PASSWORD) for f in _fragments(s)
        ), argv
        arguments = argv.splitlines()
        assert arguments.count("-K") == 1, arguments
        assert arguments[arguments.index("-K") + 1] == "-", arguments
        stdin = (record / f"stdin.{n}").read_text(encoding="utf-8")
        assert stdin == f'header = "Authorization: Bearer {BEARER}"\n', stdin


def test_the_upload_is_for_validation_and_records_the_deployment(
    tmp_path: Path,
) -> None:
    result, record = _bearer_step(
        "Upload for validation", tmp_path, [f"{UUID}\n".encode()]
    )
    assert result.returncode == 0, result.stdout + result.stderr
    argv = (record / "argv.1").read_text(encoding="utf-8").splitlines()
    assert argv[-1] == (
        "https://central.sonatype.com/api/v1/publisher/upload"
        f"?publishingType=USER_MANAGED&name=controlvar-sdk-{VERSION}"
    )
    assert f"bundle=@{tmp_path}/runner_temp/signed/{BUNDLE}" in argv
    assert "--fail-with-body" in argv
    assert _outputs(tmp_path) == {"deployment": UUID}
    assert f"uploaded for validation: deployment {UUID}" in result.stdout


@pytest.mark.parametrize(
    ("case", "reply", "code", "refusal"),
    [
        (
            "an answer that is not a deployment id",
            b"<html>sign in</html>",
            0,
            "is not a deployment id",
        ),
        ("an empty answer", b"", 0, "is not a deployment id"),
        (
            "a refused upload",
            b'{"error": "forbidden"}',
            22,
            "the Central Portal refused the upload",
        ),
    ],
)
def test_the_upload_refuses(
    case: str, reply: bytes, code: int, refusal: str, tmp_path: Path
) -> None:
    record = tmp_path / "curl"
    _fake_curl(tmp_path / "bin", record, [reply], [code])
    result = _run_step(
        _step("maven", "Upload for validation"),
        {
            "secrets.CENTRAL_TOKEN_USERNAME": USERNAME,
            "secrets.CENTRAL_TOKEN_PASSWORD": PASSWORD,
            "needs.resolve.outputs.version": VERSION,
        },
        tmp_path,
        tmp_path,
    )
    assert result.returncode == 1, (case, result.stdout)
    assert refusal in result.stdout, (case, result.stdout)
    assert _outputs(tmp_path) == {}, case


@pytest.mark.parametrize(
    ("which", "username", "password"),
    [
        ("Upload for validation", "", PASSWORD),
        ("Upload for validation", USERNAME, ""),
        ("Wait for Central's validation", "", PASSWORD),
    ],
)
def test_an_empty_central_secret_is_refused_before_curl(
    which: str, username: str, password: str, tmp_path: Path
) -> None:
    record = tmp_path / "curl"
    _fake_curl(tmp_path / "bin", record, [UUID.encode()])
    result = _run_step(
        _step("maven", which),
        {
            "secrets.CENTRAL_TOKEN_USERNAME": username,
            "secrets.CENTRAL_TOKEN_PASSWORD": password,
            "needs.resolve.outputs.version": VERSION,
            "steps.upload.outputs.deployment": UUID,
        },
        tmp_path,
        tmp_path,
    )
    assert result.returncode == 1, result.stdout
    assert "is empty in the sdk-release environment" in result.stdout
    assert _calls(record) == 0


# --------------------------------------------------------------------------
# Waiting for Central's verdict (V19)
# --------------------------------------------------------------------------


def _state(name: str, **extra) -> bytes:
    import json

    return json.dumps({"deploymentId": UUID, "deploymentState": name, **extra}).encode()


@pytest.mark.parametrize(
    ("case", "replies", "ok", "expected"),
    [
        (
            "validated after pending and validating",
            [_state("PENDING"), _state("VALIDATING"), _state("VALIDATED")],
            True,
            f"Deployment {UUID} is VALIDATED and NOT published.",
        ),
        (
            "failed, with Central's errors",
            [
                _state("VALIDATING"),
                _state("FAILED", errors={"pkg:maven/x": ["Missing signature"]}),
            ],
            False,
            "Missing signature",
        ),
        (
            "still pending after every poll",
            [_state("PENDING")],
            False,
            "is still PENDING after 3 polls",
        ),
        (
            "published by something else",
            [_state("PUBLISHED")],
            False,
            "so something else published it",
        ),
        ("publishing", [_state("PUBLISHING")], False, "so something else published it"),
        ("an unreadable answer", [b"<html>"], False, "the state 'unreadable'"),
        ("an unknown state", [_state("PAUSED")], False, "the state 'PAUSED'"),
    ],
)
def test_the_wait_ends_only_on_validated(
    case: str, replies: list[bytes], ok: bool, expected: str, tmp_path: Path
) -> None:
    record = tmp_path / "curl"
    _fake_curl(tmp_path / "bin", record, replies)
    temp = tmp_path / "runner_temp"
    temp.mkdir()
    (temp / "primary-files.txt").write_text(
        "".join(f"{name}\n" for name in sorted(_primary())), encoding="utf-8"
    )
    step = _step("maven", "Wait for Central's validation")
    step = {**step, "env": {**step["env"], "POLL_SECONDS": "0", "POLL_ATTEMPTS": "3"}}
    result = _run_step(
        step,
        {
            "secrets.CENTRAL_TOKEN_USERNAME": USERNAME,
            "secrets.CENTRAL_TOKEN_PASSWORD": PASSWORD,
            "needs.resolve.outputs.version": VERSION,
            "steps.upload.outputs.deployment": UUID,
        },
        tmp_path,
        tmp_path,
    )
    assert (result.returncode == 0) is ok, (case, result.stdout, result.stderr)
    assert expected in result.stdout, (case, result.stdout)
    for n in range(1, _calls(record) + 1):
        argv = (record / f"argv.{n}").read_text(encoding="utf-8").splitlines()
        assert (
            argv[-1]
            == f"https://central.sonatype.com/api/v1/publisher/status?id={UUID}"
        )
    if ok:
        assert "check that the deployment id is" in result.stdout
        assert "lists these 9 files:" in result.stdout
        assert f"- controlvar-sdk-{VERSION}-javadoc.jar" in result.stdout
        assert "then press Publish. To abandon it, press Drop." in result.stdout


def test_the_real_wait_is_thirty_minutes_inside_the_job_timeout() -> None:
    job = _workflow()["jobs"]["maven"]
    env = _step("maven", "Wait for Central's validation")["env"]
    waited = int(env["POLL_ATTEMPTS"]) * int(env["POLL_SECONDS"])
    assert waited == 30 * 60
    assert job["timeout-minutes"] * 60 > waited + 10 * 60


# --------------------------------------------------------------------------
# The signing key: import, keyserver, sign, verify (V18, V19, V20). A real
# gpg with throwaway keys, in homes under a short directory: gpg puts its
# agent's socket in the home on macOS, and a socket path has a length limit.
# --------------------------------------------------------------------------

PASSPHRASE = "throwaway passphrase for the release tests"


def _short_root() -> str | None:
    return "/tmp" if os.path.isdir("/tmp") else None


def _gpg(home: Path, *args: str, stdin: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["gpg", "--homedir", str(home), "--batch", "--no-tty", *args],
        input=stdin,
        capture_output=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr.decode()
    return result.stdout


def _kill_agent(home: Path) -> None:
    if home.is_dir() and shutil.which("gpgconf"):
        subprocess.run(
            ["gpgconf", "--homedir", str(home), "--kill", "gpg-agent"],
            capture_output=True,
            timeout=30,
        )


@pytest.fixture(scope="module")
def keys():
    """Two throwaway signing keys: (fingerprint, secret, public) each."""
    if shutil.which("gpg") is None:
        if os.environ.get("CI"):
            pytest.fail(
                "gpg is not installed; on CI the key steps' tests must run, not skip"
            )
        pytest.skip("gpg is not installed")
    homes, made = [], []
    try:
        for uid in (
            "Release Test One <one@example.invalid>",
            "Release Test Two <two@example.invalid>",
        ):
            home = Path(tempfile.mkdtemp(prefix="cvk", dir=_short_root()))
            home.chmod(0o700)
            homes.append(home)
            _gpg(
                home,
                "--pinentry-mode",
                "loopback",
                "--passphrase",
                PASSPHRASE,
                "--quick-gen-key",
                uid,
                "ed25519",
                "sign",
                "never",
            )
            listing = _gpg(home, "--with-colons", "--list-secret-keys").decode()
            fingerprint = re.search(r"^fpr:+([0-9A-F]{40}):", listing, re.M).group(1)
            secret = _gpg(
                home,
                "--pinentry-mode",
                "loopback",
                "--passphrase",
                PASSPHRASE,
                "--armor",
                "--export-secret-keys",
                fingerprint,
            ).decode()
            public = _gpg(home, "--armor", "--export", fingerprint).decode()
            made.append((fingerprint, secret, public))
        yield made
    finally:
        for home in homes:
            _kill_agent(home)
            shutil.rmtree(home, ignore_errors=True)


@pytest.fixture
def short_tmp():
    """A short tmp_path, for the steps that make $RUNNER_TEMP/gnupg."""
    path = Path(tempfile.mkdtemp(prefix="cvs", dir=_short_root()))
    try:
        yield path
    finally:
        _kill_agent(path / "runner_temp" / "gnupg")
        shutil.rmtree(path, ignore_errors=True)


def _import(tmp: Path, secret: str, passphrase: str, fingerprint: str):
    return _run_step(
        _step("maven", "Import the signing key"),
        {
            "secrets.GPG_PRIVATE_KEY": secret,
            "secrets.GPG_PASSPHRASE": passphrase,
            "vars.MAVEN_GPG_FINGERPRINT": fingerprint,
        },
        tmp,
        tmp,
    )


def test_the_key_is_imported_and_its_fingerprint_recorded(
    keys, short_tmp: Path
) -> None:
    fingerprint, secret, _ = keys[0]
    spaced = " ".join(fingerprint[i : i + 4] for i in range(0, 40, 4)).lower()
    result = _import(short_tmp, secret, PASSPHRASE, spaced)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _outputs(short_tmp) == {"fingerprint": fingerprint}
    assert PASSPHRASE not in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("case", "refusal"),
    [
        ("an empty key", "empty in the sdk-release environment: GPG_PRIVATE_KEY"),
        ("an empty passphrase", "empty in the sdk-release environment: GPG_PASSPHRASE"),
        (
            "an empty fingerprint",
            "empty in the sdk-release environment: MAVEN_GPG_FINGERPRINT",
        ),
        ("a fingerprint that is not one", "is not a 40-digit hex fingerprint"),
        (
            "two secret keys",
            "GPG_PRIVATE_KEY holds 2 secret keys; it must hold exactly one",
        ),
        ("another key's fingerprint", "but MAVEN_GPG_FINGERPRINT says"),
    ],
)
def test_the_key_import_refuses(keys, case: str, refusal: str, short_tmp: Path) -> None:
    """V18."""
    (one, secret_one, _), (two, secret_two, _) = keys
    secret, passphrase, fingerprint = {
        "an empty key": ("", PASSPHRASE, one),
        "an empty passphrase": (secret_one, "", one),
        "an empty fingerprint": (secret_one, PASSPHRASE, ""),
        "a fingerprint that is not one": (secret_one, PASSPHRASE, one[:39] + "G"),
        "two secret keys": (secret_one + "\n" + secret_two, PASSPHRASE, one),
        "another key's fingerprint": (secret_one, PASSPHRASE, two),
    }[case]
    result = _import(short_tmp, secret, passphrase, fingerprint)
    assert result.returncode == 1, (case, result.stdout, result.stderr)
    assert refusal in result.stdout, (case, result.stdout)
    assert _outputs(short_tmp) == {}, case


@pytest.mark.parametrize(
    ("case", "reply", "code", "ok", "expected"),
    [
        ("the key is published", "own", 0, True, "keyserver.ubuntu.com serves"),
        ("the keyserver does not have it", "none", 22, False, "does not serve the key"),
        ("the keyserver answers with another key", "other", 0, False, "answered for"),
    ],
)
def test_the_keyserver_check(
    keys, case, reply, code, ok, expected, short_tmp: Path
) -> None:
    """V19: Central reads the public key from a keyserver."""
    (one, secret_one, public_one), (_, _, public_two) = keys
    assert _import(short_tmp, secret_one, PASSPHRASE, one).returncode == 0
    body = {"own": public_one, "other": public_two, "none": "Not Found"}[reply].encode()
    record = short_tmp / "curl"
    _fake_curl(short_tmp / "bin", record, [body], [code])
    result = _run_step(
        _step("maven", "The key is on keyserver.ubuntu.com"),
        {"steps.key.outputs.fingerprint": one},
        short_tmp,
        short_tmp,
    )
    assert (result.returncode == 0) is ok, (case, result.stdout, result.stderr)
    assert expected in result.stdout, (case, result.stdout)
    argv = (record / "argv.1").read_text(encoding="utf-8").splitlines()
    assert argv[-1] == (
        f"https://keyserver.ubuntu.com/pks/lookup?op=get&options=mr&search=0x{one}"
    )


def _sign_and_verify(
    keys, short_tmp: Path, *, flip: str | None = None, verify_as: str | None = None
):
    (one, secret_one, _), _ = keys
    assert _import(short_tmp, secret_one, PASSPHRASE, one).returncode == 0
    temp = short_tmp / "runner_temp"
    primary = _primary()
    for name, data in primary.items():
        path = temp / "bundle" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (temp / "primary-files.txt").write_text(
        "".join(f"{n}\n" for n in sorted(primary)), encoding="utf-8"
    )
    signed = _run_step(
        _step("maven", "Sign each file"),
        {"secrets.GPG_PASSPHRASE": PASSPHRASE, "steps.key.outputs.fingerprint": one},
        short_tmp,
        short_tmp,
    )
    assert signed.returncode == 0, signed.stdout + signed.stderr
    assert "signed 9 files" in signed.stdout
    if flip:
        path = temp / "bundle" / flip
        data = path.read_bytes()
        path.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    return _run_step(
        _step("maven", "Verify every signature"),
        {"steps.key.outputs.fingerprint": verify_as or one},
        short_tmp,
        short_tmp,
    )


def test_every_file_is_signed_and_verified(keys, short_tmp: Path) -> None:
    result = _sign_and_verify(keys, short_tmp)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"9 signatures verify, each by {keys[0][0]}" in result.stdout

    zipped = _run_step(
        _step("maven", "Zip the signed bundle"), {}, short_tmp, short_tmp
    )
    assert zipped.returncode == 0, zipped.stdout + zipped.stderr
    assert "18 entries, 9 of them signatures" in zipped.stdout
    with zipfile.ZipFile(short_tmp / "runner_temp" / "signed" / BUNDLE) as archive:
        names = archive.namelist()
    assert names == sorted(names)
    assert sorted(n for n in names if n.endswith(".asc")) == sorted(
        f"{n}.asc" for n in _primary()
    )


def test_a_byte_changed_after_signing_fails_the_verification(
    keys, short_tmp: Path
) -> None:
    """V20."""
    result = _sign_and_verify(keys, short_tmp, flip=f"{CORE}.jar")
    assert result.returncode == 1, result.stdout
    assert f"{CORE}.jar: the signature does not verify" in result.stdout


def test_a_signature_by_another_key_fails_the_verification(
    keys, short_tmp: Path
) -> None:
    result = _sign_and_verify(keys, short_tmp, verify_as=keys[1][0])
    assert result.returncode == 1, result.stdout
    assert f"is signed by '{keys[0][0]}', not by {keys[1][0]}" in result.stdout


def test_the_zip_refuses_a_missing_signature(tmp_path: Path) -> None:
    temp = tmp_path / "runner_temp"
    primary = _primary()
    for name, data in primary.items():
        path = temp / "bundle" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if name != f"{CORE}.jar":
            (temp / "bundle" / f"{name}.asc").write_bytes(b"sig")
    (temp / "primary-files.txt").write_text(
        "".join(f"{n}\n" for n in sorted(primary)), encoding="utf-8"
    )
    result = _run_step(_step("maven", "Zip the signed bundle"), {}, tmp_path, tmp_path)
    assert result.returncode == 1, result.stdout
    assert "expected one signature for each of" in result.stdout
    assert not (temp / "signed" / BUNDLE).exists()


# --------------------------------------------------------------------------
# scripts/check_sdk_version.py reads the Maven version (V11)
# --------------------------------------------------------------------------


def _java_tree(tmp_path: Path, change=None) -> Path:
    """A copy of the script and the three POMs, with `change(path, text)`
    applied to each POM's text."""
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "check_sdk_version.py", tmp_path / "scripts")
    for relative in ("pom.xml", "core/pom.xml", "spring-boot-starter/pom.xml"):
        text = (JAVA / relative).read_text(encoding="utf-8")
        target = tmp_path / "sdk" / "java" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(change(relative, text) if change else text, encoding="utf-8")
    return tmp_path


def _check_version(tree: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(tree / "scripts" / "check_sdk_version.py"), "java", *args],
        cwd=tree,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_the_maven_version_and_name_come_from_the_parent_pom(tmp_path: Path) -> None:
    tree = _java_tree(tmp_path)
    result = _check_version(tree, "--expect", VERSION)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"sdk/java: {NAME} {VERSION} (maven)\n"
    assert _check_version(tree, "--print-name").stdout == f"{NAME}\n"


def _child_parent_version(relative: str, text: str) -> str:
    if relative == "core/pom.xml":
        return text.replace(
            f"<version>{VERSION}</version>", "<version>0.2.0</version>", 1
        )
    return text


@pytest.mark.parametrize(
    ("case", "change", "refusal"),
    [
        (
            "a module whose parent is another version",
            _child_parent_version,
            f"sdk/java/pom.xml is {NAME}:{VERSION}, but module core's <parent> names {NAME}:0.2.0",
        ),
        (
            "a module with another groupId",
            lambda r, t: (
                t.replace(
                    "<artifactId>controlvar-sdk</artifactId>",
                    "<groupId>com.example</groupId>\n    <artifactId>controlvar-sdk</artifactId>",
                )
                if r == "core/pom.xml"
                else t
            ),
            "module core sets groupId com.example; the bundle is com.controlvar",
        ),
        (
            "a module with its own version",
            lambda r, t: (
                t.replace(
                    "<artifactId>controlvar-spring-boot-starter</artifactId>",
                    "<artifactId>controlvar-spring-boot-starter</artifactId>\n    <version>9.9.9</version>",
                )
                if r == "spring-boot-starter/pom.xml"
                else t
            ),
            "module spring-boot-starter sets version 9.9.9; the parent is 0.1.0",
        ),
        (
            "a parent with no version",
            lambda r, t: (
                t.replace(
                    f"    <version>{VERSION}</version>\n    <packaging>pom",
                    "    <packaging>pom",
                )
                if r == "pom.xml"
                else t
            ),
            "sdk/java/pom.xml declares no <version>",
        ),
        (
            "a module with no POM",
            lambda r, t: (
                t.replace(
                    "<module>core</module>",
                    "<module>core</module>\n        <module>missing</module>",
                )
                if r == "pom.xml"
                else t
            ),
            "module 'missing' has no pom.xml beside the parent's",
        ),
    ],
)
def test_the_maven_version_check_refuses(
    case: str, change, refusal: str, tmp_path: Path
) -> None:
    tree = _java_tree(tmp_path, change)
    result = _check_version(tree)
    assert result.returncode != 0, (case, result.stdout)
    assert refusal in result.stderr, (case, result.stderr)
