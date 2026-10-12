"""
Pattern conditions (the ``regex`` / ``match_regex`` operator), evaluated with RE2.

Every stored or request-supplied pattern is compiled and searched here and
nowhere else; ``backend/tests/unit/core/test_regex_call_sites.py`` fails on a
call into :mod:`re` whose pattern is not a literal or a module constant.

Syntax and semantics are RE2's (https://github.com/google/re2/wiki/Syntax),
which differ from Python's :mod:`re` in ways a stored pattern can notice:

* ``\\w``, ``\\d``, ``\\s`` and ``\\b`` are ASCII-only (use ``\\p{L}``,
  ``\\p{N}`` or ``[[:alpha:]]`` for other scripts);
* ``$`` matches only at the very end of the value, not before a final newline;
* lookaround, backreferences, ``\\Z``, ``(?x)``, ``(?u)``, ``(?a)`` and
  ``\\N{...}`` are refused, as is a counted repetition above 1000;
* a pattern that needs more than :data:`PATTERN_MEMORY_BYTES` to compile is
  refused (``pattern too large``), which includes very large Unicode
  repetitions such as ``[\\p{L}\\p{N}]{1,300}``.

A condition whose pattern is refused, or whose value is longer than
:data:`MAX_REGEX_INPUT` characters or cannot be encoded as UTF-8, is
*unevaluable*. :func:`search` then raises :class:`PatternUnevaluable`, and
the evaluator that owns the ruleset abandons the whole ruleset for that
evaluation (a flag is disabled with reason ``error``, a user is ineligible for
an experiment, a user is not a segment member). It is never reduced to
``False`` inside a group, where an enclosing ``NOT`` would turn it into a
match.

The rules engine raises the same :class:`PatternUnevaluable`, with reason
``out_of_range``, when a comparison's value is out of range for it: an
integer too large for a float, or a timestamp no date can hold. The owner of
the ruleset abandons it in the same way.
"""

from __future__ import annotations

import functools
import hashlib
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterator, Optional, Tuple, Union

import re2

from backend.app.core.log_once import OnceLog

logger = logging.getLogger(__name__)

#: Values longer than this many characters are not evaluated: the condition is
#: unevaluable and the ruleset is abandoned (see the module docstring).
MAX_REGEX_INPUT = 256

#: RE2's per-pattern memory budget. A pattern that needs more is refused.
PATTERN_MEMORY_BYTES = 2 << 20

#: Compiled patterns (and refusals) kept in memory, least recently used first
#: out. google-re2 keeps its own functools.lru_cache (128 entries) inside
#: re2.compile, and the two caches can hold different patterns, so up to
#: 2 * MAX_COMPILED_PATTERNS compiled objects can be alive at once: a
#: theoretical resident ceiling of 256 * PATTERN_MEMORY_BYTES (512 MiB).
MAX_COMPILED_PATTERNS = 128

#: The operator names a stored condition uses for a pattern, in any shape:
#: ``regex`` (dashboard editor) and ``match_regex`` (native rules, segments).
PATTERN_OPERATORS = frozenset({"regex", "match_regex"})


def pattern_options() -> "re2.Options":
    """The RE2 options every pattern is compiled with (also used by the upgrade check)."""
    options = re2.Options()
    options.max_mem = PATTERN_MEMORY_BYTES
    options.log_errors = False
    return options


_OPTIONS = pattern_options()


class PatternResult(Enum):
    """Outcome of one pattern condition."""

    MATCH = "match"
    NO_MATCH = "no_match"
    UNEVALUABLE = "unevaluable"


class UnevaluableReason(str, Enum):
    """Why a condition could not be evaluated (never the pattern or value)."""

    REFUSED = "refused"
    INPUT_LENGTH = "input_length"
    INPUT_ENCODING = "input_encoding"
    NOT_A_STRING = "not_a_string"
    ERROR = "error"
    #: A comparison's value is out of range for it (rules engine; the digest
    #: is the operator's, see :func:`report_unevaluable`).
    OUT_OF_RANGE = "out_of_range"


_UNEVALUABLE_MESSAGE = "pattern condition could not be evaluated"


