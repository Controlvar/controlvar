"""
The ruleset limits on ``POST /api/v1/segments/{segment_id}/preview``.

The body's rules are segment rules, checked by ``validate_segment_rules``
before the route runs (#440): the targeting rule format, one level of groups,
and at most

(a) 50 conditions,
(a') 20 groups,
(b) 10 ``regex`` conditions, and
(c) 1,000 list elements across condition values,

each answered 422 at ``["body", "rules"]`` with the validator's fixed text.
These are the preview's own limits (a) to (c) in
``backend.app.core.segment_preview_limits``, so every saved segment passes
them. The preview then refuses, before any database query,

(d) ``regex`` conditions x the requested ``sample_size`` above 500, and
(e) conditions plus groups x the requested ``sample_size`` above 50,000,

at ``["query", "sample_size"]``.

The endpoint tests run the real endpoint and the real
``AudienceService.preview_audience_size`` against a ``MagicMock`` session whose
exposure read returns no rows (an empty table). Every call made on the
session is recorded in ``session.mock_calls``, which is how the "no database
query" property is observed.
"""

import json
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import jsonschema
import pytest
from fastapi.testclient import TestClient

from backend.app.api import deps
from backend.app.core.segment_preview_limits import (
    MAX_PREVIEW_CONDITIONS,
    MAX_PREVIEW_GROUPS,
    MAX_PREVIEW_LIST_ELEMENTS,
    MAX_PREVIEW_NODE_EVALUATIONS,
    MAX_PREVIEW_REGEX_CONDITIONS,
    MAX_PREVIEW_REGEX_EVALUATIONS,
    count_ruleset,
    preview_ruleset_violations,
)
from backend.app.main import app
from backend.app.models.user import User, UserRole

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
STABLE_SNAPSHOT = REPO_ROOT / "docs" / "api" / "openapi-v1.stable.json"
PREVIEW_PATH = "/api/v1/segments/{segment_id}/preview"

# Present in the request body so a test can prove no message echoes it.
MARKER = "zq_request_marker_7c1f"


# ---------------------------------------------------------------------------
# Builders (the targeting rule format)
# ---------------------------------------------------------------------------


def _eq(i: int = 0) -> dict:
    return {"attribute": f"{MARKER}_attr_{i}", "operator": "equals", "value": MARKER}


def _regex(i: int = 0) -> dict:
    return {
        "attribute": f"{MARKER}_attr_{i}",
        "operator": "regex",
        "value": f"^{MARKER}[a-z]+$",
    }


def _in(n: int) -> dict:
    return {
        "attribute": "country",
        "operator": "in",
        "value": [f"{MARKER}{i}" for i in range(n)],
    }


def _groups(*conditions_per_group: list) -> dict:
    return {
        "logical_operator": "AND",
        "groups": [
            {"logical_operator": "OR", "conditions": list(conditions)}
            for conditions in conditions_per_group
        ],
    }


def _flat(conditions: list) -> dict:
    """One group holding ``conditions``."""
    return _groups(conditions)


