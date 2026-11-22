"""
Assignment and flag evaluation never log the user id, at any level (#269).

Every per-request path in ``AssignmentService`` -- a new assignment, a sticky
one, an ineligible user, targeting assignment, a targeting error, a
reassignment, and a linked id's new and sticky answers (#449) -- the batch
assignment route's two failure lines (#441), ``identity_aliases.link``
storing and refusing a link (#449), the alias route storing, refusing and
failing to store a link and refusing two equal ids (#449), and the flag
service's two error lines are driven with a distinctive user id. For each:

* no record, at any level, contains the user id;
* the routine per-request lines are at DEBUG, not INFO;
* an error line carries the exception's type, not its text (the text can
  repeat the user id or an attribute value);
* at least one record was captured (a positive control: a capture that records
  nothing would otherwise pass everything above).

Capture replaces each module's ``logger`` with a recorder that keeps every call
at every level, as ``backend/tests/unit/core/test_evaluation_logs_no_attribute_values.py``
does, and for the same reason: caplog would miss DEBUG records once
``setup_logging()`` has set the root level to INFO.
"""

from __future__ import annotations

import asyncio
import traceback
from types import SimpleNamespace
from typing import List, Tuple
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from backend.app.models.experiment import Experiment, ExperimentStatus
from backend.app.models.feature_flag import FeatureFlagStatus
from backend.app.services import assignment_service as asvc
from backend.app.services import feature_flag_service as ffs
from backend.app.services import identity_aliases

pytestmark = [pytest.mark.unit, pytest.mark.regression]

USER = "alice.user269@example.com"
LEVELS = ("debug", "info", "warning", "error", "critical")

NATIVE_US_ONLY = {
    "version": "1.0",
    "rules": [
        {
            "id": "us_only",
            "rule": {
                "operator": "and",
                "conditions": [
                    {"attribute": "country", "operator": "eq", "value": "US"}
                ],
            },
            "rollout_percentage": 100,
            "priority": 1,
        }
    ],
}


class Recorder:
    """Stands in for a module logger and keeps every call, formatted, at every level."""

    def __init__(self) -> None:
        self.records: List[Tuple[str, str]] = []

    def bind(self) -> SimpleNamespace:
        def at(level: str):
            def log(message, *args, **kwargs):
                text = message % args if args else str(message)
                if kwargs.get("exc_info"):
                    text += "\n" + traceback.format_exc()
                if kwargs.get("extra"):
                    text += " " + repr(kwargs["extra"])
                self.records.append((level, text))

            return log

        methods = {level: at(level) for level in LEVELS}
        methods["warn"] = methods["warning"]
        methods["exception"] = lambda message, *a, **k: at("error")(
            message, *a, exc_info=True
        )
        methods["isEnabledFor"] = lambda _level: True
        return SimpleNamespace(**methods)

    def with_user(self) -> List[Tuple[str, str]]:
        return [r for r in self.records if USER in r[1]]

    def at(self, *levels: str) -> List[Tuple[str, str]]:
        return [r for r in self.records if r[0] in levels]


@pytest.fixture
def logs(monkeypatch) -> Recorder:
    recorder = Recorder()
    for module in (asvc, ffs):
        monkeypatch.setattr(module, "logger", recorder.bind())
    return recorder


def _variant(name: str, is_control: bool):
    variant = MagicMock()
    variant.id = uuid4()
    variant.name = name
    variant.is_control = is_control
    variant.traffic_allocation = 50
    return variant


@pytest.fixture
def experiment():
    exp = MagicMock(spec=Experiment)
    exp.id = uuid4()
    exp.status = ExperimentStatus.ACTIVE
    exp.mutual_exclusion_group_id = None
    exp.targeting_rules = None
    exp.variants = [_variant("control", True), _variant("treatment", False)]
    return exp


def _service(experiment, existing=None):
    db = MagicMock()
    db.query.return_value.options.return_value.filter.return_value.first.return_value = experiment
    db.query.return_value.filter.return_value.order_by.return_value.first.return_value = existing
    db.query.return_value.filter.return_value.first.return_value = existing
    svc = asvc.AssignmentService(db)
    svc.event_service = MagicMock()
    svc.global_holdout_service = MagicMock()
    svc.global_holdout_service.is_user_in_holdout.return_value = (False, 0, 57)
    svc.mutual_exclusion_service = MagicMock()
    svc.mutual_exclusion_service.is_user_eligible_for_experiment.return_value = True
    return svc


