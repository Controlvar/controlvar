"""
The CloudFront distribution that sends split URL visitors to their variant (#393).

``SplitUrlDistribution`` puts a CloudFront distribution in front of a site's
origin, with one Lambda@Edge function, the split URL router
(``modules/lambda/split_url_router/handler.py``), associated with both the
viewer-request and the viewer-response event of its only behaviour. So every
request for every path through the distribution runs two Lambda@Edge
invocations, and the distribution caches nothing: the router has to see every
request for the pages under test, and a cached control page would answer the
next visitor without it.

Lambda@Edge takes no environment variables, so the router's settings are
written into ``config.json`` beside ``handler.py`` in the function's code
asset. Changing one changes the asset, and so needs a redeploy.

The only user is the edge app, ``modules/infrastructure/split_url_edge``, a
CDK app of its own in us-east-1. The platform app (``infrastructure/cdk``)
does not use it.
"""

import atexit
import json
import shutil
import tempfile
from pathlib import Path
from typing import Mapping, Optional, Sequence
from urllib.parse import urlsplit

from aws_cdk import CfnOutput, Duration, Stack, Token
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_lambda as lambda_
from constructs import Construct

ROUTER_DIR = Path(__file__).resolve().parents[2] / "lambda" / "split_url_router"
REGION = "us-east-1"

#: The edge app's settings: its environment variable, then the construct's
#: keyword argument.
API_URL = ("SPLIT_URL_API_URL", "api_url")
API_KEY = ("SPLIT_URL_API_KEY", "api_key")
ORIGIN = ("SPLIT_URL_ORIGIN", "origin_domain")
COOKIE_NAME = ("SPLIT_URL_COOKIE_NAME", "cookie_name")
COOKIE_MAX_AGE_DAYS = ("SPLIT_URL_COOKIE_MAX_AGE_DAYS", "cookie_max_age_days")
COOKIE_DOMAIN = ("SPLIT_URL_COOKIE_DOMAIN", "cookie_domain")
DOMAIN_NAMES = ("SPLIT_URL_DOMAIN_NAMES", "domain_names")
CERTIFICATE_ARN = ("SPLIT_URL_CERTIFICATE_ARN", "certificate_arn")
SETTINGS = (
    API_URL,
    API_KEY,
    ORIGIN,
    COOKIE_NAME,
    COOKIE_MAX_AGE_DAYS,
    COOKIE_DOMAIN,
    DOMAIN_NAMES,
    CERTIFICATE_ARN,
)

DEFAULT_COOKIE_NAME = "controlvar_uid"
DEFAULT_COOKIE_MAX_AGE_DAYS = 30
COOKIE_NAME_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)
LABEL_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"
)


def _named(setting) -> str:
    variable, keyword = setting
    return f"{variable} ({keyword})"


def _is_host_name(value: str, wildcard: bool = False) -> bool:
    """A DNS name of at least two labels (not an IP address), each 1-63
    letters, digits or hyphens, none starting or ending with a hyphen."""
    if not isinstance(value, str) or not 0 < len(value) <= 253:
        return False
    if wildcard and value.startswith("*."):
        value = value[2:]
    labels = value.split(".")
    if len(labels) < 2 or all(label.isdigit() for label in labels):
        return False
    return all(
        0 < len(label) <= 63
        and set(label) <= LABEL_CHARACTERS
        and not label.startswith("-")
        and not label.endswith("-")
        for label in labels
    )


def _check_api_url(value) -> str:
    message = (
        f"{_named(API_URL)} must be an https:// URL with no path, query or "
        "fragment, such as https://api.example.com"
    )
    if not isinstance(value, str) or not value.isascii() or not value.isprintable():
        raise ValueError(message)
    if any(c.isspace() for c in value) or "\\" in value:
        raise ValueError(message)
    try:
        parts = urlsplit(value)
        _ = parts.port  # an invalid port raises ValueError
    except ValueError:
        raise ValueError(message) from None
    if (
        parts.scheme != "https"
        or "@" in parts.netloc
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or value.endswith(("?", "#"))
        or not _is_host_name(parts.hostname or "")
    ):
        raise ValueError(message)
    return value.rstrip("/")


def _check_api_key(value) -> str:
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= 255
        or not all("!" <= c <= "~" for c in value)
    ):
        raise ValueError(
            f"{_named(API_KEY)} must be an API key: 1 to 255 characters, with no "
            "space or control character"
        )
    return value


