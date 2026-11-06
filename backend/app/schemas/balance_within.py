"""``balance_within``: what an experiment balances its split within (#448).

``{"attribute": "<key>", "values": ["...", ...]}``: one top-level key of the
assign ``context`` and the values to keep apart, 1 to 20 strings of 1 to 100
characters. A closed key (``web_device``, ``web_visitor``, ``web_browser``)
needs no list; a list given for one must be part of its set.

``{"segments": ["<segment id>", ...]}``: 1 to 10 saved segments, each listed
once; a user is balanced in the first listed segment they belong to. Whether
each segment exists and can be used is checked against the database by the
routes (``backend.app.api.segment_rules.refuse_unusable_balance_segments``);
the schema checks the shape.

Every refusal is a 422 whose message is one of the texts below, at
``["body", "balance_within", ...]``: the key, the values, the segments, or the
setting as a whole. What a balanced experiment refuses beyond its own setting
(a bandit, a split not in steps of 5, a split URL experiment) is judged with
the rest of the experiment by
``backend.app.core.balanced_blocks.balance_problem``.
"""

from __future__ import annotations

import re
from typing import Annotated, List, Optional

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
from backend.app.core.targeting_adapter import MAX_REFERENCED_SEGMENTS, is_segment_id
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
#: R8: an attribute and segments in one setting.
BOTH = "Balance within an attribute or within segments, not both."
#: R8: more segments than one ruleset may reference.
TOO_MANY_SEGMENTS = (
    f"Balance within at most {MAX_REFERENCED_SEGMENTS} segments, not {{count}}."
)
#: R8: an id that names no segment that can be used. Also what the routes
#: answer for a segment that does not exist or is archived.
UNUSABLE_SEGMENT = "Segment {segment_id} does not exist or is archived."
#: What the routes answer for a segment that exists but cannot be used.
SEGMENT_NOT_USABLE = "Segment {segment_id} is inactive or its rules are not valid."
#: A segment listed twice.
REPEATED_SEGMENT = "Segment {segment_id} is listed twice."
#: R13.
BAD_ATTRIBUTE = (
    "Balance on a top-level key of the user's attributes, made of letters, "
    "digits, _ and - (up to 100 characters), other than user_id and segment."
)
#: A setting that names neither an attribute nor a segment.
NEITHER = "Balance within an attribute, such as country, or within segments."

#: A segment id is the canonical text of a UUID: 36 characters.
SEGMENT_ID_LENGTH = 36

BalanceValue = Annotated[
    str, StringConstraints(min_length=1, max_length=MAX_VALUE_LENGTH)
]
BalanceSegment = Annotated[
    str, StringConstraints(min_length=1, max_length=SEGMENT_ID_LENGTH)
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
    segments: Optional[List[BalanceSegment]] = Field(
        None,
        description=(
            "Saved segments to balance within instead of an attribute: 1 to 10 "
            "segment ids, each listed once, each an active segment. A new user "
            "is balanced in the first listed segment they belong to, in "
            "`other` when they belong to none, and in `unknown` when their "
            "membership of a segment listed before it cannot be decided."
        ),
    )

    @field_validator("segments")
    @classmethod
    def check_segments(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        """R8 on the list: at most 10, each a segment id, none twice."""
        if value is None:
            return value
        if len(value) > MAX_REFERENCED_SEGMENTS:
            raise _refuse(TOO_MANY_SEGMENTS.format(count=len(value)))
        seen = set()
        for segment_id in value:
            if not is_segment_id(segment_id):
                raise _refuse(UNUSABLE_SEGMENT.format(segment_id=segment_id))
            if segment_id in seen:
                raise _refuse(REPEATED_SEGMENT.format(segment_id=segment_id))
            seen.add(segment_id)
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
        """Exactly one of an attribute and segments (R8), then the list,
        judged with its key: R1, R2, R3, R4, R9."""
        if self.segments is not None:
            if self.attribute is not None or self.values is not None:
                raise _refuse(BOTH)
            if not self.segments:
                raise _refuse(NEITHER)
            return self
        if self.attribute is None:
            raise _refuse(NEITHER)
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
