# backend/app/schemas/feature_flag.py
"""
Feature flag schema models for validation and serialization.

The create/update contract (#94, D39 as narrowed by D40):

* A request names only fields the API reads. Anything else answers 422
  (``extra="forbid"``), instead of being dropped without a word.
* The fields every response carries but no request writes (``id``,
  ``owner_id``, ``created_at``, ``updated_at``, ``status``) are declared on the
  request models as read-only, so a GET body can be sent back unchanged. They
  are ignored -- removed by ``FeatureFlagService``, which is also where a sent
  ``status`` is compared with the stored one.
* A new flag is inactive unless ``is_active: true`` is sent.
* A flag has a ``value_type`` (``boolean``, the default, ``string``,
  ``number`` or ``json``), set on create and read-only afterwards, and two
  values: ``on_value`` for the users it is on for, ``off_value`` for everyone
  else (#1497, DECISIONS T229). A boolean flag's are ``true`` and ``false``.
  The value rules are ``backend.app.core.flag_values``; a create is checked
  here, an update by ``FeatureFlagService`` against the stored type.
* ``default_value`` is stored and type-checked, and accepts only ``false``
  for every flag (D40). It is deprecated in favour of ``off_value``.
* ``targeting_rules`` the flag evaluator would not apply as written answer
  422 (#535). That includes rules sent back from a GET: a GET body can be sent
  back unchanged unless its ``targeting_rules`` are ones PUT now refuses (D41).
* There is one response representation, :class:`FeatureFlagRead`. It shares no
  base with the request models: ``extra="forbid"`` is inherited, and a response
  model must never refuse a stored row.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_core import InitErrorDetails, PydanticCustomError

from backend.app.core.flag_values import (
    BOOLEAN,
    MAX_SAFE_INTEGER,
    MAX_VALUE_BYTES,
    MAX_VALUE_DEPTH,
    VALUE_FIELDS,
    read_values,
    write_errors,
)
from backend.app.core.targeting_adapter import (
    TargetingRulesError,
    validate_flag_targeting,
)

#: The fields a flag response carries that no request writes.  A request may
#: send them back -- a GET body round-trips -- and they are ignored, except that
#: a ``status`` must equal the stored one (``FeatureFlagService``).
READ_ONLY_FIELDS = frozenset({"id", "owner_id", "created_at", "updated_at", "status"})

#: Fields stored in a NOT NULL column, or never null in a request: an explicit
#: null is refused, while leaving one out of an update keeps the stored value.
#: ``on_value`` and ``off_value`` are NULL columns for a boolean flag, but a
#: request never sends null for them: JSON ``null`` is no flag's value.
NOT_NULL_FIELDS = (
    "key",
    "name",
    "is_active",
    "rollout_percentage",
    "default_value",
    "value_type",
    "on_value",
    "off_value",
)

#: The answer to a ``default_value`` other than ``false`` (PF-4).
DEFAULT_VALUE_UNSUPPORTED = (
    "default_value is deprecated and accepts only false; to serve another value "
    "when the flag is off, create a string, number or json flag and set off_value"
)

#: What a flag can serve.  A request names one of these; a response reads any
#: string, so a row of a type a later release added is still answered.
ValueType = Literal["boolean", "string", "number", "json"]

VALUE_TYPE_DESCRIPTION = (
    "What the flag serves: `boolean` (the default; `true` to users the flag is "
    "on for, `false` to everyone else), `string`, `number`, or `json` (an object "
    "or an array). Set when the flag is created; an update may send it back "
    "unchanged, and any other value answers 422."
)
ON_VALUE_DESCRIPTION = (
    "What a user the flag is on for gets: a user who matches a targeting rule "
    "and falls inside its percentage, or matches no rule and falls inside "
    "`rollout_percentage`. Required for a string, number or json flag, and of "
    "that type; a boolean flag's is `true`."
)
OFF_VALUE_DESCRIPTION = (
    "What everyone else gets, and what everyone gets while the flag is off. "
    "Required for a string, number or json flag, and of that type; a boolean "
    "flag's is `false`."
)
#: Added to the two value descriptions on a request.
VALUE_LIMITS_DESCRIPTION = (
    f" At most {MAX_VALUE_BYTES} bytes as compact JSON and {MAX_VALUE_DEPTH} "
    f"levels deep; every number in it finite and from -{MAX_SAFE_INTEGER} to "
    f"{MAX_SAFE_INTEGER}; no NUL or unpaired surrogate character; the two "
    "values differ."
)
DEFAULT_VALUE_REQUEST_DESCRIPTION = (
    "Deprecated: `off_value` replaces it. Only `false` is accepted, for every flag."
)
DEFAULT_VALUE_READ_DESCRIPTION = "Deprecated: always false. Read `off_value`."

#: The answer to a ``status`` that differs from the stored one.
STATUS_READ_ONLY = "status is read-only. Use is_active to turn a flag on or off."

_READ_ONLY = {"readOnly": True}

#: The description of ``targeting_rules`` on a create and an update.
TARGETING_RULES_DESCRIPTION = (
    "Who gets the flag: the dashboard rule builder's shape "
    '({"logical_operator", "groups": [{"logical_operator", "conditions": '
    '[{"attribute", "operator", "value"}]}]}) or the native rules shape '
    '({"rules": [...]}). null, {} and {"groups": []} mean no rules. Rules the '
    "flag evaluator would not apply as written answer 422, with the place and "
    "the reason in the message, for example "
    '"groups[0].conditions[1].operator: unknown operator". A list of rules '
    "is refused. Stored as sent."
)


class _FeatureFlagRequest(BaseModel):
    """What a create and an update have in common.  Never a response base."""

    model_config = ConfigDict(extra="forbid")

    # Read-only: accepted so a GET body can be sent back, and ignored.
    id: Any = Field(
        None, description="Read-only; ignored.", json_schema_extra=_READ_ONLY
    )
    owner_id: Any = Field(
        None, description="Read-only; ignored.", json_schema_extra=_READ_ONLY
    )
    created_at: Any = Field(
        None, description="Read-only; ignored.", json_schema_extra=_READ_ONLY
    )
    updated_at: Any = Field(
        None, description="Read-only; ignored.", json_schema_extra=_READ_ONLY
    )
    status: Any = Field(
        None,
        description=(
            "Read-only. Accepted when it equals the flag's status, in any case "
            "(on create: the status is_active gives it); any other value answers "
            "422. Turn a flag on or off with is_active."
        ),
        json_schema_extra=_READ_ONLY,
    )

    @field_validator(*NOT_NULL_FIELDS, mode="before", check_fields=False)
    @classmethod
    def refuse_null(cls, value: Any, info: ValidationInfo) -> Any:
        """Refuse an explicit null for a field stored in a NOT NULL column.

        A ``before`` validator does not run for a field left out: pydantic does
        not validate a default, so omission still means "not sent".
        """
        if value is None:
            raise PydanticCustomError(
                "null_not_allowed", f"{info.field_name} cannot be null"
            )
        return value

    @field_validator("key", check_fields=False)
    @classmethod
    def validate_key(cls, v: str) -> str:
        """Keys are lowercase letters, digits, hyphens and underscores, and
        start with a letter or a digit."""
        import re

        if not re.match(r"^[a-z0-9][a-z0-9_-]*$", v):
            raise ValueError(
                "Key must be lowercase alphanumeric characters, hyphens, or underscores only"
            )
        return v

    @field_validator("targeting_rules", check_fields=False)
    @classmethod
    def checked_targeting_rules(cls, value: Any) -> Any:
        """Refuse rules the flag evaluator would not apply as written (#535).

        The value is returned as given, never rewritten. The message is fixed
        text naming a place and a reason, never the submitted rules (see
        ``validate_flag_targeting``).
        """
        try:
            validate_flag_targeting(value)
        except TargetingRulesError as err:
            raise ValueError(str(err)) from None
        return value

    @field_validator("default_value", check_fields=False)
    @classmethod
    def only_false_for_now(cls, v: bool) -> bool:
        """``default_value`` may only be false, for every flag (D40, T229).

        A boolean flag whose off-value is ``true`` would keep serving ``true``
        when it is turned off.  The field is deprecated: a flag that serves
        another value when it is off is a string, number or json flag with an
        ``off_value``.  The message names no submitted value.
        """
        if v is True:
            raise PydanticCustomError(
                "default_value_unsupported", DEFAULT_VALUE_UNSUPPORTED
            )
        return v


def value_errors_to_raise(
    title: str, errors: List[Any], prefix: tuple = ()
) -> ValidationError:
    """The ``ValidationError`` for ``flag_values.write_errors`` pairs.

    Each error sits at its own field (``prefix`` + the field name), so a 422
    names ``["body", "on_value"]`` rather than the body as a whole.  ``input``
    is ``None``: the value is never repeated (and the 422 handler drops
    ``input`` anyway).
    """
    return ValidationError.from_exception_data(
        title,
        [
            InitErrorDetails(type=error, loc=(*prefix, field), input=None)
            for field, error in errors
        ],
    )


class FeatureFlagCreate(_FeatureFlagRequest):
    """The body of a flag create. A field not listed here answers 422."""

    key: str = Field(..., min_length=1, max_length=100)
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=2000)
    is_active: bool = Field(
        False, description="Create the flag on. Left out, the flag starts off."
    )
    rollout_percentage: int = Field(0, ge=0, le=100)
    targeting_rules: Optional[Any] = Field(
        None, description=TARGETING_RULES_DESCRIPTION
    )
    default_value: StrictBool = Field(
        False, description=DEFAULT_VALUE_REQUEST_DESCRIPTION
    )
    value_type: ValueType = Field(BOOLEAN, description=VALUE_TYPE_DESCRIPTION)
    on_value: Any = Field(
        None, description=ON_VALUE_DESCRIPTION + VALUE_LIMITS_DESCRIPTION
    )
    off_value: Any = Field(
        None, description=OFF_VALUE_DESCRIPTION + VALUE_LIMITS_DESCRIPTION
    )
    tags: Optional[List[str]] = None

    @model_validator(mode="after")
    def values_fit_the_type(self) -> "FeatureFlagCreate":
        """The two values follow the value rules for ``value_type``.

        A string, number or json flag sends both, of its type and within the
        limits, and different; a boolean flag sends neither, or ``true`` and
        ``false``.  Each refusal is at its own field.
        """
        sent = {
            field: getattr(self, field)
            for field in VALUE_FIELDS
            if field in self.model_fields_set
        }
        errors = write_errors(self.value_type, sent, None)
        if errors:
            raise value_errors_to_raise(type(self).__name__, errors)
        return self


class FeatureFlagUpdate(_FeatureFlagRequest):
    """The body of a flag update.

    Every field is optional, and a field left out keeps its value. key, name,
    is_active, rollout_percentage, default_value, value_type, on_value and
    off_value refuse null; description, targeting_rules and tags accept null,
    which clears them. A field not listed here answers 422.

    ``value_type`` and the two values are checked against the stored flag, by
    ``FeatureFlagService.update_feature_flag``: this model cannot see the
    stored type.
    """

    # The NOT_NULL_FIELDS default to None only to mean "not sent": pydantic
    # does not validate a default, while an explicit null reaches refuse_null.
    key: str = Field(None, min_length=1, max_length=100)
    name: str = Field(None, min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=2000)
    is_active: bool = Field(None, description="Turn the flag on or off.")
    rollout_percentage: int = Field(None, ge=0, le=100)
    targeting_rules: Optional[Any] = Field(
        None, description=TARGETING_RULES_DESCRIPTION
    )
    default_value: StrictBool = Field(
        None, description=DEFAULT_VALUE_REQUEST_DESCRIPTION
    )
    value_type: ValueType = Field(None, description=VALUE_TYPE_DESCRIPTION)
    on_value: Any = Field(
        None, description=ON_VALUE_DESCRIPTION + VALUE_LIMITS_DESCRIPTION
    )
    off_value: Any = Field(
        None, description=OFF_VALUE_DESCRIPTION + VALUE_LIMITS_DESCRIPTION
    )
    tags: Optional[List[str]] = None


class FeatureFlagEvaluation(BaseModel):
    """Model for feature flag evaluation results."""

    key: str
    value: Any
    reason: str
    rule_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

    model_config = ConfigDict(from_attributes=True)


class FeatureFlagRead(BaseModel):
    """A feature flag, as every flag route returns it.

    Any of these bodies can be sent back with an update unchanged unless its
    `targeting_rules` are ones PUT now refuses (422); omitting the field
    still works.
    """

    # Create, get, update, activate, deactivate and the list items all answer
    # with this, and nothing else.  It holds types only -- no key pattern, no
    # length limit, and ``value_type`` a ``str``, never a Literal -- because a
    # response model that refuses a stored row turns a read into a 500, and an
    # older image must answer a row of a type a later release added.  The
    # normaliser below is the only logic: ``status`` is lower-cased and
    # ``is_active`` is derived from it, so the two never disagree and
    # ``is_active`` is never null; a boolean flag (both value columns NULL)
    # reads ``on_value: true`` and ``off_value: false``.
    model_config = ConfigDict(
        from_attributes=True, json_schema_serialization_defaults_required=True
    )

    id: UUID
    key: str
    name: str
    description: Optional[str] = None
    status: Literal["active", "inactive", "archived"] = Field(
        ..., description="The flag's status, lower-cased."
    )
    is_active: bool = Field(..., description='True exactly when status is "active".')
    rollout_percentage: int
    targeting_rules: Optional[Any] = Field(
        None, description="The flag's targeting rules, as stored."
    )
    default_value: bool = Field(..., description=DEFAULT_VALUE_READ_DESCRIPTION)
    value_type: str = Field(..., description=VALUE_TYPE_DESCRIPTION)
    on_value: Any = Field(..., description=ON_VALUE_DESCRIPTION)
    off_value: Any = Field(..., description=OFF_VALUE_DESCRIPTION)
    tags: Optional[List[str]] = None
    owner_id: Optional[UUID] = Field(
        None, description="The user who created the flag; null once that user is gone."
    )
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="before")
    @classmethod
    def _normalise(cls, data: Any) -> Any:
        """Read a stored row (or a mapping), lower-case ``status``, derive
        ``is_active`` from it, and read a boolean flag's two values."""
        if isinstance(data, Mapping):
            values = dict(data)
        else:
            values = {
                name: getattr(data, name, None)
                for name in cls.model_fields
                if name != "is_active"
            }
        status = values.get("status")
        status = getattr(status, "value", status)
        if status is not None:
            status = str(status).lower()
        values["status"] = status
        values["is_active"] = status == "active"
        value_type = values.get("value_type") or BOOLEAN
        values["value_type"] = value_type
        values["on_value"], values["off_value"] = read_values(
            value_type, values.get("on_value"), values.get("off_value")
        )
        return values


def flag_to_read(flag: Any) -> FeatureFlagRead:
    """The one serializer from a stored flag to its response."""
    return FeatureFlagRead.model_validate(flag)


class FeatureFlagListResponse(BaseModel):
    """
    Paginated response model for feature flags.
    """

    items: List[FeatureFlagRead]
    total: int
    skip: int
    limit: int

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "items": [
                    {
                        "id": "123e4567-e89b-12d3-a456-426614174000",
                        "key": "new_feature",
                        "name": "New Feature Flag",
                        "description": "Controls access to new feature",
                        "status": "inactive",
                        "is_active": False,
                        "rollout_percentage": 0,
                        "targeting_rules": None,
                        "default_value": False,
                        "value_type": "boolean",
                        "on_value": True,
                        "off_value": False,
                        "tags": None,
                        "owner_id": "5f0c8a52-1d7e-4c3b-9a6f-2b8e4d1c7a90",
                        "created_at": "2023-01-01T00:00:00Z",
                        "updated_at": "2023-01-01T00:00:00Z",
                    }
                ],
                "total": 1,
                "skip": 0,
                "limit": 100,
            }
        },
    )
