"""Every function that can touch the ``assignments`` table is classified (#445).

A global holdout is measured from ``holdout_population``, which only
``AssignmentService.assign_user`` writes, on its new-user path.  A second way
to create assignments that skips it would put users into experiments without
recording them, and the comparison would quietly lose them.  So this generates,
from the source, the set of functions in ``backend/app`` and
``modules/backend/app`` that reference the assignments table in any form, and
requires that set to equal ``INVENTORY`` exactly -- a new one fails until it is
classified here, and a stale entry fails too.

"Reference" is deliberately broad, because a scanner that only looks for
``Assignment(...)`` calls misses most writers:

* the ``Assignment`` model under any name it is imported as
  (``from ... import Assignment as Row``), or as ``<module>.Assignment``, used
  in any way -- a call, ``bulk_insert_mappings(Assignment, ...)``,
  ``Assignment.__table__.insert()``, a query;
* the string ``"Assignment"``;
* raw SQL naming the table (``INSERT INTO assignments``, ``FROM
  experimentation.assignments``, ...);
* ``Table("assignments", ...)`` and ``metadata.tables["...assignments"]``.

The set is generated, never pinned as a count: it differs between a full and a
core tree (one modules file references it).

The writers that do NOT record the population are exempt only while nothing
calls them: ``test_unrecorded_writers_have_no_callers`` fails on the first call
site outside the function itself.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Dict, Iterator, Set, Tuple

import pytest

pytestmark = [pytest.mark.smoke, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[3]
ROOTS = (REPO_ROOT / "backend" / "app", REPO_ROOT / "modules" / "backend" / "app")
MODEL = "Assignment"
TABLE = "assignments"

#: Raw SQL that names the table: the verbs and clauses that take a table name,
#: then an optional schema and quotes.  Bounded quantifiers only.
RAW_SQL = re.compile(
    r"\b(?:insert\s{1,8}into|update|delete\s{1,8}from|from|join|into)\s{1,8}"
    r"(?:\"?\w{1,63}\"?\.)?\"?assignments\"?(?![\w])",
    re.IGNORECASE,
)

WRITES_POPULATION = "writes: records holdout_population on its new-user path"
READS = "reads assignments only"
DELETES = "deletes assignments only (no new user is assigned)"
UNRECORDED = (
    "exempt: writes assignments without the eligibility checks or the "
    "population, and has no caller (test_unrecorded_writers_have_no_callers)"
)

#: ``"<path from the repo root>::<qualified function name>"`` -> classification.
INVENTORY: Dict[str, str] = {
    "backend/app/services/assignment_service.py::AssignmentService.assign_user": WRITES_POPULATION,
    "backend/app/services/assignment_service.py::AssignmentService.assign_user_with_targeting": UNRECORDED,
    "backend/app/services/assignment_service.py::AssignmentService.reassign_user": UNRECORDED,
    "backend/app/services/assignment_service.py::AssignmentService.delete_assignments_by_experiment": DELETES,
    "backend/app/api/v1/endpoints/results.py::_get_sequential_data._count_assignments": READS,
    # Raw SQL in an f-string (FROM {schema}.assignments).
    "backend/app/api/v1/endpoints/results.py::_compute_dimensional_breakdown": READS,
    "backend/app/api/v1/endpoints/results.py::get_sample_size_status": READS,
    # /track and /batch read the user's stored variant through it.
    "backend/app/api/v1/endpoints/tracking.py::_variant_at_receipt": READS,
    "backend/app/core/bandit_scheduler.py::BanditScheduler._count_assignments_by_variant": READS,
    "backend/app/core/bandit_scheduler.py::BanditScheduler._stats_from_postgres": READS,
    "backend/app/services/analysis_service.py::AnalysisService._bayesian_observations": READS,
    "backend/app/services/analysis_service.py::AnalysisService.calculate_experiment_summary": READS,
    "backend/app/services/analysis_service.py::AnalysisService.calculate_metric_results": READS,
    "backend/app/services/analysis_service.py::AnalysisService.get_daily_results": READS,
    "backend/app/services/analysis_service.py::AnalysisService.get_segmented_results": READS,
    # assign_user's helpers: the stored-row lookup, and the sticky answer
    # (named here by its ``Assignment`` annotation; it reads through
    # get_assignment).
    "backend/app/services/assignment_service.py::AssignmentService._sticky_result": READS,
    "backend/app/services/assignment_service.py::AssignmentService._stored_assignment": READS,
    "backend/app/services/assignment_service.py::AssignmentService.get_assignment": READS,
    "backend/app/services/assignment_service.py::AssignmentService.get_user_assignments": READS,
    "backend/app/services/audience_service.py::AudienceService.preview_audience_size": READS,
    "backend/app/services/event_matching.py::_assigned_pairs": READS,
    "backend/app/services/event_matching.py::assignment_times": READS,
    "backend/app/services/event_service.py::EventService.track_conversion": READS,
    "backend/app/services/export_service.py::ExportService._count_assignments": READS,
    "backend/app/services/export_service.py::ExportService._experiments_to_rows": READS,
    "backend/app/services/holdout_results.py::count_statement": READS,
    "backend/app/services/interaction_detection_service.py::InteractionDetectionService._arm_totals": READS,
    "backend/app/services/interaction_detection_service.py::InteractionDetectionService._get_experiment_users": READS,
    "backend/app/services/interaction_detection_service.py::InteractionDetectionService._shared_assignments": READS,
    "backend/app/services/mutual_exclusion_service.py::MutualExclusionService.has_sibling_assignment": READS,
    "backend/app/services/results_streaming_service.py::ResultsStreamingService._compute_variant_results": READS,
    "backend/app/services/srm_service.py::compute_srm_for_experiment": READS,
}

#: The functions classified UNRECORDED, by their bare names, for the
#: no-caller check.
UNRECORDED_NAMES = {
    key.rsplit("::", 1)[1].rsplit(".", 1)[-1]
    for key, why in INVENTORY.items()
    if why == UNRECORDED
}

#: Each WRITES_POPULATION function, and the behaviour test that shows it
#: records the population.
BEHAVIOUR_TESTS = {
    "backend/app/services/assignment_service.py::AssignmentService.assign_user": (
        "backend/tests/integration/api/test_tracking_api.py::TestHoldoutPopulation"
        "::test_a2_holdout_rows_are_exactly_the_users_answered_holdout"
    ),
}


def _python_files() -> Iterator[Path]:
    for root in ROOTS:
        if root.is_dir():
            yield from sorted(root.rglob("*.py"))


def _aliases(tree: ast.AST) -> Tuple[Set[str], Set[str]]:
    """(names bound to the model, names bound to a module that holds it)."""
    model_names: Set[str] = set()
    module_names: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == MODEL:
                    model_names.add(alias.asname or alias.name)
                elif alias.name == "assignment":
                    module_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.endswith(".assignment"):
                    module_names.add(alias.asname or alias.name.split(".")[0])
    return model_names, module_names


def _is_table_string(value: str) -> bool:
    return value == MODEL or bool(RAW_SQL.search(value))


def _references(node: ast.AST, model_names: Set[str], module_names: Set[str]) -> bool:
    """Whether the subtree touches the model or the table in any of the forms."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in model_names:
            return True
        if isinstance(sub, ast.Attribute) and sub.attr == MODEL:
            # ``<module>.Assignment``, whatever the module was imported as.
            return True
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            if _is_table_string(sub.value):
                return True
        if isinstance(sub, ast.JoinedStr):
            # An f-string: each placeholder stands for a schema or a value.
            joined = "".join(
                part.value
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
                else "s"
                for part in sub.values
            )
            if _is_table_string(joined):
                return True
        if isinstance(sub, ast.Call):
            func = sub.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", "")
            )
            if name == "Table" and sub.args:
                first = sub.args[0]
                if isinstance(first, ast.Constant) and first.value == TABLE:
                    return True
        if isinstance(sub, ast.Subscript):
            target = sub.value
            if isinstance(target, ast.Attribute) and target.attr == "tables":
                key = sub.slice
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    if key.value.split(".")[-1] == TABLE:
                        return True
    return False


