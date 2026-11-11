"""A date the database can store: the UTC offset rule of the date parameters (#956).

PostgreSQL stores a UTC offset of at most 15:59:59 either way and fails a query
that compares with a larger one ("time zone displacement out of range"), which
answered 500. The parser accepts offsets up to 23:59, so a date such as
``2026-01-01T00:00:00+23:00`` reached the query.

Two forms of the one rule, both answering 422 at ``["query" or "body", <field>]`` with a
fixed message that never repeats the value sent:

- :func:`storable_offset`, the ``AfterValidator`` inside a parameter's
  ``Annotated``, with the ``Query`` beside it and ``None`` as the plain default
  (given as ``= Query(None, ...)`` instead, the validator is not run at all).
  It runs while the request is parsed, i.e. before the route's own body.
- :func:`refuse_unstorable_offset`, called by a route after its permission
  check, as :mod:`backend.app.api.enum_filters` is, so a caller who may not use
  the route still answers 403 and learns nothing from a 422.

A naive date, ``Z`` and offsets up to 15:59 either way pass unchanged.
"""

from datetime import datetime, timedelta
from typing import Optional

from fastapi.exceptions import RequestValidationError

#: The first offset the database cannot store, either way.
OFFSET_LIMIT = timedelta(hours=16)

#: The fixed message. Pydantic prefixes a validator's message with
#: "Value error, "; :func:`refuse_unstorable_offset` writes the same text.
OFFSET_TOO_LARGE = "the UTC offset must be less than 16 hours"


def storable_offset(value: Optional[datetime]) -> Optional[datetime]:
    """Refuse a date whose UTC offset is 16 hours or more either way.

    A refusal is a validation error, i.e. 422 at ``["query" or "body", <field>]``. A
    naive date, ``Z`` and offsets up to 15:59 either way pass unchanged.
    """
    if value is not None:
        offset = value.utcoffset()
        if offset is not None and abs(offset) >= OFFSET_LIMIT:
            raise ValueError(OFFSET_TOO_LARGE)
    return value


def refuse_unstorable_offset(param: str, value: Optional[datetime]) -> None:
    """Raise the 422 of :func:`storable_offset` for query parameter *param*.

    For a route that checks permission first; the error is the one the
    ``AfterValidator`` form raises, field for field, with nothing from the
    request.
    """
    try:
        storable_offset(value)
    except ValueError:
        raise RequestValidationError(
            [
                {
                    "type": "value_error",
                    "loc": ("query", param),
                    "msg": f"Value error, {OFFSET_TOO_LARGE}",
                    # What pydantic adds to a validator's refusal, so that the
                    # two forms answer with the same bytes.
                    "ctx": {"error": {}},
                }
            ]
        ) from None
