"""The one path between ``on_exposure`` and the client (#1499). Not part of the public API."""

from __future__ import annotations

import inspect
from typing import Callable

from .types import ExperimentationError, Exposure

__all__ = ["HOOK_ERROR", "call_exposure_hook", "exposure_error"]

#: ``ExperimentationError.code`` of the error ``on_error`` receives with the operation ``"exposure"``.
HOOK_ERROR = "HOOK_ERROR"


def call_exposure_hook(
    hook: Callable[[Exposure], object],
    exposure: Exposure,
    report: Callable[[Exception], None],
) -> None:
    """Call ``hook`` with ``exposure`` and pass anything it raises to ``report``. Nothing reaches
    the caller: a hook's failure must never change or delay an answer.

    ``Exception`` only: ``KeyboardInterrupt`` and ``SystemExit`` still propagate. A returned
    coroutine (from an object whose ``__call__`` is ``async def``, which the constructor cannot
    see) is closed, so it neither runs nor warns that it was never awaited, and is reported. Any
    other return value, another awaitable included (a Task the hook scheduled on its own loop
    runs there), is left alone and not reported. Anything ``report`` itself raises is dropped.
    """
    try:
        result = hook(exposure)
        if inspect.iscoroutine(result):
            result.close()
            raise TypeError(
                "it returned a coroutine, which the client closed without running: on_exposure must not be async"
            )
    except Exception as thrown:  # noqa: BLE001 - nothing a hook raises may reach the caller
        try:
            report(thrown)
        except Exception:  # noqa: BLE001 - a failing report must not reach the caller either
            pass


def exposure_error(thrown: BaseException) -> ExperimentationError:
    """The error ``on_error`` receives, with the operation ``"exposure"``, for what a hook raised."""
    try:
        detail = str(thrown) or type(thrown).__name__
    except Exception:  # noqa: BLE001 - a __str__ that raises
        detail = type(thrown).__name__
    error = ExperimentationError(f"on_exposure failed: {detail}", code=HOOK_ERROR)
    error.__cause__ = thrown
    return error
