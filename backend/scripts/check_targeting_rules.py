"""
List stored targeting rules that a create or an update now refuses.

Run after upgrading to the release in which flag targeting rules are validated
when saved (#535)::

    python -m backend.scripts.check_targeting_rules

Stored rules are not re-judged: they are evaluated as before, and a flag whose
rules are listed here keeps working. But a ``PUT`` that sends those rules back
unchanged answers 422, so fix them (or leave ``targeting_rules`` out of the
request). The report reads, and never writes, ``feature_flags.targeting_rules``
and prints one tab-separated line per flag it lists::

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

    feature_flag  3f0c...  office-hours  rules[0].rule.conditions[0].value  unreadable time window; see the time_window example on the Targeting Rules: Operator Reference page

Such a stored rule keeps answering as it did: the condition does not hold
(and a stored window that cannot be read abandons the whole ruleset).

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

Connection settings are the ``POSTGRES_*`` environment variables that
``backend.app.db.bootstrap`` reads. Exit status: 0 whether or not anything is
listed (the report is advisory); 2 when the database cannot be read.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, List, Optional, Sequence, Tuple

from backend.app.core.targeting_adapter import (
    TargetingRulesError,
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


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.scripts.check_targeting_rules",
        description=(
            "List stored feature-flag and experiment targeting rules that a "
            "create or an update now refuses, and segments whose rules are "
            "not valid or that a save now refuses."
        ),
    )
    parser.add_argument(
        "--enrolment-changes",
        action="store_true",
        help=(
            "list instead the experiments whose stored rules hold an "
            "array_length or attribute_type condition: the new users they "
            "enrol may change (#1159)"
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
