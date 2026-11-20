"""The new-experiment form's split URL request is one the API accepts (#1961).

``frontend/src/tests/fixtures/split-url-create.json`` holds the create request
the dashboard's form sends for a split URL experiment, case by case, and the
URLs the client must never refuse. The form's own test
(``frontend/src/tests/pages/experiment-new-split-url.test.tsx``) sends each case
through both views and compares the body with the file. This file holds the same
bodies to the API's rules, so a body the form could send but the API would
refuse fails here, and so does an API that grows stricter about a URL the form
accepts.

Core profile: nothing here needs ``modules/``. ``ExperimentCreate`` and
``find_split_url_config_problem`` are in ``backend/app``; the 501 a core API
answers to a split URL create comes later, in the route.
"""

from __future__ import annotations

import copy
import json
import pathlib
from typing import Any, Dict, List

import pytest

from backend.app.schemas.experiment import ExperimentCreate
from backend.app.schemas.split_url_config import (
    DESTINATION_IS_ENTRY,
    UNKNOWN_URL_VARIANT,
    find_split_url_config_problem,
    url_problem,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
FIXTURE = (
    REPO_ROOT / "frontend" / "src" / "tests" / "fixtures" / "split-url-create.json"
)

_DATA: Dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
CASES: List[Dict[str, Any]] = _DATA["cases"]
ACCEPTED_URLS: List[str] = _DATA["accepted_urls"]


def _problem(body: Dict[str, Any]):
    return find_split_url_config_problem(body["split_url_config"], body["variants"])


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_the_form_body_passes_the_api_schema_and_cross_field_rules(case):
    body = case["body"]
    assert body["experiment_type"] == "split_url"
    ExperimentCreate.model_validate(body)
    assert _problem(body) is None


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_each_case_types_the_urls_the_body_carries(case):
    sent = [v["url"] for v in case["body"]["split_url_config"]["variants"]]
    assert sent == case["urls"], "the body's URLs are the typed ones, exactly as typed"
    names = [v["name"] for v in case["body"]["variants"]]
    assert [v["name"] for v in case["body"]["split_url_config"]["variants"]] == names
    assert [
        v["traffic_allocation"] for v in case["body"]["split_url_config"]["variants"]
    ] == [v["traffic_allocation"] for v in case["body"]["variants"]]


def test_a_treatment_url_equal_to_the_controls_is_refused():
    """Positive control: the check above is not vacuous."""
    body = copy.deepcopy(CASES[0]["body"])
    body["split_url_config"]["variants"][1]["url"] = body["split_url_config"][
        "variants"
    ][0]["url"]
    problem = _problem(body)
    assert problem is not None
    assert problem.message == DESTINATION_IS_ENTRY.format(name="Treatment")


def test_a_renamed_url_variant_is_refused():
    """Positive control: a URL variant must name an experiment variant."""
    body = copy.deepcopy(CASES[0]["body"])
    body["split_url_config"]["variants"][1]["name"] = "Treatmnt"
    problem = _problem(body)
    assert problem is not None
    assert problem.message == UNKNOWN_URL_VARIANT.format(
        name="Treatmnt", names="Control, Treatment"
    )


@pytest.mark.parametrize("url", ACCEPTED_URLS)
def test_the_api_accepts_every_url_the_client_must_not_refuse(url):
    assert url_problem(url) is None


def test_the_accepted_list_holds_the_urls_where_the_client_and_url_parsers_differ():
    """The eight a WHATWG URL parser refuses and the API accepts."""
    for url in (
        "https://a<b.example/x",
        "https://a^b.example/x",
        "https://a|b.example/x",
        "https://a>b.example/x",
        "https://1.2.3.256/x",
        "https://999999999999/x",
        "https://example.1/x",
        "https://foo.0x/x",
    ):
        assert url in ACCEPTED_URLS
