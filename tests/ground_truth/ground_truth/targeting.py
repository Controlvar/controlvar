"""Who a targeted flag serves, written from the docs without asking the server
(O-TARGET).

Two flags, each active at a global rollout of 0:

* ``DOCUMENTED_RULES``, the example under "Add targeting rules" in
  ``docs/feature-flags/create.md``, exactly as the page sends it: enterprise-plan
  users in the US, CA or GB, or any employee;
* ``SEMVER_RULES``, one dashboard condition, ``app_version semver_gte 3.2.0``.

A rule a user matches serves 100% of its users and the global rollout serves
none, so the user id never decides: a user the rules match gets
``(True, "targeting_rule")``, anyone else ``(False, "rollout")``.

Each row is the docs' answer, not the server's. It carries the sentence that
gives it, verbatim, and the file the sentence is in.
``backend/tests/unit/docs/test_targeting_example_docs.py`` fails when a sentence
leaves its page or the example changes, and
``backend/tests/unit/ground_truth/test_targeting_use_cases.py`` asks the
server's evaluator every row. A row the server disagrees with is a finding to
settle against the docs: a row is never changed to match the server unless the
docs say the same.

``local`` is the leg an SDK evaluating locally takes for the row: ``"local"``
when it answers from the ruleset, ``"server"`` when it asks the server. Every
SEM row is ``"server"`` (semantic version is not a local operator), and so is
d10 (a boolean attribute against a string rule value). ``LEG_BASIS`` quotes the
docs for both, and for the local DOC rows.

Standard library only, literal data, no evaluator. Importing the module runs
:func:`check`, so a malformed table cannot be imported.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

#: The two reasons a row may give. ``inactive`` and ``error`` are not
#: targeting answers.
REASONS = ("targeting_rule", "rollout")

#: Both flags' global rollout percentage.
ROLLOUT_PERCENTAGE = 0

#: ``docs/feature-flags/create.md``, "Add targeting rules", verbatim.
DOCUMENTED_RULES: dict[str, Any] = {
    "logical_operator": "OR",
    "groups": [
        {
            "logical_operator": "AND",
            "conditions": [
                {"attribute": "user.plan", "operator": "equals", "value": "enterprise"},
                {
                    "attribute": "user.country",
                    "operator": "in",
                    "value": ["US", "CA", "GB"],
                },
            ],
        },
        {
            "logical_operator": "AND",
            "conditions": [
                {"attribute": "employee", "operator": "equals", "value": "true"}
            ],
        },
    ],
}

#: The dashboard shape, one group of one condition.
SEMVER_RULES: dict[str, Any] = {
    "logical_operator": "AND",
    "groups": [
        {
            "logical_operator": "AND",
            "conditions": [
                {"attribute": "app_version", "operator": "semver_gte", "value": "3.2.0"}
            ],
        }
    ],
}

CREATE = "docs/feature-flags/create.md"
RULES_REFERENCE = "docs/Enhanced_Rules_Engine_Reference.md"
LOCAL_EVALUATION = "docs/sdk/local-evaluation.md"
CONCEPTS = "docs/getting-started/concepts.md"


@dataclass(frozen=True)
class Row:
    """One user: the context sent, the answer the docs give, and why."""

    id: str
    context: dict[str, Any] | None
    enabled: bool
    reason: str
    local: str
    basis_file: str
    basis_quote: str
    #: ``"docs"``: ``basis_quote`` is a sentence on ``basis_file``.
    #: ``"behaviour"``: no docs sentence states the case; ``basis_file`` is a
    #: server test file and ``basis_quote`` the name of the test in it that
    #: pins the behaviour.
    basis_kind: str = "docs"


#: Rows for ``DOCUMENTED_RULES``.
DOC_ROWS: tuple[Row, ...] = (
    Row(
        "d1",
        {"plan": "enterprise", "country": "US"},
        True,
        "targeting_rule",
        "local",
        CREATE,
        "So a dashboard rule on `user.country` matches an SDK that sends "
        '`{"country": "US"}` without any renaming on either side.',
    ),
    Row(
        "d2",
        {"plan": "enterprise", "country": "GB"},
        True,
        "targeting_rule",
        "local",
        RULES_REFERENCE,
        "the attribute equals one of the values, compared as `equals` compares.",
    ),
    Row(
        "d3",
        {"plan": "enterprise", "country": "DE"},
        False,
        "rollout",
        "local",
        CREATE,
        "Users who match a rule are bucketed with the rule's `rollout_percentage` "
        "(100 unless set on the rules object); users who match no rule fall "
        "through to the flag's global `rollout_percentage`.",
    ),
    Row(
        "d4",
        {"plan": "free", "country": "US"},
        False,
        "rollout",
        "local",
        CONCEPTS,
        "Rules can be combined using AND (all must match), OR (at least one must "
        "match), and NOT (inverts the result).",
    ),
    Row(
        "d5",
        {"country": "US"},
        False,
        "rollout",
        "local",
        RULES_REFERENCE,
        "A condition on an attribute the request's context does not carry does not "
        "hold, except `is_null`; a segment condition reads no attribute.",
    ),
    Row(
        "d6",
        None,
        False,
        "rollout",
        "local",
        CREATE,
        "A condition whose attribute is absent from the context does not match "
        "(except `is_null`, which does).",
    ),
    Row(
        "d7",
        {"plan": "Enterprise", "country": "US"},
        False,
        "rollout",
        "local",
        RULES_REFERENCE,
        'Text and a number or boolean are compared as that type (`"17"` equals '
        '`17`, `"true"` equals `true`); two texts are compared as they are.',
    ),
    Row(
        "d8",
        {"user": {"plan": "enterprise", "country": "US"}},
        True,
        "targeting_rule",
        "local",
        CREATE,
        'nested objects are flattened to dotted keys (`{"app": {"version": '
        '"3.2.1"}}` answers `app.version`);',
    ),
    Row(
        "d9",
        {"plan": "free", "employee": "true"},
        True,
        "targeting_rule",
        "local",
        CREATE,
        "This example targets enterprise-plan users in the US, CA or GB, **or** "
        "any employee:",
    ),
    Row(
        "d10",
        {"plan": "free", "employee": True},
        True,
        "targeting_rule",
        "server",
        CREATE,
        "Values typed in the dashboard are strings; they are compared leniently "
        'against typed context values (`"true"` matches `true`, `"17"` matches '
        '`17`, `"beta, staff"` is a list for `in`/`not_in`, `"17.4"` is padded to '
        "`17.4.0` for `semver_*`).",
    ),
    Row(
        "d11",
        {"plan": "enterprise", "country": "US", "seats": 5},
        True,
        "targeting_rule",
        "local",
        RULES_REFERENCE,
        # No docs sentence says an attribute no condition names is ignored, and
        # no server test pins it alone; this is the nearest sentence.
        "Each level combines its conditions and groups with `AND` (all of them "
        "hold), `OR` (at least one holds) or `NOT` (not all of them hold).",
    ),
)

#: Rows for ``SEMVER_RULES``. The flag is remote, so every leg is the server.
SEM_ROWS: tuple[Row, ...] = (
    Row(
        "s1",
        {"app_version": "3.1.0"},
        False,
        "rollout",
        "server",
        RULES_REFERENCE,
        "the attribute is a version that compares so with the value",
    ),
    Row(
        "s2",
        {"app_version": "3.2.0"},
        True,
        "targeting_rule",
        "server",
        RULES_REFERENCE,
        "the attribute is a version that compares so with the value",
    ),
    # The docs never say versions compare by numeric parts, so s3's basis is a
    # server test with the same shape (1.10.0 above 1.9.0).
    Row(
        "s3",
        {"app_version": "3.10.0"},
        True,
        "targeting_rule",
        "server",
        "backend/tests/unit/core/test_rules_engine_semver.py",
        "test_minor_version_greater",
        "behaviour",
    ),
    Row(
        "s4",
        {"app_version": "3.2.0-beta.1"},
        False,
        "rollout",
        "server",
        RULES_REFERENCE,
        "A pre-release comes before its release (`1.2.3-beta.1` is lower than "
        "`1.2.3`), and `eq` ignores build metadata (`1.0.0+build.1` equals "
        "`1.0.0+build.2`).",
    ),
    # s5 relies on s4's sentence for the ordering (a pre-release sorts below its
    # release); its own quote only shows a pre-release is a valid version.
    Row(
        "s5",
        {"app_version": "3.2.1-beta.1"},
        True,
        "targeting_rule",
        "server",
        RULES_REFERENCE,
        "The attribute and the value are versions such as `1.2.3` or `1.2.3-beta.1`",
    ),
    Row(
        "s6",
        {"app_version": "v3.2.0"},
        True,
        "targeting_rule",
        "server",
        RULES_REFERENCE,
        "The attribute and the value are versions such as `1.2.3` or "
        "`1.2.3-beta.1`; a leading `v` on the attribute is ignored.",
    ),
    Row(
        "s7",
        {"app_version": "3.2"},
        False,
        "rollout",
        "server",
        RULES_REFERENCE,
        'An attribute that is not a whole version, such as `"17.4"`, does not match.',
    ),
    Row(
        "s8",
        None,
        False,
        "rollout",
        "server",
        RULES_REFERENCE,
        "A condition on an attribute the request's context does not carry does not "
        "hold, except `is_null`; a segment condition reads no attribute.",
    ),
)

ROWS: tuple[Row, ...] = DOC_ROWS + SEM_ROWS

#: Why the rows' ``local`` legs are what they are: (rows, file, quote).
LEG_BASIS: tuple[tuple[str, str, str], ...] = (
    (
        "DOC local",
        LOCAL_EVALUATION,
        "A flag is local when every condition in its rules uses one of these "
        "operators, with a value the SDK can compare exactly as the server does:",
    ),
    (
        "d10 server",
        LOCAL_EVALUATION,
        "an attribute whose type does not match the rule (a string against a "
        'number, a boolean, a numeric string such as `"10"`); the server\'s type '
        "coercions are not reproduced;",
    ),
    (
        "SEM server",
        LOCAL_EVALUATION,
        "flags using any other operator (semantic version, `contains_all` / "
        "`contains_any`, date and time windows, geo distance, array length, JSON "
        "path, percentage bucket, and pattern matching, which is server-only "
        "because Python and JavaScript regular expressions are different "
        "dialects), and flags stored in the legacy list shape: `status()` lists "
        "them as server-evaluated flags;",
    ),
)


def check(doc_rows: Sequence[Row], sem_rows: Sequence[Row]) -> None:
    """Refuse a table that cannot be right, whatever the server answers.

    Raises ``ValueError`` on: a row id used twice; a reason other than
    ``targeting_rule`` or ``rollout``; a row enabled with reason ``rollout``
    (at a global rollout of 0 the rollout enables no one); a SEM row whose
    local leg is ``local`` (the flag is remote).
    """
    seen: set[str] = set()
    for row in (*doc_rows, *sem_rows):
        if row.id in seen:
            raise ValueError(f"row id {row.id} is used twice")
        seen.add(row.id)
        if row.reason not in REASONS:
            raise ValueError(
                f"{row.id}: reason {row.reason!r} is not one of {', '.join(REASONS)}"
            )
        if row.basis_kind not in ("docs", "behaviour"):
            raise ValueError(f"{row.id}: basis kind {row.basis_kind!r} is unknown")
        if row.enabled and row.reason == "rollout":
            raise ValueError(
                f"{row.id}: enabled with reason rollout, which a global rollout of "
                f"{ROLLOUT_PERCENTAGE} never gives"
            )
    for row in sem_rows:
        if row.local == "local":
            raise ValueError(
                f"{row.id}: a semantic-version row is answered by the server, "
                "never locally"
            )


check(DOC_ROWS, SEM_ROWS)
