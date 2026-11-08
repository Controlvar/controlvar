"""A cloned experiment keeps its analysis settings (#255).

``ExperimentService.clone_experiment`` copied the name, rules, variants and
metrics, but not the analysis settings: a clone of an experiment with Bayesian
analysis, sequential testing or CUPED came back with all three off. Now every
setting in ``CLONED_ANALYSIS_FIELDS`` is copied, each JSON value deep-copied so
the clone and its source never share a dict.

The last test classifies every column of ``Experiment``: a new column fails it
until it is named as one a clone copies or one it deliberately does not.

The same clone against a database, through the API, is in
``backend/tests/integration/api/test_clone_keeps_analysis_settings.py``.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from backend.app.models.experiment import (
    Experiment,
    ExperimentStatus,
    ExperimentType,
)
from backend.app.services.experiment_service import (
    CLONED_ANALYSIS_FIELDS,
    ExperimentService,
)

pytestmark = pytest.mark.unit


def _settings() -> dict:
    """A value for every analysis setting, none of them the column default."""
    return {
        "optimization_type": "thompson_sampling",
        "sequential_testing_enabled": True,
        "sequential_testing_method": "always_valid",
        "sequential_testing_config": {
            "method": "always_valid",
            "alpha": 0.1,
            "tau_squared": 0.002,
            "spending_function": "pocock",
        },
        "variance_reduction_config": {
            "method": "cuped",
            "covariate_metric_id": "pre_revenue",
            "covariate_lookback_days": 14,
            "winsorization_percentile": 95.0,
        },
        "bayesian_enabled": True,
        "bayesian_config": {
            "prior_family": "beta",
            "alpha": 2.0,
            "beta": 3.0,
            "loss_threshold": 0.01,
            "rope": [-0.01, 0.01],
            "credible_level": 0.9,
        },
        # #580: neither is the default (benjamini_hochberg, 0.95).
        "correction_method": "none",
        "confidence_level": 0.90,
    }


def _source(**values) -> Experiment:
    experiment = Experiment(
        id=uuid.uuid4(),
        name="Checkout button",
        description="d",
        hypothesis="h",
        experiment_type=ExperimentType.A_B,
        status=ExperimentStatus.COMPLETED,
        owner_id=uuid.uuid4(),
        tags=["checkout"],
        **values,
    )
    experiment.variants = []
    experiment.metric_definitions = []
    return experiment


def _clone(source: Experiment) -> Experiment:
    """The ``Experiment`` the clone adds to the session."""
    db = MagicMock()
    service = ExperimentService(db)
    with patch.object(service, "_experiment_to_dict", return_value={}):
        service.clone_experiment(source, uuid.uuid4())
    added = [c.args[0] for c in db.add.call_args_list]
    clones = [obj for obj in added if isinstance(obj, Experiment)]
    assert len(clones) == 1
    return clones[0]


@pytest.mark.regression
def test_a_clone_keeps_every_analysis_setting():
    settings = _settings()
    clone = _clone(_source(**settings))

    assert {name: getattr(clone, name) for name in settings} == settings
    assert clone.status == ExperimentStatus.DRAFT
    # The fixture covers every setting the clone copies, so none is untested.
    assert set(settings) == set(CLONED_ANALYSIS_FIELDS)


@pytest.mark.regression
def test_a_clone_of_an_active_experiment_is_a_draft_with_its_correction_settings():
    """#580: the settings lock once an experiment leaves draft, but a clone is a
    draft, so it starts with the source's choices and can still change them."""
    source = _source(correction_method="none", confidence_level=0.90)
    source.status = ExperimentStatus.ACTIVE
    clone = _clone(source)

    assert clone.status == ExperimentStatus.DRAFT
    assert (clone.correction_method, clone.confidence_level) == ("none", 0.90)


@pytest.mark.regression
def test_changing_the_clones_settings_leaves_the_source_alone():
    source = _source(**_settings())
    clone = _clone(source)

    for name in ("sequential_testing_config", "variance_reduction_config"):
        assert getattr(clone, name) is not getattr(source, name)
        getattr(clone, name)["method"] = "changed"
    clone.bayesian_config["rope"].append(0.5)
    clone.bayesian_config["alpha"] = 9.0

    assert source.sequential_testing_config == _settings()["sequential_testing_config"]
    assert source.variance_reduction_config == _settings()["variance_reduction_config"]
    assert source.bayesian_config == _settings()["bayesian_config"]


