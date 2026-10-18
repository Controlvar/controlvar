"""chart-kind resolves N-1's chart and images from N-1's own tag.

``scripts/chart_kind.sh`` used to read N-1 through this checkout's names: the
``previous`` phase asked the registry for ``$API_REPO:<profile>-<N-1>`` and the
``upgrade-previous`` phase pulled, tagged and loaded N-1's images under
``$API_REPO``/``$WEB_REPO`` and installed ``git archive v<N-1> $CHART_DIR``.
Those are HEAD's names. A release whose chart directory or image repositories
differ from HEAD's -- every release before a change to either -- then read as
"not published" and the upgrade leg could not start.

Now each candidate tag's chart and image repositories come from that tag's own
``charts/*/Chart.yaml`` and ``values.yaml`` (``tag_images``): the manifest
reads, the pulls, the names loaded into kind, the chart installed, and the
names ``assert_images`` expects are all N-1's.

Run as shell, the script's own functions extracted, with ``git``, ``docker``,
``kind``, ``helm`` and ``kubectl`` stubbed: no registry and no cluster. The
fixture has two namespaces: the tags name ``ghcr.io/example/old`` and
``charts/oldchart``, "this checkout" names ``ghcr.io/example/new`` and
``charts/newchart``.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import textwrap

import pytest
import yaml

pytestmark = [pytest.mark.unit, pytest.mark.regression]

ROOT = pathlib.Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "chart_kind.sh"

OLD_API = "ghcr.io/example/old"
OLD_WEB = "ghcr.io/example/old-web"
NEW_API = "ghcr.io/example/new"
NEW_WEB = "ghcr.io/example/new-web"

# This checkout's names, as the harness sets them in place of the script's.
HEAD = (
    f"API_REPO={NEW_API}",
    f"WEB_REPO={NEW_WEB}",
    "CHART_DIR=charts/newchart",
    "CLUSTER=chart-kind",
    "HOST=kind.test",
    "ADMIN=admin@example.com",
)


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


def _harness(*functions: str, extra: str = "") -> str:
    parts = ["set -euo pipefail", "PROFILE=core", 'WORK="$TMPDIR/work"', *HEAD]
    parts.append(_assignment("PULL_FAILED"))
    parts += [_function(f) for f in ("say", "fail", "values_repo", "tag_images")]
    parts += [_function(f) for f in functions]
    return "\n".join(parts) + "\n" + textwrap.dedent(extra)


def _stub(bindir: pathlib.Path, name: str, body: str) -> None:
    path = bindir / name
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body), encoding="utf-8")
    path.chmod(0o755)


def _values(api: str, web: str) -> str:
    return textwrap.dedent(
        f"""\
        profile: core
        image:
          # -- a comment at the key's indent
          tag: ""
          pullPolicy: IfNotPresent
          api:
            repository: {api}
            digest: ""
          web:
            repository: "{web}"  # quoted, with a comment
            digest: ""
        postgres:
          image:
            repository: docker.io/library/postgres
        """
    )


def _tree(
    trees: pathlib.Path,
    tag: str,
    *,
    charts: tuple[str, ...] = ("oldchart",),
    values: str | None = None,
) -> None:
    for chart in charts:
        d = trees / f"v{tag}" / "charts" / chart
        d.mkdir(parents=True)
        (d / "Chart.yaml").write_text(
            f'name: {chart}\nappVersion: "{tag}"\n', encoding="utf-8"
        )
        (d / "values.yaml").write_text(
            _values(OLD_API, OLD_WEB) if values is None else values,
            encoding="utf-8",
        )


@pytest.fixture
def sandbox(tmp_path: pathlib.Path) -> tuple[pathlib.Path, dict[str, str]]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    trees = tmp_path / "trees"
    trees.mkdir()
    (tmp_path / "unpublished").write_text("", encoding="utf-8")
    # git: ls-remote lists the tags that have a tree; archive serves a tag's
    # tree; fetch is logged.
    _stub(
        bindir,
        "git",
        """\
        printf 'git %s\\n' "$*" >>"$LOG"
        case "$1" in
            ls-remote)
                for d in "$TREES"/v*; do
                    printf 'x\\trefs/tags/%s\\n' "$(basename "$d")"
                done ;;
            fetch) ;;
            archive) tag=$2; shift 2; tar -c -C "$TREES/$tag" "$@" ;;
            *) exit 64 ;;
        esac
        """,
    )
    # docker: a manifest read fails like GHCR for a ref listed as unpublished
    # or never published (any ref not under ghcr.io/example/old); an image
    # inspect answers an ID made of the ref.
    _stub(
        bindir,
        "docker",
        """\
        printf 'docker %s\\n' "$*" >>"$LOG"
        ref="${@: -1}"
        case "$1 $2" in
            "manifest inspect")
                case "$ref" in ghcr.io/example/old:* | ghcr.io/example/old-web:*) ;;
                    *) printf 'manifest unknown\\n' >&2; exit 1 ;; esac
                if grep -Fqx "$ref" "$UNPUBLISHED"; then
                    printf 'manifest unknown\\n' >&2
                    exit 1
                fi
                printf '{"schemaVersion": 2}\\n' ;;
            "image inspect") printf 'id:%s\\n' "$ref" ;;
        esac
        """,
    )
    for tool in ("kind", "helm", "kubectl"):
        _stub(bindir, tool, f'printf \'{tool} %s\\n\' "$*" >>"$LOG"\n')
    _stub(bindir, "openssl", "printf 'x\\n'\n")
    env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "LOG": str(tmp_path / "calls.log"),
        "TREES": str(trees),
        "UNPUBLISHED": str(tmp_path / "unpublished"),
        "GITHUB_OUTPUT": str(tmp_path / "github_output"),
    }
    return tmp_path, env


def _run(script: str, cwd: pathlib.Path, env: dict[str, str]):
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    log = pathlib.Path(env["LOG"])
    return result, log.read_text(encoding="utf-8") if log.exists() else ""


def test_previous_reads_each_tag_under_its_own_repositories(sandbox):
    tmp, env = sandbox
    (tmp / "VERSION").write_text("0.17.0\n", encoding="utf-8")
    for tag in ("0.16.2", "0.17.0"):
        _tree(tmp / "trees", tag)
    result, log = _run(_harness("published", "do_previous") + "do_previous\n", tmp, env)
    assert result.returncode == 0, result.stdout + result.stderr
    out = (tmp / "github_output").read_text(encoding="utf-8")
    assert out.splitlines() == ["version=0.17.0"]
    assert f"docker manifest inspect {OLD_API}:core-0.17.0" in log
    assert f"docker manifest inspect {OLD_WEB}:core-0.17.0" in log
    assert NEW_API not in log, log
    assert (
        "git fetch --quiet --no-tags --depth 1 origin +refs/tags/v0.17.0:refs/tags/v0.17.0"
        in log
    )


def test_the_fallback_reads_the_older_tag_under_its_own_repositories(sandbox):
    tmp, env = sandbox
    (tmp / "VERSION").write_text("0.17.0\n", encoding="utf-8")
    _tree(tmp / "trees", "0.16.2")
    _tree(
        tmp / "trees",
        "0.17.0",
        charts=("midchart",),
        values=_values("ghcr.io/example/mid", "ghcr.io/example/mid-web"),
    )
    result, log = _run(_harness("published", "do_previous") + "do_previous\n", tmp, env)
    # 0.17.0 names repositories the registry does not have; 0.16.2 is read
    # under ITS names, not 0.17.0's and not this checkout's.
    assert result.returncode == 0, result.stdout + result.stderr
    out = (tmp / "github_output").read_text(encoding="utf-8")
    assert out.splitlines() == ["version=0.16.2"]
    assert "docker manifest inspect ghcr.io/example/mid:core-0.17.0" in log
    assert f"docker manifest inspect {OLD_API}:core-0.16.2" in log
    assert NEW_API not in log, log


def test_upgrade_previous_pulls_loads_and_installs_under_the_tags_names(sandbox):
    tmp, env = sandbox
    _tree(tmp / "trees", "0.17.0")
    work = tmp / "work"
    work.mkdir()
    (work / "images.txt").write_text(
        f"api=id:this\nweb=id:this-web\napi_repo={NEW_API}\nweb_repo={NEW_WEB}\n",
        encoding="utf-8",
    )
    env["PREVIOUS_VERSION"] = "0.17.0"
    # The cluster-side helpers, recorded rather than run.
    fakes = """\
        revisions() { printf 'r1\\n'; }
        assert_images() { printf 'assert_images %s\\n' "$*" >>"$LOG"; }
        upgrade_watched() { printf 'upgrade_watched %s\\n' "$*" >>"$LOG"; }
        applied_revisions() { : >"$3"; }
        assert_no_restarts() { :; }
        do_upgrade_previous
        """
    result, log = _run(
        _harness("pull_anonymously", "do_upgrade_previous", extra=fakes), tmp, env
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"docker pull {OLD_API}:core-0.17.0" in log
    assert f"docker pull {OLD_WEB}:core-0.17.0" in log
    assert f"docker tag {OLD_API}:core-0.17.0 {OLD_API}:core-0.17.0-previous" in log
    assert (
        f"kind load docker-image --name chart-kind "
        f"{OLD_API}:core-0.17.0-previous {OLD_WEB}:core-0.17.0-previous"
    ) in log
    install = [line for line in log.splitlines() if line.startswith("helm install")]
    assert len(install) == 1, log
    assert f"helm install prev {work}/tags/v0.17.0/charts/oldchart -n " in install[0]
    assert NEW_API not in log, log
    assert "charts/newchart" in log  # the upgrade goes to this checkout's chart
    previous = (work / "images-previous.txt").read_text(encoding="utf-8")
    assert previous.splitlines() == [
        f"api=id:{OLD_API}:core-0.17.0-previous",
        f"web=id:{OLD_WEB}:core-0.17.0-previous",
        f"api_repo={OLD_API}",
        f"web_repo={OLD_WEB}",
    ]
    assert f"assert_images chart-kind-previous {work}/images-previous.txt" in log


@pytest.mark.parametrize(
    ("charts", "values", "message"),
    [
        (("a", "b"), None, "v0.17.0 ships 2 charts/*/Chart.yaml, expected exactly 1"),
        (
            ("oldchart",),
            'image:\n  tag: ""\n',
            "v0.17.0's values.yaml names no image.api/image.web repository",
        ),
    ],
)
def test_a_tag_whose_chart_cannot_be_read_fails_closed(
    sandbox, charts, values, message
):
    tmp, env = sandbox
    _tree(tmp / "trees", "0.17.0", charts=charts, values=values)
    result, _ = _run(
        _harness(extra="tag_images 0.17.0\nprintf 'still-running\\n'\n"), tmp, env
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "still-running" not in result.stdout
    assert f"::error::N-1 cannot be resolved: {message}" in result.stdout


def test_a_tag_with_no_charts_fails_closed(sandbox):
    tmp, env = sandbox
    (tmp / "trees" / "v0.17.0").mkdir()
    result, _ = _run(
        _harness(extra="tag_images 0.17.0\nprintf 'still-running\\n'\n"), tmp, env
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "still-running" not in result.stdout
    assert "::error::N-1 cannot be resolved: v0.17.0 has no charts/" in result.stdout


def test_a_repository_under_another_parent_is_not_the_images(sandbox):
    """values_repo reads image.<component>.repository only: an api/web block
    under another top-level key, before image:, is not N-1's image."""
    tmp, env = sandbox
    values = (
        "sidecar:\n  api:\n    repository: ghcr.io/example/wrong\n"
        "  web:\n    repository: ghcr.io/example/wrong-web\n"
        + _values(OLD_API, OLD_WEB)
    )
    _tree(tmp / "trees", "0.17.0", values=values)
    result, _ = _run(
        _harness(
            extra='tag_images 0.17.0\nprintf "%s|%s\\n" "$TAG_API_REPO" "$TAG_WEB_REPO"\n'
        ),
        tmp,
        env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().splitlines()[-1] == f"{OLD_API}|{OLD_WEB}"


def test_a_tag_that_cannot_be_fetched_fails_closed(sandbox):
    tmp, env = sandbox
    _tree(tmp / "trees", "0.17.0")
    _stub(
        tmp / "bin",
        "git",
        """\
        printf 'git %s\\n' "$*" >>"$LOG"
        case "$1" in
            fetch) printf 'fatal: could not read from remote\\n' >&2; exit 128 ;;
            *) exit 64 ;;
        esac
        """,
    )
    result, _ = _run(
        _harness(extra="tag_images 0.17.0\nprintf 'still-running\\n'\n"), tmp, env
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "still-running" not in result.stdout
    assert (
        "::error::N-1 cannot be resolved: tag v0.17.0 could not be fetched"
        in result.stdout
    )


def test_the_parser_reads_this_checkouts_own_chart(sandbox):
    # The real chart, served as a tag's tree: what tag_images reads equals what
    # a YAML parser reads, so a restructured values.yaml fails here first.
    tmp, env = sandbox
    shutil.copytree(ROOT / "charts", tmp / "trees" / "v0.17.0" / "charts")
    charts = sorted((ROOT / "charts").glob("*/Chart.yaml"))
    assert len(charts) == 1, charts
    image = yaml.safe_load((charts[0].parent / "values.yaml").read_text())["image"]
    result, _ = _run(
        _harness(
            extra='tag_images 0.17.0\nprintf "%s|%s|%s\\n" "${TAG_CHART#"$WORK"/}" '
            '"$TAG_API_REPO" "$TAG_WEB_REPO"\n'
        ),
        tmp,
        env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().splitlines()[-1] == (
        f"tags/v0.17.0/charts/{charts[0].parent.name}|"
        f"{image['api']['repository']}|{image['web']['repository']}"
    )


@pytest.mark.parametrize(
    ("running", "ok"),
    [
        (
            f"{OLD_API}:core-0.17.0-previous id:a\n{OLD_WEB}:core-0.17.0-previous id:w",
            True,
        ),
        (f"ghcr.io/example/bold:t id:a\n{OLD_WEB}:t id:w", False),
        (f"{NEW_API}:core-0.17.0 id:a\n{OLD_WEB}:core-0.17.0-previous id:w", False),
        (f"{OLD_API}:core-0.17.0-previous id:w\n{OLD_WEB}:t id:w", False),
    ],
)
def test_assert_images_expects_the_repositories_the_images_were_loaded_under(
    sandbox, running, ok
):
    tmp, env = sandbox
    images = tmp / "images.txt"
    images.write_text(
        f"api=id:a\nweb=id:w\napi_repo={OLD_API}\nweb_repo={OLD_WEB}\n",
        encoding="utf-8",
    )
    (tmp / "pods").write_text(running + "\n", encoding="utf-8")
    _stub(tmp / "bin", "kubectl", 'cat "$PODS"\n')
    env["PODS"] = str(tmp / "pods")
    result, _ = _run(
        _harness("assert_images", extra=f"assert_images ns {images}\n"), tmp, env
    )
    assert (result.returncode == 0) is ok, result.stdout + result.stderr