class PatternUnevaluable(Exception):
    """
    A pattern condition could not be evaluated; the enclosing ruleset is abandoned.

    Raised from the rules engine's ``match_regex`` branch and caught only where
    a whole ruleset is decided (the flag service, the experiment targeting
    evaluator and the segment membership paths). Its ``str()`` and ``repr()``
    carry neither the pattern nor the value: the generic handlers that may see
    it log ``str(exc)``. ``digest`` is a short hash of the pattern, for
    correlating log lines with the upgrade check's output.

    The rules engine also raises it, with reason
    :attr:`UnevaluableReason.OUT_OF_RANGE`, when a comparison's value is out
    of range for it; ``digest`` is then the hash of the operator's name.
    """

    def __init__(self, reason: UnevaluableReason, digest: str) -> None:
        super().__init__(_UNEVALUABLE_MESSAGE)
        self.reason = reason
        self.digest = digest

    def __str__(self) -> str:
        return _UNEVALUABLE_MESSAGE

    def __repr__(self) -> str:
        return f"PatternUnevaluable(reason={self.reason.value!r})"


@dataclass(frozen=True)
class _Refusal:
    """Cached in place of a compiled pattern that RE2 refused."""

    message: str


def pattern_digest(pattern: Any) -> str:
    """Twelve hex characters identifying ``pattern`` in logs, without its text."""
    text = pattern if isinstance(pattern, str) else repr(pattern)
    data = text.encode("utf-8", "surrogatepass")
    return hashlib.sha256(data).hexdigest()[:12]


def _error_text(exc: BaseException) -> str:
    """RE2's own message for a refused pattern (``invalid perl operator: (?<``)."""
    arg = exc.args[0] if exc.args else exc
    if isinstance(arg, bytes):
        return arg.decode("utf-8", "replace")
    return str(arg)


def _compile_uncached(pattern: str) -> Union["re2._Regexp", _Refusal]:
    """Compile ``pattern`` with :func:`pattern_options`; a refusal is returned, not raised."""
    try:
        return re2.compile(pattern, _OPTIONS)
    except re2.error as exc:
        return _Refusal(_error_text(exc))
    except UnicodeError:
        return _Refusal("pattern is not valid UTF-8")
    except Exception:
        return _Refusal("pattern could not be compiled")


@functools.lru_cache(maxsize=MAX_COMPILED_PATTERNS)
def _compiled(pattern: str) -> Union["re2._Regexp", _Refusal]:
    return _compile_uncached(pattern)


def clear_compiled_patterns() -> None:
    """Forget every compiled pattern and refusal (tests)."""
    _compiled.cache_clear()


def compile_error(pattern: Any) -> Optional[str]:
    """
    Why RE2 refuses ``pattern`` under :func:`pattern_options`, or ``None``.

    The message is RE2's own text, e.g. ``invalid perl operator: (?<`` or
    ``pattern too large - compile failed``.
    """
    if not isinstance(pattern, str):
        return "pattern is not a string"
    compiled = _compiled(pattern)
    if isinstance(compiled, _Refusal):
        return compiled.message
    return None


def _classify(
    pattern: Any, value: Any
) -> Tuple[PatternResult, Optional[UnevaluableReason]]:
    if not isinstance(pattern, str):
        return PatternResult.UNEVALUABLE, UnevaluableReason.NOT_A_STRING
    if not isinstance(value, str):
        value = str(value)
    if len(value) > MAX_REGEX_INPUT:
        return PatternResult.UNEVALUABLE, UnevaluableReason.INPUT_LENGTH
    compiled = _compiled(pattern)
    if isinstance(compiled, _Refusal):
        return PatternResult.UNEVALUABLE, UnevaluableReason.REFUSED
    try:
        found = compiled.search(value)
    except Exception as exc:
        # One handler for everything the search can raise: a lone surrogate
        # raises UnicodeEncodeError, which is not an re2.error.
        if isinstance(exc, UnicodeError):
            return PatternResult.UNEVALUABLE, UnevaluableReason.INPUT_ENCODING
        return PatternResult.UNEVALUABLE, UnevaluableReason.ERROR
    return (PatternResult.MATCH if found else PatternResult.NO_MATCH), None


