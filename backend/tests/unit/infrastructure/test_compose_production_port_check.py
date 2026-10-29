"""Compose Production's "publishes no host port" check reads both Compose answers.

``.github/workflows/compose-production.yml`` (step "Production facts") asks
``docker compose port <service> <port>`` for postgres, redis and the API and
fails if any of them is published on the host.  It treated an answer as
"unpublished" only when it was exactly ``:0``.  On the ubuntu-26.04 runner
(Docker 29.4.2) Compose answers ``invalid IP:0`` for the same unpublished
port, so every run there failed with "postgres publishes invalid IP:0"
(run 40787685453).  Now a port is published when any line of the answer does
not end in ``:0``: a real binding is never port 0.

The condition is taken from the workflow itself and run in bash on planted
answers, so the test follows the file.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

import pytest
import yaml

pytestmark = [pytest.mark.unit, pytest.mark.regression]

ROOT = pathlib.Path(__file__).resolve().parents[4]
WORKFLOW = ROOT / ".github" / "workflows" / "compose-production.yml"


def _condition() -> str:
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    steps = [s for job in jobs.values() for s in job.get("steps", [])]
    facts = [s for s in steps if s.get("name") == "Production facts"]
    assert len(facts) == 1, "the Production facts step is gone or doubled"
    lines = [
        line
        for line in facts[0]["run"].splitlines()
        if 'port "${svc%%:*}" "${svc##*:}" >port.out 2>&1' in line
    ]
    assert len(lines) == 1, "the docker compose port check is gone or doubled"
    match = re.search(r">port\.out 2>&1 && (.*); then$", lines[0].strip())
    assert match, f"cannot read the condition from: {lines[0]!r}"
    return match.group(1)


def _published(tmp_path: pathlib.Path, answer: str) -> bool:
    (tmp_path / "port.out").write_text(answer, encoding="utf-8")
    result = subprocess.run(
        ["bash", "-c", _condition()],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode in (0, 1), result.stderr
    return result.returncode == 0


@pytest.mark.parametrize(
    "answer",
    [
        ":0\n",
        # The ubuntu-26.04 runner's answer: this failed every run there.
        "invalid IP:0\n",
        "",
        ":0",
    ],
)
def test_an_unpublished_port_passes(tmp_path, answer):
    assert not _published(tmp_path, answer)


@pytest.mark.parametrize(
    "answer",
    [
        "0.0.0.0:15432\n",
        "127.0.0.1:5432\n",
        "[::]:5432\n",
        # One unpublished line must not hide a real binding on another.
        "invalid IP:0\n127.0.0.1:15432\n",
    ],
)
def test_a_published_port_fails(tmp_path, answer):
    assert _published(tmp_path, answer)
