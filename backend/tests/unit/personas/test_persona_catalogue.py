"""The persona catalogue and the role matrix, checked statically (no browser).

``tests/personas/catalogue.py`` loads ``tests/personas/personas/*.yaml`` and
``tests/personas/role-matrix.yaml``. Each refusal below is a *plant*: the real
file's data with one thing broken, which must be refused with the named
message. Every plant is paired with the unbroken data, which must be accepted,
so a plant that fails for some other reason cannot pass for the right one.

Runs per pull request in the Unit job and, because it reads docs/ (the cited
anchors), on a docs-only pull request through the "Docs content tests" step.
It reads files and parses YAML: no database, no network, no browser.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
PERSONAS_ROOT = REPO_ROOT / "tests" / "personas"

if str(PERSONAS_ROOT) not in sys.path:
    sys.path.insert(0, str(PERSONAS_ROOT))

import catalogue  # the path is set first

pytestmark = pytest.mark.unit

#: The number of matrix rows. A row removed, or added without this changing,
#: fails here: the count cannot shrink silently. The number of cards is held
#: to tests/personas/pinned.yaml instead (each card is one day, one test).
MATRIX_ROWS = 10


def _card() -> dict[str, Any]:
    return yaml.safe_load(
        (PERSONAS_ROOT / "personas" / "u-dev.yaml").read_text("utf-8")
    )


def _matrix() -> dict[str, Any]:
    return yaml.safe_load((PERSONAS_ROOT / "role-matrix.yaml").read_text("utf-8"))


def _card_problems(data: dict[str, Any]) -> list[str]:
    """Every refusal of a card: its shape first, then what the repo says."""
    try:
        persona = catalogue.parse_persona(data)
    except catalogue.CatalogueError as exc:
        return str(exc).splitlines()
    return catalogue.persona_problems(persona, REPO_ROOT)


def _matrix_problems(data: dict[str, Any]) -> list[str]:
    try:
        matrix = catalogue.parse_matrix(data)
    except catalogue.CatalogueError as exc:
        return str(exc).splitlines()
    return catalogue.matrix_problems(matrix, REPO_ROOT)


# ----------------------------------------------------------- the real files


def test_the_real_cards_load_and_each_is_one_pinned_day():
    """Each card is one day and each day one test, so the cards number
    what pinned.yaml pins (which check_run.py holds the run to): a card
    removed, or added without its day, fails here or in the run."""
    personas = catalogue.load_personas(PERSONAS_ROOT / "personas", REPO_ROOT)
    ids = [p.id for p in personas]
    assert "U-DEV" in ids
    pinned = yaml.safe_load((PERSONAS_ROOT / "pinned.yaml").read_text("utf-8"))
    assert len(ids) == pinned["tests"], (ids, pinned)


def test_the_u_dev_day_is_eight_consecutive_steps_that_write_or_read_on_purpose():
    personas = catalogue.load_personas(PERSONAS_ROOT / "personas", REPO_ROOT)
    (persona,) = [p for p in personas if p.id == "U-DEV"]
    assert [s.n for s in persona.day] == list(range(1, 9))
    assert {s.layer for s in persona.day} == {"A"}
    # Only step 5 (the results page) reads without writing.
    assert [s.n for s in persona.day if not s.writes] == [5]
    # The numbers on that page are decided by a ledger, not by the page alone.
    step5 = persona.day[4]
    assert step5.numeric
    assert {o.kind for o in step5.oracles} == {"count_delta", "ui_matches_api"}


def test_the_u_sdk_day_reads_every_answer_back_and_checks_both_bucketings():
    """What the SDK page shows is decided by the API and by each documented
    bucketing, never by the page: dropping either kind from step 1 fails."""
    personas = catalogue.load_personas(PERSONAS_ROOT / "personas", REPO_ROOT)
    (persona,) = [p for p in personas if p.id == "U-SDK"]
    assert persona.role == "SDK"
    assert [s.n for s in persona.day] == [1, 2, 3]
    assert {s.layer for s in persona.day} == {"A"}
    assert [s.n for s in persona.day if not s.writes] == [2]
    assert [o.kind for o in persona.day[0].oracles] == [
        "api_state",
        "hash_vector",
        "api_state",
        "hash_vector",
    ]
    assert [o.kind for o in persona.day[1].oracles] == ["count_delta"]
    assert [o.kind for o in persona.day[2].oracles] == ["count_delta"]


def test_the_real_matrix_loads_and_the_row_count_is_pinned():
    matrix = catalogue.load_matrix(PERSONAS_ROOT / "role-matrix.yaml", REPO_ROOT)
    assert len(matrix.rows) == MATRIX_ROWS


def test_the_matrix_diverges_exactly_where_the_rbac_page_says_it_does():
    matrix = catalogue.load_matrix(PERSONAS_ROOT / "role-matrix.yaml", REPO_ROOT)
    diverging = {
        (row.id, role)
        for row in matrix.rows
        for role in catalogue.ROLES
        if not catalogue.agrees(getattr(row.cells, role))
    }
    assert diverging == {
        ("api-keys-list-all", "ADMIN"),
        ("api-key-delete-others", "ADMIN"),
        ("custom-role-create", "ADMIN"),
    }
    # Users and the global safety settings refuse ADMIN on both sides.
    for row_id in ("users-list", "safety-settings-change"):
        (row,) = [r for r in matrix.rows if r.id == row_id]
        assert (row.cells.ADMIN.ui, row.cells.ADMIN.api) == ("refusal_page", "403")
    # The full-profile row is marked, so a core build can leave it out.
    assert [r.id for r in matrix.rows if r.profile == "full"] == ["custom-role-create"]


# ------------------------------------------------------------- card plants


def _step(data: dict[str, Any], n: int) -> dict[str, Any]:
    return next(s for s in data["day"] if s["n"] == n)


def _no_oracle(d: dict[str, Any]) -> None:
    _step(d, 3)["oracles"] = []


def _kind_outside_the_eight(d: dict[str, Any]) -> None:
    _step(d, 3)["oracles"][0]["kind"] = "screenshot_matches"


def _write_decided_by_text_only(d: dict[str, Any]) -> None:
    _step(d, 3)["oracles"] = [
        {"id": "u-dev.3.toast", "kind": "text_exact", "expect": "Experiment started"}
    ]


def _write_decided_by_network_allowlist_only(d: dict[str, Any]) -> None:
    _step(d, 3)["oracles"] = [
        {"id": "u-dev.3.net", "kind": "network_allowlist", "expect": "no 5xx"}
    ]


def _number_decided_by_the_page_alone(d: dict[str, Any]) -> None:
    _step(d, 5)["oracles"] = [
        {"id": "u-dev.5.page", "kind": "ui_matches_api", "expect": "page equals API"}
    ]


def _source_with_a_line_number(d: dict[str, Any]) -> None:
    _step(d, 1)["oracles"][0]["source"] = {
        "path": "docs/security/api-keys.md:76",
        "anchor": "shown only once",
    }


def _source_with_a_line_field(d: dict[str, Any]) -> None:
    _step(d, 1)["oracles"][0]["source"]["line"] = 76


def _source_anchor_absent(d: dict[str, Any]) -> None:
    _step(d, 1)["oracles"][0]["source"]["anchor"] = "this sentence is in no file"


def _source_file_absent(d: dict[str, Any]) -> None:
    _step(d, 1)["oracles"][0]["source"]["path"] = "docs/security/no-such-page.md"


def _covered_by_a_missing_file(d: dict[str, Any]) -> None:
    step = _step(d, 3)
    step.update(layer="covered", covered_by="docs/no-such-journey.yaml", oracles=[])


def _covered_by_a_missing_job(d: dict[str, Any]) -> None:
    step = _step(d, 3)
    step.update(layer="covered", covered_by=".github/workflows/lint.yml#no-such-job")
    step["oracles"] = []


def _covered_by_a_missing_workflow(d: dict[str, Any]) -> None:
    step = _step(d, 3)
    step.update(layer="covered", covered_by=".github/workflows/nope.yml#lint")
    step["oracles"] = []


def _duplicate_step_number(d: dict[str, Any]) -> None:
    _step(d, 4)["n"] = 3


def _duplicate_oracle_id(d: dict[str, Any]) -> None:
    _step(d, 4)["oracles"][0]["id"] = _step(d, 3)["oracles"][0]["id"]


def _a_url_on_the_card(d: dict[str, Any]) -> None:
    d["base_url"] = "http://127.0.0.1:8000"


def _a_url_on_a_step(d: dict[str, Any]) -> None:
    _step(d, 2)["url"] = "http://127.0.0.1:3100/experiments/new"


def _a_url_on_an_oracle(d: dict[str, Any]) -> None:
    _step(d, 2)["oracles"][0]["url"] = "http://127.0.0.1:8000/api/v1/experiments/"


def _covered_without_covered_by(d: dict[str, Any]) -> None:
    _step(d, 3).update(layer="covered", oracles=[])


def _covered_by_on_a_layer_a_step(d: dict[str, Any]) -> None:
    _step(d, 3)["covered_by"] = ".github/workflows/lint.yml#lint"


CARD_PLANTS: list[tuple[str, Callable[[dict[str, Any]], None], str]] = [
    ("no_oracle", _no_oracle, "U-DEV day 3: no oracle"),
    ("kind_outside_the_eight", _kind_outside_the_eight, "day.2.oracles.0.kind"),
    (
        "write_decided_by_text_only",
        _write_decided_by_text_only,
        "U-DEV day 3: writes, no state oracle (text_exact is additional only)",
    ),
    (
        "write_decided_by_network_allowlist_only",
        _write_decided_by_network_allowlist_only,
        "U-DEV day 3: writes, no state oracle (text_exact is additional only)",
    ),
    (
        "number_decided_by_the_page_alone",
        _number_decided_by_the_page_alone,
        "U-DEV day 5: numeric, only oracle is ui_matches_api",
    ),
    (
        "source_with_a_line_number",
        _source_with_a_line_number,
        "day.0.oracles.0.source.path",
    ),
    (
        "source_with_a_line_field",
        _source_with_a_line_field,
        "day.0.oracles.0.source.line",
    ),
    (
        "source_anchor_absent",
        _source_anchor_absent,
        'source docs/security/api-keys.md: anchor "this sentence is in no file" not found',
    ),
    (
        "source_file_absent",
        _source_file_absent,
        "source docs/security/no-such-page.md: file not found",
    ),
    (
        "covered_by_a_missing_file",
        _covered_by_a_missing_file,
        "covered_by docs/no-such-journey.yaml: file not found",
    ),
    (
        "covered_by_a_missing_job",
        _covered_by_a_missing_job,
        "no job 'no-such-job' in .github/workflows/lint.yml",
    ),
    (
        "covered_by_a_missing_workflow",
        _covered_by_a_missing_workflow,
        "covered_by .github/workflows/nope.yml#lint: file not found",
    ),
    (
        "duplicate_step_number",
        _duplicate_step_number,
        "U-DEV day 3: duplicate step number",
    ),
    ("duplicate_oracle_id", _duplicate_oracle_id, "duplicate oracle id u-dev.3.active"),
    ("url_on_the_card", _a_url_on_the_card, "base_url"),
    ("url_on_a_step", _a_url_on_a_step, "day.1.url"),
    ("url_on_an_oracle", _a_url_on_an_oracle, "day.1.oracles.0.url"),
    (
        "covered_without_covered_by",
        _covered_without_covered_by,
        "U-DEV day 3: layer covered names no covered_by",
    ),
    (
        "covered_by_on_a_layer_a_step",
        _covered_by_on_a_layer_a_step,
        "U-DEV day 3: covered_by on a layer A step",
    ),
]


def test_the_unbroken_card_is_accepted():
    # The negative control of every plant below.
    assert _card_problems(_card()) == []


@pytest.mark.parametrize(
    ("plant", "expected"),
    [(p, e) for _, p, e in CARD_PLANTS],
    ids=[name for name, _, _ in CARD_PLANTS],
)
def test_a_card_with_this_planted_defect_is_refused(plant, expected):
    data = copy.deepcopy(_card())
    plant(data)
    problems = _card_problems(data)
    assert any(expected in line for line in problems), (expected, problems)


def test_a_covered_step_that_names_a_real_workflow_job_is_accepted():
    # The control for the covered_by plants: they fail on the missing target,
    # not on the shape of a covered step.
    data = copy.deepcopy(_card())
    step = _step(data, 3)
    step.update(layer="covered", covered_by=".github/workflows/lint.yml#lint")
    step["oracles"] = []
    assert _card_problems(data) == []


def test_a_persona_id_used_by_two_cards_is_refused():
    persona = catalogue.parse_persona(_card())
    problems = catalogue.cross_card_problems([persona, persona])
    assert "U-DEV: duplicate persona id" in problems
    assert any("is used by another card" in line for line in problems)
    assert catalogue.cross_card_problems([persona]) == []


def test_a_card_that_is_not_an_object_is_refused_not_crashed():
    with pytest.raises(catalogue.CatalogueError):
        catalogue.parse_persona(["not", "a", "card"])


def test_loading_a_directory_names_the_bad_card_and_refuses_the_whole(tmp_path):
    bad = _card()
    _no_oracle(bad)
    (tmp_path / "u-dev.yaml").write_text(yaml.safe_dump(bad), encoding="utf-8")
    with pytest.raises(catalogue.CatalogueError, match="U-DEV day 3: no oracle"):
        catalogue.load_personas(tmp_path, REPO_ROOT)


# ----------------------------------------------------------- matrix plants


def _row(data: dict[str, Any], row_id: str) -> dict[str, Any]:
    return next(r for r in data["rows"] if r["id"] == row_id)


def _renamed_path(d: dict[str, Any]) -> None:
    _row(d, "flag-change")["path"] = "/api/v1/feature-flags/{flag_identifier}"


def _unknown_method(d: dict[str, Any]) -> None:
    _row(d, "flag-change")["method"] = "DELETE"
    _row(d, "flag-change")["path"] = "/api/v1/experiments/"


def _admin_ui_and_api_differ_without_an_anchor(d: dict[str, Any]) -> None:
    del _row(d, "api-keys-list-all")["cells"]["ADMIN"]["divergence"]


def _a_divergence_on_agreeing_cells(d: dict[str, Any]) -> None:
    _row(d, "users-list")["cells"]["ADMIN"]["divergence"] = {
        "path": "docs/rbac/README.md",
        "anchor": "Through the API, the Admin role alone may list and delete",
    }


def _a_divergence_anchor_absent(d: dict[str, Any]) -> None:
    _row(d, "custom-role-create")["cells"]["ADMIN"]["divergence"]["anchor"] = (
        "a divergence the page does not state"
    )


def _a_divergence_anchored_elsewhere(d: dict[str, Any]) -> None:
    _row(d, "custom-role-create")["cells"]["ADMIN"]["divergence"]["path"] = (
        "docs/api/rbac.md"
    )


def _a_row_source_anchor_absent(d: dict[str, Any]) -> None:
    _row(d, "flag-list")["source"]["anchor"] = "a sentence no page contains"


def _a_row_without_a_source(d: dict[str, Any]) -> None:
    del _row(d, "flag-list")["source"]


def _a_role_without_cells(d: dict[str, Any]) -> None:
    del _row(d, "flag-list")["cells"]["VIEWER"]


def _a_cell_without_its_api_half(d: dict[str, Any]) -> None:
    del _row(d, "flag-list")["cells"]["ANALYST"]["api"]


def _an_unknown_role(d: dict[str, Any]) -> None:
    _row(d, "flag-list")["cells"]["SUPPORT"] = {"ui": "offered", "api": "2xx"}


def _an_api_answer_outside_the_two(d: dict[str, Any]) -> None:
    _row(d, "flag-list")["cells"]["VIEWER"]["api"] = "404"


def _a_duplicate_row_id(d: dict[str, Any]) -> None:
    _row(d, "flag-list")["id"] = "flag-create"


def _the_same_operation_twice(d: dict[str, Any]) -> None:
    d["rows"].append(copy.deepcopy(_row(d, "flag-list")))
    d["rows"][-1]["id"] = "flag-list-again"


def _a_url_on_a_row(d: dict[str, Any]) -> None:
    _row(d, "flag-list")["url"] = "http://127.0.0.1:8000/api/v1/feature-flags/"


MATRIX_PLANTS: list[tuple[str, Callable[[dict[str, Any]], None], str]] = [
    (
        "renamed_path",
        _renamed_path,
        "role-matrix row 5: PUT /api/v1/feature-flags/{flag_identifier} not in OpenAPI",
    ),
    (
        "method_the_path_does_not_have",
        _unknown_method,
        "role-matrix row 5: DELETE /api/v1/experiments/ not in OpenAPI",
    ),
    (
        "admin_ui_and_api_differ_without_an_anchor",
        _admin_ui_and_api_differ_without_an_anchor,
        "role-matrix row 8 ADMIN: ui and api differ, no documented divergence",
    ),
    (
        "divergence_on_agreeing_cells",
        _a_divergence_on_agreeing_cells,
        "role-matrix row 6 ADMIN: a divergence is cited but ui and api agree",
    ),
    (
        "divergence_anchor_absent",
        _a_divergence_anchor_absent,
        'role-matrix row 10 ADMIN source docs/rbac/README.md: anchor "a divergence '
        'the page does not state" not found',
    ),
    (
        "divergence_anchored_outside_the_rbac_page",
        _a_divergence_anchored_elsewhere,
        "role-matrix row 10 ADMIN: a divergence is anchored in docs/rbac/README.md, "
        "not docs/api/rbac.md",
    ),
    (
        "row_source_anchor_absent",
        _a_row_source_anchor_absent,
        'role-matrix row 4 source docs/rbac/README.md: anchor "a sentence no page '
        'contains" not found',
    ),
    ("row_without_a_source", _a_row_without_a_source, "rows.3.source"),
    ("role_without_cells", _a_role_without_cells, "rows.3.cells.VIEWER"),
    (
        "cell_without_its_api_half",
        _a_cell_without_its_api_half,
        "rows.3.cells.ANALYST.api",
    ),
    ("unknown_role", _an_unknown_role, "rows.3.cells.SUPPORT"),
    (
        "api_answer_outside_the_two",
        _an_api_answer_outside_the_two,
        "rows.3.cells.VIEWER.api",
    ),
    (
        "duplicate_row_id",
        _a_duplicate_row_id,
        "role-matrix row 4: duplicate row id flag-create",
    ),
    (
        "same_operation_twice",
        _the_same_operation_twice,
        "role-matrix row 11: GET /api/v1/feature-flags/ listed twice",
    ),
    ("url_on_a_row", _a_url_on_a_row, "rows.3.url"),
]


def test_the_unbroken_matrix_is_accepted():
    assert _matrix_problems(_matrix()) == []


@pytest.mark.parametrize(
    ("plant", "expected"),
    [(p, e) for _, p, e in MATRIX_PLANTS],
    ids=[name for name, _, _ in MATRIX_PLANTS],
)
def test_a_matrix_with_this_planted_defect_is_refused(plant, expected):
    data = copy.deepcopy(_matrix())
    plant(data)
    problems = _matrix_problems(data)
    assert any(expected in line for line in problems), (expected, problems)


def test_every_row_is_an_operation_of_the_full_openapi_snapshot():
    # The check behind the rows, stated outside the loader so a loader that
    # stopped looking would still fail this.
    import json

    paths = json.loads(
        (REPO_ROOT / "docs/api/openapi-v1.full.json").read_text(encoding="utf-8")
    )["paths"]
    for row in _matrix()["rows"]:
        assert row["method"].lower() in paths[row["path"]], row["id"]


def test_the_matrix_loader_refuses_a_file_that_is_not_a_matrix(tmp_path):
    path = tmp_path / "role-matrix.yaml"
    path.write_text("rows: []\n", encoding="utf-8")
    with pytest.raises(catalogue.CatalogueError):
        catalogue.load_matrix(path, REPO_ROOT)
