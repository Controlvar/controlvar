"""The release attaches the packaged Helm chart, and checks it first.

Two things are pinned here:

* ``release.yml``'s job graph around the ``chart`` job -- it runs after
  ``publish`` (the release must exist before an asset can be attached), and
  ``floating-tags`` waits for it, so a release whose chart failed never moves
  ``:core`` / ``:full``;
* ``scripts/check_chart_package.py``, the step that compares the packaged
  chart with the tag, broken on purpose against archives built here.

Cheap reads and in-memory tarballs, no helm: these run in the ordinary unit job.
"""

from __future__ import annotations

import io
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
CHART_CI = ROOT / ".github" / "workflows" / "chart.yml"
CHECKER = ROOT / "scripts" / "check_chart_package.py"

pytestmark = pytest.mark.unit


def _jobs(path: Path = RELEASE) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]


def _needs(job: dict) -> set[str]:
    needs = job.get("needs", [])
    return {needs} if isinstance(needs, str) else set(needs)


def _runs(job: dict) -> list[str]:
    return [step.get("run", "") for step in job["steps"]]


def _index(runs: list[str], needle: str) -> int:
    hits = [i for i in range(len(runs)) if needle in runs[i]]
    assert len(hits) == 1, f"expected one step running {needle!r}, found {len(hits)}"
    return hits[0]


# --- the job graph -----------------------------------------------------------


def test_every_site_that_follows_the_chart_name_agrees() -> None:
    """`helm package` names the archive after Chart.yaml's `name:`; the checker
    and the upload must expect that file. The chart's directory keeps its
    former name, so nothing here may be derived from the path."""
    chart = yaml.safe_load(
        (ROOT / "charts" / "experimently" / "Chart.yaml").read_text()
    )
    assert chart["name"] == "controlvar"
    checker = CHECKER.read_text(encoding="utf-8")
    assert f'CHART_NAME = "{chart["name"]}"' in checker
    release = RELEASE.read_text(encoding="utf-8")
    assert f'"dist/{chart["name"]}-${{VERSION}}.tgz"' in release
    assert f'grep -Fx "{chart["name"]}-${{VERSION}}.tgz"' in release


def test_chart_job_runs_after_the_release_exists() -> None:
    assert _needs(_jobs()["chart"]) == {"verify", "publish"}


def test_floating_tags_wait_for_the_chart() -> None:
    """A release whose chart did not attach must not move :core / :full."""
    assert _needs(_jobs()["floating-tags"]) == {
        "verify",
        "api-images",
        "web-images",
        "publish",
        "chart",
    }


def test_publish_does_not_wait_for_the_chart() -> None:
    """The reverse edge would be a cycle: the chart attaches to what publish made."""
    assert "chart" not in _needs(_jobs()["publish"])


def test_chart_job_runs_for_pre_releases_too() -> None:
    """An rc gets its chart like any release; only floating-tags skips them."""
    assert "if" not in _jobs()["chart"]


# --- what the job does ---------------------------------------------------------


def test_chart_job_lints_packages_checks_then_attaches_in_that_order() -> None:
    runs = _runs(_jobs()["chart"])
    lint = _index(runs, "helm lint --strict charts/experimently")
    package = _index(runs, "helm package charts/experimently")
    check = _index(runs, "scripts/check_chart_package.py")
    upload = _index(runs, "gh release upload")
    assert lint < package < check < upload


def test_chart_job_lints_both_ci_values_files() -> None:
    run = _runs(_jobs()["chart"])[_index(_runs(_jobs()["chart"]), "helm lint")]
    assert "charts/experimently/ci/values-core.yaml" in run
    assert "charts/experimently/ci/values-full.yaml" in run


def test_helm_package_takes_its_version_from_chart_yaml() -> None:
    """`--version` / `--app-version` would make the version check vacuous."""
    runs = _runs(_jobs()["chart"])
    run = runs[_index(runs, "helm package")]
    assert "--version" not in run
    assert "--app-version" not in run


def test_the_check_is_against_the_release_version() -> None:
    job = _jobs()["chart"]
    step = next(s for s in job["steps"] if "check_chart_package.py" in s.get("run", ""))
    assert '--expect "$VERSION"' in step["run"]
    assert '"dist/controlvar-${VERSION}.tgz"' in step["run"]
    assert step["env"]["VERSION"] == "${{ needs.verify.outputs.version }}"


