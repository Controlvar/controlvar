"""Every site that counts or groups users is classified for linked ids (IA-A9, #449).

#449 counts a linked person once by storing data written after a link under
the person's holder, not by changing any analysis path. A path that counts or
groups users is then right or a stated limit for a reason that has to be
known. This generates, from the source of ``backend/app`` and
``modules/backend/app``, every function that uses one of these forms (the
user-grouping patterns):

* ``Assignment.user_id``;
* ``count(Assignment.id)``;
* ``group_by(Event.user_id)`` or ``distinct(Event.user_id)``;
* ``HoldoutPopulation``;

and requires that set to equal ``INVENTORY``'s scanned entries exactly: a new
one fails until it is classified here, a stale one fails too. The classes are
these:

* ``PER_ROW``: counts assignment rows; an alias adds no row, so a conflict
  that existed before a link counts once per row, as documented;
* ``RESOLVED_AT_WRITE``: right because events and exposures for an
  experiment are stored under the person's holder;
* ``RESOLVED_IN_CODE``: #449's own resolution (D3, D4, D5), or a helper only
  it calls with a resolved id;
* ``NOT_ROUTED``: no route reaches it;
* ``STATED_LIMIT``: counts ids, pinned as it behaves by IA-A3, IA-A4, IA-A7
  or IA-A11.

``BY_HAND`` are the grouping sites the patterns do not reach (a whole-row count, a
covariate lookup by id, the audience preview's read); each must still exist.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, Iterator, Set, Tuple

import pytest

pytestmark = [pytest.mark.smoke]

REPO_ROOT = Path(__file__).resolve().parents[3]
ROOTS = (REPO_ROOT / "backend" / "app", REPO_ROOT / "modules" / "backend" / "app")

PER_ROW = "per-row count"
RESOLVED_AT_WRITE = "resolved at write"
RESOLVED_IN_CODE = "resolved in code"
NOT_ROUTED = "not routed"
STATED_LIMIT = "stated limit"

_R = "backend/app/api/v1/endpoints/results.py::"
_A = "backend/app/services/analysis_service.py::AnalysisService."
_S = "backend/app/services/assignment_service.py::AssignmentService."
_I = (
    "backend/app/services/interaction_detection_service.py::"
    "InteractionDetectionService."
)

#: ``"<path>::<qualified function name>"`` -> class.
INVENTORY: Dict[str, str] = {
    _R + "get_sample_size_status": PER_ROW,
    _R + "_get_sequential_data._count_assignments": PER_ROW,
    # Called by _holder_at_receipt for an id never linked only.
    "backend/app/api/v1/endpoints/tracking.py::_variant_at_receipt": RESOLVED_IN_CODE,
    "backend/app/core/bandit_scheduler.py::BanditScheduler"
    "._count_assignments_by_variant": PER_ROW,
    _A + "event_counts": RESOLVED_AT_WRITE,
    _A + "value_totals": RESOLVED_AT_WRITE,
    _A + "calculate_metric_results": PER_ROW,
    _A + "calculate_experiment_summary": PER_ROW,
    _A + "get_daily_results": PER_ROW,
    _A + "_bayesian_observations": PER_ROW,
    # assign_user's helpers, each called with the holder or an id never
    # linked. _stored_assignment is also the split URL preview's read of the
    # raw id: a stated non-resolution, pinned by the preview's inverted pin
    # in modules/backend/tests/integration/split_url.
    _S + "get_assignment": RESOLVED_IN_CODE,
    _S + "_record_holdout_population": RESOLVED_IN_CODE,
    _S + "_recorded_holdout_arm": RESOLVED_IN_CODE,
    _S + "_stored_assignment": RESOLVED_IN_CODE,
    # GET /tracking/assignments/{user_id} (D5): an id never linked, a person.
    _S + "get_user_assignments": RESOLVED_IN_CODE,
    _S + "get_person_assignments": RESOLVED_IN_CODE,
    _S + "assign_user_with_targeting": NOT_ROUTED,
    _S + "reassign_user": NOT_ROUTED,
    _S + "delete_assignments_by_experiment": PER_ROW,
    "backend/app/services/balanced_groups_results.py::stored_strata": PER_ROW,
    "backend/app/services/balanced_groups_results.py::users_outside_blocks": PER_ROW,
    "backend/app/services/event_matching.py::_assigned_pairs": RESOLVED_AT_WRITE,
    "backend/app/services/event_matching.py::_converters": RESOLVED_AT_WRITE,
    "backend/app/services/event_matching.py::assignment_times": PER_ROW,
    "backend/app/services/event_service.py::EventService.track_conversion": NOT_ROUTED,
    "backend/app/services/holdout_results.py::count_statement": STATED_LIMIT,
    "backend/app/services/identity_aliases.py::holder_of": RESOLVED_IN_CODE,
    _I + "_arm_totals": PER_ROW,
    _I + "_shared_assignments": STATED_LIMIT,
    _I + "_get_experiment_users": STATED_LIMIT,
    "backend/app/services/mutual_exclusion_service.py::MutualExclusionService"
    ".has_sibling_assignment": RESOLVED_IN_CODE,
    "backend/app/services/results_streaming_service.py::ResultsStreamingService"
    "._compute_variant_results": PER_ROW,
    "backend/app/services/segment_breakdown.py::user_values": RESOLVED_AT_WRITE,
    "backend/app/services/segment_results.py::_member_values": RESOLVED_AT_WRITE,
    "backend/app/services/segment_results.py::experiment_users": PER_ROW,
    "backend/app/services/srm_service.py::compute_srm_for_experiment": PER_ROW,
    # BY_HAND below.
    "backend/app/services/export_service.py::ExportService._count_assignments": PER_ROW,
    "backend/app/services/event_matching.py::covariate_user_ids": STATED_LIMIT,
    "backend/app/services/audience_service.py::recent_exposures_statement": STATED_LIMIT,
}

BY_HAND = {
    "backend/app/services/export_service.py::ExportService._count_assignments",
    "backend/app/services/event_matching.py::covariate_user_ids",
    "backend/app/services/audience_service.py::recent_exposures_statement",
}

#: Per class, at this base, in both profiles (modules/backend/app has none).
COUNTS = {
    PER_ROW: 16,
    RESOLVED_AT_WRITE: 6,
    RESOLVED_IN_CODE: 9,
    NOT_ROUTED: 3,
    STATED_LIMIT: 5,
}


def _is_model_attr(node: ast.AST, model: str, attr: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == attr
        and isinstance(node.value, ast.Name)
        and node.value.id == model
    )


def _uses(node: ast.AST) -> bool:
    """Whether *node* is one of the four forms."""
    if _is_model_attr(node, "Assignment", "user_id"):
        return True
    if isinstance(node, ast.Name) and node.id == "HoldoutPopulation":
        return True
    if isinstance(node, ast.Call):
        name = getattr(node.func, "attr", getattr(node.func, "id", ""))
        for arg in node.args:
            if name == "count" and _is_model_attr(arg, "Assignment", "id"):
                return True
            if name in ("group_by", "distinct") and _is_model_attr(
                arg, "Event", "user_id"
            ):
                return True
    return False


def _functions(tree: ast.AST) -> Iterator[Tuple[str, ast.AST]]:
    def visit(node: ast.AST, prefix: str):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}{child.name}"
                yield name, child
                yield from visit(child, f"{name}.")
            elif isinstance(child, ast.ClassDef):
                yield from visit(child, f"{prefix}{child.name}.")
            else:
                yield from visit(child, prefix)

    yield from visit(tree, "")


def _own_body_uses(func: ast.AST) -> bool:
    nested = [
        n
        for n in ast.walk(func)
        if n is not func and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    hidden = {id(x) for n in nested for x in ast.walk(n)}
    return any(_uses(n) for n in ast.walk(func) if id(n) not in hidden)


def grouping_sites(files=None) -> Set[str]:
    """``{"<path>::<qualified name>"}`` of every function using a form.

    ``files`` maps a display path to source, for the checker's own test.
    """
    if files is None:
        files = {
            str(p.relative_to(REPO_ROOT)): p.read_text(encoding="utf-8")
            for root in ROOTS
            if root.is_dir()
            for p in sorted(root.rglob("*.py"))
        }
    return {
        f"{display}::{name}"
        for display, source in files.items()
        for name, func in _functions(ast.parse(source))
        if _own_body_uses(func)
    }


def _present(key: str) -> bool:
    return (REPO_ROOT / key.split("::", 1)[0]).is_file()


def test_every_user_grouping_site_is_classified():
    found = grouping_sites()
    expected = {k for k in INVENTORY if k not in BY_HAND and _present(k)}
    unclassified = sorted(found - expected)
    stale = sorted(expected - found)
    assert unclassified == [], (
        f"unclassified user-grouping site: {unclassified}. Decide whether it "
        "counts rows, is right by write-time resolution, is resolved by #449's "
        "code, has no route, or is a stated limit with its pin."
    )
    assert stale == [], f"classified but no longer a user-grouping site: {stale}"


def test_the_sites_listed_by_hand_exist():
    for key in BY_HAND:
        path, name = key.split("::", 1)
        tree = ast.parse((REPO_ROOT / path).read_text(encoding="utf-8"))
        assert name in {n for n, _ in _functions(tree)}, key


def test_the_counts_per_class_at_this_base():
    counts: Dict[str, int] = {}
    for key, why in INVENTORY.items():
        counts[why] = counts.get(why, 0) + 1
    assert counts == COUNTS
    assert set(INVENTORY.values()) == set(COUNTS)


def test_the_scanner_sees_each_form():
    forms = {
        "user_id": "def f(db):\n    db.query(Assignment).filter(Assignment.user_id == 1)\n",
        "count": "def f(db):\n    db.query(func.count(Assignment.id))\n",
        "group_by": "def f(db):\n    db.query(Event.user_id).group_by(Event.user_id)\n",
        "distinct": "def f(db):\n    db.query(Event.user_id).distinct(Event.user_id)\n",
        "population": "def f(db):\n    db.query(HoldoutPopulation)\n",
    }
    for form, source in forms.items():
        found = grouping_sites({"backend/app/services/plant.py": source})
        assert found == {"backend/app/services/plant.py::f"}, form
    quiet = "def g(db):\n    db.query(Event.id).filter(Event.user_id == 1)\n"
    assert grouping_sites({"backend/app/services/quiet.py": quiet}) == set()
