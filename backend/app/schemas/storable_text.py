"""Refuse request text the database cannot store (#543, #402).

PostgreSQL refuses U+0000 in ``text``, ``varchar`` and ``jsonb``, and a lone
UTF-16 surrogate (U+D800-U+DFFF) cannot be encoded as UTF-8 at all. JSON can
carry both (``"\\u0000"``, ``"\\ud800"``), so a request schema accepts them
unless something refuses them, and the database driver then fails the
request with a 500.

:class:`StorableTextModel` refuses them in every field of a request schema --
a string, or any string inside a dict or list, keys included -- with a
validation error, i.e. 422. The message names the field and never the value.
:func:`storable_text_param` does the same for a path or query parameter, and
:func:`storable_text_fields` for named fields of a route's body.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ValidationError,
    ValidationInfo,
    field_validator,
)
from pydantic_core import InitErrorDetails
from pydantic_core.core_schema import ValidatorFunctionWrapHandler

#: One character class, so the search is linear and cannot backtrack.
_UNSTORABLE = re.compile("[\x00\ud800-\udfff]")


def contains_unstorable_text(value: Any) -> bool:
    """True if *value*, or any string nested in it, holds NUL or a lone surrogate.

    Walks dicts (keys and values), lists and tuples without recursion, so a
    deeply nested body cannot exhaust the stack.
    """
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if _UNSTORABLE.search(item):
                return True
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return False


def unstorable_text_message(field: str) -> str:
    """The fixed validation message for *field*; it never includes the value."""
    return f"{field} must not contain NUL or unpaired surrogate characters"


def storable_text_param(name: str) -> AfterValidator:
    """A validator refusing NUL and lone surrogates in the parameter *name* (#547).

    For a path or query parameter whose value reaches a database query::

        user_id: Annotated[str, Path(description=...), storable_text_param("user_id")]

    It runs after the parameter's own validation and adds nothing to the
    parameter's JSON schema, so the OpenAPI document is unchanged. A refused
    value is a validation error, i.e. 422, with the message of
    :func:`unstorable_text_message`, which never includes the value.
    """

    def _refuse_unstorable(value: Any) -> Any:
        if contains_unstorable_text(value):
            raise ValueError(unstorable_text_message(name))
        return value

    return AfterValidator(_refuse_unstorable)


def storable_text_fields(*fields: str) -> BeforeValidator:
    """A validator refusing NUL and lone surrogates in the named *fields* of a
    route's body (#2175).

    For a body model that another route shares and must accept unchanged, so
    the check cannot sit on the model itself; it goes on the parameter of the
    routes that store the text::

        data: Annotated[SegmentCreate, storable_text_fields("name", "rules")]

    It runs before the model reads the body, so no check of the model sees
    the text, and it adds nothing to the JSON schema. Each refused field is
    its own error, at ``["body", <field>]``, the one a field validator raising
    ``ValueError(unstorable_text_message(field))`` gives; the value is never
    included. A body that is not an object is left to the model.
    """

    def _refuse_unstorable(value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        refused = [
            field for field in fields if contains_unstorable_text(value.get(field))
        ]
        if refused:
            raise ValidationError.from_exception_data(
                "body",
                [
                    InitErrorDetails(
                        type="value_error",
                        loc=(field,),
                        input=None,
                        ctx={"error": ValueError(unstorable_text_message(field))},
                    )
                    for field in refused
                ],
            )
        return value

    return BeforeValidator(_refuse_unstorable)


class StorableTextModel(BaseModel):
    """Base for request schemas whose text reaches the database.

    Every field is checked twice: the input as received, so the refusal comes
    before any type error about the same value, and the validated value, so
    text produced by a field's own parsing (a JSON-encoded string, say) is
    checked too.
    """

    @field_validator("*", mode="wrap")
    @classmethod
    def _refuse_unstorable_text(
        cls, value: Any, handler: ValidatorFunctionWrapHandler, info: ValidationInfo
    ) -> Any:
        if contains_unstorable_text(value):
            raise ValueError(unstorable_text_message(info.field_name))
        result = handler(value)
        if contains_unstorable_text(result):
            raise ValueError(unstorable_text_message(info.field_name))
        return result