def test_the_upload_replaces_on_a_rerun_and_confirms_the_asset() -> None:
    job = _jobs()["chart"]
    step = next(s for s in job["steps"] if "gh release upload" in s.get("run", ""))
    assert '"dist/controlvar-${VERSION}.tgz" --clobber' in step["run"]
    assert 'grep -Fx "controlvar-${VERSION}.tgz"' in step["run"]
    assert step["env"]["TAG"] == "${{ needs.verify.outputs.tag }}"


def test_chart_job_checks_out_the_tag() -> None:
    checkout = _jobs()["chart"]["steps"][0]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"]["ref"] == "${{ needs.verify.outputs.tag }}"


def test_chart_job_is_a_release_asset_only() -> None:
    """No registry push from this job: it may write the release, nothing else.

    Publishing the chart to a registry is a separate, later change with its own
    approval step; widening these permissions belongs to that change.
    """
    job = _jobs()["chart"]
    assert job["permissions"] == {"contents": "write"}
    runs = "\n".join(_runs(job))
    assert "helm push" not in runs
    assert "oras " not in runs


def test_chart_job_uses_the_same_helm_as_the_chart_ci() -> None:
    ours = _jobs()["chart"]["env"]
    theirs = yaml.safe_load(CHART_CI.read_text(encoding="utf-8"))["env"]
    assert ours["HELM_VERSION"] == theirs["HELM_VERSION"]
    assert ours["HELM_SHA256"] == theirs["HELM_SHA256"]


# --- scripts/check_chart_package.py --------------------------------------------


PACKAGED = """\
annotations:
  licenses: Apache-2.0
apiVersion: v2
appVersion: {app}
description: 'Controlvar: the API and the dashboard.'
name: {name}
type: application
version: {version}
"""


def _archive(
    tmp_path: Path,
    *,
    version: str = "1.2.3",
    app: str = "1.2.3",
    name: str = "controlvar",
    filename: str | None = None,
    extra: dict[str, str] | None = None,
) -> Path:
    """A .tgz shaped like `helm package` output (checked against helm 3.19)."""
    path = tmp_path / (filename or f"controlvar-{version}.tgz")
    files = {
        "controlvar/Chart.yaml": PACKAGED.format(app=app, name=name, version=version)
    }
    files["controlvar/values.yaml"] = "replicaCount: 1\n"
    files.update(extra or {})
    with tarfile.open(path, "w:gz") as tar:
        for member, text in files.items():
            data = text.encode("utf-8")
            info = tarfile.TarInfo(member)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def _check(archive: Path, expect: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CHECKER), str(archive), "--expect", expect],
        capture_output=True,
        text=True,
    )


def test_checker_accepts_the_release(tmp_path: Path) -> None:
    result = _check(_archive(tmp_path), "1.2.3")
    assert result.returncode == 0, result.stderr
    assert "packaged chart is controlvar 1.2.3" in result.stdout


def test_checker_accepts_a_pre_release_and_quoted_values(tmp_path: Path) -> None:
    archive = _archive(tmp_path, version="1.2.3-rc.1", app='"1.2.3-rc.1"')
    result = _check(archive, "1.2.3-rc.1")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {"version": "1.2.2", "filename": "controlvar-1.2.3.tgz"},
            "version is '1.2.2'",
        ),
        ({"app": "1.2.2"}, "appVersion is '1.2.2'"),
        ({"app": "v1.2.3"}, "appVersion is 'v1.2.3'"),
        ({"version": "1.2.3.0", "filename": "controlvar-1.2.3.tgz"}, "version is"),
        ({"name": "other"}, "name is 'other'"),
        ({"filename": "chart.tgz"}, "file is 'chart.tgz'"),
        (
            {"extra": {"controlvar/charts/sub/Chart.yaml": "name: sub\n"}},
            "expected exactly one Chart.yaml",
        ),
    ],
)
def test_checker_rejects(tmp_path: Path, kwargs: dict, message: str) -> None:
    result = _check(_archive(tmp_path, **kwargs), "1.2.3")
    assert result.returncode != 0, result.stdout
    assert message in result.stderr


def test_checker_rejects_a_missing_archive(tmp_path: Path) -> None:
    result = _check(tmp_path / "controlvar-1.2.3.tgz", "1.2.3")
    assert result.returncode != 0
    assert "No such file" in result.stderr


def test_checker_rejects_a_chart_yaml_with_no_app_version(tmp_path: Path) -> None:
    path = tmp_path / "controlvar-1.2.3.tgz"
    data = b"apiVersion: v2\nname: controlvar\nversion: 1.2.3\n"
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo("controlvar/Chart.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    result = _check(path, "1.2.3")
    assert result.returncode != 0
    assert "found 0" in result.stderr
