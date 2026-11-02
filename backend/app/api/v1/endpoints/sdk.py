"""
Server-side SDK endpoints.

``GET /api/v1/sdk/ruleset`` (beta): every feature flag in the deployment, in
the one normalised shape a server-side SDK evaluates locally. The document and
its rules are described in :mod:`backend.app.services.sdk_ruleset`.

Authentication is an ``X-API-Key`` that carries the ``sdk:ruleset`` scope; any
other valid key gets 403. The ruleset holds the values written into targeting
rules, and the values string, number and json flags serve, so a key with this
scope belongs on a server, never in a browser or a mobile app.

Each response carries ``ETag: "<version>"``. A poll that sends the ETag back in
``If-None-Match`` gets ``304 Not Modified`` with no body while nothing that
affects evaluation has changed.

The route is ``x-stability: beta``: its shape may still change. The document's
``schema`` integer is how a released SDK recognises a format it does not know.
"""

from __future__ import annotations

from typing import Any, List, Literal, Optional

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, load_only

from backend.app.api import deps
from backend.app.api.sdk_scope import require_sdk_ruleset_key
from backend.app.models.api_key import APIKey
from backend.app.models.feature_flag import FeatureFlag
from backend.app.services.sdk_ruleset import build_ruleset, canonical_json

router = APIRouter()

#: If-None-Match entries read at most (a poll sends one).
MAX_IF_NONE_MATCH_ENTRIES = 32


# ---------------------------------------------------------------------------
# Response schema (documentation: the route returns the canonical JSON bytes)
# ---------------------------------------------------------------------------


class SdkRulesetCondition(BaseModel):
    """One condition, with the engine's operator name."""

    attribute: str = Field(..., description="Attribute name, as the rule wrote it")
    operator: str = Field(
        ...,
        description=(
            "One of eq, neq, in, not_in, contains, not_contains, starts_with, "
            "ends_with, gt, gte, lt, lte, is_null, is_not_null"
        ),
    )
    value: Any = Field(
        None,
        description=(
            "A string, a number, or a list of strings or of numbers; null for "
            "is_null and is_not_null"
        ),
    )


class SdkRulesetGroup(BaseModel):
    """Conditions and nested groups combined with ``op``."""

    op: Literal["and", "or", "not"]
    conditions: List[SdkRulesetCondition]
    groups: List["SdkRulesetGroup"]


SdkRulesetGroup.model_rebuild()


class SdkRulesetRule(BaseModel):
    """A targeting rule, in the order the server tries them."""

    id: str
    rollout_percentage: int
    match: SdkRulesetGroup


class SdkRulesetDefaultRule(BaseModel):
    """The rule the server applies when no rule matches (conditions not evaluated)."""

    id: str
    rollout_percentage: int


class SdkRulesetFlag(BaseModel):
    """One flag. Inactive flags carry only ``key`` and ``active``, plus
    ``value_type`` and ``off_value`` for a string, number or json flag. The
    exception is a flag whose stored values cannot be served: it is a remote
    entry whatever its status, so turning it off reaches a local SDK through
    the SDK's cache of the server's answer (up to 300 s by default)."""

    key: str
    active: bool
    evaluation: Optional[Literal["local", "remote"]] = Field(
        None,
        description=(
            "local: the SDK may evaluate this flag; remote: it asks the server "
            "(no rules or values are included)"
        ),
    )
    rollout_percentage: Optional[int] = None
    rules: Optional[List[SdkRulesetRule]] = None
    default_rule: Optional[SdkRulesetDefaultRule] = None
    value_type: Optional[str] = Field(
        None,
        description=(
            "What the flag serves: string, number, or json (an object or an "
            "array). Present on the local and inactive entries of such a flag; "
            "absent for a boolean flag, which serves true to the users it is on "
            "for and false to everyone else. An SDK that does not know the type "
            "asks the server for that flag's value."
        ),
    )
    on_value: Optional[Any] = Field(
        None,
        description=(
            "What a user the flag is on for gets: a user who matches a rule and "
            "falls inside its percentage, or matches no rule and falls inside "
            "rollout_percentage. Of value_type; on local entries only."
        ),
    )
    off_value: Optional[Any] = Field(
        None,
        description=(
            "What everyone else gets, and what everyone gets while the flag is "
            "off. Of value_type; on local and inactive entries."
        ),
    )


