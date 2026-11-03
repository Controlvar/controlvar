"""
List stored targeting rules that a create or an update now refuses.

Run after upgrading to the release in which flag targeting rules are validated
when saved (#535)::

    python -m backend.scripts.check_targeting_rules

Stored rules are not re-judged: they are evaluated as before, and a flag whose
rules are listed here keeps working, with one exception: native rules holding
a ``time_window`` without ``start`` or ``end`` were ignored and are read from
#1527, so their answer changes (``--time-window-changes``, below, lists them).
But a ``PUT`` that sends listed rules back unchanged answers 422, so fix them
(or leave ``targeting_rules`` out of the request). The report reads, and
never writes, ``feature_flags.targeting_rules`` and prints one tab-separated
line per flag it lists::

    feature_flag  3f0c...  checkout-v2  groups[0].conditions[1].operator  unknown operator

The reason is the text the API answers with. A problem that belongs to the
rules as a whole has the path ``targeting_rules``. Two kinds of line:

* rules the API now refuses, with the API's own path and reason;
* legacy (list-shaped) rules holding a condition whose operator is not ``eq``,
  ``ne``, ``gt``, ``lt``, ``contains`` or ``in`` (or that has none). Such a
  rule matches no user (#733); the line says so, with the path of the first
  such condition. Every other legacy row gets the API's list refusal.

Experiments get the same treatment (``experiments.targeting_rules``, also
read and never written), with the experiment's status after its key
(``DRAFT``, ``ACTIVE``, ...)::

    experiment  7c2d...  checkout-test  ACTIVE  groups[0].conditions[0].operator  unknown operator

Since #1204 a save also refuses a word the evaluator never matches, and the
lines include such rules, with the API's path and reason:

* on a flag, the operators ``json_path`` and ``percentage_bucket``, which flag
  evaluation does not implement (experiments accept both);
* a ``semantic_version`` comparison other than ``eq``, ``gt``, ``gte``, ``lt``
  and ``lte``, and an ``array_length`` one other than those and ``between``;
* a ``geo_distance`` ``comparison`` other than ``lte``, ``lt``, ``gt``,
  ``gte`` and ``eq``, and a distance ``unit`` other than ``km``,
  ``kilometers``, ``kilometre``, ``kilometres``, ``miles``, ``mi``,
  ``meters``, ``metres`` and ``m``, in any case;
* in the native shape, a ``time_window`` the engine cannot read, such as one
  given only as ``start`` and ``end``::

    feature_flag  3f0c...  office-hours  rules[0].rule.conditions[0].value  unreadable time window; give days (0 to 6), start_time and end_time (HH:MM), or start_date and end_date (YYYY-MM-DD); a timezone must be a name such as Europe/Paris

Such a stored rule keeps answering as it did: the condition does not hold
(and a stored window that cannot be read abandons the whole ruleset). The
exception is a native window without ``start`` or ``end``, such as ``{}``:
its rules used to be ignored (``--time-window-changes``).

Segments get the same treatment (#440). Segment rules use the targeting
rule format and are checked when saved; a segment stored before that whose
rules fail the check is left as it is, and is never evaluated:
``POST /api/v1/segments/{id}/evaluate`` answers 409 and bulk-evaluate answers
``false`` for it. Each such segment that is not archived gets a line, with
its name in place of a key and a reason starting ``rules not valid``::

    segment  9a1e...  Enterprise  rules  rules not valid: unknown key

A problem that belongs to a segment's rules as a whole has the path ``rules``.
A segment whose rules pass that check but use a distance unit membership
never matches (#1204) is evaluated, and its condition never holds; it gets a
line with the API's own path and reason, without that prefix::

    segment  9a1e...  Bay Area  groups[0].conditions[0].value  unknown distance unit; use km, mi or m, or leave it out for km

Rewrite the rules with ``PUT /api/v1/segments/{id}``.

``--enrolment-changes`` prints a different report: every experiment whose
stored rules hold a condition experiment targeting now reads as flag targeting
does (#1159), so the users it enrols from now on may differ, in either
direction. That is a native condition with the ``array_length`` operator
(before, an experiment read only an exact length of a list, whatever
``additional_value`` said; a ``default_rule``'s conditions are never read, so
one there changes nothing), or one that carries ``attribute_type`` (before,
a context value that failed the type refused the whole ruleset), at any depth,
inside ``NOT`` too. One line per experiment, whatever its status, with the
path of the first such condition::

    experiment  7c2d...  checkout-test  ACTIVE  rules[0].rule.groups[1].conditions[0].operator  enrolment of new users may change: array_length

Stored rules that assignment cannot read are listed too, with the path
``targeting_rules`` and the reason ``could not be checked``. Users already
assigned keep their variant.

``--time-window-changes`` prints a third report (#1527): every flag and
experiment whose stored native rules hold a ``time_window`` object without
``start`` or ``end``, at any depth, inside ``NOT`` and in ``default_rule``
too, read from the raw JSON. The ``Condition`` schema refused such a window
until #1527, so the whole ruleset was ignored: the flag served its global
rollout, and the experiment made no new user eligible. From that release
the rules are read, so the answer changes. Run it from the new image before
it takes traffic. One line per flag or experiment (an experiment's status
after its key), with the path of the first evaluated window the engine
cannot read, else of the first window without ``start`` or ``end``. Both kinds
print the status after the key; a flag that is ``INACTIVE`` or ``ARCHIVED``
answers off, reason ``inactive``, before and after, so its reason ends
``(inactive: no change while it stays inactive)`` (it is listed all the same,
because switching it on makes the change real)::

    feature_flag  3f0c...  weekdays  ACTIVE  rules[0].rule.conditions[0].value  answer changes on upgrade: the flag's rules were ignored (it served its global rollout) because this time_window has no start or end; they now apply
    feature_flag  8b41...  office  ACTIVE  rules[0].rule.conditions[0].value  answer changes on upgrade: the flag's rules were ignored (it served its global rollout); now this time_window cannot be read, so the flag answers off, reason error, for each user whose evaluation reaches it

An experiment's line starts ``answer may change on upgrade`` when a window
cannot be read, because a new user whose evaluation reaches it is still not
eligible. A window in ``default_rule`` is never evaluated, so it decides only
whether the rules are read. Rules that cannot be read for another reason are
not listed: they are still ignored (the default report lists them). Segments
are not in this report: their rules take the dashboard shape only.

Connection settings are the ``POSTGRES_*`` environment variables that
``backend.app.db.bootstrap`` reads. Exit status: 0 whether or not anything is
listed (the report is advisory); 2 when the database cannot be read.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from backend.app.core.rules_engine import TimeWindowUnreadable, check_time_window
from backend.app.core.targeting_adapter import (
    TargetingRulesError,
    _is_dashboard_rules_shape,
    check_segment_vocabulary,
    validate_experiment_targeting,
    validate_flag_targeting,
    validate_segment_rules,
)
from backend.app.schemas.targeting_rule import (
    OperatorType,
    RuleGroup,
    TargetingRules,
)
from backend.app.services.assignment_service import AssignmentService
from backend.app.services.feature_flag_service import _LEGACY_OPERATORS

ENTITY = "feature_flag"
SEGMENT_ENTITY = "segment"
EXPERIMENT_ENTITY = "experiment"

#: Every ``--enrolment-changes`` reason starts with this.
ENROLMENT_MAY_CHANGE = "enrolment of new users may change"

#: The path printed for a problem that belongs to a segment's rules as a whole.
WHOLE_SEGMENT_RULES_PATH = "rules"

#: Every segment reason starts with this: such a segment is never evaluated.
SEGMENT_RULES_NOT_VALID = "rules not valid"

#: The path printed for a problem that belongs to the rules as a whole.
WHOLE_RULES_PATH = "targeting_rules"

#: The reason printed for a legacy rule that matches no user.
LEGACY_MATCHES_NO_USER = (
    "legacy condition whose operator is not eq, ne, gt, lt, contains or in; "
    "its rule matches no user"
)

#: The reason printed for a stored value the check itself could not read.
NOT_CHECKED = "could not be checked"

#: ``--time-window-changes`` (#1527): the start of every reason but one.
ANSWER_CHANGES = "answer changes on upgrade"

#: ``--time-window-changes``: the start of an experiment's reason when every
#: new user may still be refused, as before.
ANSWER_MAY_CHANGE = "answer may change on upgrade"

#: ``--time-window-changes``: a flag in one of these statuses answers off,
#: reason ``inactive``, before and after (the SDK serves it inactive), so its
#: line says so.
NOT_ACTIVE_FLAG_STATUSES = ("INACTIVE", "ARCHIVED")

#: ``--time-window-changes``: a flag whose windows can all be read.
FLAG_RULES_NOW_APPLY = (
    f"{ANSWER_CHANGES}: the flag's rules were ignored (it served its global "
    "rollout) because this time_window has no start or end; they now apply"
)

#: ``--time-window-changes``: a flag with a window that cannot be read.
FLAG_WINDOW_ANSWERS_ERROR = (
    f"{ANSWER_CHANGES}: the flag's rules were ignored (it served its global "
    "rollout); now this time_window cannot be read, so the flag answers off, "
    "reason error, for each user whose evaluation reaches it"
)

#: ``--time-window-changes``: an experiment whose windows can all be read.
EXPERIMENT_RULES_NOW_APPLY = (
    f"{ANSWER_CHANGES}: the experiment's rules made no new user eligible because "
    "this time_window has no start or end; they now apply"
)

#: ``--time-window-changes``: an experiment with a window that cannot be read.
EXPERIMENT_WINDOW_UNREADABLE = (
    f"{ANSWER_MAY_CHANGE}: the experiment's rules made no new user eligible; they "
    "now apply, and this time_window cannot be read, so each new user whose "
    "evaluation reaches it is still not eligible"
)


@dataclass(frozen=True)
class Finding:
    row_id: str
    key: str
    path: str
    reason: str
    entity: str = ENTITY
    #: An experiment's status, printed after its key; ``None`` for the others.
    status: Optional[str] = None

    def line(self) -> str:
        fields = [self.entity, self.row_id, self.key]
        if self.status is not None:
            fields.append(self.status)
        return "\t".join(fields + [self.path, self.reason])

    @property
    def matches_no_user(self) -> bool:
        return self.reason == LEGACY_MATCHES_NO_USER


def legacy_no_match_path(rules: List[Any]) -> Optional[str]:
    """Path of the first legacy condition that cannot match, or ``None``.

    Mirrors ``FeatureFlagService._evaluate_rule``: only a ``context`` rule
    reads conditions, and a condition matches only through one of
    ``_LEGACY_OPERATORS``.
    """
    for ri, rule in enumerate(rules):
        if not isinstance(rule, dict) or rule.get("type", "") != "context":
            continue
        conditions = rule.get("conditions", [])
        if not isinstance(conditions, list):
            continue
        for ci, condition in enumerate(conditions):
            if not isinstance(condition, dict):
                continue
            if condition.get("operator", "") not in _LEGACY_OPERATORS:
                return f"[{ri}].conditions[{ci}].operator"
    return None


def finding_for(row_id: Any, key: Any, rules: Any) -> Optional[Finding]:
    """The line for one stored flag, or ``None`` when the API accepts its rules."""
    row_id, key = str(row_id), str(key or "")
    if isinstance(rules, list):
        path = legacy_no_match_path(rules)
        if path is not None:
            return Finding(row_id, key, path, LEGACY_MATCHES_NO_USER)
    try:
        validate_flag_targeting(rules)
    except TargetingRulesError as err:
        return Finding(row_id, key, err.path or WHOLE_RULES_PATH, err.code)
    except Exception:
        return Finding(row_id, key, WHOLE_RULES_PATH, NOT_CHECKED)
    return None


def findings_for(rows: Iterable[Tuple[Any, Any, Any]]) -> Iterator[Finding]:
    """Findings for ``(id, key, targeting_rules)`` rows."""
    for row_id, key, rules in rows:
        found = finding_for(row_id, key, rules)
        if found is not None:
            yield found


def _one_line(text: Any) -> str:
    """A segment name is free text: keep the report one line per finding."""
    return " ".join(str(text or "").split())


def segment_finding_for(row_id: Any, name: Any, rules: Any) -> Optional[Finding]:
    """The line for one stored segment, or ``None`` when its rules are valid."""
    row_id, name = str(row_id), _one_line(name)
    try:
        engine_rules = validate_segment_rules(rules)
    except TargetingRulesError as err:
        reason = f"{SEGMENT_RULES_NOT_VALID}: {err.code}"
        path = err.path or WHOLE_SEGMENT_RULES_PATH
        return Finding(row_id, name, path, reason, SEGMENT_ENTITY)
    except Exception:
        reason = f"{SEGMENT_RULES_NOT_VALID}: {NOT_CHECKED}"
        return Finding(row_id, name, WHOLE_SEGMENT_RULES_PATH, reason, SEGMENT_ENTITY)
    try:
        # Evaluated, so not "not valid": a save now refuses it (#1204).
        check_segment_vocabulary(engine_rules)
    except TargetingRulesError as err:
        path = err.path or WHOLE_SEGMENT_RULES_PATH
        return Finding(row_id, name, path, err.code, SEGMENT_ENTITY)
    return None


def segment_findings_for(rows: Iterable[Tuple[Any, Any, Any]]) -> Iterator[Finding]:
    """Findings for ``(id, name, rules)`` segment rows."""
    for row_id, name, rules in rows:
        found = segment_finding_for(row_id, name, rules)
        if found is not None:
            yield found


def _status(value: Any) -> str:
    """The status as stored (``ACTIVE``), whether it is read as text or an enum."""
    return str(getattr(value, "name", value) or "")


def experiment_finding_for(
    row_id: Any, key: Any, status: Any, rules: Any
) -> Optional[Finding]:
    """The line for one stored experiment, or ``None`` when the API accepts its
    rules."""
    row_id, key, status = str(row_id), str(key or ""), _status(status)
    try:
        validate_experiment_targeting(rules)
    except TargetingRulesError as err:
        path = err.path or WHOLE_RULES_PATH
        return Finding(row_id, key, path, err.code, EXPERIMENT_ENTITY, status)
    except Exception:
        return Finding(
            row_id, key, WHOLE_RULES_PATH, NOT_CHECKED, EXPERIMENT_ENTITY, status
        )
    return None


def experiment_findings_for(
    rows: Iterable[Tuple[Any, Any, Any, Any]],
) -> Iterator[Finding]:
    """Findings for ``(id, key, status, targeting_rules)`` experiment rows."""
    for row_id, key, status, rules in rows:
        found = experiment_finding_for(row_id, key, status, rules)
        if found is not None:
            yield found


def conditions_read_differently(rules: TargetingRules) -> List[Tuple[str, str]]:
    """``(path, what)`` for each condition experiment targeting reads differently
    since #1159, in rule order, depth first, ``NOT`` groups included.

    ``array_length`` counts only in ``rules``: a ``default_rule`` is returned
    without its conditions being read. ``attribute_type`` counts in both, as
    the check that read it did. An explicit stack, not recursion, so a deep
    stored ruleset cannot stop the report.
    """
    found: List[Tuple[str, str]] = []
    roots: List[Tuple[str, RuleGroup, bool]] = [
        (f"rules[{index}].rule", rule.rule, True)
        for index, rule in enumerate(rules.rules)
    ]
    if rules.default_rule is not None:
        roots.append(("default_rule.rule", rules.default_rule.rule, False))
    stack = list(reversed(roots))
    while stack:
        path, group, read = stack.pop()
        for index, condition in enumerate(group.conditions):
            at = f"{path}.conditions[{index}]"
            if read and condition.operator == OperatorType.ARRAY_LENGTH:
                found.append((f"{at}.operator", "array_length"))
            if condition.attribute_type is not None:
                found.append((f"{at}.attribute_type", "attribute_type"))
        nested = list(enumerate(group.groups or []))
        for index, child in reversed(nested):
            stack.append((f"{path}.groups[{index}]", child, read))
    return found


def enrolment_change_for(
    row_id: Any, key: Any, status: Any, rules: Any
) -> Optional[Finding]:
    """The ``--enrolment-changes`` line for one stored experiment, or ``None``.

    The rules are read as assignment reads them: an empty value is no rules
    (everyone is eligible), anything else goes through
    ``AssignmentService._coerce_targeting_rules``, and a value that cannot be
    read there is listed, not skipped.
    """
    row_id, key, status = str(row_id), str(key or ""), _status(status)
    if not rules:
        return None
    try:
        rules, _ = AssignmentService._coerce_targeting_rules(
            rules, owner=f"experiment:{row_id}"
        )
        found = [] if rules is None else conditions_read_differently(rules)
    except Exception:
        return Finding(
            row_id, key, WHOLE_RULES_PATH, NOT_CHECKED, EXPERIMENT_ENTITY, status
        )
    if not found:
        return None
    what = ", ".join(sorted({kind for _, kind in found}))
    reason = f"{ENROLMENT_MAY_CHANGE}: {what}"
    return Finding(row_id, key, found[0][0], reason, EXPERIMENT_ENTITY, status)


def enrolment_changes_for(
    rows: Iterable[Tuple[Any, Any, Any, Any]],
) -> Iterator[Finding]:
    """``--enrolment-changes`` findings for ``(id, key, status, rules)`` rows."""
    for row_id, key, status, rules in rows:
        found = enrolment_change_for(row_id, key, status, rules)
        if found is not None:
            yield found


def _stored_native_rules(rules: Any, entity: str) -> Optional[Dict[str, Any]]:
    """The stored value, when the reader of ``entity`` reads it as native rules.

    A flag's reader is ``normalise_targeting_rules``: a dict with ``groups``
    is dashboard-shaped, else one with ``rules`` is native, and text is never
    read. An experiment's is ``AssignmentService._coerce_targeting_rules``:
    text is parsed first, and a dict that is not dashboard-shaped
    (``_is_dashboard_rules_shape``) is native.
    """
    if entity == EXPERIMENT_ENTITY and isinstance(rules, str):
        try:
            rules = json.loads(rules)
        except Exception:
            # Assignment cannot read it either, before or after the upgrade.
            return None
    if not isinstance(rules, dict) or not rules:
        return None
    if entity == ENTITY:
        return rules if "groups" not in rules and "rules" in rules else None
    return None if _is_dashboard_rules_shape(rules) else rules


def native_time_windows(rules: Dict[str, Any]) -> List[Tuple[str, Any, bool]]:
    """``(path, value, evaluated)`` for each ``time_window`` condition in stored
    native rules, read from the raw JSON, not through ``TargetingRules``.

    In rule order, depth first (a group's conditions, then its groups),
    ``NOT`` groups and ``default_rule`` included. ``evaluated`` is false in
    ``default_rule``, whose conditions are never evaluated. An explicit
    stack, not recursion, so a deep stored ruleset cannot stop the report.
    """
    roots: List[Tuple[str, Any, bool]] = []
    stored_rules = rules.get("rules")
    if isinstance(stored_rules, list):
        for index, rule in enumerate(stored_rules):
            if isinstance(rule, dict):
                roots.append((f"rules[{index}].rule", rule.get("rule"), True))
    default_rule = rules.get("default_rule")
    if isinstance(default_rule, dict):
        roots.append(("default_rule.rule", default_rule.get("rule"), False))
    found: List[Tuple[str, Any, bool]] = []
    stack = list(reversed(roots))
    while stack:
        path, group, evaluated = stack.pop()
        if not isinstance(group, dict):
            continue
        conditions = group.get("conditions")
        if isinstance(conditions, list):
            for index, condition in enumerate(conditions):
                if (
                    isinstance(condition, dict)
                    and condition.get("operator") == OperatorType.TIME_WINDOW.value
                ):
                    at = f"{path}.conditions[{index}].value"
                    found.append((at, condition.get("value"), evaluated))
        groups = group.get("groups")
        if isinstance(groups, list):
            for index, child in reversed(list(enumerate(groups))):
                stack.append((f"{path}.groups[{index}]", child, evaluated))
    return found


def _lacks_start_or_end(window: Any) -> bool:
    """A window object the ``Condition`` schema refused until #1527."""
    return isinstance(window, dict) and ("start" not in window or "end" not in window)


def _silent(_operator: Any, _reason: str) -> None:
    """A ``note`` for ``check_time_window`` that logs nothing (a report is not
    an evaluation, and must not use up a stored window's once-only warning)."""


def _readable(window: Any) -> bool:
    """Whether the engine can read ``window`` (its own check)."""
    try:
        check_time_window(OperatorType.TIME_WINDOW, window, note=_silent)
    except TimeWindowUnreadable:
        return False
    return True


def time_window_change_for(
    row_id: Any, key: Any, rules: Any, entity: str = ENTITY, status: Any = None
) -> Optional[Finding]:
    """The ``--time-window-changes`` line for one stored flag or experiment, or
    ``None`` (#1527).

    Listed: native rules holding a ``time_window`` object without ``start`` or
    ``end``, which the ``Condition`` schema refused until #1527, so the reader
    ignored the whole ruleset, and which it now reads (they pass
    ``TargetingRules``). Rules it still cannot read are not listed: they are
    still ignored, so their answer does not change. The path is that of the
    first evaluated window the engine cannot read, if there is one (each user
    whose evaluation reaches it is refused), else that of the first window
    without ``start`` or ``end``.
    """
    row_id, key = str(row_id), str(key or "")
    status = None if status is None else _status(status)
    native = _stored_native_rules(rules, entity)
    if native is None:
        return None
    windows = native_time_windows(native)
    changed = [path for path, window, _ in windows if _lacks_start_or_end(window)]
    if not changed:
        return None
    try:
        TargetingRules.model_validate(native)
    except Exception:
        return None
    unreadable = [
        path
        for path, window, evaluated in windows
        if evaluated and not _readable(window)
    ]
    flag = entity == ENTITY
    if unreadable:
        path = unreadable[0]
        reason = FLAG_WINDOW_ANSWERS_ERROR if flag else EXPERIMENT_WINDOW_UNREADABLE
    else:
        path = changed[0]
        reason = FLAG_RULES_NOW_APPLY if flag else EXPERIMENT_RULES_NOW_APPLY
    if flag and status in NOT_ACTIVE_FLAG_STATUSES:
        # Listed all the same: a flag that is switched on later is concerned,
        # and listing too much is safe where hiding one that matters is not.
        reason += f" ({status.lower()}: no change while it stays {status.lower()})"
    return Finding(row_id, key, path, reason, entity, status)


def time_window_changes_for(
    flag_rows: Iterable[Tuple[Any, Any, Any, Any]],
    experiment_rows: Iterable[Tuple[Any, Any, Any, Any]],
) -> Iterator[Finding]:
    """``--time-window-changes`` findings: flags, then experiments."""
    for row_id, key, status, rules in flag_rows:
        found = time_window_change_for(row_id, key, rules, ENTITY, status)
        if found is not None:
            yield found
    for row_id, key, status, rules in experiment_rows:
        found = time_window_change_for(row_id, key, rules, EXPERIMENT_ENTITY, status)
        if found is not None:
            yield found


def read_experiment_rows(
    connection: Any, schema: str
) -> List[Tuple[Any, Any, Any, Any]]:
    """Every experiment with stored targeting rules, whatever its status."""
    from sqlalchemy import text

    statement = text(
        f'SELECT id, "key", "status", "targeting_rules" FROM "{schema}"."experiments" '
        'WHERE "targeting_rules" IS NOT NULL ORDER BY id'
    )
    return [tuple(row) for row in connection.execute(statement).all()]


def read_segment_rows(connection: Any, schema: str) -> List[Tuple[Any, Any, Any]]:
    """Every segment that is not archived, rules ``NULL`` included."""
    from sqlalchemy import text

    statement = text(
        f'SELECT id, "name", "rules" FROM "{schema}"."segments" '
        "WHERE \"status\" <> 'ARCHIVED' ORDER BY id"
    )
    return [tuple(row) for row in connection.execute(statement).all()]


def read_flag_rows_with_status(
    connection: Any, schema: str
) -> List[Tuple[Any, Any, Any, Any]]:
    """Every flag with stored targeting rules, with its status."""
    from sqlalchemy import text

    statement = text(
        f'SELECT id, "key", "status", "targeting_rules" FROM "{schema}"."feature_flags" '
        'WHERE "targeting_rules" IS NOT NULL ORDER BY id'
    )
    return [tuple(row) for row in connection.execute(statement).all()]


def read_rows(connection: Any, schema: str) -> List[Tuple[Any, Any, Any]]:
    from sqlalchemy import text

    statement = text(
        f'SELECT id, "key", "targeting_rules" FROM "{schema}"."feature_flags" '
        'WHERE "targeting_rules" IS NOT NULL ORDER BY id'
    )
    return [tuple(row) for row in connection.execute(statement).all()]


def scan(connection: Any, schema: str) -> List[Finding]:
    """Every finding in the database behind ``connection`` (read-only)."""
    return (
        list(findings_for(read_rows(connection, schema)))
        + list(experiment_findings_for(read_experiment_rows(connection, schema)))
        + list(segment_findings_for(read_segment_rows(connection, schema)))
    )


def scan_enrolment_changes(connection: Any, schema: str) -> List[Finding]:
    """The ``--enrolment-changes`` findings (read-only)."""
    return list(enrolment_changes_for(read_experiment_rows(connection, schema)))


def scan_time_window_changes(connection: Any, schema: str) -> List[Finding]:
    """The ``--time-window-changes`` findings (read-only)."""
    return list(
        time_window_changes_for(
            read_flag_rows_with_status(connection, schema),
            read_experiment_rows(connection, schema),
        )
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.scripts.check_targeting_rules",
        description=(
            "List stored feature-flag and experiment targeting rules that a "
            "create or an update now refuses, and segments whose rules are "
            "not valid or that a save now refuses."
        ),
    )
    report = parser.add_mutually_exclusive_group()
    report.add_argument(
        "--enrolment-changes",
        action="store_true",
        help=(
            "list instead the experiments whose stored rules hold an "
            "array_length or attribute_type condition: the new users they "
            "enrol may change (#1159)"
        ),
    )
    report.add_argument(
        "--time-window-changes",
        action="store_true",
        help=(
            "list instead the flags and experiments whose stored native rules "
            "hold a time_window without start or end: they were ignored, and "
            "from this release they are read, so their answer changes (#1527)"
        ),
    )
    args = parser.parse_args(argv)

    from sqlalchemy import create_engine

    from backend.app.db.bootstrap import database_url, schema_name

    schema = schema_name()
    try:
        engine = create_engine(database_url())
    except Exception as exc:
        print(
            "check_targeting_rules: could not read the database "
            f"({type(exc).__name__}).",
            file=sys.stderr,
        )
        return 2
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            if args.enrolment_changes:
                found = scan_enrolment_changes(connection, schema)
            elif args.time_window_changes:
                found = scan_time_window_changes(connection, schema)
            else:
                found = scan(connection, schema)
            connection.rollback()
    except Exception as exc:
        print(
            "check_targeting_rules: could not read the database "
            f"({type(exc).__name__}).",
            file=sys.stderr,
        )
        return 2
    finally:
        engine.dispose()

    for finding in found:
        print(finding.line())
    if args.enrolment_changes:
        print(
            f"{len(found)} experiment(s) listed whose enrolment of new users may "
            f"change (schema {schema!r}).",
            file=sys.stderr,
        )
        return 0
    if args.time_window_changes:
        changed_flags = sum(1 for f in found if f.entity == ENTITY)
        print(
            f"{changed_flags} flag(s) and {len(found) - changed_flags} "
            "experiment(s) listed whose stored rules hold a time_window without "
            f"start or end, read from this release (schema {schema!r}).",
            file=sys.stderr,
        )
        return 0
    flags = [f for f in found if f.entity == ENTITY]
    experiments = sum(1 for f in found if f.entity == EXPERIMENT_ENTITY)
    segments = [f for f in found if f.entity == SEGMENT_ENTITY]
    not_valid = sum(1 for f in segments if f.reason.startswith(SEGMENT_RULES_NOT_VALID))
    no_match = sum(1 for f in flags if f.matches_no_user)
    print(
        f"{len(flags)} flag(s) listed: {len(flags) - no_match} with rules the API "
        f"now refuses, {no_match} with a legacy rule that matches no user; "
        f"{experiments} experiment(s) listed with rules the API now refuses; "
        f"{len(segments)} segment(s) listed: {not_valid} with rules not valid, "
        f"{len(segments) - not_valid} with rules the API now refuses "
        f"(schema {schema!r}).",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