def _check_origin(value) -> str:
    if not _is_host_name(value):
        raise ValueError(
            f"{_named(ORIGIN)} must be the host name of the site's origin, such as "
            "origin.example.com, with no scheme, port or path"
        )
    return value


def _check_cookie_name(value) -> str:
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= 64
        or not set(value) <= COOKIE_NAME_CHARACTERS
    ):
        raise ValueError(
            f"{_named(COOKIE_NAME)} must be 1 to 64 letters, digits, '.', '_' or '-'"
        )
    return value


COOKIE_MAX_AGE_DAYS_MESSAGE = (
    f"{_named(COOKIE_MAX_AGE_DAYS)} must be a whole number of days from 1 to 400"
)


def _check_cookie_max_age_days(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 400:
        raise ValueError(COOKIE_MAX_AGE_DAYS_MESSAGE)
    return value


def _check_cookie_domain(value) -> Optional[str]:
    if value is None:
        return None
    name = value[1:] if isinstance(value, str) and value.startswith(".") else value
    if not _is_host_name(name):
        raise ValueError(
            f"{_named(COOKIE_DOMAIN)} must be a domain name, such as example.com"
        )
    return value


def _check_domains(names, certificate_arn) -> tuple:
    names = tuple(names or ())
    if bool(names) != bool(certificate_arn):
        raise ValueError(
            f"{_named(DOMAIN_NAMES)} and {_named(CERTIFICATE_ARN)} go together: "
            "set both or neither"
        )
    if not all(_is_host_name(name, wildcard=True) for name in names):
        raise ValueError(
            f"{_named(DOMAIN_NAMES)} must be host names separated by commas, such "
            "as shop.example.com,www.example.com"
        )
    if certificate_arn is not None:
        prefix = f"arn:aws:acm:{REGION}:"
        rest = certificate_arn[len(prefix) :] if isinstance(certificate_arn, str) else ""
        account, _, resource = rest.partition(":certificate/")
        if (
            not certificate_arn.startswith(prefix)
            or not (len(account) == 12 and account.isdigit())
            or not resource
            or not set(resource) <= LABEL_CHARACTERS
        ):
            raise ValueError(
                f"{_named(CERTIFICATE_ARN)} must be an ACM certificate in "
                f"{REGION} (arn:aws:acm:{REGION}:<account>:certificate/<id>): "
                "CloudFront takes its certificate only from there"
            )
    return names


def settings_from_environment(environ: Mapping[str, str]) -> dict:
    """The construct's keyword arguments from the edge app's environment
    variables. Raises ValueError naming the setting that is missing or wrong."""
    for setting in (API_URL, API_KEY, ORIGIN):
        if not environ.get(setting[0]):
            raise ValueError(f"{_named(setting)} is required")
    days = environ.get(COOKIE_MAX_AGE_DAYS[0]) or str(DEFAULT_COOKIE_MAX_AGE_DAYS)
    if not (days.isascii() and days.isdigit() and len(days) <= 3):
        raise ValueError(COOKIE_MAX_AGE_DAYS_MESSAGE)
    names = [
        name.strip()
        for name in (environ.get(DOMAIN_NAMES[0]) or "").split(",")
        if name.strip()
    ]
    return {
        "api_url": environ[API_URL[0]],
        "api_key": environ[API_KEY[0]],
        "origin_domain": environ[ORIGIN[0]],
        "cookie_name": environ.get(COOKIE_NAME[0]) or DEFAULT_COOKIE_NAME,
        "cookie_max_age_days": int(days),
        "cookie_domain": environ.get(COOKIE_DOMAIN[0]) or None,
        "domain_names": names,
        "certificate_arn": environ.get(CERTIFICATE_ARN[0]) or None,
    }


class SplitUrlDistribution(Construct):
    """
    A CloudFront distribution in front of ``origin_domain`` whose only
    behaviour runs the split URL router at viewer request and viewer response.

    The distribution:

    - caches nothing (TTL 0), so the router sees every request;
    - redirects http to https, since the visitor-id cookie is Secure;
    - forwards every viewer header, cookie and query string to the origin
      (the ALL_VIEWER origin request policy) and allows every method.

    The router is one function version, on python3.12 and x86_64 (AWS does
    not run arm64 Lambda@Edge functions), associated twice. Its stack must be
    in us-east-1, where AWS runs Lambda@Edge functions from; anything else,
    including a stack with no region, is refused.

    Attributes:
        distribution: the CloudFront Distribution.
        function: the router's Lambda function.
        domain_name: the distribution's domain name (``d1234.cloudfront.net``).
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        origin_domain: str,
        api_url: str,
        api_key: str,
        cookie_name: str = DEFAULT_COOKIE_NAME,
        cookie_max_age_days: int = DEFAULT_COOKIE_MAX_AGE_DAYS,
        cookie_domain: Optional[str] = None,
        domain_names: Sequence[str] = (),
        certificate_arn: Optional[str] = None,
        **kwargs,
    ) -> None:
        """
        Args:
            scope: the stack, which must be in us-east-1.
            construct_id: the construct's id.
            origin_domain: the host name of the site's origin.
            api_url: the Controlvar API's https URL, with no path.
            api_key: a Controlvar API key made for the router.
            cookie_name: the visitor-id cookie's name.
            cookie_max_age_days: its lifetime, 1 to 400 days.
            cookie_domain: its Domain attribute, if any.
            domain_names: the distribution's alternate domain names, if any.
            certificate_arn: their ACM certificate, in us-east-1; required
                with ``domain_names``.
        """
        super().__init__(scope, construct_id, **kwargs)
        region = Stack.of(self).region
        if Token.is_unresolved(region) or region != REGION:
            raise ValueError(
                f"The split URL edge stack must be in {REGION}, where AWS runs "
                "Lambda@Edge functions from: create its Stack with "
                f'env=Environment(region="{REGION}")'
            )
        settings = {
            "api_url": _check_api_url(api_url),
            "api_key": _check_api_key(api_key),
            "cookie_name": _check_cookie_name(cookie_name),
            "cookie_max_age_days": _check_cookie_max_age_days(cookie_max_age_days),
            "cookie_domain": _check_cookie_domain(cookie_domain),
        }
        _check_origin(origin_domain)
        names = _check_domains(domain_names, certificate_arn)

        # The code asset: the router and its settings, in a private temporary
        # directory. CDK copies it into the cloud assembly at synth, so it is
        # removed when the process exits, not before.
        bundle = tempfile.mkdtemp(prefix="split-url-router-")
        atexit.register(shutil.rmtree, bundle, ignore_errors=True)
        shutil.copyfile(ROUTER_DIR / "handler.py", Path(bundle) / "handler.py")
        (Path(bundle) / "config.json").write_text(
            json.dumps(settings, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        self.function = lambda_.Function(
            self,
            "Router",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.X86_64,
            handler="handler.handler",
            code=lambda_.Code.from_asset(bundle),
            memory_size=128,
            timeout=Duration.seconds(5),
            description="Controlvar split URL router (Lambda@Edge)",
        )

        version = self.function.current_version
        no_cache_policy = cloudfront.CachePolicy(
            self,
            "NoCachePolicy",
            cache_policy_name=f"{construct_id}-no-cache",
            comment="Caches nothing: the split URL router sees every request",
            default_ttl=Duration.seconds(0),
            min_ttl=Duration.seconds(0),
            max_ttl=Duration.seconds(0),
        )
        default_behavior = cloudfront.BehaviorOptions(
            origin=origins.HttpOrigin(origin_domain),
            viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            cache_policy=no_cache_policy,
            allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
            origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER,
            edge_lambdas=[
                cloudfront.EdgeLambda(
                    function_version=version,
                    event_type=cloudfront.LambdaEdgeEventType.VIEWER_REQUEST,
                    include_body=False,
                ),
                cloudfront.EdgeLambda(
                    function_version=version,
                    event_type=cloudfront.LambdaEdgeEventType.VIEWER_RESPONSE,
                ),
            ],
        )
        certificate = None
        if certificate_arn:
            certificate = acm.Certificate.from_certificate_arn(
                self, "Certificate", certificate_arn
            )
        self.distribution = cloudfront.Distribution(
            self,
            "Distribution",
            default_behavior=default_behavior,
            domain_names=list(names) or None,
            certificate=certificate,
            comment=f"{construct_id}: split URL router",
        )
        self.domain_name: str = self.distribution.distribution_domain_name
        CfnOutput(
            self,
            "DistributionDomainName",
            value=self.domain_name,
            description="CloudFront domain name for the split URL distribution",
            export_name=f"{construct_id}-domain-name",
        )
