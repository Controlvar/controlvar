"""chart-kind's N-1 falls back one release while the newest one's images publish.

Right after a release its tag exists minutes before release.yml has published
its images. ``scripts/chart_kind.sh previous`` used to resolve N-1 to that tag
regardless, the upgrade leg's pull failed, and main went red until the images
existed (#519). Now ``previous`` reads each candidate's two images (this
profile's api and dashboard) from GHCR anonymously, and:

* the newest tag at or below VERSION is published -> N-1 is that tag;
* it is not -> N-1 is the release before it, with a warning and a line in the
  job summary saying which and why;
* neither is -> the job fails (a package that is no longer public reads as
  "manifest unknown" too, so the fallback goes one step and no further);
* a PREVIOUS_VERSION given by name is never replaced.

Run as shell, the script's own functions extracted, with ``git`` and
``docker`` stubbed: no registry and no cluster. Each tag's tree carries a chart
naming the repositories below, which ``published`` reads from that tag
(test_chart_kind_previous_per_tag.py).
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import textwrap

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.regression]

ROOT = pathlib.Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "chart_kind.sh"

API = "ghcr.io/controlvar/controlvar"
WEB = "ghcr.io/controlvar/controlvar-web"


def _function(name: str) -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    match = re.search(rf"(?m)^{name}\(\) {{ .*}}\n", text) or re.search(
        rf"(?ms)^{name}\(\) {{\n.*?^}}\n", text
    )
    assert match, f"{name}() not found in {SCRIPT}"
    return match.group(0)


def _assignment(name: str) -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    match = re.search(rf"(?m)^{name}=.*$", text)
    assert match, f"{name}= not found in {SCRIPT}"
    return match.group(0)


def _harness() -> str:
    parts = ["set -euo pipefail", "PROFILE=core", 'WORK="$TMPDIR/work"']
    parts += [_assignment(a) for a in ("API_REPO", "WEB_REPO", "PULL_FAILED")]
    parts += [
        _function(f)
        for f in (
            "say",
            "fail",
            "values_repo",
            "tag_images",
            "published",
            "do_previous",
        )
    ]
    return "\n".join(parts) + "\ndo_previous\nprintf 'still-running\\n'\n"


def _stub(bindir: pathlib.Path, name: str, body: str) -> None:
    path = bindir / name
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body), encoding="utf-8")
    path.chmod(0o755)


def _resolve(
    tmp_path: pathlib.Path,
    unpublished: list[str],
    *,
    tags: tuple[str, ...] = ("0.16.1", "0.16.2", "0.17.0", "0.18.0"),
    version: str = "0.17.0",
    previous_version: str = "",
) -> tuple[subprocess.CompletedProcess, str, str, str]:
    """Run ``previous``; the registry answers "manifest unknown" for UNPUBLISHED refs."""
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True)
    (tmp_path / "VERSION").write_text(version + "\n", encoding="utf-8")
    (tmp_path / "unpublished").write_text(
        "".join(r + "\n" for r in unpublished), encoding="utf-8"
    )
    lines = "".join(f"printf 'x\\trefs/tags/v{t}\\n'\n" for t in tags)
    for t in tags:
        chart = tmp_path / "trees" / f"v{t}" / "charts" / "experimently"
        chart.mkdir(parents=True)
        (chart / "Chart.yaml").write_text(f'appVersion: "{t}"\n', encoding="utf-8")
        (chart / "values.yaml").write_text(
            f"image:\n  api:\n    repository: {API}\n  web:\n    repository: {WEB}\n",
            encoding="utf-8",
        )
    # ls-remote lists the tags; archive serves a tag's tree; fetch succeeds.
    _stub(
        bindir,
        "git",
        'case "$1" in\n'
        f"  ls-remote)\n{lines}  ;;\n"
        "  fetch) ;;\n"
        f'  archive) tag=$2; shift 2; tar -c -C "{tmp_path}/trees/$tag" "$@" ;;\n'
        "  *) exit 64 ;;\n"
        "esac\n",
    )
    # docker manifest inspect REF: logs the call and the client configuration
    # it was given; fails like GHCR for a ref listed as unpublished.
    _stub(
        bindir,
        "docker",
        """\
        ref="${@: -1}"
        printf '%s|%s\\n' "$*" "$(cat "${DOCKER_CONFIG:-/nonexistent}/config.json" 2>/dev/null)" >>"$LOG"
        [ "$1 $2" = "manifest inspect" ] || exit 64
        if grep -Fqx "$ref" "$UNPUBLISHED"; then
            printf 'manifest unknown\\n' >&2
            exit 1
        fi
        printf '{"schemaVersion": 2}\\n'
        """,
    )
    out = tmp_path / "github_output"
    summary = tmp_path / "step_summary"
    log = tmp_path / "docker.log"
    # A logged-in client: a manifest read that used it would not be anonymous.
    home_cfg = tmp_path / ".docker"
    home_cfg.mkdir()
    (home_cfg / "config.json").write_text(
        '{"auths": {"ghcr.io": {"auth": "c3R1YjpzdHVi"}}}\n', encoding="utf-8"
    )
    env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "DOCKER_CONFIG": str(home_cfg),
        "LOG": str(log),
        "UNPUBLISHED": str(tmp_path / "unpublished"),
        "GITHUB_OUTPUT": str(out),
        "GITHUB_STEP_SUMMARY": str(summary),
        "PREVIOUS_VERSION": previous_version,
    }
    result = subprocess.run(
        ["bash", "-c", _harness()],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    def _read(p: pathlib.Path) -> str:
        return p.read_text(encoding="utf-8") if p.exists() else ""

    return result, _read(out), _read(summary), _read(log)


def test_the_newest_published_release_is_n_minus_1(tmp_path):
    result, out, summary, log = _resolve(tmp_path, [])
    assert result.returncode == 0, result.stdout + result.stderr
    assert out.splitlines() == ["version=0.17.0"]
    assert summary == ""
    assert "::warning::" not in result.stdout
    # Both of this profile's images were read, anonymously; the tag above
    # VERSION was never considered.
    assert f"manifest inspect {API}:core-0.17.0|{{}}" in log
    assert f"manifest inspect {WEB}:core-0.17.0|{{}}" in log
    assert "auths" not in log
    assert "0.18.0" not in log


def test_an_unpublished_newest_release_falls_back_to_the_one_before(tmp_path):
    result, out, summary, _ = _resolve(tmp_path, [f"{API}:core-0.17.0"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "still-running" in result.stdout
    assert out.splitlines() == ["version=0.16.2"]
    note = (
        "N-1 is 0.16.2, not 0.17.0: v0.17.0 is tagged but its core images are "
        "not published yet"
    )
    assert note in summary, summary
    assert f"::warning::{note}" in result.stdout


def test_one_missing_image_is_enough_to_fall_back(tmp_path):
    # The api image published, the dashboard's not yet: N-1 needs both.
    result, out, summary, _ = _resolve(tmp_path, [f"{WEB}:core-0.17.0"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert out.splitlines() == ["version=0.16.2"]
    assert "N-1 is 0.16.2, not 0.17.0" in summary


def test_no_published_release_fails_closed(tmp_path):
    result, out, summary, _ = _resolve(
        tmp_path, [f"{API}:core-0.17.0", f"{API}:core-0.16.2"]
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "still-running" not in result.stdout
    assert out == ""
    assert "::error::N-1 cannot be resolved: neither v0.17.0 nor v0.16.2" in (
        result.stdout
    )
    assert "is the package still public?" in result.stdout


def test_the_fallback_goes_one_release_back_and_no_further(tmp_path):
    # 0.16.1 is published, but two releases back is not the publishing window.
    result, out, _, _ = _resolve(tmp_path, [f"{API}:core-0.17.0", f"{WEB}:core-0.16.2"])
    assert result.returncode != 0, result.stdout + result.stderr
    assert out == ""


def test_an_unpublished_only_release_fails_closed(tmp_path):
    result, out, _, _ = _resolve(
        tmp_path, [f"{API}:core-0.17.0"], tags=("0.17.0", "0.18.0")
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert out == ""
    assert "no earlier release to fall back to" in result.stdout


def test_a_previous_version_given_by_name_is_never_replaced(tmp_path):
    result, out, summary, _ = _resolve(
        tmp_path, [f"{API}:core-0.16.2"], previous_version="0.16.2"
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert out == ""
    assert summary == ""
    assert "PREVIOUS_VERSION 0.16.2 has no published core images" in result.stdout

    named, named_out, _, _ = _resolve(
        tmp_path / "published", [], previous_version="0.16.1"
    )
    assert named.returncode == 0, named.stdout + named.stderr
    assert named_out.splitlines() == ["version=0.16.1"]
