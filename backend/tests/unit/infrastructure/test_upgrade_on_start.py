"""Where a container start runs the schema upgrade, and what a failed one leaves serving.

Two deployments upgrade the schema when a container starts:

* Docker Compose: the one API container runs the image's entrypoint with
  ``RUN_MIGRATIONS=true``, so it upgrades, then serves. When the upgrade
  fails the entrypoint exits before uvicorn, with the upgrade's own message
  in the container's output, and compose's ``restart: unless-stopped`` starts
  it again; once the database is fixed, the next start serves.
* The Helm chart: each API pod's ``migrate`` init container runs the same
  entrypoint with ``RUN_MIGRATIONS=true`` and the command ``true``, so it
  upgrades and ends; the ``api`` container has ``RUN_MIGRATIONS=false``. The
  rolling update adds one pod at a time and takes none away until a new one
  is Ready (``maxUnavailable: 0``), so the old pods keep serving while the
  upgrade runs, and when it fails the new pod never becomes Ready and the
  update goes no further.

The chart is read as text (its templates are not YAML until helm renders
them, and the render tests run only in the chart job), the compose files as
YAML, and the real entrypoint runs with ``python``, ``uvicorn`` and ``true``
replaced by stubs that record their arguments, so no database, cluster or
container is needed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Tuple

import pytest
import yaml

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
ENTRYPOINT = REPO_ROOT / "backend" / "docker-entrypoint.sh"
DOCKERFILE = REPO_ROOT / "backend" / "Dockerfile"
QUICK_START = REPO_ROOT / "docker-compose.yml"
PRODUCTION = REPO_ROOT / "deploy" / "compose" / "compose.yml"
CHART = REPO_ROOT / "charts" / "experimently"
TEMPLATES = CHART / "templates"
BASH = shutil.which("bash")

BOOTSTRAP = "python -m backend.app.db.bootstrap"
#: What the bootstrap prints when it refuses an upgrade (the stub's stand-in).
REFUSAL = "Refusing to upgrade schema experimentation: the stub refused"

#: Records every call; the bootstrap fails, printing REFUSAL, when
#: BOOTSTRAP_FAILS is set.
STUB = """#!/usr/bin/env bash
printf '%s %s\\n' "$(basename "$0")" "$*" >> "$STUB_LOG"
if [ -n "${BOOTSTRAP_FAILS:-}" ] && [ "$*" = "-m backend.app.db.bootstrap" ]; then
    printf '%s\\n' "$BOOTSTRAP_FAILS" >&2
    exit 1