class SdkRuleset(BaseModel):
    """The flag ruleset for server-side local evaluation."""

    model_config = ConfigDict(populate_by_name=True)

    ruleset_schema: int = Field(
        ...,
        alias="schema",
        description="Format version. An SDK that does not know it evaluates nothing locally.",
    )
    version: str = Field(
        ..., description="sha256 of the content; the same value as the ETag"
    )
    bucketing: str = Field(
        ...,
        description=(
            "The flag bucketing function: md5-mod100-v1 is "
            "int(md5(user_id + ':' + flag_key), 16) % 100 < percentage"
        ),
    )
    flags: List[SdkRulesetFlag]


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


def if_none_match_hits(header: Optional[str], version: str) -> bool:
    """Whether an ``If-None-Match`` header names the current version.

    Weak comparison (RFC 9110 13.1.2): ``W/`` is ignored, ``*`` matches.
    Only the first :data:`MAX_IF_NONE_MATCH_ENTRIES` entries are read.
    """
    if not header:
        return False
    current = f'"{version}"'
    for raw in header.split(",", MAX_IF_NONE_MATCH_ENTRIES)[:MAX_IF_NONE_MATCH_ENTRIES]:
        tag = raw.strip()
        if tag == "*":
            return True
        if tag.startswith("W/"):
            tag = tag[2:]
        if tag == current:
            return True
    return False


@router.get(
    "/ruleset",
    # Documentation only: the route returns a Response, so this is never validated.
    response_model=SdkRuleset,
    summary="Flag ruleset for server-side local evaluation (beta)",
    response_description="Every feature flag, normalised for local evaluation",
    responses={
        status.HTTP_304_NOT_MODIFIED: {
            "description": "If-None-Match names the current version; no body"
        },
        status.HTTP_401_UNAUTHORIZED: {"description": "Missing or invalid API key"},
        status.HTTP_403_FORBIDDEN: {
            "description": "The API key does not carry the sdk:ruleset scope"
        },
    },
    openapi_extra={"x-stability": "beta"},
)
def get_ruleset(
    request: Request,
    db: Session = Depends(deps.get_db),
    api_key: APIKey = Depends(require_sdk_ruleset_key),
) -> Response:
    """
    Every feature flag in the deployment, for a server-side SDK that evaluates
    flags locally.

    **Authentication**: an `X-API-Key` whose scopes include `sdk:ruleset`. A
    valid key without it gets 403. The ruleset contains the values written in
    targeting rules, and the values string, number and json flags serve: keep
    such a key on a server.

    **Caching**: the response carries `ETag`. Send it back in `If-None-Match`
    and an unchanged ruleset answers 304 with no body.

    A flag the SDK may evaluate itself is `"evaluation": "local"` and carries
    its rollout percentage and rules; `"remote"` means ask
    `GET /api/v1/feature-flags/evaluate/{key}`; an inactive flag is
    `{"key", "active": false}`. A string, number or json flag also carries
    `value_type` and its two values: `on_value` and `off_value` on a local
    entry, `off_value` on an inactive one. A remote entry carries no value.
    A flag whose stored values cannot be served is a remote entry whatever its
    status, so switching it off reaches a local SDK only when its cached
    answer from the server expires (up to 300 s by default in the 0.1.1 and
    0.2.0 SDKs); `getAllFlags` always asks the server.
    """
    # Every column the builder reads, loaded by this one query: a column left
    # out would be loaded by one more query per flag (PF-2).
    flags = (
        db.query(FeatureFlag)
        .options(
            load_only(
                FeatureFlag.key,
                FeatureFlag.status,
                FeatureFlag.rollout_percentage,
                FeatureFlag.targeting_rules,
                FeatureFlag.value_type,
                FeatureFlag.on_value,
                FeatureFlag.off_value,
            )
        )
        .all()
    )
    ruleset = build_ruleset(flags)
    headers = {
        "ETag": f'"{ruleset["version"]}"',
        "Cache-Control": "private, no-cache",
    }
    if if_none_match_hits(request.headers.get("if-none-match"), ruleset["version"]):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return Response(
        content=canonical_json(ruleset),
        media_type="application/json",
        headers=headers,
    )