def test_a_clone_of_an_experiment_with_no_analysis_settings_has_none():
    clone = _clone(
        _source(
            optimization_type="fixed",
            sequential_testing_enabled=False,
            bayesian_enabled=False,
        )
    )

    assert clone.optimization_type == "fixed"
    assert clone.sequential_testing_enabled is False
    assert clone.bayesian_enabled is False
    for name in (
        "sequential_testing_method",
        "sequential_testing_config",
        "variance_reduction_config",
        "bayesian_config",
    ):
        assert getattr(clone, name) is None, name


@pytest.mark.regression
def test_a_clone_gets_a_generated_key_of_its_own():
    """The tracking API finds an experiment by key; a clone had none (#609)."""
    source = _source(key="checkout-button-abc123")
    clone = _clone(source)

    assert clone.key
    assert clone.key != source.key
    assert clone.key.startswith("copy-of-checkout-button-")


@pytest.mark.regression
def test_a_clone_carries_the_split_url_config_as_its_own_copy():
    """A clone of a split URL experiment had no split_url_config, so its start
    answered 400 (#1781). It carries the source's, deep-copied: changing the
    clone's leaves the source's alone."""
    config = {
        "variants": [
            {"name": "Control", "url": "https://h.example/a", "traffic_allocation": 50},
            {
                "name": "Treatment",
                "url": "https://h.example/b",
                "traffic_allocation": 50,
            },
        ],
        "cookie_ttl_days": 30,
    }
    source = _source(split_url_config=config)
    source.experiment_type = ExperimentType.SPLIT_URL
    clone = _clone(source)

    assert clone.split_url_config == config
    assert clone.split_url_config is not source.split_url_config
    assert clone.split_url_config["variants"] is not config["variants"]
    clone.split_url_config["variants"][0]["url"] = "https://h.example/changed"
    assert source.split_url_config["variants"][0]["url"] == "https://h.example/a"


# Copied by ``clone_experiment`` outside ``CLONED_ANALYSIS_FIELDS``.
COPIED_ELSEWHERE = {
    "name",  # as "Copy of <name>"
    "description",
    "hypothesis",
    "experiment_type",
    "targeting_rules",  # rules_for_clone (#533)
    "split_url_config",  # deep-copied, so a split URL clone can start (#1781)
    "tags",
}

# Not copied, each for a reason.
NOT_COPIED = {
    "id": "the clone's own",
    "key": "unique; the clone gets a generated key of its own (#609)",
    "status": "a clone starts in DRAFT",
    "owner_id": "the user who cloned it",
    "created_at": "the clone's own",
    "updated_at": "the clone's own",
    "start_date": "the source's schedule, not the clone's",
    "end_date": "the source's schedule, not the clone's",
    "resume_at": "the source's schedule, not the clone's",
    "bayesian_decision": "the source's result, not a setting",
    "experiment_metadata": "notes on the source's results",
    "metrics": "legacy list; the metric definitions are cloned as rows",
    "mutual_exclusion_group_id": "group membership is chosen per experiment",
    "workspace_id": "set by the workspaces module, not by core create",
}


def test_every_experiment_column_is_classified_for_clone():
    """A new ``Experiment`` column fails here until it is classified.

    An analysis setting belongs in ``CLONED_ANALYSIS_FIELDS``, so a clone
    keeps it; anything else goes in ``NOT_COPIED`` with the reason.
    """
    columns = {column.name for column in Experiment.__table__.columns}
    classified = set(CLONED_ANALYSIS_FIELDS) | COPIED_ELSEWHERE | set(NOT_COPIED)

    assert columns - classified == set(), "classify these columns for clone"
    assert classified - columns == set(), "these are not Experiment columns"
    overlap = set(CLONED_ANALYSIS_FIELDS) & (COPIED_ELSEWHERE | set(NOT_COPIED))
    assert overlap == set()


def test_every_analysis_column_is_copied():
    """The columns named like an analysis setting are all in the copied set."""
    analysis_prefixes = ("sequential_testing_", "bayesian_", "variance_reduction_")
    analysis = {
        column.name
        for column in Experiment.__table__.columns
        if column.name.startswith(analysis_prefixes)
        or column.name in ("optimization_type", "correction_method", "confidence_level")
    } - {"bayesian_decision"}

    assert analysis - set(CLONED_ANALYSIS_FIELDS) == set()