def _assert_clean(logs: Recorder, *, routine: bool) -> None:
    assert logs.records, "nothing was captured: the recorder is not wired in"
    assert logs.with_user() == [], logs.with_user()
    if routine:
        assert logs.at("info", "warning", "error", "critical") == [], logs.records


def test_new_assignment(logs, experiment):
    svc = _service(experiment)
    treatment = experiment.variants[1]
    with (
        patch.object(svc, "_assigned_result", return_value={}),
        patch.object(svc, "_hash_user_to_variant", return_value=treatment.id),
    ):
        svc.assign_user(USER, experiment.id)
    _assert_clean(logs, routine=True)


def test_sticky_assignment(logs, experiment):
    svc = _service(experiment, existing=MagicMock(variant_id=uuid4()))
    with patch.object(svc, "_assigned_result", return_value={}):
        svc.assign_user(USER, experiment.id)
    _assert_clean(logs, routine=True)


#: The known id USER is linked to, in the linked-person cases (#449).
KNOWN = "bob.known449@example.com"


def _linked(monkeypatch, row=None):
    """USER linked to KNOWN; the holder is KNOWN with no row, or USER with
    *row*."""
    person = identity_aliases.Person(KNOWN, (KNOWN, USER))
    monkeypatch.setattr(asvc, "person_of", lambda db, user_id: person)
    holder = (KNOWN, None) if row is None else (USER, row)
    monkeypatch.setattr(asvc, "holder_of", lambda db, p, experiment_id: holder)


def _assert_no_id(records) -> None:
    assert records, "nothing was captured: the recorder is not wired in"
    leaked = [r for r in records if USER in r[1] or KNOWN in r[1]]
    assert leaked == [], leaked


def test_linked_new_assignment(logs, experiment, monkeypatch):
    """IA-P1: a linked id assigned under its known id."""
    _linked(monkeypatch)
    svc = _service(experiment)
    with (
        patch.object(svc, "_assigned_result", return_value={}),
        patch.object(svc, "_hash_user_to_variant", return_value=uuid4()),
    ):
        svc.assign_user(USER, experiment.id)
    _assert_no_id(logs.records)
    _assert_clean(logs, routine=True)


def test_linked_sticky_assignment(logs, experiment, monkeypatch):
    """IA-P1: a linked id answered with the row its person holds."""
    _linked(monkeypatch, row=MagicMock(variant_id=uuid4()))
    svc = _service(experiment)
    with patch.object(svc, "_assigned_result", return_value={}):
        svc.assign_user(KNOWN, experiment.id)
    _assert_no_id(logs.records)
    _assert_clean(logs, routine=True)


class _LinkSession:
    """A session for ``identity_aliases.link``: the two hashes, then the
    anonymous id's current link (``linked_to``), no other link, no count."""

    def __init__(self, linked_to=None):
        self.linked_to = linked_to

    def execute(self, *args, **kwargs):
        result = MagicMock()
        result.one.return_value = (11, 7)
        result.scalar_one_or_none.return_value = self.linked_to
        result.first.return_value = None
        result.scalar_one.return_value = 0
        return result

    def commit(self):
        pass

    def rollback(self):
        pass


def test_linking_logs_neither_id(monkeypatch):
    """IA-P1: a link stored, and one refused (its rule's name at DEBUG)."""
    recorder = Recorder()
    monkeypatch.setattr(identity_aliases, "logger", recorder.bind())

    assert identity_aliases.link(_LinkSession(), USER, KNOWN) is True
    with pytest.raises(identity_aliases.AliasConflict) as refused:
        identity_aliases.link(_LinkSession(linked_to="someone-else"), USER, KNOWN)

    assert USER not in str(refused.value) and KNOWN not in str(refused.value)
    _assert_no_id(recorder.records)
    assert [r[0] for r in recorder.records] == ["debug"], recorder.records


def test_ineligible_user(logs, experiment):
    svc = _service(experiment)
    svc.global_holdout_service.is_user_in_holdout.return_value = (True, 10, 3)
    result = svc.assign_user(USER, experiment.id)
    assert result["assigned"] is False
    _assert_clean(logs, routine=True)


