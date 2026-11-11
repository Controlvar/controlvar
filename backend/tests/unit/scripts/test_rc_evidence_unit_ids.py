"""The unit session's node ids are the same in every process (#1895).

rc-evidence.yml runs the unit session in four shards (``LEGS`` in
scripts/rc_evidence.py), each its own process, and a test belongs to the shard
its node id hashes to (backend/tests/shard.py). A node id that is different in
each process -- a parametrize id holding an object's address
(``repr(object())``) or a ``uuid4()`` -- puts its test in no shard, or in two,
and the verifier fails the release candidate on it (its shard check). That
check runs only at a release tag. This one runs on every pull request, in the
Unit Tests job, so an unstable id is found when it is added rather than at the
tag. The integration session is held the same way on every pull request, by
integration-tests.yml's shard check.

Two ``--collect-only`` sessions of the unit session's own arguments, in two
processes with different hash seeds, must collect the same node ids. Before
this change they did not: nine ids differed between two runs, in
test_synthetic_check.py and test_lower_is_better_fingerprint.py.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Set

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
#: Generous: one collection takes about 15 s on a laptop.
TIMEOUT = 900


def _verifier():
    spec = importlib.util.spec_from_file_location(
        "rc_evidence_for_unit_ids", REPO_ROOT / "scripts" / "rc_evidence.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rc = _verifier()


def _session_args() -> List[str]:
    """The arguments of rc-evidence's sharded unit session."""
    (session,) = [i for i in rc.LEGS["unit-1"].sessions if i.shard]
    return list(session.args)


def _node_ids(output: str) -> Set[str]:
    return {line.strip() for line in output.splitlines() if "::" in line}


def test_the_unit_session_collects_the_same_node_ids_in_every_process():
    if not (REPO_ROOT / "modules").is_dir():
        # rc-evidence shards the full tree, which the Unit Tests job holds;
        # two more collections would only lengthen core-build.
        pytest.skip("scripts/core_build.sh's copy: modules/ deleted")
    command = [
        sys.executable,
        "-m",
        "pytest",
        *_session_args(),
        "--collect-only",
        "-q",
        "-p",
        "no:cov",
        "-p",
        "no:cacheprovider",
        "-o",
        "log_cli=false",
    ]
    runs = []
    for seed in ("1", "2"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        # Unsliced: the outer session may itself be a shard.
        env.pop(rc.SHARD_SPEC, None)
        env.pop(rc.SHARD_REPORT, None)
        runs.append(
            subprocess.Popen(
                command,
                cwd=REPO_ROOT,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        )
    outputs = []
    for run in runs:
        out, _ = run.communicate(timeout=TIMEOUT)
        assert run.returncode == 0, out[-3000:]
        outputs.append(out)
    first, second = (_node_ids(out) for out in outputs)
    assert len(first) > 1000, "collected almost nothing: this compared nothing"
    differ = sorted(first ^ second)
    assert not differ, (
        f"{len(differ)} node ids of the unit session differ between two processes,"
        " so a sharded run puts their tests in no shard, or two; give each an"
        f" id that is the same in every process (ids=, pytest.param(id=)): {differ[:10]}"
    )
