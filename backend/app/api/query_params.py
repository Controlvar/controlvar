"""Rules shared by the routes' query parameters.

The paging bounds (#1001). PostgreSQL takes an ``OFFSET`` of at most
9223372036854775807, the largest ``bigint`` (2**63 - 1), and fails a query
given a larger one ("bigint out of range"), which answered 500. So every
``skip`` and ``offset`` is ``Query(0, ge=0, le=MAX_OFFSET, ...)``, and every
``page`` is ``Query(1, ge=1, le=MAX_PAGE, ...)``: a value past the bound answers
422 at ``["query", <param>]`` with pydantic's own ``less_than_equal`` item,
before the route runs. The bound is the database's own limit, so no request
the database could answer is refused.

A page route reads ``(page - 1) * <size>`` rows past the start, where
``<size>`` is its own size parameter (``limit`` or ``page_size``). Each of the
two can be in range while the product is not, so the route also calls
:func:`check_page_offset` with both. It raises a request validation error, so
it must be called where nothing turns an exception into a 500: outside any
``try`` whose ``except Exception`` would catch it.

The OpenAPI document gives such a bound as ``"format": "int64"``, not as a
``maximum``: a release rewrites the snapshots through JavaScript, which prints
an integer above 2**53 as a different number
(``backend/tests/smoke/test_openapi_release_rewrite.py``; the warehouse
filter's ``IntLiteral`` is described the same way). So each of these
parameters also passes ``json_schema_extra=describe_as_int64``, which leaves
the rest of its schema (``minimum``, ``default``, ``description``) as it was.
"""

from typing import Any, Dict

from fastapi.exceptions import RequestValidationError

#: The largest OFFSET PostgreSQL takes: 2**63 - 1, the largest bigint (#1001).
MAX_OFFSET = 9223372036854775807

#: The largest ``page``: with a size of 1, page ``MAX_PAGE`` starts at
#: ``MAX_OFFSET``. It is one past int64's largest value, which the document's
#: ``int64`` for ``page`` leaves out.
MAX_PAGE = MAX_OFFSET + 1


def describe_as_int64(schema: Dict[str, Any]) -> None:
    """The ``json_schema_extra`` of a parameter bounded at :data:`MAX_OFFSET`
    or :data:`MAX_PAGE`: ``"format": "int64"`` in place of the ``maximum``.

    Only the document changes; the ``le`` still refuses a larger value.
    FastAPI types ``json_schema_extra`` as a dict and hands it to pydantic,
    which also takes a callable (as ``backend/app/schemas/user.py`` does).
    """
    schema.pop("maximum", None)
    schema["format"] = "int64"


def page_offset_too_large(size_param: str) -> str:
    """The refusal's fixed message; it names the size parameter, never a value
    sent."""
    return f"(page - 1) * {size_param} must be at most {MAX_OFFSET}"


def check_page_offset(page: int, size: int, size_param: str) -> None:
    """Refuse a page that starts past the largest offset the database takes.

    *size* is the route's own size parameter's value and *size_param* its
    name. Raises a 422 at ``["query", "page"]``, ``"type": "value_error"``,
    when ``(page - 1) * size`` is more than :data:`MAX_OFFSET`; anything else
    passes. Call it outside any ``try`` whose ``except Exception`` would turn
    the 422 into a 500.
    """
    if (page - 1) * size > MAX_OFFSET:
        raise RequestValidationError(
            [
                {
                    "type": "value_error",
                    "loc": ("query", "page"),
                    "msg": page_offset_too_large(size_param),
                }
            ]
        )