def test_targeting_assignment_and_ineligible(logs, experiment):
    svc = _service(experiment)
    treatment = experiment.variants[1]
    with (
        patch.object(svc, "get_assignment", return_value={}),
        patch.object(svc, "_hash_user_to_variant", return_value=treatment.id),
    ):
        svc.assign_user_with_targeting(USER, experiment.id, {})
    with patch.object(
        svc,
        "_evaluate_experiment_targeting",
        return_value={"eligible": False, "reason": f"no match for {USER}"},
    ):
        svc.assign_user_with_targeting(USER, experiment.id, {})
    _assert_clean(logs, routine=True)


def test_sticky_targeting_assignment(logs, experiment):
    svc = _service(experiment, existing=MagicMock(variant_id=uuid4()))
    with patch.object(svc, "get_assignment", return_value={}):
        svc.assign_user_with_targeting(USER, experiment.id, {})
    _assert_clean(logs, routine=True)


def test_targeting_error_logs_the_type_not_the_text(logs, experiment):
    experiment.targeting_rules = NATIVE_US_ONLY
    svc = _service(experiment)
    svc.rules_evaluation_service = MagicMock()
    svc.rules_evaluation_service.evaluate_rules_with_validation.side_effect = (
        ValueError(f"attribute value {USER} is not valid")
    )
    result = svc._evaluate_experiment_targeting(experiment, {"user_id": USER}, True)
    assert result["eligible"] is False
    _assert_clean(logs, routine=False)
    assert [r[0] for r in logs.records] == ["error"], logs.records
    assert "ValueError" in logs.records[0][1]


@pytest.fixture
def route_logs(monkeypatch) -> Recorder:
    """The batch route's logger, recorded: message with its args formatted,
    and the traceback whenever ``exc_info`` is passed or ``exception`` used."""
    from backend.app.api.v1.endpoints import tracking

    recorder = Recorder()
    monkeypatch.setattr(tracking, "logger", recorder.bind())
    return recorder


def _run_batch_failing_with(monkeypatch, exc):
    """Drive POST /tracking/assign/batch to a failure raising *exc* for USER."""
    from fastapi import HTTPException

    from backend.app.api.v1.endpoints import tracking
    from backend.app.schemas.tracking import AssignmentBatchRequest

    exp = MagicMock(spec=Experiment)
    exp.id = uuid4()
    exp.key = "batch-269"
    exp.optimization_type = "fixed"
    exp.variants = [_variant("control", True), _variant("treatment", False)]
    for variant in exp.variants:
        variant.configuration = {}
    db = MagicMock()
    # The route's query leaves out the visual columns with options (#1545).
    db.query.return_value.options.return_value.filter.return_value.first.return_value = exp

    class FailingService:
        def __init__(self, _db):
            pass

        def assign_user(self, **_kwargs):
            raise exc

    monkeypatch.setattr(tracking, "AssignmentService", FailingService)
    request = AssignmentBatchRequest(
        experiment_key=exp.key,
        users=[{"user_id": "first-user"}, {"user_id": USER, "context": {"e": USER}}],
    )
    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            tracking.assign_users_to_experiment_batch(
                request=request, db=db, api_key=MagicMock()
            )
        )
    return caught.value


@pytest.mark.parametrize(
    "exc, status, level",
    [
        (
            RuntimeError(f"(psycopg2) [parameters: {{'user_id': '{USER}'}}]"),
            500,
            "error",
        ),
        (ValueError(f"Override variant not valid for {USER}"), 409, "warning"),
    ],
    ids=["database-error", "value-error"],
)
def test_batch_route_failure_logs_the_class_not_the_text(
    route_logs, monkeypatch, exc, status, level
):
    assert USER in str(exc)  # the probe: the text does carry it
    error = _run_batch_failing_with(monkeypatch, exc)
    assert error.status_code == status
    assert USER not in str(error.detail)
    assert route_logs.records, "nothing was captured: the recorder is not wired in"
    assert route_logs.with_user() == [], route_logs.with_user()
    assert [r[0] for r in route_logs.records] == [level], route_logs.records
    assert type(exc).__name__ in route_logs.records[0][1]