def evaluate(pattern: Any, value: Any) -> PatternResult:
    """
    Search ``value`` (converted with ``str()`` when it is not a string) for ``pattern``.

    Returns :attr:`PatternResult.UNEVALUABLE` when the pattern is refused or
    not a string, or the value is longer than :data:`MAX_REGEX_INPUT`
    characters or cannot be encoded.
    """
    return _classify(pattern, value)[0]


def search(pattern: Any, value: Any) -> bool:
    """
    :func:`evaluate` for the rules engine: ``True``/``False``, or raise.

    Raises:
        PatternUnevaluable: the condition cannot be evaluated; the caller that
            owns the ruleset abandons it.
    """
    result, reason = _classify(pattern, value)
    if result is PatternResult.UNEVALUABLE:
        raise PatternUnevaluable(
            reason or UnevaluableReason.ERROR, pattern_digest(pattern)
        )
    return result is PatternResult.MATCH


# ---------------------------------------------------------------------------
# Logging an abandoned ruleset: once per (pattern, place), never the text.
# ---------------------------------------------------------------------------

_REPORTED_LIMIT = 4096
_once = OnceLog(limit=_REPORTED_LIMIT)
#: The keys already reported (the same set object as ``_once.seen``).
_reported = _once.seen


def _operator_named_by(digest: str) -> str:
    """The rules-engine operator whose name hashes to ``digest`` (``OUT_OF_RANGE``)."""
    from backend.app.schemas.targeting_rule import OperatorType

    for operator in OperatorType:
        if pattern_digest(operator.value) == digest:
            return operator.value
    return "unknown"


def report_unevaluable(exc: PatternUnevaluable, where: str) -> None:
    """
    Log a warning the first time a pattern is unevaluable at ``where``.

    ``where`` names the ruleset owner (``flag:<key>``, ``experiment:<id>``,
    ``segment:<id>``). The pattern and the value are never logged; the digest
    matches the upgrade check's output.

    For :attr:`UnevaluableReason.OUT_OF_RANGE` the warning names the owner and
    the operator, once per pair, and never the value.
    """
    if not _once.first(where, (exc.digest, exc.reason.value)):
        return
    if exc.reason is UnevaluableReason.OUT_OF_RANGE:
        logger.warning(
            "Targeting rules for %s were not applied: a value was out of range "
            "for the %s operator (reason=%s).",
            where,
            _operator_named_by(exc.digest),
            exc.reason.value,
        )
        return
    logger.warning(
        "Targeting rules for %s were not applied: a pattern condition could not "
        "be evaluated (reason=%s, pattern=%s). Run "
        "`python -m backend.scripts.check_regex_rules` to list stored patterns.",
        where,
        exc.reason.value,
        exc.digest,
    )


# ---------------------------------------------------------------------------
# Finding patterns in stored rules, whatever their shape.
# ---------------------------------------------------------------------------


def iter_rule_patterns(rules: Any, path: str = "") -> Iterator[Tuple[str, Any]]:
    """
    Yield ``(field_path, pattern)`` for every pattern condition in ``rules``.

    Shape-agnostic: it walks any nesting of dicts and lists (the dashboard
    editor shape, native ``TargetingRules`` including ``default_rule``,
    segment rule groups, the legacy list shape) and yields the ``value`` of
    every object whose ``operator`` is ``regex`` or ``match_regex``, in any
    case. Paths look like ``groups[0].conditions[1].value``. The pattern is
    yielded as stored, which may not be a string.
    """
    if isinstance(rules, dict):
        operator = rules.get("operator")
        if (
            isinstance(operator, str)
            and operator.strip().lower() in PATTERN_OPERATORS
            and "value" in rules
        ):
            yield (f"{path}.value" if path else "value"), rules["value"]
        for key, child in rules.items():
            if isinstance(child, (dict, list)):
                child_path = f"{path}.{key}" if path else str(key)
                yield from iter_rule_patterns(child, child_path)
    elif isinstance(rules, list):
        for index, child in enumerate(rules):
            if isinstance(child, (dict, list)):
                yield from iter_rule_patterns(child, f"{path}[{index}]")
