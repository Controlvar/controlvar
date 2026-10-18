"""The release's image-label gate, against the shapes a registry really returns.

``.github/workflows/release.yml`` runs ``scripts/check_image_labels.py`` on
every image it has just pushed, before signing it.  The labels are set from a
``--build-arg``, which means a renamed ``ARG`` in either Dockerfile silently
produces a whole release labelled ``0.0.0+unknown`` -- the exact shape of
defect this repository keeps finding: a check that passes for the wrong reason,
or none at all.

So the gate itself is tested, and tested by *breaking* it.  The fixtures below
are trimmed from real ``docker buildx imagetools inspect --format
'{{ json .Image }}'`` output captured against a registry: a single-platform
image (one config object at the top level) and a two-platform one built with
``--provenance=mode=max --sbom=true`` (a map keyed by platform -- and note that
the attestation manifests riding in the same index do *not* appear here, which
is why the script needs no special case for them).

Cheap text reads, no Docker: these run in the ordinary unit job.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
CHECKER = ROOT / "scripts" / "check_image_labels.py"
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
PR_GATE = ROOT / ".github" / "workflows" / "pr-qa-gate.yml"

SOURCE = "https://github.com/Controlvar/controlvar"
NGINX_SOURCE = "https://github.com/nginx/docker-nginx-unprivileged"

pytestmark = pytest.mark.unit


def labels(version: str = "0.1.0", profile: str = "core") -> dict:
    return {
        "org.opencontainers.image.title": "Controlvar API",
        "org.opencontainers.image.licenses": "Apache-2.0",
        "org.opencontainers.image.version": version,
        "org.opencontainers.image.source": SOURCE,
        "org.opencontainers.image.url": SOURCE,
        "io.experimently.profile": profile,
    }


def single_platform(**kwargs) -> dict:
    """One image config at the top level, as a single-platform manifest gives."""
    return {
        "created": "2026-09-18T00:00:00Z",
        "architecture": "arm64",
        "os": "linux",
        "config": {"Labels": labels(**kwargs)},
        "rootfs": {"type": "layers", "diff_ids": []},
    }


def multi_platform(second: dict | None = None, **kwargs) -> dict:
    """A map keyed by platform, as a manifest list gives."""
    first = dict(single_platform(**kwargs), architecture="amd64")
    return {
        "linux/amd64": first,
        "linux/arm64": second if second is not None else single_platform(**kwargs),
    }


def run(payload: dict, tmp_path: Path, version: str = "0.1.0", profile: str = "core"):
    path = tmp_path / "image.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--file",
            str(path),
            "--version",
            version,
            "--profile",
            profile,
        ],
        capture_output=True,
        text=True,
    )


def test_single_platform_image_passes(tmp_path: Path) -> None:
    result = run(single_platform(), tmp_path)
    assert result.returncode == 0, result.stderr
    assert "1 platform config(s)" in result.stdout


def test_multi_platform_image_passes(tmp_path: Path) -> None:
    result = run(multi_platform(), tmp_path)
    assert result.returncode == 0, result.stderr
    assert "2 platform config(s)" in result.stdout


def test_image_built_without_the_version_arg_is_refused(tmp_path: Path) -> None:
    """The defect the gate exists for: the Dockerfile's placeholder default."""
    result = run(single_platform(version="0.0.0+unknown"), tmp_path)
    assert result.returncode != 0
    assert "0.0.0+unknown" in result.stderr


def test_one_bad_platform_out_of_two_is_refused(tmp_path: Path) -> None:
    """A per-platform check, not a first-one-wins check.

    An arm64 variant built without the argument while amd64 carries it is the
    failure a "read the first config" implementation would wave through.
    """
    payload = multi_platform(second=single_platform(version="0.0.0+unknown"))
    result = run(payload, tmp_path)
    assert result.returncode != 0
    assert "linux/arm64" in result.stderr


def test_wrong_profile_is_refused(tmp_path: Path) -> None:
    """A `full` tag must not be a `core` image: the profile decides what ships."""
    result = run(single_platform(profile="core"), tmp_path, profile="full")
    assert result.returncode != 0
    assert "io.experimently.profile" in result.stderr


def test_missing_licence_label_is_refused(tmp_path: Path) -> None:
    payload = single_platform()
    del payload["config"]["Labels"]["org.opencontainers.image.licenses"]
    result = run(payload, tmp_path)
    assert result.returncode != 0
    assert "licenses" in result.stderr


def test_no_labels_at_all_is_refused(tmp_path: Path) -> None:
    payload = single_platform()
    payload["config"] = {}
    result = run(payload, tmp_path)
    assert result.returncode != 0


