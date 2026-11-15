"""
The list routes' enum filters: any case accepted, an unknown value refused (#955).

A list route that filters on a stored enum takes the value as a plain string
(``?status=active``). Four routes passed it on unchecked, so a value outside the
enum, or one in a case the enum does not spell, reached the database or an
enum conversion and answered 500. Each casing rule also differed per route.

:func:`enum_filter` is the one rule, called by each route after its permission
check, so a caller who may not list learns nothing about the enum:

- an absent or empty value means no filter (``None``);
- otherwise the value matches a member when it equals the member's value with
  case ignored (``str.lower`` on both sides), and the member is returned. The
  route passes the member down, never the request's text;
- anything else is a :class:`RequestValidationError` at ``["query", <param>]``,
  type ``enum``, whose message lists the allowed values as the route's
  responses spell them. The message never repeats the value sent.
"""

from enum import Enum
from typing import Optional, Sequence, Type, TypeVar

from fastapi.exceptions import RequestValidationError

E = TypeVar("E", bound=Enum)

#: The 422's message: the parameter, then the allowed values.
ENUM_FILTER_MESSAGE = "{param} must be one of: {values} (any case)"


def enum_filter_message(param: str, spellings: Sequence[str]) -> str:
    """The fixed message for *param*: its allowed values, never the input."""
    return ENUM_FILTER_MESSAGE.format(param=param, values=", ".join(spellings))


def enum_filter(
    value: Optional[str],
    enum_cls: Type[E],
    param: str,
    spellings: Sequence[str],
) -> Optional[E]:
    """The member of *enum_cls* that *value* names, in any case; ``None`` for no filter.

    *spellings* are the allowed values as the route's responses write them,
    used only for the message. Raises the 422 described in the module docstring
    for a value no member matches.
    """
    if not value:
        return None
    folded = value.lower()
    for member in enum_cls:
        if str(member.value).lower() == folded:
            return member
    raise RequestValidationError(
        [
            {
                "type": "enum",
                "loc": ("query", param),
                "msg": enum_filter_message(param, spellings),
            }
        ]
    )
