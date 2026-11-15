"""chart-kind compares each pod's imageID with the ID kind's node gave the image.

``scripts/chart_kind.sh`` asserts that the api and web pods run the images
this job loaded, not a registry's (``assert_images``).  It recorded the
expected IDs with ``docker image inspect --format '{{.Id}}'`` on the host.
With Docker's classic image store that is the image's config digest, which
is also what the kind node's runtime, and so a pod's ``imageID``, reports.
With the containerd image store (Docker 29.4.2 on the ubuntu-26.04 runner) it
is a different digest: from 2026-10-29 every chart-kind job on main read its
own pods as "runs sha256:..., not the image this job built" (#1434).

Now ``load``, ``upgrade-next`` and ``upgrade-previous`` also record the ID the
node gives each image right after ``kind load`` (``node_image_id``: ``crictl
inspecti`` in the node, the first repo digest if there is one, else the image
ID), as ``api_node=``/``web_node=``, and ``assert_images`` compares with those.
The host IDs stay as ``api=``/``web=``: ``upgrade-previous`` reads this PR's
revisions through ``api=`` with ``docker``, which accepts the host's ID.

Run as shell, the script's own functions extracted, with ``docker``, ``kind``
and ``kubectl`` stubbed: no cluster.  The cluster job is the real gate.
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

API = "ghcr.io/example/api"
WEB = "ghcr.io/example/web"
HOST_API = "sha256:" + "a" * 64
HOST_WEB = "sha256:" + "b" * 64
NODE_API = "sha256:" + "c" * 64
NODE_WEB = "sha256:" + "d" * 64
OTHER = "sha256:" + "e" * 64


def _function(name: str) -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    match = re.search(rf"(?m)^{name}\(\) {{ .*}}\n", text) or re.search(
        rf"(?ms)^{name}\(\) {{\n.*?^}}\n", text
    )
    assert match, f"{name}() not found in {SCRIPT}"
    return match.group(0)


def _harness(*functions: str, call: str) -> str:
    parts = [
        "set -euo pipefail",
        "PROFILE=core",
        'WORK="$TMPDIR/work"',
        'CHART_DIR="$TMPDIR/chart"',
        "CLUSTER=chart-kind",
        f"API_REPO={API}",
        f"WEB_REPO={WEB}",
    ]
    parts += [_function(f) for f in ("say", "fail", *functions)]
    return "\n".join(parts) + "\n" + call + "\n"


def _stub(bindir: pathlib.Path, name: str, body: str) -> None:
    path = bindir / name
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body), encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def sandbox(tmp_path: pathlib.Path) -> tuple[pathlib.Path, dict[str, str]]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (tmp_path / "work").mkdir()
    (tmp_path / "chart").mkdir()
    (tmp_path / "chart" / "Chart.yaml").write_text(
        'name: chart\nappVersion: "0.37.0"\n', encoding="utf-8"
    )
    # docker: `image inspect` answers the host's ID for the ref; `exec` is the
    # node's crictl, answering $NODE_JSON_<api|web> (empty: no such image).
    _stub(
        bindir,
        "docker",
        f"""\
        printf 'docker %s\\n' "$*" >>"$LOG"
        ref="${{@: -1}}"
        case "$1 $2" in
            "image inspect")
                case "$ref" in {API}:*) echo {HOST_API} ;; {WEB}:*) echo {HOST_WEB} ;; esac ;;
            "exec chart-kind-control-plane")
                case "$ref" in
                    {API}:*) [ -n "$NODE_JSON_API" ] || exit 1; printf '%s\\n' "$NODE_JSON_API" ;;
                    {WEB}:*) [ -n "$NODE_JSON_WEB" ] || exit 1; printf '%s\\n' "$NODE_JSON_WEB" ;;
                esac ;;
        esac
        """,
    )
    _stub(bindir, "kind", 'printf \'kind %s\\n\' "$*" >>"$LOG"\n')
    # kubectl: the pods' "<image> <imageID>" lines, from $PODS.
    _stub(bindir, "kubectl", "printf '%s' \"$PODS\"\n")
    env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "LOG": str(tmp_path / "calls.log"),
        "API_IMAGE": "local/api:pr",
        "WEB_IMAGE": "local/web:pr",
        "NODE_JSON_API": f'{{"status": {{"id": "{NODE_API}", "repoDigests": []}}}}',
        "NODE_JSON_WEB": f'{{"status": {{"id": "{NODE_WEB}", "repoDigests": []}}}}',
        "PODS": "",
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


def _images(tmp: pathlib.Path) -> pathlib.Path:
    path = tmp / "work" / "images.txt"
    path.write_text(
        f"api={HOST_API}\nweb={HOST_WEB}\napi_repo={API}\nweb_repo={WEB}\n"
        f"api_node={NODE_API}\nweb_node={NODE_WEB}\n",
        encoding="utf-8",
    )
    return path


def _pods(api_id: str, web_id: str) -> str:
    return (
        f"{API}:core-0.37.0 {api_id}\n"
        f"{API}:core-0.37.0 {api_id}\n"
        f"{WEB}:core-0.37.0 {web_id}\n"
    )


def test_pods_running_the_loaded_images_pass_when_the_host_ids_differ(sandbox):
    # The containerd-image-store case: the host's IDs are not the node's. The
    # old comparison with api=/web= failed here on every run (#1434).
    tmp, env = sandbox
    _images(tmp)
    env["PODS"] = _pods(NODE_API, NODE_WEB)
    result, _ = _run(_harness("assert_images", call="assert_images ns"), tmp, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "every api/web container runs this job's images" in result.stdout


@pytest.mark.parametrize(
    ("api_id", "web_id", "image"),
    [
        (OTHER, NODE_WEB, f"{API}:core-0.37.0 runs {OTHER}"),
        (NODE_API, OTHER, f"{WEB}:core-0.37.0 runs {OTHER}"),
        # The host's ID is not the loaded image as the node knows it.
        (HOST_API, NODE_WEB, f"{API}:core-0.37.0 runs {HOST_API}"),
    ],
)
def test_a_pod_running_any_other_image_fails(sandbox, api_id, web_id, image):
    tmp, env = sandbox
    _images(tmp)
    env["PODS"] = _pods(api_id, web_id)
    result, _ = _run(_harness("assert_images", call="assert_images ns"), tmp, env)
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"::error::{image}, not the image this job built" in result.stdout


def test_images_without_node_ids_fail_closed(sandbox):
    tmp, env = sandbox
    (tmp / "work" / "images.txt").write_text(
        f"api={HOST_API}\nweb={HOST_WEB}\napi_repo={API}\nweb_repo={WEB}\n",
        encoding="utf-8",
    )
    env["PODS"] = _pods(HOST_API, HOST_WEB)
    result, _ = _run(_harness("assert_images", call="assert_images ns"), tmp, env)
    assert result.returncode != 0
    assert "no api_node image id" in result.stderr


def test_load_records_the_node_ids_after_loading(sandbox):
    tmp, env = sandbox
    result, log = _run(
        _harness("app_version", "node_image_id", "do_load", call="do_load"), tmp, env
    )
    assert result.returncode == 0, result.stdout + result.stderr
    lines = (tmp / "work" / "images.txt").read_text(encoding="utf-8").splitlines()
    assert lines == [
        f"api={HOST_API}",
        f"web={HOST_WEB}",
        f"api_repo={API}",
        f"web_repo={WEB}",
        f"api_node={NODE_API}",
        f"web_node={NODE_WEB}",
    ]
    calls = log.splitlines()
    load = calls.index(
        f"kind load docker-image --name chart-kind {API}:core-0.37.0 {WEB}:core-0.37.0"
    )
    read = calls.index(
        f"docker exec chart-kind-control-plane crictl inspecti -o json {API}:core-0.37.0"
    )
    assert load < read, log


def test_the_node_id_is_the_first_repo_digest_when_there_is_one(sandbox):
    tmp, env = sandbox
    digest = f"{API}@sha256:" + "f" * 64
    env["NODE_JSON_API"] = (
        f'{{"status": {{"id": "{NODE_API}", "repoDigests": ["{digest}", "x"]}}}}'
    )
    result, _ = _run(
        _harness("node_image_id", call=f"node_image_id {API}:core-0.37.0"), tmp, env
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == digest + "\n"


def test_load_fails_closed_when_the_node_has_no_such_image(sandbox):
    tmp, env = sandbox
    env["NODE_JSON_WEB"] = ""
    result, _ = _run(
        _harness("app_version", "node_image_id", "do_load", call="do_load"), tmp, env
    )
    assert result.returncode != 0
    assert f"no image id on the kind node for {WEB}:core-0.37.0" in result.stderr
    assert not (tmp / "work" / "images.txt").exists()
