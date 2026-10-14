"""The CUPED covariate's description says what the code does (#1194).

``VarianceReductionConfig.covariate_metric_id`` used to be described as
"Required when method is 'cuped' or 'cuped_plus'" (and its class docstring as
"required for CUPED and CUPED_PLUS methods"). It is not: without it, each
metric's own event before the user's assignment is the history
(``get_cuped_results_data`` in ``api/v1/endpoints/results.py``, and
``test_cuped_without_a_covariate_metric_reads_each_metrics_own_event``).

The old sentence must appear nowhere in the code (the dashboard's OpenAPI
fixture, which holds paths only, included) or in either snapshot under
``docs/api/``, and the new one must be in each snapshot: a snapshot that was
not regenerated fails here, as does the old sentence written back anywhere.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

import pytest

from backend.app.schemas.variance_reduction import VarianceReductionConfig

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]

#: What the old description and class docstring said, as far as it can be
#: searched for: each is a fragment of one line.
OLD_SENTENCES = (
    "Required when method is",
    "required for CUPED and CUPED_PLUS",
)

#: What the new ones say, whole (the docstring wraps it over lines; compared
#: with white space collapsed).
NEW_FIELD_SENTENCE = (
    "Not required: without it, each metric's own event before the user's "
    "assignment is the history."
)

#: The history is read before the user's ASSIGNMENT (``Assignment.created_at``,
#: ``event_matching.covariate_user_ids``), the word cuped.md and endpoints.md
#: use. The platform also records separate exposure events, which this is not.
ASSIGNMENT_WORDING = "before the user's assignment is the history"

#: The code to search, relative to the repository root. ``modules/`` is absent
#: from a core tree, so a missing root is skipped, never an error.
CODE_ROOTS = ("backend/app", "modules/backend/app", "frontend/src")
CODE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".json"}
SKIPPED_DIRS = {"node_modules", ".next", "__pycache__"}

#: The two OpenAPI snapshots, which carry the schema. The dashboard's fixture
#: (frontend/src/tests/fixtures/openapi.json) is paths and methods only, so it
#: has no description to be stale; it is searched with the code above.
OPENAPI_COPIES = (
    REPO_ROOT / "docs" / "api" / "openapi-v1.full.json",
    REPO_ROOT / "docs" / "api" / "openapi-v1.stable.json",
)


def _code_files() -> List[Path]:
    found = []
    for root in CODE_ROOTS:
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.suffix in CODE_SUFFIXES and path.is_file():
                if not SKIPPED_DIRS.intersection(path.relative_to(base).parts):
                    found.append(path)
    return found


def _schema(copy: Path) -> Dict:
    return json.loads(copy.read_text("utf-8"))["components"]["schemas"][
        "VarianceReductionConfig"
    ]


@pytest.mark.regression
def test_the_old_sentence_appears_nowhere_in_the_code():
    files = _code_files()
    # The search covers what it should: the schema itself, and a real tree.
    assert REPO_ROOT / "backend/app/schemas/variance_reduction.py" in files
    assert len(files) > 500, len(files)

    hits = [
        f"{path.relative_to(REPO_ROOT)}: {sentence!r}"
        for path in files
        for sentence in OLD_SENTENCES
        if sentence in path.read_text("utf-8", errors="replace")
    ]

    assert hits == []


@pytest.mark.regression
@pytest.mark.parametrize("copy", OPENAPI_COPIES, ids=lambda p: p.name)
def test_no_openapi_copy_carries_the_old_sentence(copy):
    text = copy.read_text("utf-8")
    assert "VarianceReductionConfig" in text  # it is the document that has the schema

    assert [s for s in OLD_SENTENCES if s in text] == []


@pytest.mark.regression
@pytest.mark.parametrize("copy", OPENAPI_COPIES, ids=lambda p: p.name)
def test_every_openapi_copy_carries_the_new_description(copy):
    schema = _schema(copy)

    field = schema["properties"]["covariate_metric_id"]["description"]
    assert (
        field == VarianceReductionConfig.model_fields["covariate_metric_id"].description
    )
    assert NEW_FIELD_SENTENCE in field
    assert (
        "covariate_metric_id: ID of one of the experiment's metrics"
        in schema["description"]
    )
    assert " ".join(NEW_FIELD_SENTENCE.split()) in " ".join(
        schema["description"].split()
    )


@pytest.mark.regression
def test_the_schema_says_the_covariate_is_not_required():
    field = VarianceReductionConfig.model_fields["covariate_metric_id"]
    assert field.is_required() is False
    assert field.default is None
    assert NEW_FIELD_SENTENCE in field.description
    docstring = " ".join(VarianceReductionConfig.__doc__.split())
    assert NEW_FIELD_SENTENCE in docstring
    # What the description claims is what the model does: it validates without.
    assert VarianceReductionConfig(method="cuped").covariate_metric_id is None
    assert VarianceReductionConfig(method="cuped_plus").covariate_metric_id is None


@pytest.mark.regression
@pytest.mark.parametrize("copy", OPENAPI_COPIES, ids=lambda p: p.name)
def test_the_history_is_said_to_be_read_before_assignment_in_every_copy(copy):
    schema = _schema(copy)
    field = schema["properties"]["covariate_metric_id"]["description"]

    for text in (field, " ".join(schema["description"].split())):
        assert ASSIGNMENT_WORDING in text
        assert "exposure" not in text


@pytest.mark.regression
def test_the_history_is_said_to_be_read_before_assignment_in_the_code():
    field = VarianceReductionConfig.model_fields["covariate_metric_id"].description
    docstring = " ".join(VarianceReductionConfig.__doc__.split())

    for text in (field, docstring):
        assert ASSIGNMENT_WORDING in text
        assert "exposure" not in text
