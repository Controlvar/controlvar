"""Every field of ``PUT /api/v1/experiments/{id}`` is classified once (#848).

Outside draft the update route refuses, for every role, a request naming any
entry of ``FIXED_AFTER_DRAFT`` (``backend/app/api/v1/endpoints/experiments.py``),
the list it iterates. This file enumerates ``ExperimentUpdate.model_fields``
and puts each field in exactly one class:

* fixed after draft: these must be exactly the list's entries that are fields
  of the request;
* editable after draft, with the reason it may change while the experiment
  runs;
* never written by the update.

So a field added to the schema and left unclassified fails here, by name, and
so does a field dropped from the route's list. ``start_date`` and ``end_date``
are the list's only entries that are not fields: the schema drops them before
the route looks, and
``test_experiment_analysis_settings_fixed.py::test_start_date_and_end_date_in_a_body_change_nothing``
pins that a body naming them changes nothing.
"""

import pytest

from backend.app.api.v1.endpoints.experiments import FIXED_AFTER_DRAFT
from backend.app.schemas.experiment import ExperimentUpdate

pytestmark = [pytest.mark.unit]

#: Refused outside draft, for every role, a superuser included.
FIXED = frozenset(
    {
        "variants",
        "metrics",
        "correction_method",
        "confidence_level",
        "balance_within",
        "variance_reduction_config",
        "sequential_testing_enabled",
        "sequential_testing_config",
        "bayesian_enabled",
        "bayesian_config",
    }
)

#: Changeable outside draft (by a superuser; targeting by any role that may
#: update, while paused), and why that is allowed.
EDITABLE = {
    "name": "describes the experiment; no analysis reads it",
    "description": "describes the experiment; no analysis reads it",
    "hypothesis": "describes the experiment; no analysis reads it",
    "tags": "labels for finding the experiment; no analysis reads them",
    "targeting_rules": (
        "decides who may enter from now on, only while draft or paused (#523); "
        "people already assigned keep their variant"
    ),
    "experiment_type": (
        "not one of the analysis settings #848 fixes; a change to split_url is "
        "held to the variants (#393)"
    ),
    "optimization_type": (
        "out of scope of #848: how traffic is allocated, not how the results are judged"
    ),
    "split_url_config": (
        "out of scope of #848 (#1560): where each variant's visitors are sent"
    ),
}

#: Accepted by the schema, never written by the update.
NEVER_WRITTEN = {
    "status": (
        "a value equal to the stored one changes nothing, any other is 422; "
        "status moves through the lifecycle routes (#542)"
    ),
    "schedule": (
        "refused with 422 whatever its value; scheduling is "
        "PUT /api/v1/experiments/{experiment_id}/schedule (#595)"
    ),
}

#: The list's order decides which field the 400 names when a body sends
#: several: #580's six, then #448's balance_within, then #848's five, so a
#: request refused before #848 keeps the field its detail names.
ROUTE_ORDER = (
    "variants",
    "metrics",
    "start_date",
    "end_date",
    "correction_method",
    "confidence_level",
    "balance_within",
    "variance_reduction_config",
    "sequential_testing_enabled",
    "sequential_testing_config",
    "bayesian_enabled",
    "bayesian_config",
)

NOT_FIELDS = ("start_date", "end_date")


def test_every_update_field_is_classified_exactly_once():
    fields = set(ExperimentUpdate.model_fields)
    classes = {
        "FIXED": set(FIXED),
        "EDITABLE": set(EDITABLE),
        "NEVER_WRITTEN": set(NEVER_WRITTEN),
    }
    classified = set().union(*classes.values())

    unclassified = sorted(fields - classified)
    assert not unclassified, (
        f"ExperimentUpdate fields in no class: {unclassified}. Put each in "
        "FIXED (and FIXED_AFTER_DRAFT in experiments.py), EDITABLE with the "
        "reason it may change on a running experiment, or NEVER_WRITTEN."
    )
    assert not sorted(classified - fields), "classified names that are not fields"
    names = sorted(classes)
    for i, first in enumerate(names):
        for second in names[i + 1 :]:
            both = sorted(classes[first] & classes[second])
            assert not both, f"in both {first} and {second}: {both}"
    for name, reason in {**EDITABLE, **NEVER_WRITTEN}.items():
        assert reason.strip(), f"{name} has no reason"


def test_the_fixed_fields_are_exactly_the_routes_list():
    fields = set(ExperimentUpdate.model_fields)
    listed = {name for name in FIXED_AFTER_DRAFT if name in fields}

    assert not sorted(FIXED - listed), (
        f"fixed after draft but not refused by the route: {sorted(FIXED - listed)}"
    )
    assert not sorted(listed - FIXED), (
        f"refused by the route but not classified FIXED: {sorted(listed - FIXED)}"
    )


def test_the_routes_list_has_no_other_entry_that_is_not_a_field():
    fields = set(ExperimentUpdate.model_fields)

    assert tuple(n for n in FIXED_AFTER_DRAFT if n not in fields) == NOT_FIELDS


def test_the_routes_list_is_in_this_order_without_repeats():
    assert FIXED_AFTER_DRAFT == ROUTE_ORDER
    assert len(set(FIXED_AFTER_DRAFT)) == len(FIXED_AFTER_DRAFT)
