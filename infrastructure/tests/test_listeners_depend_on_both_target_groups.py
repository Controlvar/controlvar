"""Deleting the Fargate stack works in one run (#1618).

CodeDeploy swaps which of the blue and green target groups the live listener
rules point at, outside CloudFormation. CloudFormation deletes in reverse
dependency order, and a rule depended on only the group the template names,
so on a stack left on the other colour it could delete that group first and
fail with "in use by a listener". Every listener rule and the test listener
therefore depend on BOTH groups. Asserted on a real ``app.synth()``.
"""

from __future__ import annotations

import pytest

from .test_dashboard_service import _by_type, _fargate, _one_id, _synth

LB = "AWS::ElasticLoadBalancingV2::"


@pytest.mark.regression
@pytest.mark.parametrize("live", ["blue", "green"])
def test_every_listener_rule_and_the_test_listener_depend_on_both_groups(live):
    resources = _fargate(_synth("dev", {"api_live_target_group": live}))
    both = {
        _one_id(resources, "BlueTargetGroup", LB + "TargetGroup"),
        _one_id(resources, "GreenTargetGroup", LB + "TargetGroup"),
    }
    rules = _by_type(resources, LB + "ListenerRule")
    assert rules, "no listener rules were synthesised"
    test_listener = _one_id(resources, "BackendALBTestListener", LB + "Listener")
    subjects = {**rules, test_listener: resources[test_listener]}
    for logical_id, resource in subjects.items():
        depends_on = resource.get("DependsOn", [])
        if isinstance(depends_on, str):
            depends_on = [depends_on]
        assert both <= set(depends_on), (
            f"{logical_id} (live={live}) depends on {sorted(depends_on)}, "
            f"not on both target groups {sorted(both)}"
        )