def _across_groups(total: int) -> dict:
    """``total`` conditions in three groups: 5 in the first, the rest split."""
    first = min(total, 5)
    rest = total - first
    return _groups(
        [_eq(i) for i in range(first)],
        [_eq(i) for i in range(rest // 2)],
        [_eq(i) for i in range(rest - rest // 2)],
    )


def _with_groups(groups: int, per_group: int = 1) -> dict:
    """``groups`` groups of ``per_group`` conditions each."""
    return _groups(*[[_eq(i) for i in range(per_group)] for _ in range(groups)])


def _viewer() -> User:
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.username = "viewer"
    user.email = "viewer@example.com"
    user.is_active = True
    user.is_superuser = False
    user.role = UserRole.VIEWER
    return user


def _empty_table_session() -> MagicMock:
    """A session whose segment lookup finds a row and whose exposure read is empty.

    The read is ``session.execute(...)``, iterated: a ``MagicMock`` iterates
    as empty.
    """
    session = MagicMock()
    session.execute.return_value.__iter__.return_value = iter([])
    return session


@pytest.fixture
def preview():
    """POST a preview as a VIEWER; returns (response, session)."""
    app.dependency_overrides.clear()
    client = TestClient(app)

    def _post(rules: dict, sample_size: int | None = None):
        session = _empty_table_session()
        app.dependency_overrides[deps.get_current_active_user] = _viewer
        app.dependency_overrides[deps.get_db] = lambda: session
        url = f"/api/v1/segments/{uuid.uuid4()}/preview"
        if sample_size is not None:
            url += f"?sample_size={sample_size}"
        response = client.post(url, json={"name": "Preview", "rules": rules})
        return response, session

    yield _post
    app.dependency_overrides.clear()


def _assert_refused(response, loc: list) -> list:
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail, detail
    assert all(item["type"] == "value_error" for item in detail), detail
    assert loc in [item["loc"] for item in detail], detail
    return detail


def _assert_previewed(response) -> None:
    assert response.status_code == 200, response.text
    body = response.json()
    # The empty table: the real service ran and sampled nothing.
    assert body == {
        "estimated_percentage": None,
        "sample_size": 0,
        "matched": 0,
        "source": "exposure_metadata",
        "window_days": 30,
        "sampled_since": None,
        "ended_by": "window",
        "users_without_context": 0,
        "reason": "no_exposures_with_context",
    }


# ---------------------------------------------------------------------------
# (b) regex conditions
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_eleven_regex_conditions_are_refused(preview):
    rules = _flat([_regex(i) for i in range(MAX_PREVIEW_REGEX_CONDITIONS + 1)])
    # sample_size 10 keeps (d) at 110, so only (b) applies.
    response, _ = preview(rules, sample_size=10)
    detail = _assert_refused(response, ["body", "rules"])
    assert len(detail) == 1
    assert detail[0]["msg"] == (
        "Value error, rules: at most 10 regex conditions are allowed"
    )


@pytest.mark.regression
def test_ten_regex_conditions_with_sample_size_45_are_previewed(preview):
    # 10 conditions + 1 group = 11 nodes; (d) 10 x 45 = 450.
    rules = _flat([_regex(i) for i in range(MAX_PREVIEW_REGEX_CONDITIONS)])
    response, _ = preview(rules, sample_size=45)
    _assert_previewed(response)


# ---------------------------------------------------------------------------
# (a) conditions in total, across groups
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_51_conditions_split_across_groups_are_refused(preview):
    rules = _across_groups(MAX_PREVIEW_CONDITIONS + 1)
    # Nearly all of them sit outside the first group.
    assert len(rules["groups"][0]["conditions"]) == 5
    response, _ = preview(rules)
    detail = _assert_refused(response, ["body", "rules"])
    assert detail[0]["msg"] == "Value error, rules: at most 50 conditions are allowed"


@pytest.mark.regression
def test_50_conditions_split_across_groups_are_previewed(preview):
    # 50 conditions + 3 groups = 53 nodes; at sample_size 943, 49,979 (e).
    response, _ = preview(_across_groups(MAX_PREVIEW_CONDITIONS), 943)
    _assert_previewed(response)


# ---------------------------------------------------------------------------
# (c) list elements across condition values
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_1001_list_elements_are_refused(preview):
    response, _ = preview(_groups([_in(500)], [_in(501)]))
    detail = _assert_refused(response, ["body", "rules"])
    assert detail[0]["msg"] == (
        "Value error, rules: at most 1000 list values are allowed in total"
    )


@pytest.mark.regression
def test_1000_list_elements_are_previewed(preview):
    rules = _groups([_in(500)], [_in(MAX_PREVIEW_LIST_ELEMENTS - 500)])
    response, _ = preview(rules)
    _assert_previewed(response)


# ---------------------------------------------------------------------------
# (d) regex conditions x the requested sample_size
# ---------------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("patterns,sample_size", [(1, 501), (3, 167), (7, 72)])
def test_regex_conditions_times_sample_size_over_500_are_refused(
    preview, patterns, sample_size
):
    assert patterns * sample_size > MAX_PREVIEW_REGEX_EVALUATIONS
    rules = _flat([_regex(i) for i in range(patterns)])
    # The table is empty: the refusal must come from the REQUESTED sample_size.
    response, _ = preview(rules, sample_size=sample_size)
    detail = _assert_refused(response, ["query", "sample_size"])
    assert len(detail) == 1
    assert f"= {patterns * sample_size}" in detail[0]["msg"]


@pytest.mark.regression
@pytest.mark.parametrize("patterns,sample_size", [(1, 500), (5, 100), (10, 45)])
def test_regex_conditions_times_sample_size_of_500_are_previewed(
    preview, patterns, sample_size
):
    rules = _flat([_regex(i) for i in range(patterns)])
    response, _ = preview(rules, sample_size=sample_size)
    _assert_previewed(response)


def test_the_default_sample_size_counts_for_d(preview):
    # One pattern with the default sample_size of 1000 is 1000 > 500.
    response, _ = preview(_flat([_regex()]))
    _assert_refused(response, ["query", "sample_size"])


# ---------------------------------------------------------------------------
# (a') groups in total
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_21_groups_are_refused(preview):
    response, _ = preview(_with_groups(MAX_PREVIEW_GROUPS + 1))
    detail = _assert_refused(response, ["body", "rules"])
    assert len(detail) == 1
    assert detail[0]["msg"] == "Value error, groups: at most 20 groups are allowed"


@pytest.mark.regression
def test_20_groups_are_previewed(preview):
    response, _ = preview(_with_groups(MAX_PREVIEW_GROUPS))
    _assert_previewed(response)


@pytest.mark.regression
def test_a_group_nested_inside_another_is_refused(preview):
    """Segment rules have one level of groups; nesting is the native shape."""
    rules = {"groups": [{"groups": [{"conditions": [_eq()]}]}]}
    response, session = preview(rules)
    _assert_refused(response, ["body", "rules"])
    assert session.mock_calls == []


@pytest.mark.regression
def test_an_empty_group_is_refused(preview):
    response, _ = preview(_groups([_eq()], []))
    detail = _assert_refused(response, ["body", "rules"])
    assert detail[0]["msg"] == (
        "Value error, groups[1].conditions: at least one condition is required"
    )


# ---------------------------------------------------------------------------
# (e) conditions plus groups x the requested sample_size
# ---------------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize(
    "groups,sample_size",
    # 6 + 6 = 12 nodes x 4167 = 50,004; 20 + 20 = 40 nodes x 1251 = 50,040.
    [(6, 4167), (20, 1251)],
)
def test_nodes_times_sample_size_over_50000_are_refused(preview, groups, sample_size):
    conditions = groups  # one condition per group
    nodes = conditions + groups
    assert nodes * sample_size > MAX_PREVIEW_NODE_EVALUATIONS
    # Conditions alone stay inside the limit: only counting the groups as
    # well refuses these.
    assert conditions * sample_size <= MAX_PREVIEW_NODE_EVALUATIONS
    response, _ = preview(_with_groups(groups), sample_size)
    detail = _assert_refused(response, ["query", "sample_size"])
    assert len(detail) == 1
    assert f"= {nodes * sample_size}" in detail[0]["msg"]
    largest = MAX_PREVIEW_NODE_EVALUATIONS // nodes
    assert f"sample_size of {largest} or less" in detail[0]["msg"]


@pytest.mark.regression
@pytest.mark.parametrize(
    "groups,per_group,sample_size",
    # 20 + 20 = 40 x 1250; 1 + 49 = 50 at the default 1000; 1 + 4 x 10,000.
    [(20, 1, 1250), (1, 49, None), (1, 4, 10000)],
)
def test_nodes_times_sample_size_of_50000_are_previewed(
    preview, groups, per_group, sample_size
):
    response, _ = preview(_with_groups(groups, per_group), sample_size)
    _assert_previewed(response)


def test_the_largest_segment_needs_a_smaller_sample(preview):
    """50 conditions in one group are 51 nodes: the default 1000 is over (e)."""
    response, _ = preview(_with_groups(1, MAX_PREVIEW_CONDITIONS))
    detail = _assert_refused(response, ["query", "sample_size"])
    assert "sample_size of 980 or less" in detail[0]["msg"]


def test_no_node_sample_size_suggestion_below_the_endpoint_minimum():
    errors = preview_ruleset_violations(_flat([_eq(i) for i in range(6000)]), 10)
    node_error = errors[-1]
    assert node_error["loc"] == ["query", "sample_size"]
    assert "sample_size of" not in node_error["msg"]
    assert "fewer conditions and groups" in node_error["msg"]


# ---------------------------------------------------------------------------
# A refusal issues no database query
# ---------------------------------------------------------------------------

REFUSED = [
    (_flat([_regex(i) for i in range(11)]), 10),
    (_across_groups(51), None),
    (_flat([_in(1001)]), None),
    (_flat([_regex()]), 501),
    (_with_groups(21), None),
    (_with_groups(6), 4167),
]
REFUSED_IDS = ["b-regex", "a-conditions", "c-list", "d-sample", "a2-groups", "e-nodes"]


@pytest.mark.regression
@pytest.mark.parametrize("rules,sample_size", REFUSED, ids=REFUSED_IDS)
def test_a_refusal_issues_no_database_query(preview, rules, sample_size):
    response, session = preview(rules, sample_size)
    assert response.status_code == 422, response.text
    assert session.mock_calls == []


def test_the_session_probe_sees_the_queries_of_an_accepted_preview(preview):
    """The probe above is live: an accepted preview does query the session."""
    response, session = preview(_flat([_eq()]))
    _assert_previewed(response)
    queried = [c for c in session.mock_calls if c[0] in ("query", "execute")]
    # The segment, then the exposures.
    assert [c[0] for c in queried] == ["query", "execute"], session.mock_calls


# ---------------------------------------------------------------------------
# The 422 body is the declared HTTPValidationError
# ---------------------------------------------------------------------------


def _http_validation_error_schema() -> dict:
    snapshot = json.loads(STABLE_SNAPSHOT.read_text())
    operation = snapshot["paths"][PREVIEW_PATH]["post"]
    declared = operation["responses"]["422"]["content"]["application/json"]["schema"]
    assert declared == {"$ref": "#/components/schemas/HTTPValidationError"}
    return {"components": snapshot["components"], **declared}


def _every_limit() -> dict:
    """Rules over (a), (a'), (b) and (c) at once."""
    return _groups(
        [_in(1001)] + [_regex(i) for i in range(11)],
        *[[_eq(i), _eq(i + 1), _eq(i + 2)] for i in range(MAX_PREVIEW_GROUPS)],
    )


@pytest.mark.regression
@pytest.mark.parametrize(
    "rules,sample_size",
    REFUSED + [(_every_limit(), 1000)],
    ids=REFUSED_IDS + ["all"],
)
def test_the_refusal_matches_the_snapshot_and_echoes_no_request_content(
    preview, rules, sample_size
):
    response, _ = preview(rules, sample_size)
    assert response.status_code == 422, response.text
    body = response.json()
    jsonschema.validate(body, _http_validation_error_schema())
    for item in body["detail"]:
        # ``ctx`` is kept for a validator's own error (validation_errors.py).
        assert set(item) - {"ctx"} == {"loc", "msg", "type"}
        assert MARKER not in json.dumps(item)
        assert item["loc"] in (["body", "rules"], ["query", "sample_size"])


def test_the_preview_function_still_reports_every_limit_at_once():
    """The route stops at the rules check; the preview's own check lists all."""
    errors = preview_ruleset_violations(_every_limit(), 1000)
    assert [item["loc"] for item in errors] == [
        ["body", "rules"],  # (a) conditions
        ["body", "rules"],  # (a') groups
        ["body", "rules"],  # (b) match_regex
        ["body", "rules"],  # (c) list elements
        ["query", "sample_size"],  # (d)
        ["query", "sample_size"],  # (e)
    ]


# ---------------------------------------------------------------------------
# The counting, directly
# ---------------------------------------------------------------------------


def test_counts_the_dashboard_shape_and_the_regex_alias():
    rules = {
        "logical_operator": "AND",
        "groups": [
            {
                "logical_operator": "OR",
                "conditions": [
                    {"attribute": "email", "operator": "regex", "value": "x"},
                    {"attribute": "email", "operator": "MATCH_REGEX", "value": "y"},
                    {"attribute": "plan", "operator": "in", "value": ["a", "b"]},
                ],
            }
        ],
    }
    assert count_ruleset(rules) == {
        "conditions": 3,
        "groups": 1,
        "regex_conditions": 2,
        "list_elements": 2,
    }


def test_counts_nested_lists_and_additional_value():
    condition = {
        "attribute": "a",
        "operator": "between",
        "value": [[1, 2], [3]],  # 2 outer + 3 inner
        "additional_value": {"k": [4, 5]},  # 2
    }
    assert count_ruleset({"conditions": [condition]})["list_elements"] == 7


def test_malformed_items_still_count_as_conditions():
    rules = {"conditions": ["x", 1, None, {"operator": 5}], "groups": ["y", None]}
    assert count_ruleset(rules) == {
        "conditions": 4,
        "groups": 2,
        "regex_conditions": 0,
        "list_elements": 0,
    }


@pytest.mark.parametrize("rules", [None, [], "rules", 3, {"conditions": "x"}])
def test_rules_that_are_not_a_group_count_as_empty(rules):
    assert preview_ruleset_violations(rules, 10_000) == []


def test_deep_nesting_is_counted_without_recursion():
    rules: dict = {"conditions": [_regex()]}
    for _ in range(5000):
        rules = {"groups": [rules]}
    assert count_ruleset(rules)["regex_conditions"] == 1


def test_the_largest_accepted_sample_size_is_suggested():
    [error] = preview_ruleset_violations(_flat([_regex()] * 3), 200)
    assert "sample_size of 166 or less" in error["msg"]


def test_no_sample_size_suggestion_below_the_endpoint_minimum():
    errors = preview_ruleset_violations(_flat([_regex()] * 60), 10)
    assert "sample_size of" not in errors[-1]["msg"]
    assert errors[-1]["loc"] == ["query", "sample_size"]
