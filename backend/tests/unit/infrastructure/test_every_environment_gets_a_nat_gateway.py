"""Every AWS environment the CDK app builds has a NAT gateway (#1142).

The demo guide counts NAT gateways among the resources the AWS stacks create
and AWS bills for. ``stacks.environments.nat_gateway_count`` decides how many
each environment gets, and ``app.py`` hands that number to the VPC stack. The
module has no imports, so it is loaded from its file without the CDK
(``infrastructure/tests/test_environments_do_not_collide.py`` checks the
synthesised templates where the CDK is installed).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

CDK_DIR = Path(__file__).resolve().parents[4] / "infrastructure" / "cdk"


def _environments():
    spec = importlib.util.spec_from_file_location(
        "cdk_environments", CDK_DIR / "stacks" / "environments.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("env_name", ["dev", "staging", "prod"])
def test_every_environment_gets_a_nat_gateway(env_name):
    assert _environments().nat_gateway_count(env_name) >= 1


def test_the_app_gives_the_vpc_its_environments_nat_gateways():
    app = (CDK_DIR / "app.py").read_text(encoding="utf-8")
    assert "nat_gateways=nat_gateway_count(env_name)" in app
