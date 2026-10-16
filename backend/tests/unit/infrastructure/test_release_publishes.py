"""What a release publishes, read from ``.github/workflows/release.yml`` (#1142).

A release starts from a tag. It builds four images, the API and the dashboard
in the core and full profiles, each tagged ``<profile>-<version>`` and built
for ``linux/amd64`` only; it signs each one and attests its SPDX SBOM, and
attaches the same SBOM files to the GitHub release. A pre-release (a version
with a ``-``) never moves the floating ``:core`` / ``:full`` tags. And the
version the API reports is the repository's ``VERSION`` file.

YAML and text reads, plus one in-process request: no Docker, no registry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest
import yaml
from fastapi.testclient import TestClient

from backend.app.core.config import settings
from backend.app.main import app

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
RELEASE = REPO_ROOT / ".github" / "workflows" / "release.yml"
IMAGE_JOBS = ("api-images", "web-images")

needs_workflows = pytest.mark.skipif(
    not RELEASE.is_file(), reason="this tree has no .github/workflows"
)


@pytest.fixture(scope="module")
def workflow() -> Dict[str, Any]:
    return yaml.safe_load(RELEASE.read_text(encoding="utf-8"))


def triggers(workflow: Dict[str, Any]) -> Dict[str, Any]:
    # PyYAML reads the bare key `on` as True.
    return workflow.get("on", workflow.get(True))


def runs(job: Dict[str, Any]) -> str:
    return "\n".join(str(step.get("run", "")) for step in job["steps"])


@needs_workflows
def test_a_release_starts_from_a_tag(workflow):
    on = triggers(workflow)
    assert set(on) == {"workflow_call", "push", "workflow_dispatch"}
    assert on["push"] == {"tags": ["v*"]}
    assert on["workflow_call"]["inputs"]["tag"]["required"] is True
    assert on["workflow_dispatch"]["inputs"]["tag"]["required"] is True
    verify = workflow["jobs"]["verify"]
    checkout = next(
        s for s in verify["steps"] if s.get("uses", "").startswith("actions/checkout")
    )
    assert checkout["with"]["ref"] == "${{ steps.resolve.outputs.tag }}"


@needs_workflows
@pytest.mark.parametrize("job", IMAGE_JOBS)
def test_each_image_is_both_profiles_tagged_with_the_version_and_amd64_only(
    workflow, job
):
    assert workflow["env"]["API_PLATFORMS"] == "linux/amd64"
    assert workflow["env"]["WEB_PLATFORMS"] == "linux/amd64"
    spec = workflow["jobs"][job]
    assert spec["strategy"]["matrix"]["profile"] == ["core", "full"]
    assert 'tags="${IMAGE}:${PROFILE}-${VERSION}"' in runs(spec)
    platforms = {
        step["with"]["platforms"]
        for step in spec["steps"]
        if "platforms" in (step.get("with") or {})
    }
    family = "API" if job == "api-images" else "WEB"
    assert platforms == {"${{ env.%s_PLATFORMS }}" % family}


@needs_workflows
def test_the_web_image_is_the_api_image_name_with_web(workflow):
    resolve = workflow["jobs"]["verify"]["steps"][0]["run"]
    assert '"web_image=${image}-web"' in resolve


@needs_workflows
def test_the_image_path_and_signing_identity_follow_the_repository(workflow):
    """Nothing names the repository: the image path and the identity the notes
    give come from github.repository, so a rename or a transfer moves both."""
    resolve = workflow["jobs"]["verify"]["steps"][0]
    assert resolve["env"]["REPOSITORY"] == "${{ github.repository }}"
    assert (
        "image=\"${REGISTRY}/$(echo \"$REPOSITORY\" | tr '[:upper:]' '[:lower:]')\""
        in resolve["run"]
    )
    notes = "\n".join(runs(job) for job in workflow["jobs"].values())
    assert (
        'identity="^https://github\\\\.com/${REPOSITORY}/\\\\.github/workflows/'
        in notes
    )


@needs_workflows
@pytest.mark.parametrize("job", IMAGE_JOBS)
def test_each_image_is_signed_and_carries_its_sbom_as_an_attestation(workflow, job):
    text = runs(workflow["jobs"][job])
    assert 'cosign sign --yes "${IMAGE}@${DIGEST}"' in text
    assert "cosign attest --yes --type spdxjson" in text


@needs_workflows
def test_the_github_release_carries_the_same_sbom_files(workflow):
    publish = workflow["jobs"]["publish"]
    download = next(
        s
        for s in publish["steps"]
        if s.get("uses", "").startswith("actions/download-artifact")
    )
    assert download["with"]["pattern"] == "sbom-*"
    assert 'gh release upload "$TAG" sboms/* --clobber' in runs(publish)
    for job in IMAGE_JOBS:
        uploads = [
            s["with"]["path"]
            for s in workflow["jobs"][job]["steps"]
            if s.get("uses", "").startswith("actions/upload-artifact")
            and str(s["with"].get("name", "")).startswith("sbom-")
        ]
        assert uploads and all(p.endswith(".spdx.json") for p in uploads), job


@needs_workflows
def test_a_pre_release_never_moves_the_floating_tags(workflow):
    resolve = workflow["jobs"]["verify"]["steps"][0]["run"]
    assert "*-*) prerelease=true ;;" in resolve
    assert workflow["jobs"]["floating-tags"]["if"] == (
        "needs.verify.outputs.prerelease == 'false'"
    )


def test_the_api_reports_the_version_file():
    version = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert settings.VERSION == version
    assert TestClient(app).get("/api/v1/modules").json()["version"] == version