def _functions(tree: ast.AST) -> Iterator[Tuple[str, ast.AST]]:
    """Every function with its qualified name (``Class.method``, ``outer.inner``)."""

    def visit(node: ast.AST, prefix: str) -> Iterator[Tuple[str, ast.AST]]:
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


def _own_body_references(func: ast.AST, model_names, module_names) -> bool:
    """References in ``func`` itself, not in a function nested inside it."""
    nested = [
        n
        for n in ast.walk(func)
        if n is not func and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    hidden = {id(x) for n in nested for x in ast.walk(n)}
    for node in ast.walk(func):
        if node is func or id(node) in hidden:
            continue
        if _references(node, model_names, module_names) and not any(
            id(x) in hidden for x in ast.walk(node)
        ):
            return True
    return False


def referencing_functions(files=None) -> Set[str]:
    """``{"<path>::<qualified name>"}`` for every function touching the table.

    ``files`` maps a display path to source text, for the tamper tests;
    otherwise the real trees are read.
    """
    found: Set[str] = set()
    sources = (
        files.items()
        if files is not None
        else (
            (str(p.relative_to(REPO_ROOT)), p.read_text(encoding="utf-8"))
            for p in _python_files()
        )
    )
    for display, source in sources:
        tree = ast.parse(source)
        model_names, module_names = _aliases(tree)
        for name, func in _functions(tree):
            if _own_body_references(func, model_names, module_names):
                found.add(f"{display}::{name}")
    return found


def test_the_scan_reads_the_tree():
    """A scanner that found nothing would pass the exact-set check vacuously."""
    found = referencing_functions()
    assert (
        "backend/app/services/assignment_service.py::AssignmentService.assign_user"
        in found
    ), sorted(found)
    assert len(found) >= 10, sorted(found)


def test_the_inventory_is_exact():
    found = referencing_functions()
    expected = {
        key for key in INVENTORY if (REPO_ROOT / key.split("::", 1)[0]).is_file()
    }
    unclassified = sorted(found - expected)
    stale = sorted(expected - found)
    assert unclassified == [], (
        "These functions touch the assignments table and are not classified in "
        f"INVENTORY: {unclassified}. Decide whether each writes the holdout "
        "population, only reads, or is exempt and why."
    )
    assert stale == [], f"Classified but no longer touching the table: {stale}"


def test_every_population_writer_has_a_behaviour_test():
    writers = {k for k, why in INVENTORY.items() if why == WRITES_POPULATION}
    assert writers == set(BEHAVIOUR_TESTS)
    for test_id in BEHAVIOUR_TESTS.values():
        path, *names = test_id.split("::")
        source = (REPO_ROOT / path).read_text(encoding="utf-8")
        assert all(f" {n}(" in source or f"class {n}" in source for n in names), test_id


def test_unrecorded_writers_have_no_callers():
    """An exempt writer stays exempt only while nothing calls it."""
    assert UNRECORDED_NAMES, "nothing is classified UNRECORDED"
    callers = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name, func in _functions(tree):
            for node in ast.walk(func):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in UNRECORDED_NAMES
                    and name.rsplit(".", 1)[-1] != node.func.attr
                ):
                    callers.append(
                        f"{path.relative_to(REPO_ROOT)}::{name} -> {node.func.attr}"
                    )
    assert callers == [], callers


