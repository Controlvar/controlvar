"""``balance_within``: what an experiment balances its split within (#448).

``{"attribute": "<key>", "values": ["...", ...]}``: one top-level key of the
assign ``context`` and the values to keep apart, 1 to 20 strings of 1 to 100
characters. A closed key (``web_device``, ``web_visitor``, ``web_browser``)
needs no list; a list given for one must be part of its set. ``segments``
(saved segments) is part of the shape and refused until it is available.

Every refusal is a 422 whose message is one of the texts below, at
``["body", "balance_within", ...]``: the key, the values, or the setting as a
whole. What a balanced experiment refuses beyond its own setting (a bandit, a
split not in steps of 5, a split URL experiment) is judged with the rest of
the experiment by ``backend.app.core.balanced_blocks.balance_problem``.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, List, Optional

from pydantic import (
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from backend.app.core.balanced_blocks import (
    CLOSED_KEYS,
    MAX_GROUPS,
    MAX_VALUE_LENGTH,
    RESERVED_GROUPS,
)
from backend.app.schemas.storable_text import StorableTextModel

#: A top-level key of the user's attributes: letters, digits, ``_`` and
#: ``-``, not starting with ``-``, at most 100 characters. No dot: the group
#: is read from the top-level key only.
ATTRIBUTE_PATTERN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,99}")
#: Keys that are not attributes of the user.
REFUSED_ATTRIBUTES = frozenset({"user_id", "segment"})

#: R1, for a key with no list.
NO_VALUES = (
    "List the values of {attribute} to balance within (1 to 20). Users with any "
    "other value are balanced together as other, and users without "
    "{attribute} as unknown."
)
#: R2.
TOO_MANY_VALUES = "Balance within at most 20 values of {attribute}, not {count}."
#: R3.
RESERVED_VALUE = (
    "other and unknown name the groups for any other value and for no value, "
    "so they cannot be listed."
)
#: R4.
REPEATED_VALUE = "{attribute}'s value {value} is listed twice."
#: R5.
DOTTED_ATTRIBUTE = (
    "Balance on a top-level key of the user's attributes, such as country; a "
    "key with a dot, such as user.country, is not read."
)
#: R9, for a value outside a closed key's set.
CLOSED_VALUE = "{attribute} is {allowed}; {value} is not one of them."
#: R11: saved segments come later.
SEGMENTS_NOT_YET = (
    "Balancing within saved segments is not available yet. Balance within an "
    "attribute instead, such as country."
)
#: R13.
BAD_ATTRIBUTE = (
    "Balance on a top-level key of the user's attributes, made of letters, "
    "digits, _ and - (up to 100 characters), other than user_id and segment."
)
#: A setting that names no attribute.
NO_ATTRIBUTE = "Name the attribute to balance within, such as country."

BalanceValue = Annotated[
    str, StringConstraints(min_length=1, max_length=MAX_VALUE_LENGTH)
]


def _refuse(message: str) -> PydanticCustomError:
    return PydanticCustomError("value_error", message)


def _listed(values: List[str]) -> str:
    """``phone, tablet or desktop``."""
    if len(values) == 1:
        return values[0]
    return ", ".join(values[:-1]) + " or " + values[-1]


class BalanceWithin(StorableTextModel):
    """Balance the split within each group of users (beta)."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {"attribute": "tier", "values": ["high_value", "churn_risk"]}
        },
    )

    attribute: Optional[str] = Field(
        None,
        description=(
            "The top-level key of the assign `context` whose value puts a user "
            "in a group: letters, digits, `_` and `-`, at most 100 characters, "
            "no dot, not `user_id` or `segment`."
        ),
    )
    values: Optional[List[BalanceValue]] = Field(
        None,
        description=(
            "The values to balance within, 1 to 20 strings of 1 to 100 "
            "characters, matched exactly. Users with any other value are "
            "balanced together as `other`, users without the key as `unknown`, "
            "so neither name can be listed. Left out only for a closed key "
            "(`web_device`, `web_visitor`, `web_browser`), whose set is the "
            "list."
        ),
    )
    segments: Optional[List[str]] = Field(
        None,
        description="Not available yet: a request that sends it is refused with 422.",
    )

    @field_validator("segments", mode="before")
    @classmethod
    def refuse_segments(cls, value: Any) -> Any:
        """R11 on presence, before the list is read."""
        if value is not None:
            raise _refuse(SEGMENTS_NOT_YET)
        return value

    @field_validator("attribute")
    @classmethod
    def check_attribute(cls, value: Optional[str]) -> Optional[str]:
        """R5 for a dot, then R13 for anything else that is not a plain key."""
        if value is None:
            return value
        if "." in value:
            raise _refuse(DOTTED_ATTRIBUTE)
        if not ATTRIBUTE_PATTERN.fullmatch(value) or value in REFUSED_ATTRIBUTES:
            raise _refuse(BAD_ATTRIBUTE)
        return value

    @model_validator(mode="after")
    def check_values(self) -> "BalanceWithin":
        """The list, judged with its key: R1, R2, R3, R4, R9."""
        if self.attribute is None:
            if self.segments is None:
                raise _refuse(NO_ATTRIBUTE)
            return self
        attribute = self.attribute
        closed = CLOSED_KEYS.get(attribute)
        values = self.values
        if values is None and closed is not None:
            return self
        if not values:
            raise _refuse(NO_VALUES.format(attribute=attribute))
        if len(values) > MAX_GROUPS:
            raise _refuse(
                TOO_MANY_VALUES.format(attribute=attribute, count=len(values))
            )
        if any(value in RESERVED_GROUPS for value in values):
            raise _refuse(RESERVED_VALUE)
        seen = set()
        for value in values:
            if value in seen:
                raise _refuse(REPEATED_VALUE.format(attribute=attribute, value=value))
            seen.add(value)
        if closed is not None:
            allowed = [value for value in closed if value not in RESERVED_GROUPS]
            for value in values:
                if value not in allowed:
                    raise _refuse(
                        CLOSED_VALUE.format(
                            attribute=attribute, allowed=_listed(allowed), value=value
                        )
                    )
        return self

    def stored(self) -> dict:
        """The value the ``balance_within`` column stores."""
        return self.model_dump(exclude_none=True)