def test_reassignment(logs, experiment):
    svc = _service(experiment, existing=MagicMock())
    with (
        patch.object(svc, "get_assignment", return_value={}),
        patch.object(svc, "_hash_user_to_variant", return_value=uuid4()),
    ):
        svc.reassign_user(USER, experiment.id, track_exposure=False)
    _assert_clean(logs, routine=False)


def _flag():
    return SimpleNamespace(
        id="00000000-0000-0000-0000-000000000269",
        key="no-user-id",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=100,
        targeting_rules=None,
    )


def test_flag_evaluation_error_logs_the_type_not_the_text(logs, monkeypatch):
    metrics = MagicMock()
    monkeypatch.setattr(ffs, "MetricsService", metrics)
    service = ffs.FeatureFlagService(db=MagicMock())
    with patch.object(
        service,
        "_match_targeting_rule",
        side_effect=ValueError(f"cannot evaluate for {USER}"),
    ):
        result = service.evaluate_flag_detailed(_flag(), USER, {})
    assert result["reason"] == "error"
    _assert_clean(logs, routine=False)
    errors = logs.at("error")
    assert len(errors) == 1 and "ValueError" in errors[0][1], logs.records
    # The operator's record of the failure keeps the full text, in error_logs.
    assert USER in metrics.log_error.call_args.kwargs["data"].message


def test_flag_metrics_failure_logs_the_type_not_the_text(logs, monkeypatch):
    metrics = MagicMock()
    metrics.record_flag_evaluation.side_effect = RuntimeError(
        f"[parameters: {{'user_id': '{USER}'}}]"
    )
    monkeypatch.setattr(ffs, "MetricsService", metrics)
    service = ffs.FeatureFlagService(db=MagicMock())
    service.evaluate_flag_detailed(_flag(), USER, {})
    _assert_clean(logs, routine=False)
    errors = logs.at("error")
    assert len(errors) == 1 and "RuntimeError" in errors[0][1], logs.records


# -- exposure tracking and the evaluation cache --------------------------------


def _integrity_error():
    from sqlalchemy.exc import IntegrityError

    return IntegrityError(
        "INSERT INTO events (user_id) VALUES (%(user_id)s)",
        {"user_id": USER},
        Exception("duplicate key value violates unique constraint"),
    )


def test_exposure_write_failure_logs_the_type_not_the_parameters(monkeypatch):
    """Assignment reaches this through track_exposure."""
    from backend.app.services import event_service as evs

    recorder = Recorder()
    monkeypatch.setattr(evs, "logger", recorder.bind())
    db = MagicMock()
    db.commit.side_effect = _integrity_error()
    assert USER in str(db.commit.side_effect)  # the probe: the text does carry it
    service = evs.EventService(db)

    with pytest.raises(Exception):
        service.track_exposure(
            user_id=USER, experiment_id=str(uuid4()), variant_id=str(uuid4())
        )
    with pytest.raises(Exception):
        service.track_events_batch(
            [
                {
                    "user_id": USER,
                    "experiment_id": str(uuid4()),
                    "event_type": "purchase",
                    "event_name": "purchase",
                }
            ]
        )

    assert recorder.records, "nothing was captured"
    assert recorder.with_user() == [], recorder.with_user()
    assert [r[0] for r in recorder.records] == ["error", "error"], recorder.records
    assert all("IntegrityError" in r[1] for r in recorder.records), recorder.records


def test_evaluation_cache_invalidation_does_not_log_the_user(monkeypatch):
    from backend.app.core import evaluation_cache as ec

    recorder = Recorder()
    monkeypatch.setattr(ec, "logger", recorder.bind())
    ec.EvaluationCache().invalidate_user(USER)
    assert recorder.records, "nothing was captured"
    assert recorder.with_user() == [], recorder.with_user()


# ---------------------------------------------------------------------------
# POST /api/v1/tracking/alias (#449): stored, refused, failed and equal ids
# ---------------------------------------------------------------------------


class _FailingLinkSession(_LinkSession):
    """A session for ``identity_aliases.link`` whose insert fails as the
    driver fails, with both ids in the error's text and parameters."""

    def execute(self, statement, *args, **kwargs):
        from sqlalchemy.exc import IntegrityError

        if str(statement).lstrip().upper().startswith("INSERT"):
            raise IntegrityError(
                "INSERT INTO identity_aliases (anonymous_id, user_id) VALUES (...)",
                {"anonymous_id": USER, "user_id": KNOWN},
                Exception(f"Key (anonymous_id)=({USER}) conflicts with ({KNOWN})"),
            )
        return super().execute(statement, *args, **kwargs)