# ---------------------------------------------------------------------------
# The scanner catches each form (QA A8's plants)
# ---------------------------------------------------------------------------
PLANTS = {
    "alias": (
        "from backend.app.models.assignment import Assignment as Row\n"
        "def plant(db):\n    db.add(Row(user_id='u'))\n"
    ),
    "module attribute": (
        "from backend.app.models import assignment as amod\n"
        "def plant(db):\n    db.add(amod.Assignment(user_id='u'))\n"
    ),
    "raw SQL": (
        "from sqlalchemy import text\n"
        "def plant(db):\n"
        "    db.execute(text('INSERT INTO experimentation.assignments (user_id) "
        "VALUES (:u)'), {'u': 'x'})\n"
    ),
    "raw SQL in an f-string": (
        "from sqlalchemy import text\n"
        "def plant(db, schema):\n"
        "    db.execute(text(f'INSERT INTO {schema}.assignments (user_id) "
        "VALUES (:u)'), {'u': 'x'})\n"
    ),
    "bulk_insert_mappings": (
        "from backend.app.models.assignment import Assignment\n"
        "def plant(db, rows):\n    db.bulk_insert_mappings(Assignment, rows)\n"
    ),
    "__table__.insert()": (
        "from backend.app.models.assignment import Assignment\n"
        "def plant(db, rows):\n    db.execute(Assignment.__table__.insert(), rows)\n"
    ),
    "Table('assignments')": (
        "from sqlalchemy import MetaData, Table\n"
        "def plant(db, engine):\n"
        "    t = Table('assignments', MetaData(), autoload_with=engine)\n"
        "    db.execute(t.insert(), [])\n"
    ),
    "metadata.tables[...]": (
        "from backend.app.models.base import Base\n"
        "def plant(db):\n"
        "    t = Base.metadata.tables['experimentation.assignments']\n"
        "    db.execute(t.insert(), [])\n"
    ),
}


@pytest.mark.parametrize("form", sorted(PLANTS))
def test_the_scanner_sees_each_form(form):
    found = referencing_functions({"backend/app/services/plant.py": PLANTS[form]})
    assert found == {"backend/app/services/plant.py::plant"}, form


def test_the_scanner_ignores_unrelated_code():
    source = (
        "def other(db):\n"
        "    db.execute('SELECT * FROM experiments')\n"
        "    return 'assignment' + 'assignments_count'\n"
    )
    assert referencing_functions({"backend/app/services/other.py": source}) == set()