fi
exit 0
"""

pytestmark.append(
    pytest.mark.skipif(
        BASH is None or not ENTRYPOINT.is_file(),
        reason="needs bash and backend/docker-entrypoint.sh",
    )
)


def _start(tmp_path: Path, *command: str, **env_vars: str) -> Tuple[int, str, list]:
    """One container start: the real entrypoint with *command*, ``cd /app``
    sent to *tmp_path*. Returns the exit status, stderr and the calls."""
    tmp_path.mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("python", "uvicorn", "true"):
        (bin_dir / name).write_text(STUB)
        (bin_dir / name).chmod(0o755)
    log = tmp_path / "calls.log"
    log.write_text("")
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "HOME": str(tmp_path),
        "STUB_LOG": str(log),
        "APP_DIR_FOR_TEST": str(tmp_path),
        "WAIT_FOR_DB": "false",
        "OPENSSL_armcap": "0",
        "ENVIRONMENT": "production",
        **env_vars,
    }
    driver = (
        'cd() { builtin cd "$APP_DIR_FOR_TEST"; }; export -f cd; exec bash "$0" "$@"'
    )
    result = subprocess.run(
        [BASH, "-c", driver, str(ENTRYPOINT), *command],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    calls = [
        line
        for line in log.read_text().splitlines()
        if not line.startswith("python -m backend.app.core.preflight")
    ]
    return result.returncode, result.stderr, calls


def _api_service(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["services"]["api"]


def _template(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


def _deployment_parts() -> Tuple[str, str, str]:
    """The API Deployment's spec before the pod template, its init
    containers, and its containers, as template text."""
    deployment = _template("api-deployment.yaml")
    head, init_marker, rest = deployment.partition("\n      initContainers:\n")
    assert init_marker, "the API Deployment has no initContainers"
    init, containers_marker, containers = rest.partition("\n      containers:\n")
    assert containers_marker, "the API Deployment has no containers"
    return head, init, containers


#: Rolling update, one new pod at a time, no old pod removed before a new one
#: is Ready.
ROLLING_UPDATE = re.compile(
    r"^  strategy:\n"
    r"    type: RollingUpdate\n"
    r"    rollingUpdate:\n"
    r"      maxUnavailable: 0\n"
    r"      maxSurge: 1\n",
    re.M,
)


def _containers(text: str) -> dict:
    """``- name: X`` at the containers' indent -> that container's text."""
    parts = re.split(r"^        - name: ([a-z-]+)\n", text, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2]))


def test_compose_restarts_the_api_container_that_a_failed_upgrade_stopped(tmp_path):
    """Both compose files: the API container runs the image's entrypoint with
    RUN_MIGRATIONS true and is restarted when it exits. A start whose upgrade
    fails exits non-zero before uvicorn, with the upgrade's message in the
    output ``docker compose logs api`` shows; a start after the database is
    fixed upgrades and serves."""
    for path in (QUICK_START, PRODUCTION):
        api = _api_service(path)
        assert api.get("restart") == "unless-stopped", (path.name, api.get("restart"))
        assert "entrypoint" not in api and "command" not in api, path.name
        assert str(api["environment"]["RUN_MIGRATIONS"]) in (
            "true",
            "${RUN_MIGRATIONS:-true}",
        ), path.name
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert 'ENTRYPOINT ["/app/backend/docker-entrypoint.sh"]' in dockerfile

    code, stderr, calls = _start(
        tmp_path / "refused", RUN_MIGRATIONS="true", BOOTSTRAP_FAILS=REFUSAL
    )
    assert code != 0, stderr
    assert calls == [BOOTSTRAP], calls
    assert REFUSAL in stderr, stderr

    code, stderr, calls = _start(tmp_path / "fixed", RUN_MIGRATIONS="true")
    assert code == 0, stderr
    assert calls[0] == BOOTSTRAP and len(calls) == 2, calls
    assert calls[1].startswith("uvicorn backend.app.main:app "), calls


def test_the_migrate_init_container_upgrades_while_the_old_pods_keep_serving(
    tmp_path,
):
    """The chart: ``migrate`` runs the image's entrypoint with RUN_MIGRATIONS
    true and the command ``true``, so it upgrades and ends; ``api`` does not
    upgrade and has a readiness probe; the rolling update takes no old pod
    away before a new one is Ready."""
    head, init, containers = _deployment_parts()
    assert ROLLING_UPDATE.search(head), head

    init_containers = _containers(init)
    assert list(init_containers) == ["migrate"], list(init_containers)
    migrate = init_containers["migrate"]
    assert "image: {{ $image }}" in migrate
    assert 'args: ["true"]' in migrate
    assert "command:" not in migrate
    assert '"runMigrations" "true")' in migrate

    api = _containers(containers)["api"]
    assert "image: {{ $image }}" in api
    assert '"runMigrations" "false")' in api
    assert "readinessProbe:" in api
    helpers = _template("_helpers.tpl")
    assert "- name: RUN_MIGRATIONS\n  value: {{ .runMigrations | quote }}" in helpers

    code, stderr, calls = _start(tmp_path / "migrate", "true", RUN_MIGRATIONS="true")
    assert code == 0, stderr
    assert calls == [BOOTSTRAP, "true "], calls

    code, stderr, calls = _start(
        tmp_path / "api", "uvicorn", "backend.app.main:app", RUN_MIGRATIONS="false"
    )
    assert code == 0, stderr
    assert len(calls) == 1 and calls[0].startswith("uvicorn "), calls


def test_a_failed_migrate_stalls_the_rolling_update_and_the_old_pods_keep_serving(
    tmp_path,
):
    """When the upgrade fails, ``migrate`` exits non-zero with the upgrade's
    message (``kubectl logs <pod> -c migrate``) and the new pod's ``api``
    container never starts, so it is never Ready; with ``maxUnavailable: 0``
    the update removes no old pod."""
    head, _, _ = _deployment_parts()
    assert ROLLING_UPDATE.search(head), head
    assert "restartPolicy" not in _template("api-deployment.yaml")

    code, stderr, calls = _start(
        tmp_path / "migrate", "true", RUN_MIGRATIONS="true", BOOTSTRAP_FAILS=REFUSAL
    )
    assert code != 0, stderr
    assert calls == [BOOTSTRAP], calls
    assert REFUSAL in stderr, stderr


def test_an_api_pod_runs_the_commands_the_compose_api_container_runs():
    """``kubectl exec`` into an API pod's ``api`` container: the image the
    production compose file runs, in its own working directory (the chart
    sets no command or workingDir), with every POSTGRES_* setting the compose
    API container has."""
    values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    repository = values["image"]["api"]["repository"]
    compose_api = _api_service(PRODUCTION)
    assert str(compose_api["image"]).split(":")[0] == repository

    _, _, containers = _deployment_parts()
    api = _containers(containers)["api"]
    for key in ("command:", "args:", "workingDir:"):
        assert key not in api, key
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"^WORKDIR /app$", dockerfile, re.M)
    assert "COPY --chown=appuser:appgroup backend/ /app/backend/" in dockerfile
    assert (REPO_ROOT / "backend" / "app" / "db" / "alembic.ini").is_file()

    assert "configMapRef:" in api and 'include "controlvar.secretEnv"' in api
    config = _template("config.yaml")
    secret_env = _template("_helpers.tpl").split('define "controlvar.secretEnv"')[1]
    secret_keys = re.search(r"\$keys := list ([^}]*)\}\}", secret_env)
    assert secret_keys, "controlvar.secretEnv lists no keys"
    wanted = sorted(k for k in compose_api["environment"] if k.startswith("POSTGRES_"))
    assert len(wanted) >= 6, wanted
    missing = [
        name
        for name in wanted
        if f"\n  {name}: " not in config and f'"{name}"' not in secret_keys.group(1)
    ]
    assert not missing, f"the chart's api container does not get {missing}"