def _alias_handler(session):
    """Run the alias route's handler for USER and KNOWN on *session*, with the
    real ``identity_aliases.link``; the answer, or the HTTPException raised."""
    from fastapi import HTTPException

    from backend.app.api.v1.endpoints import tracking
    from backend.app.schemas.tracking import AliasRequest

    request = AliasRequest(anonymous_id=USER, user_id=KNOWN)
    try:
        return asyncio.run(
            tracking.link_anonymous_id(request=request, db=session, api_key=None)
        )
    except HTTPException as refused:
        return refused


@pytest.fixture
def alias_logs(monkeypatch) -> Recorder:
    from backend.app.api.v1.endpoints import tracking

    recorder = Recorder()
    for module in (tracking, identity_aliases):
        monkeypatch.setattr(module, "logger", recorder.bind())
    return recorder


def test_the_alias_route_stores_and_refuses_a_link_logging_neither_id(alias_logs):
    """IA-P1 (the route): a link stored, then one refused with its 409."""
    stored = _alias_handler(_LinkSession())
    refused = _alias_handler(_LinkSession(linked_to="someone-else"))

    assert stored.created is True
    assert refused.status_code == 409
    assert USER not in refused.detail and KNOWN not in refused.detail
    _assert_no_id(alias_logs.records)
    assert [r[0] for r in alias_logs.records] == ["debug"], alias_logs.records


def test_the_alias_route_logs_a_failures_class_not_its_text(alias_logs):
    """IA-P1 (the route): the driver's text carries both ids; the route logs
    the class at ERROR, answers a fixed 500 and repeats neither id."""
    session = _FailingLinkSession()
    with pytest.raises(Exception) as probe:
        session.execute("INSERT INTO identity_aliases")
    assert USER in str(probe.value)  # the probe: the text does carry it

    failed = _alias_handler(_FailingLinkSession())

    assert failed.status_code == 500
    assert USER not in failed.detail and KNOWN not in failed.detail
    _assert_no_id(alias_logs.records)
    assert [r[0] for r in alias_logs.records] == ["error"], alias_logs.records
    assert "IntegrityError" in alias_logs.records[0][1]


def test_two_equal_ids_are_refused_before_linking_and_logged_nowhere(
    alias_logs, monkeypatch
):
    """IA-P1 (the route): the 422 for equal ids comes from the request model,
    before ``link`` runs; neither the answer nor any record, at any level and
    from any logger, carries the id."""
    import logging

    from fastapi.testclient import TestClient

    from backend.app.api import deps, sdk_scope
    from backend.app.api.v1.endpoints import tracking
    from backend.app.main import app

    linked = []
    monkeypatch.setattr(tracking, "link", lambda *args: linked.append(args))

    class Everything(logging.Handler):
        def __init__(self) -> None:
            super().__init__(level=logging.DEBUG)
            self.lines: List[str] = []

        def emit(self, record: logging.LogRecord) -> None:
            text = record.getMessage()
            if record.exc_info:
                text += "\n" + "".join(traceback.format_exception(*record.exc_info))
            self.lines.append(text)

    def stub_db():
        yield MagicMock()

    root = logging.getLogger()
    handler, level = Everything(), root.level
    app.dependency_overrides[deps.get_db] = stub_db
    app.dependency_overrides[sdk_scope.require_sdk_ruleset_key] = lambda: MagicMock()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/api/v1/tracking/alias",
            json={"anonymous_id": USER, "user_id": USER},
            headers={"X-API-Key": "stubbed"},
        )
    finally:
        root.removeHandler(handler)
        root.setLevel(level)
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(sdk_scope.require_sdk_ruleset_key, None)

    assert response.status_code == 422, response.text
    assert USER not in response.text, response.text
    assert linked == [], "link ran for two equal ids"
    assert handler.lines, "nothing was captured: the handler is not wired in"
    assert [line for line in handler.lines if USER in line] == []
    assert [r for r in alias_logs.records if USER in r[1]] == []
