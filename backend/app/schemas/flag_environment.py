"""
Request and response models of the flag environment routes (#2011, beta).

A flag has one configuration per environment: production's is the flag row,
staging's and development's are side rows, at most one each
(``backend.app.core.flag_environments``). These models are how the routes of
``backend.app.api.v1.endpoints.flag_environments`` read and write them.

An environment is named by exactly ``production``, ``staging`` or
``development``; any other name, in a path or in a body, is the standard 422
(``Input should be 'production', 'staging' or 'development'``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator
from pydantic_core import PydanticCustomError

from backend.app.core.flag_environments import (
    PRODUCTION,
    FlagEnvironmentName,
    flag_in_environment,
)
from backend.app.core.targeting_adapter import (
    TargetingRulesError,
    validate_flag_targeting,
)
from backend.app.models.feature_flag import flag_status_name
from backend.app.schemas.feature_flag import (
    SPLIT_READ_DESCRIPTION,
    SPLIT_REQUEST_DESCRIPTION,
    TARGETING_RULES_DESCRIPTION,
    _split_schema,
    read_split,
)
from backend.app.schemas.storable_text import (
    contains_unstorable_text,
    unstorable_text_message,
)


class FlagEnvironmentRead(BaseModel):
    """One of the installation's three flag environments."""

    key: FlagEnvironmentName = Field(..., description="The environment's name.")
    name: str = Field(..., description="The name a reader sees, such as `Staging`.")
    default: bool = Field(
        ...,
        description=(
            "True for production, the environment a flag's own settings belong to."
        ),
    )


class FlagEnvironmentSettings(BaseModel):
    """What a flag serves in one environment."""

    # Every field is in every answer, so the document says so.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    environment: FlagEnvironmentName
    status: Literal["active", "inactive", "archived"] = Field(
        ...,
        description=(
            "`archived` in every environment when the flag is archived; otherwise "
            "`active` or `inactive` in this one."
        ),
    )
    is_active: bool = Field(..., description='True exactly when status is "active".')
    rollout_percentage: int
    targeting_rules: Optional[Any] = Field(
        None, description="The environment's targeting rules, as stored."
    )
    split: Optional[Any] = Field(
        None, description="The environment's split. " + SPLIT_READ_DESCRIPTION
    )
    configured: bool = Field(
        ...,
        description=(
            "False when nobody has set the flag up in this environment: it is off "
            "there, at 0% with no rules. Always true for production."
        ),
    )
    updated_at: Optional[datetime] = Field(
        None,
        description=(
            "When these settings last changed; the flag's own `updated_at` for "
            "production, null when not configured."
        ),
    )

    @classmethod
    def of(
        cls, flag: Any, setting: Optional[Any], environment: str
    ) -> "FlagEnvironmentSettings":
        """*flag* in *environment*, given its side row there or ``None``.

        Every value comes from ``flag_in_environment``, so what these routes
        say is what an evaluation in that environment would serve.
        """
        view = flag_in_environment(flag, setting, environment)
        status = flag_status_name(view.status).lower()
        row = flag if environment == PRODUCTION else setting
        return cls(
            environment=environment,
            status=status,
            is_active=status == "active",
            rollout_percentage=view.rollout_percentage,
            targeting_rules=view.targeting_rules,
            split=read_split(view.value_type, view.split),
            configured=row is not None,
            updated_at=None if row is None else row.updated_at,
        )


class FlagEnvironments(BaseModel):
    """A flag in every environment, production first."""

    flag_id: UUID
    key: str
    environments: List[FlagEnvironmentSettings]


class EnvironmentFlagSettings(BaseModel):
    """Several flags in one environment, by flag id."""

    environment: FlagEnvironmentName
    settings: Dict[str, FlagEnvironmentSettings] = Field(
        ...,
        description=(
            "One entry per requested flag that exists, keyed by its id. An id that "
            "names no flag has no entry."
        ),
    )


class FlagEnvironmentUpdate(BaseModel):
    """The settings of a flag in one environment to change.

    Only the fields sent change. ``is_active`` and ``rollout_percentage``
    refuse null; a null ``targeting_rules`` clears the rules, and a null
    ``split`` the split. A field not listed here answers 422. The split and
    the rules' own values are checked against the flag's type by the route
    (``flag_values.serving_errors``): this model cannot see it.
    """

    model_config = ConfigDict(extra="forbid")

    # The two fields below default to None only to mean "not sent": pydantic
    # does not validate a default, while an explicit null reaches refuse_null.
    is_active: bool = Field(
        None, description="Turn the flag on or off in this environment."
    )
    rollout_percentage: int = Field(None, ge=0, le=100)
    targeting_rules: Optional[Any] = Field(
        None, description=TARGETING_RULES_DESCRIPTION
    )
    split: Any = Field(
        None,
        description=SPLIT_REQUEST_DESCRIPTION,
        json_schema_extra=_split_schema(),
    )

    @field_validator("is_active", "rollout_percentage", mode="before")
    @classmethod
    def refuse_null(cls, value: Any, info: ValidationInfo) -> Any:
        """Refuse an explicit null; leaving the field out means "not sent"."""
        if value is None:
            raise PydanticCustomError(
                "null_not_allowed", f"{info.field_name} cannot be null"
            )
        return value

    @field_validator("targeting_rules", mode="before")
    @classmethod
    def refuse_unstorable_text(cls, value: Any, info: ValidationInfo) -> Any:
        """Refuse NUL and unpaired surrogates in the rules, as a flag update
        does (#2163)."""
        if contains_unstorable_text(value):
            raise ValueError(unstorable_text_message(info.field_name))
        return value

    @field_validator("targeting_rules")
    @classmethod
    def checked_targeting_rules(cls, value: Any) -> Any:
        """Refuse rules the flag evaluator would not apply as written, exactly
        as a flag update does (#535). The value is returned as given."""
        try:
            validate_flag_targeting(value)
        except TargetingRulesError as err:
            raise ValueError(str(err)) from None
        return value


class FlagPromotion(BaseModel):
    """The environment to copy a flag's settings from."""

    model_config = ConfigDict(extra="forbid")

    source: FlagEnvironmentName = Field(
        ...,
        alias="from",
        description=(
            "The environment whose settings are copied. It must not be the one "
            "they are copied into."
        ),
    )
