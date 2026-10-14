"""Every test the claims registries name is one the release candidate runs.

``scripts/rc_evidence.py`` fails a ``test:`` target that no suite in its
``SUITES`` runs, by name, at the release (#1142). This holds the registries
(``docs/_claims``) to the same rule on the pull request that adds the target,
which is a docs-only change: a target in a tree no RC suite runs is refused
here, not found at the release. A target in another SDK's tree is held by
that SDK's job in sdk-unit-tests.yml instead, as the verifier holds it.

Reads files only (the two scripts are loaded from the tree), so it runs the
same in the docs-only lane and in ``scripts/core_build.sh``'s copy.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]


def _verifier():
    spec = importlib.util.spec_from_file_location(
        "rc_evidence_for_registry_test", REPO_ROOT / "scripts" / "rc_evidence.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rc = _verifier()


def test_every_registry_target_is_run_by_a_release_candidate_suite():
    targets = [target for target, _ in rc.read_registry(REPO_ROOT).targets]
    assert targets, "the registries name no test: this checked nothing"
    outside = sorted(
        t for t in targets if rc.sdk_job_of(t) is None and rc.suite_of(t) is None
    )
    assert not outside, (
        "no suite in scripts/rc_evidence.py's SUITES runs these, so the release"
        " candidate's evidence would fail them; add the tree to a suite (and to"
        f" rc-evidence.yml), or name a test a suite runs: {outside}"
    )


def test_the_rule_refuses_a_target_outside_every_suite():
    assert rc.suite_of("test:charts/experimently/tests/test_values.py::test_x") is None
    assert (
        rc.sdk_job_of("test:charts/experimently/tests/test_values.py::test_x") is None
    )
    assert rc.suite_of("test:infrastructure/tests/test_redis_sizing.py::test_x")
    assert rc.sdk_job_of("test:sdk/go/client_test.go::TestX") == "go"
