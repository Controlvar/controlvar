"""boto3 clients for the AWS services a connector uses, on fixed endpoints only.

:func:`make_client` is the one way a connector gets a boto3 client:

* only ``sts``, ``athena`` and ``glue``, only in a region from
  :data:`AWS_REGIONS` (committed data: botocore's regions for those services);
* boto3's own regional endpoint -- ``endpoint_url`` is never set, and a client
  whose resolved endpoint is anything but ``https://<service>.<region>.amazonaws.com``
  (an ``AWS_ENDPOINT_URL`` variable, a FIPS or dual-stack setting) is refused;
* no environment proxy (``proxies={}``);
* :func:`boto_config`'s limits: connect 5 s and each read 30 s, two attempts --
  and, under a :class:`~.deadlines.Deadline`, each limit capped at what the
  deadline has left and a single attempt unless two fit.  The limits are fixed
  when the client is built, so :func:`call` builds one per call;
* a ``before-send`` hook (:class:`BeforeSendGuard`) that refuses the call
  unless the request's host is that endpoint, every address it resolves to is
  public (the same rule as :func:`.egress.refused_address`), and the
  operation's deadline has time left.

Every function names the warehouse it is called for (``warehouse=``, one of
:data:`AWS_WAREHOUSES`, required): a failure says which warehouse failed.

:func:`assume_role_session` is how a role-based connector gets the customer's
role: AssumeRole through :func:`call`, with the connection's external id, and
only for a role whose trust policy requires that id.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    NoCredentialsError,
    ParamValidationError,
    ReadTimeoutError,
)

from modules.backend.app.schemas.warehouse_connections import ROLE_ARN_PATTERN
from modules.backend.app.warehouse.deadlines import (
    CONNECT_SECONDS,
    READ_SECONDS,
    Deadline,
)
from modules.backend.app.warehouse.egress import (
    Resolver,
    refused_address,
    system_resolver,
)
from modules.backend.app.warehouse.errors import (
    WarehouseError,
    WarehouseErrorCode,
    error_for_status,
    sanitised,
)

#: The services a connector may call.
AWS_SERVICES = frozenset({"sts", "athena", "glue"})

#: The warehouses that reach AWS through this module.
AWS_WAREHOUSES = frozenset({"athena", "redshift"})


def _aws_warehouse(warehouse: str) -> str:
    """``warehouse`` itself, or ``ValueError`` if it is not an AWS warehouse."""
    if warehouse not in AWS_WAREHOUSES:
        raise ValueError(f"not an AWS warehouse: {warehouse!r}")
    return warehouse


#: botocore's regions for athena, sts and glue in the ``aws`` partition
#: (identical lists; 34 regions as of botocore 1.43).
AWS_REGIONS = frozenset(
    {
        "af-south-1",
        "ap-east-1",
        "ap-east-2",
        "ap-northeast-1",
        "ap-northeast-2",
        "ap-northeast-3",
        "ap-south-1",
        "ap-south-2",
        "ap-southeast-1",
        "ap-southeast-2",
        "ap-southeast-3",
        "ap-southeast-4",
        "ap-southeast-5",
        "ap-southeast-6",
        "ap-southeast-7",
        "ca-central-1",
        "ca-west-1",
        "eu-central-1",
        "eu-central-2",
        "eu-north-1",
        "eu-south-1",
        "eu-south-2",
        "eu-west-1",
        "eu-west-2",
        "eu-west-3",
        "il-central-1",
        "me-central-1",
        "me-south-1",
        "mx-central-1",
        "sa-east-1",
        "us-east-1",
        "us-east-2",
        "us-west-1",
        "us-west-2",
    }
)


def aws_endpoint_host(service: str, region: str, *, warehouse: str) -> str:
    """``<service>.<region>.amazonaws.com``, for an allowed service and region."""
    _aws_warehouse(warehouse)
    if service not in AWS_SERVICES or region not in AWS_REGIONS:
        raise WarehouseError(
            WarehouseErrorCode.DESTINATION_NOT_ALLOWED, warehouse=warehouse
        )
    return f"{service}.{region}.amazonaws.com"


#: Attempts per call, the first included, when the budget allows two.
MAX_ATTEMPTS = 2


def boto_config(deadline: Optional[Deadline] = None) -> Config:
    """Connect within 5 s, each read within 30 s, two attempts, no proxies.

    Under ``deadline`` each limit is ``min(fixed, remaining)``, and there is a
    single attempt unless two whole attempts (2 x (5 + 30) s) still fit, so
    retries cannot run past what is left.  ``time_limit`` if nothing is left.
    """
    connect, read, attempts = CONNECT_SECONDS, READ_SECONDS, MAX_ATTEMPTS
    if deadline is not None:
        remaining = deadline.check()
        connect = min(CONNECT_SECONDS, remaining)
        read = min(READ_SECONDS, remaining)
        if remaining < MAX_ATTEMPTS * (CONNECT_SECONDS + READ_SECONDS):
            attempts = 1
    return Config(
        connect_timeout=connect,
        read_timeout=read,
        # total_max_attempts counts the first attempt; botocore's
        # max_attempts counts retries only (measured: max_attempts=1 made two
        # connections).
        retries={"total_max_attempts": attempts, "mode": "standard"},
        proxies={},
    )


class BeforeSendGuard:
    """The botocore ``before-send`` handler: destination, address and deadline."""

    def __init__(
        self,
        expected_host: str,
        *,
        warehouse: str,
        deadline: Optional[Deadline] = None,
        resolver: Optional[Resolver] = None,
    ) -> None:
        self.expected_host = expected_host
        self.warehouse = _aws_warehouse(warehouse)
        self._deadline = deadline
        self._resolve: Resolver = resolver or system_resolver

    def __call__(self, request: Any, **kwargs: Any) -> None:
        if self._deadline is not None:
            self._deadline.check()
        parts = urlsplit(request.url)
        if (
            parts.scheme != "https"
            or parts.port not in (None, 443)
            or (parts.hostname or "").lower() != self.expected_host
        ):
            raise WarehouseError(
                WarehouseErrorCode.DESTINATION_NOT_ALLOWED, warehouse=self.warehouse
            )
        resolve_failed = False
        addresses = []
        try:
            addresses = list(self._resolve(self.expected_host, 443))
        except (OSError, UnicodeError):
            resolve_failed = True
        if resolve_failed or not addresses:
            raise WarehouseError(
                WarehouseErrorCode.UNREACHABLE, warehouse=self.warehouse
            )
        if any(refused_address(address) for address, _ in addresses):
            raise WarehouseError(
                WarehouseErrorCode.DESTINATION_NOT_ALLOWED, warehouse=self.warehouse
            )
        # Returning None lets botocore send the request itself.
        return


def make_client(
    session: Any,
    service: str,
    region: str,
    *,
    warehouse: str,
    deadline: Optional[Deadline] = None,
    resolver: Optional[Resolver] = None,
) -> Any:
    """A boto3 client for ``service`` in ``region`` with the guard installed.

    ``session`` is a ``boto3.session.Session``; the caller decides whose
    credentials it holds.  Under ``deadline`` the client's limits are what the
    deadline has left *now*; for a later call, build a new client (:func:`call`).
    """
    host = aws_endpoint_host(service, region, warehouse=warehouse)
    client = session.client(service, region_name=region, config=boto_config(deadline))
    if client.meta.endpoint_url.rstrip("/") != f"https://{host}":
        raise WarehouseError(
            WarehouseErrorCode.DESTINATION_NOT_ALLOWED, warehouse=warehouse
        )
    client.meta.events.register(
        "before-send",
        BeforeSendGuard(
            host, warehouse=warehouse, deadline=deadline, resolver=resolver
        ),
    )
    return client


def call(
    session: Any,
    service: str,
    region: str,
    operation: str,
    *,
    warehouse: str,
    deadline: Deadline,
    resolver: Optional[Resolver] = None,
    codes: Optional[Mapping[str, WarehouseErrorCode]] = None,
    **params: Any,
) -> Any:
    """One boto3 call under ``deadline``, on a client built for it, errors coded.

    The client is built for this call, so its connect and read limits and its
    attempts are sized from what the deadline has left at this moment.  AWS
    errors map by their structured ``Code`` -- through ``codes``, this call's
    own table, when it is given -- and then by HTTP status
    (:func:`.errors.error_for_status`).  A connect or read limit is
    ``time_limit``; no AWS identity to sign with is ``aws_identity_unavailable``;
    a parameter botocore refuses before sending is ours, so ``internal``; any
    other botocore failure is ``unreachable``.
    """
    _aws_warehouse(warehouse)
    client = make_client(
        session,
        service,
        region,
        warehouse=warehouse,
        deadline=deadline,
        resolver=resolver,
    )
    failure: Optional[WarehouseError] = None
    try:
        return getattr(client, operation)(**params)
    except WarehouseError as exc:
        failure = exc
    except ClientError as exc:
        error = exc.response.get("Error", {}) if isinstance(exc.response, dict) else {}
        meta = (
            exc.response.get("ResponseMetadata", {})
            if isinstance(exc.response, dict)
            else {}
        )
        failure = error_for_status(
            meta.get("HTTPStatusCode") or 0,
            warehouse=warehouse,
            vendor_code=error.get("Code"),
            request_id=meta.get("RequestId"),
            codes=codes,
        )
    except (ConnectTimeoutError, ReadTimeoutError):
        failure = WarehouseError(WarehouseErrorCode.TIME_LIMIT, warehouse=warehouse)
    except NoCredentialsError:
        failure = WarehouseError(
            WarehouseErrorCode.AWS_IDENTITY_UNAVAILABLE, warehouse=warehouse
        )
    except ParamValidationError:
        failure = WarehouseError(WarehouseErrorCode.INTERNAL, warehouse=warehouse)
    except BotoCoreError:
        failure = WarehouseError(WarehouseErrorCode.UNREACHABLE, warehouse=warehouse)
    # Outside the handlers: nothing from botocore is chained onto this.
    assert failure is not None
    raise sanitised(failure)


#: STS's codes for the customer's role, used by :func:`assume_role_session`
#: only: STS answers ``AccessDenied`` when the role's trust policy does not
#: admit this deployment with this external id (or there is no such role).
STS_CODES: Mapping[str, WarehouseErrorCode] = {
    "AccessDenied": WarehouseErrorCode.AUTH_FAILED,
    "Throttling": WarehouseErrorCode.WAREHOUSE_BUSY,
}

#: The session name of every assumed role (the customer's CloudTrail shows it).
ROLE_SESSION_NAME = "controlvar-warehouse"
#: The shortest session STS issues.
ROLE_SESSION_SECONDS = 900

_ROLE_ARN = re.compile(ROLE_ARN_PATTERN)
_EXTERNAL_ID = re.compile(r"exp-[0-9a-f]{32}")


def assume_role_session(
    platform_session: Any,
    role_arn: str,
    external_id: str,
    region: str,
    *,
    warehouse: str,
    deadline: Deadline,
    resolver: Optional[Resolver] = None,
) -> Any:
    """A boto3 session holding the customer's role, for one operation.

    ``platform_session`` holds this deployment's own AWS identity.  Refused
    before any call, as ``internal``: a ``role_arn`` that is not a role ARN,
    and an ``external_id`` that is not ``exp-`` and 32 hex characters.

    Then two AssumeRole calls, each through :func:`call` (so under the guard
    and the deadline), with :data:`STS_CODES`:

    1. Without the external id.  Only STS's ``AccessDenied`` lets the
       operation go on.  If STS assumes the role, its trust policy does not
       require the external id: the credentials are dropped unused and the
       operation is refused as ``role_trust_unsafe``.  Any other failure is
       the operation's failure.
    2. With the external id.  ``AccessDenied`` is ``auth_failed``.

    The temporary credentials go into the returned session only; nothing
    here logs them or keeps them.
    """
    _aws_warehouse(warehouse)
    if not isinstance(role_arn, str) or not _ROLE_ARN.fullmatch(role_arn):
        raise WarehouseError(WarehouseErrorCode.INTERNAL, warehouse=warehouse)
    if not isinstance(external_id, str) or not _EXTERNAL_ID.fullmatch(external_id):
        raise WarehouseError(WarehouseErrorCode.INTERNAL, warehouse=warehouse)
    request = {
        "RoleArn": role_arn,
        "RoleSessionName": ROLE_SESSION_NAME,
        "DurationSeconds": ROLE_SESSION_SECONDS,
    }
    sts = {
        "warehouse": warehouse,
        "deadline": deadline,
        "resolver": resolver,
        "codes": STS_CODES,
    }
    try:
        # The answer, if there is one, is not kept.
        call(platform_session, "sts", region, "assume_role", **sts, **request)
    except WarehouseError as refused:
        if not (
            refused.code is WarehouseErrorCode.AUTH_FAILED
            and refused.vendor_code == "AccessDenied"
        ):
            raise
    else:
        raise WarehouseError(WarehouseErrorCode.ROLE_TRUST_UNSAFE, warehouse=warehouse)
    answer = call(
        platform_session,
        "sts",
        region,
        "assume_role",
        **sts,
        **request,
        ExternalId=external_id,
    )
    credentials = answer["Credentials"]
    return boto3.session.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
        region_name=region,
    )


__all__ = [
    "AWS_REGIONS",
    "AWS_SERVICES",
    "AWS_WAREHOUSES",
    "STS_CODES",
    "BeforeSendGuard",
    "assume_role_session",
    "aws_endpoint_host",
    "boto_config",
    "call",
    "make_client",
]
