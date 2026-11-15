"""A number field that refuses a value that is not finite (#1742).

A ``float`` field reads the strings ``"NaN"``, ``"Infinity"``, ``"-Infinity"``
and ``"1e400"`` (a number too large for a double) as numbers that are not
finite, and accepts them. The JSON check (#1630,
``backend/app/core/finite_json.py``) does not see them: in the JSON they are
strings. ``FiniteFloat`` refuses all four with pydantic's own 422,
``finite_number`` at the field ("Input should be a finite number"), the same
type and text as the JSON check's.

Constrain the item, never the container: ``List[FiniteFloat]``,
``Dict[str, FiniteFloat]``, ``Optional[FiniteFloat]``.
``Field(allow_inf_nan=False)`` put on a ``List[float]`` or ``Dict[str, float]``
field raises ``TypeError`` on every valid value, which a request answers 500.
Bounds stay on the field (``rate: FiniteFloat = Field(..., ge=0, le=1)``).
A float query parameter takes ``Query(..., allow_inf_nan=False)``.

The OpenAPI document does not change: JSON Schema has no way to say "finite".
``backend/tests/unit/schemas/test_typed_number_fields_finite.py`` finds every
typed number field of every request from the route table and holds each to it.
"""

from typing import Annotated

from pydantic import Field

#: A ``float`` that refuses NaN, the infinities and a number too large for a
#: double, given as a number or as a string.
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