def test_confirming_the_placeholder_version_is_refused(tmp_path: Path) -> None:
    """Asking the gate to bless 0.0.0+unknown must fail, not tautologically pass."""
    result = run(
        single_platform(version="0.0.0+unknown"), tmp_path, version="0.0.0+unknown"
    )
    assert result.returncode != 0
    assert "placeholder" in result.stderr


def test_unparseable_payload_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "image.json"
    path.write_text("[]", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--file",
            str(path),
            "--version",
            "0.1.0",
            "--profile",
            "core",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0


def test_both_dockerfiles_define_the_version_arg_the_workflow_passes() -> None:
    """The other half of the gate: the ARG the label reads has to exist.

    ``docker build`` warns about an unknown ``--build-arg`` and carries on, so
    a renamed ARG produces a green build and an unlabelled image; the registry
    check above catches it after the push, and this catches it in review.
    """
    for dockerfile in ("backend/Dockerfile", "frontend/Dockerfile"):
        text = (ROOT / dockerfile).read_text(encoding="utf-8")
        assert "ARG VERSION=0.0.0+unknown" in text, (
            f"{dockerfile} must declare `ARG VERSION` with the placeholder default; "
            "release.yml passes --build-arg VERSION and the label reads it"
        )
        assert 'org.opencontainers.image.version="${VERSION}"' in text, (
            f"{dockerfile} must label the image from that ARG"
        )


# ---------------------------------------------------------------------------
# #239: the images say where they come from, and the notes say what was built.
# ---------------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("key", ["source", "url"])
def test_base_image_source_is_refused(tmp_path: Path, key: str) -> None:
    """The dashboard's nginx base labels itself; an image inheriting that fails.

    ``nginxinc/nginx-unprivileged`` sets both ``source`` and ``url`` to its own
    repository, and a LABEL is inherited unless overridden.
    """
    payload = single_platform()
    payload["config"]["Labels"][f"org.opencontainers.image.{key}"] = NGINX_SOURCE
    result = run(payload, tmp_path)
    assert result.returncode != 0
    assert f"org.opencontainers.image.{key}" in result.stderr
    assert NGINX_SOURCE in result.stderr


@pytest.mark.regression
@pytest.mark.parametrize("key", ["source", "url"])
def test_missing_source_label_is_refused(tmp_path: Path, key: str) -> None:
    payload = single_platform()
    del payload["config"]["Labels"][f"org.opencontainers.image.{key}"]
    result = run(payload, tmp_path)
    assert result.returncode != 0
    assert f"org.opencontainers.image.{key}" in result.stderr


def docker_image_inspect(**kwargs) -> list:
    """``docker image inspect <ref>``: a list, capitalised keys."""
    return [
        {
            "Id": "sha256:0",
            "Os": "linux",
            "Architecture": "amd64",
            "Config": {"Labels": labels(**kwargs)},
            "RootFS": {"Type": "layers", "Layers": []},
        }
    ]


def test_local_docker_inspect_output_passes(tmp_path: Path) -> None:
    """The shape Docker Smoke feeds it, from images it has only loaded."""
    result = run(docker_image_inspect(), tmp_path)
    assert result.returncode == 0, result.stderr
    assert "1 platform config(s)" in result.stdout


def test_local_docker_inspect_output_with_wrong_source_is_refused(
    tmp_path: Path,
) -> None:
    payload = docker_image_inspect()
    payload[0]["Config"]["Labels"]["org.opencontainers.image.source"] = NGINX_SOURCE
    result = run(payload, tmp_path)
    assert result.returncode != 0
    assert "linux/amd64" in result.stderr


def test_local_docker_inspect_with_no_labels_is_refused(tmp_path: Path) -> None:
    payload = docker_image_inspect()
    payload[0]["Config"] = {"Labels": None}
    result = run(payload, tmp_path)
    assert result.returncode != 0


@pytest.mark.regression
def test_both_dockerfiles_label_this_repository_as_the_source() -> None:
    """Caught in review, before the registry check sees a pushed image."""
    for dockerfile in ("backend/Dockerfile", "frontend/Dockerfile"):
        text = (ROOT / dockerfile).read_text(encoding="utf-8")
        for key in ("source", "url"):
            assert f'org.opencontainers.image.{key}="{SOURCE}"' in text, (
                f"{dockerfile} must set org.opencontainers.image.{key} to {SOURCE}"
            )


def _workflow(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _built_platforms(workflow: dict) -> dict[str, set[str]]:
    """What each build-push step in release.yml actually builds, per file."""
    env = workflow.get("env") or {}
    built: dict[str, set[str]] = {}
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            if "docker/build-push-action" not in str(step.get("uses", "")):
                continue
            value = str(step["with"]["platforms"])
            ref = re.fullmatch(r"\$\{\{\s*env\.(\w+)\s*\}\}", value.strip())
            if ref:
                value = str(env[ref.group(1)])
            built[step["with"]["file"]] = {
                p.strip() for p in value.split(",") if p.strip()
            }
    return built


def test_release_builds_read_their_platforms_from_one_place() -> None:
    """A literal in a build step is a second copy the notes cannot follow."""
    workflow = _workflow(RELEASE)
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            if "docker/build-push-action" in str(step.get("uses", "")):
                assert re.fullmatch(
                    r"\$\{\{\s*env\.\w+_PLATFORMS\s*\}\}",
                    str(step["with"]["platforms"]).strip(),
                ), f"{step.get('name')}: platforms must be ${{{{ env.*_PLATFORMS }}}}"


def _render_release_notes(tmp_path: Path, env_overrides: dict[str, str]) -> str:
    workflow = _workflow(RELEASE)
    step = next(
        s
        for s in workflow["jobs"]["publish"]["steps"]
        if s.get("name") == "Release notes"
    )
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "IMAGE": "ghcr.io/controlvar/controlvar",
        "WEB_IMAGE": "ghcr.io/controlvar/controlvar-web",
        "VERSION": "1.2.3",
        "REPOSITORY": "Controlvar/controlvar",
    }
    env.update({k: str(v) for k, v in (workflow.get("env") or {}).items()})
    env.update(env_overrides)
    result = subprocess.run(
        [shutil.which("bash") or "bash", "-c", step["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return (tmp_path / "notes.md").read_text(encoding="utf-8")


@pytest.mark.regression
def test_release_notes_name_only_the_platforms_that_were_built(tmp_path: Path) -> None:
    """#239: the notes said ``linux/arm64`` for images built amd64 only."""
    built = _built_platforms(_workflow(RELEASE))
    api, web = built["backend/Dockerfile"], built["frontend/Dockerfile"]
    notes = _render_release_notes(tmp_path, {})
    named = set(re.findall(r"linux/[a-z0-9]+(?:/v[0-9]+)?", notes))
    assert named, notes
    assert named <= api | web, (
        f"notes name {sorted(named - api - web)}, never built\n{notes}"
    )
    assert api <= named and web <= named, notes
    assert "multi-architecture" not in notes, notes


def test_release_notes_follow_a_second_platform(tmp_path: Path) -> None:
    """The text is derived, so adding a platform changes it, SBOM caveat and all."""
    notes = _render_release_notes(
        tmp_path, {"API_PLATFORMS": "linux/amd64,linux/arm64"}
    )
    assert "`linux/amd64`, `linux/arm64`" in notes, notes
    assert "multi-architecture index" in notes, notes


def test_release_notes_refuse_an_empty_platform_list(tmp_path: Path) -> None:
    """Notes saying "built for ;" would be worse than the literal they replace."""
    workflow = _workflow(RELEASE)
    step = next(
        s
        for s in workflow["jobs"]["publish"]["steps"]
        if s.get("name") == "Release notes"
    )
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "IMAGE": "i",
        "WEB_IMAGE": "w",
        "VERSION": "1.2.3",
        "REPOSITORY": "o/r",
        "API_PLATFORMS": " , ",
        "WEB_PLATFORMS": "linux/amd64",
    }
    result = subprocess.run(
        [shutil.which("bash") or "bash", "-c", step["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "API_PLATFORMS" in result.stdout + result.stderr


def test_docker_smoke_runs_the_label_check_on_every_image_it_builds() -> None:
    """The PR-time half of the gate: wired, and over all three smoke images."""
    job = _workflow(PR_GATE)["jobs"]["docker-smoke"]
    built = [
        step["with"]["tags"]
        for step in job["steps"]
        if "docker/build-push-action" in str(step.get("uses", ""))
    ]
    check = next(
        s
        for s in job["steps"]
        if "scripts/check_image_labels.py" in str(s.get("run", ""))
    )
    for tag in built:
        assert f"{tag}=" in check["run"], (
            f"{tag} is built but its labels are not checked"
        )
    for step in job["steps"]:
        if "docker/build-push-action" in str(step.get("uses", "")):
            assert "VERSION=" in str(step["with"].get("build-args", "")), step.get(
                "name"
            )
