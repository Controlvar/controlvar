#!/usr/bin/env python3
"""
The split URL edge app: a CDK app of its own, with one stack in us-east-1,
holding the CloudFront distribution and the Lambda@Edge router that send
split URL visitors to their variant (#393).

It is opt-in and separate from the platform app (``infrastructure/cdk``): no
``cdk deploy --all``, workflow, Makefile target or installer step deploys it.
An operator deploys it from this directory with ``cdk deploy``, which creates
a CloudFront distribution and a Lambda@Edge function that AWS bills. Its
settings are environment variables, read and checked at synth:

    SPLIT_URL_API_URL              the API's https URL, with no path
    SPLIT_URL_API_KEY              an API key made for the router
    SPLIT_URL_ORIGIN               the host name of the site's origin
    SPLIT_URL_COOKIE_NAME          default controlvar_uid
    SPLIT_URL_COOKIE_MAX_AGE_DAYS  1 to 400, default 30
    SPLIT_URL_COOKIE_DOMAIN        optional
    SPLIT_URL_DOMAIN_NAMES         optional, comma-separated, with
    SPLIT_URL_CERTIFICATE_ARN      an ACM certificate in us-east-1

The construct is loaded by file path, so that synth never imports the
``modules`` package (whose ``__init__`` pulls in the application).
"""

import importlib.util
import os
from pathlib import Path

CONSTRUCT = (
    Path(__file__).resolve().parents[1] / "constructs" / "split_url_distribution.py"
)
STACK_NAME = "controlvar-split-url-edge"


def _construct_module():
    spec = importlib.util.spec_from_file_location(
        "controlvar_split_url_distribution", CONSTRUCT
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(environ) -> None:
    import aws_cdk as cdk

    construct = _construct_module()
    app = cdk.App()
    stack = cdk.Stack(
        app,
        STACK_NAME,
        env=cdk.Environment(
            account=environ.get("CDK_DEFAULT_ACCOUNT") or None,
            region=construct.REGION,
        ),
        description="Controlvar split URL edge router: CloudFront and Lambda@Edge",
    )
    try:
        construct.SplitUrlDistribution(
            stack, "SplitUrl", **construct.settings_from_environment(environ)
        )
    except ValueError as error:
        raise SystemExit(f"split URL edge app: {error}") from None
    app.synth()


if __name__ == "__main__":
    main(os.environ)
