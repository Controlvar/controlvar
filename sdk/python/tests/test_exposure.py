"""``on_exposure`` (#1499): the client calls it once for each ``POST /api/v1/tracking/assign`` the
server answered with ``assigned`` True and the client stored into an empty or expired slot, and
never for a cached answer, the second of concurrent first calls, a failed call, an ``assigned``
False answer or a flag. What it raises goes to ``on_error(error, "exposure")`` with ``code``
``"HOOK_ERROR"``, is logged at ERROR, and never reaches the caller.

The server answers 'treatment' and the default variant is 'control', so a hook failure that
changed the answer could not pass by matching the fallback. Every count is exact. Threads are
joined with a timeout and asserted finished, never left to a plugin (CI installs only pytest).
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import gc
import inspect
import logging
import threading
import warnings
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest import mock
from urllib.parse import urlsplit

import pytest

import controlvar
from controlvar import (
    Assignment,
    ExperimentationClient,
    ExperimentationError,
    Exposure,
    TransportError,
)
from controlvar.cache import UserKeyCache
from controlvar.exposure import call_exposure_hook
from controlvar.local import EVALUATIONS_PATH, RULESET_PATH, LocalEvaluation
from controlvar.testing import FakeTransport

ASSIGN_PATH = "/api/v1/tracking/assign"
ASSIGNED: Dict[str, Any] = {
    "experiment_key": "exp-1",
    "user_id": "u-1",
    "variant_id": "v-2",
    "variant_name": "treatment",
    "is_control": False,
    "configuration": {"color": "green"},
    "assigned": True,
    "reason": "assigned",
}
EXPOSURE = Exposure(experiment_key="exp-1", user_id="u-1", variant_id="v-2", variant_name="treatment")
NOT_RUNNING = ["holdout", "mutual_exclusion", "targeting", "draft", "paused", "completed", "archived"]
COROUTINE_CLOSED = (
    "on_exposure failed: it returned a coroutine, which the client closed without running: "
    "on_exposure must not be async"
)


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport().route("POST", ASSIGN_PATH, json=ASSIGNED)


def make_client(
    transport: Any, **kwargs: Any
) -> Tuple[ExperimentationClient, List[Exposure], List[Tuple[str, ExperimentationError]]]:
    """A client whose hook records each exposure and whose ``on_error`` records each report."""
    calls: List[Exposure] = []
    errors: List[Tuple[str, ExperimentationError]] = []
    kwargs.setdefault("on_exposure", calls.append)
    kwargs.setdefault("on_error", lambda error, operation: errors.append((operation, error)))
    client = ExperimentationClient("http://test", "key", transport=transport, **kwargs)
    return client, calls, errors


def assign_requests(transport: FakeTransport) -> int:
    return [request.path for request in transport.requests].count(ASSIGN_PATH)


def raising(exc: BaseException) -> Callable[[Exposure], None]:
    def hook(exposure: Exposure) -> None:
        raise exc

    return hook


def in_thread(target: Callable[[], Any]) -> List[Any]:
    """Run ``target`` on a daemon thread; fail (never hang) if it has not returned in 5 s."""
    outcome: List[Any] = []
    thread = threading.Thread(target=lambda: outcome.append(target()), daemon=True)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive(), "get_assignment did not return: the hook ran under the cache lock"
    return outcome


# --------------------------------------------------------------------------- when it is called


class TestWhenItIsCalled:
    def test_is_called_once_with_exactly_the_experiment_key_user_id_variant_id_and_variant_name(self, transport):
        client, calls, errors = make_client(transport)
        assignment = client.get_assignment("exp-1", "u-1", {"plan": "pro"})
        assert len(calls) == 1
        assert calls == [EXPOSURE]
        assert type(calls[0]) is Exposure
        assert assignment.variant_name == "treatment"
        assert errors == []

    def test_passes_variant_id_none_when_the_server_sends_no_variant_id(self, transport):
        transport.route("POST", ASSIGN_PATH, json={k: v for k, v in ASSIGNED.items() if k != "variant_id"})
        client, calls, _ = make_client(transport)
        client.get_assignment("exp-1", "u-1")
        assert calls == [dataclasses.replace(EXPOSURE, variant_id=None)]

    def test_passes_the_user_id_the_assignment_was_asked_for(self, transport):
        transport.route("POST", ASSIGN_PATH, json={**ASSIGNED, "user_id": "an-echo-that-differs"})
        client, calls, _ = make_client(transport)
        client.get_assignment("exp-1", "u-1")
        assert [exposure.user_id for exposure in calls] == ["u-1"]

    def test_a_cached_answer_calls_nothing_however_often_the_answer_is_asked_for(self, transport):
        client, calls, _ = make_client(transport)
        assert client.get_variant("exp-1", "u-1", {"plan": "pro"}) == "treatment"
        client.get_assignment("exp-1", "u-1", {"plan": "pro"})
        client.get_assignment("exp-1", "u-1")
        assert client.get_variant("exp-1", "u-1") == "treatment"
        assert assign_requests(transport) == 1
        assert len(calls) == 1

    def test_is_called_once_per_user_and_experiment(self):
        transport = FakeTransport()
        for key, user in (("exp-1", "u-1"), ("exp-1", "u-2"), ("exp-2", "u-1")):
            transport.respond(200, json={**ASSIGNED, "experiment_key": key, "user_id": user})
        client, calls, _ = make_client(transport)
        for key, user in (("exp-1", "u-1"), ("exp-1", "u-2"), ("exp-2", "u-1"), ("exp-1", "u-1"), ("exp-2", "u-1")):
            client.get_assignment(key, user)
        assert assign_requests(transport) == 3
        assert [(e.experiment_key, e.user_id) for e in calls] == [("exp-1", "u-1"), ("exp-1", "u-2"), ("exp-2", "u-1")]

    def test_concurrent_first_calls_each_send_a_request_and_make_one_call(self, transport):
        barrier = threading.Barrier(2, timeout=5)
        send = transport.send

        def held_until_both_are_in_flight(method, url, headers, body, timeout):
            if urlsplit(url).path == ASSIGN_PATH:
                barrier.wait()
            return send(method, url, headers, body, timeout)

        transport.send = held_until_both_are_in_flight
        client, calls, errors = make_client(transport)
        answers: List[str] = []
        threads = [
            threading.Thread(target=lambda: answers.append(client.get_variant("exp-1", "u-1")), daemon=True)
            for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert not any(thread.is_alive() for thread in threads)
        # The positive control: both requests were in flight at once, so neither answer was cached.
        assert assign_requests(transport) == 2
        assert answers == ["treatment", "treatment"]
        assert len(calls) == 1
        assert errors == []

    @pytest.mark.parametrize("reason", NOT_RUNNING)
    def test_is_not_called_for_assigned_false_and_the_control_answer_comes_back_unchanged(self, reason):
        control = {**ASSIGNED, "variant_id": "v-1", "variant_name": "control", "is_control": True}
        transport = FakeTransport().route(
            "POST", ASSIGN_PATH, json={**control, "configuration": None, "assigned": False, "reason": reason}
        )
        client, calls, errors = make_client(transport)
        assert client.get_assignment("exp-1", "u-1") == Assignment(
            experiment_key="exp-1",
            user_id="u-1",
            variant_id="v-1",
            variant_name="control",
            is_control=True,
            configuration=None,
            assigned=False,
            reason=reason,
        )
        assert assign_requests(transport) == 1
        assert len(calls) == 0
        assert errors == []

    @pytest.mark.parametrize(
        "assigned", ["missing", None, 1, "true"], ids=["missing", "null", "the-number-1", "the-string-true"]
    )
    def test_is_not_called_when_assigned_is_missing_or_not_a_boolean(self, assigned):
        answer = {k: v for k, v in ASSIGNED.items() if k not in ("assigned", "reason")}
        if assigned != "missing":
            answer["assigned"] = assigned
        transport = FakeTransport().route("POST", ASSIGN_PATH, json=answer)
        client, calls, errors = make_client(transport)
        assignment = client.get_assignment("exp-1", "u-1")
        assert (assignment.variant_name, assignment.assigned) == ("treatment", None)
        assert len(calls) == 0
        assert errors == []

    @pytest.mark.parametrize(
        "outcome",
        [
            {"status": 404, "json": {"detail": "Experiment not found"}},
            {"status": 500, "json": {"detail": "boom"}},
            TransportError("connection refused"),
            {"status": 200, "json": {"experiment_key": "exp-1", "user_id": "u-1", "assigned": True}},
            {"status": 200, "body": b"<html>"},
        ],
        ids=["a-404", "a-500", "a-network-error", "an-answer-without-variant_name", "a-body-that-is-not-json"],
    )
    def test_a_failed_call_calls_nothing(self, outcome):
        transport = FakeTransport()
        for _ in range(2):  # get_assignment, then get_variant
            if isinstance(outcome, Exception):
                transport.fail(outcome)
            else:
                transport.respond(**outcome)
        client, calls, _ = make_client(transport)
        with pytest.raises(ExperimentationError):
            client.get_assignment("exp-1", "u-1")
        assert client.get_variant("exp-1", "u-1") == "control"
        assert assign_requests(transport) == 2
        assert len(calls) == 0

    def test_a_call_that_times_out_calls_nothing_even_if_the_server_recorded_the_exposure(self):
        transport = FakeTransport().fail(TimeoutError("timed out"))
        client, calls, _ = make_client(transport)
        with pytest.raises(ExperimentationError) as info:
            client.get_assignment("exp-1", "u-1")
        assert info.value.status is None
        assert len(calls) == 0

    def test_flags_never_call_it_in_server_mode(self, transport):
        flag = {"key": "f", "enabled": True, "config": None, "value": "blue", "value_type": "string"}
        for key in ("f", "g", "h"):
            transport.route("GET", f"/api/v1/feature-flags/evaluate/{key}", json={**flag, "key": key})
        transport.route("GET", "/api/v1/feature-flags/user/u-1", json={"f": True})
        client, calls, errors = make_client(transport)
        client.get_feature_flag("f", "u-1")
        client.is_feature_enabled("g", "u-1")
        assert client.get_string_value("h", "u-1", default="red") == "blue"
        client.get_all_flags("u-1")
        assert len(transport.requests) == 4
        assert len(calls) == 0
        assert errors == []

    def test_flags_never_call_it_in_local_mode_either_and_experiments_still_do(self, transport, monkeypatch):
        monkeypatch.setattr(LocalEvaluation, "_start_thread", lambda self: None)
        ruleset = {
            "schema": 1,
            "version": "v1",
            "bucketing": "md5-mod100-v1",
            "flags": [
                {
                    "key": "all-on",
                    "active": True,
                    "evaluation": "local",
                    "rollout_percentage": 100,
                    "rules": [],
                    "default_rule": None,
                }
            ],
        }
        transport.route("GET", RULESET_PATH, json=ruleset, headers={"ETag": '"v1"'})
        transport.route("POST", EVALUATIONS_PATH, status=201, json={"accepted": 2, "errors": []})
        client, calls, errors = make_client(transport, evaluation="local")
        try:
            client._local.refresh_once()
            assert client.ready(0)
            assert client.get_feature_flag("all-on", "u-1").source == "local"
            assert client.is_feature_enabled("all-on", "u-2") is True
            assert client.get_all_flags("u-3") == {"all-on": True}
            assert len(calls) == 0
            client.get_assignment("exp-1", "u-1")
            assert calls == [EXPOSURE]
        finally:
            client.close()
        assert errors == []


# ----------------------------------------------------------------------- when it is called again


class TestCalledAgainWhenTheClientAsksTheServerAgain:
    def test_after_the_cache_lifetime(self, transport):
        client, calls, _ = make_client(transport, cache_ttl_seconds=60)
        with mock.patch("controlvar.cache._now") as now:
            now.return_value = 1000.0
            client.get_assignment("exp-1", "u-1")
            now.return_value = 1059.5
            client.get_assignment("exp-1", "u-1")
            assert len(calls) == 1
            # Past the lifetime, not on its boundary.
            now.return_value = 1060.5
            client.get_assignment("exp-1", "u-1")
        assert assign_requests(transport) == 2
        assert calls == [EXPOSURE, EXPOSURE]

    def test_after_clear_cache(self, transport):
        client, calls, _ = make_client(transport)
        client.get_assignment("exp-1", "u-1")
        client.clear_cache()
        client.get_assignment("exp-1", "u-1")
        assert assign_requests(transport) == 2
        assert len(calls) == 2

    def test_in_a_new_client(self, transport):
        calls: List[Exposure] = []
        for _ in range(2):
            client = ExperimentationClient("http://test", "key", transport=transport, on_exposure=calls.append)
            client.get_assignment("exp-1", "u-1")
            client.get_assignment("exp-1", "u-1")
        assert assign_requests(transport) == 2
        assert len(calls) == 2

    def test_on_every_call_with_cache_ttl_seconds_0_which_asks_the_server_every_time(self, transport):
        client, calls, _ = make_client(transport, cache_ttl_seconds=0)
        for _ in range(3):
            client.get_assignment("exp-1", "u-1")
        assert assign_requests(transport) == 3
        assert len(calls) == 3


# ------------------------------------------------------------- what the client does around it


class TestTheClientAroundIt:
    def test_with_a_hook_an_assignment_sends_exactly_one_request(self, transport):
        client, calls, _ = make_client(transport)
        client.get_assignment("exp-1", "u-1")
        assert len(calls) == 1
        assert transport.calls() == ["POST /api/v1/tracking/assign"]

    def test_runs_in_the_calling_thread_before_get_assignment_returns(self, transport):
        order: List[Any] = []
        client, _, _ = make_client(transport, on_exposure=lambda e: order.append(("hook", threading.get_ident())))
        client.get_assignment("exp-1", "u-1")
        order.append(("returned", threading.get_ident()))
        assert order == [("hook", threading.get_ident()), ("returned", threading.get_ident())]

    def test_runs_after_the_answer_is_stored_and_recorded_a_key_less_track_in_the_hook_reaches_the_experiment(
        self, transport
    ):
        transport.route("POST", "/api/v1/tracking/batch", json={"success_count": 1, "failure_count": 0, "errors": []})
        client: Optional[ExperimentationClient] = None

        def hook(exposure: Exposure) -> None:
            assert client is not None
            client.track(exposure.user_id, "experiment_viewed")

        client, _, errors = make_client(transport, on_exposure=hook)
        client.get_assignment("exp-1", "u-1")
        assert transport.calls() == ["POST /api/v1/tracking/assign", "POST /api/v1/tracking/batch"]
        assert transport.last.json()["events"] == [
            {"event_type": "experiment_viewed", "event_name": "experiment_viewed", "user_id": "u-1", "experiment_key": "exp-1"}
        ]
        assert errors == []

    def test_a_hook_that_asks_for_the_same_assignment_gets_the_cached_answer_with_no_lock_held(self, transport):
        inner: List[str] = []
        client: Optional[ExperimentationClient] = None

        def hook(exposure: Exposure) -> None:
            assert client is not None
            inner.append(client.get_variant(exposure.experiment_key, exposure.user_id))

        client, calls, errors = make_client(transport, on_exposure=hook)
        assert [a.variant_name for a in in_thread(lambda: client.get_assignment("exp-1", "u-1"))] == ["treatment"]
        assert inner == ["treatment"]
        assert assign_requests(transport) == 1
        assert errors == []

    def test_exposure_is_a_frozen_dataclass_of_exactly_the_four_fields_exported_from_controlvar(self, transport):
        assert [field.name for field in dataclasses.fields(Exposure)] == [
            "experiment_key",
            "user_id",
            "variant_id",
            "variant_name",
        ]
        assert Exposure.__dataclass_params__.frozen
        assert "Exposure" in controlvar.__all__
        assert controlvar.Exposure is Exposure
        client, calls, _ = make_client(transport)
        client.get_assignment("exp-1", "u-1")
        with pytest.raises(dataclasses.FrozenInstanceError):
            calls[0].variant_name = "mutated"  # type: ignore[misc]
        assert client.get_assignment("exp-1", "u-1").variant_name == "treatment"

    @pytest.mark.parametrize(
        "value", ["on_exposure", 42, {"on_exposure": None}, True], ids=["a-string", "a-number", "a-dict", "true"]
    )
    def test_the_constructor_refuses_a_value_that_is_not_callable(self, value):
        with pytest.raises(TypeError) as info:
            ExperimentationClient("http://test", "key", on_exposure=value)
        assert str(info.value) == "on_exposure must be callable or None"

    def test_the_constructor_refuses_an_async_function_and_a_partial_of_one(self):
        async def hook(exposure: Exposure, extra: str = "") -> None:
            pass

        for value in (hook, functools.partial(hook, extra="x")):
            with pytest.raises(TypeError) as info:
                ExperimentationClient("http://test", "key", on_exposure=value)
            assert str(info.value) == (
                "on_exposure must not be async: the client never awaits it, so the body of an async def would never run"
            )

    def test_a_sync_function_a_bound_method_a_partial_and_a_callable_object_are_accepted(self, transport):
        received: List[Exposure] = []

        class Sink:
            def __call__(self, exposure: Exposure) -> None:
                received.append(exposure)

            def method(self, exposure: Exposure) -> None:
                received.append(exposure)

        def function(exposure: Exposure, extra: str = "") -> None:
            received.append(exposure)

        for hook in (function, Sink().method, functools.partial(function, extra="x"), Sink()):
            client = ExperimentationClient("http://test", "key", transport=transport, on_exposure=hook)
            client.get_assignment("exp-1", "u-1")
        assert received == [EXPOSURE] * 4

    def test_on_exposure_none_sets_no_hook_and_the_assignment_works_as_before(self, transport):
        client, _, errors = make_client(transport, on_exposure=None)
        assert client.get_variant("exp-1", "u-1") == "treatment"
        assert assign_requests(transport) == 1
        assert errors == []


# ------------------------------------------------------------------------ a hook that raises


class TestAHookThatRaises:
    def test_changes_nothing_get_variant_returns_the_variant_and_get_assignment_returns(self, transport):
        # ExperimentationError is the type get_variant swallows into default_variant.
        client, _, _ = make_client(transport, on_exposure=raising(ExperimentationError("hook boom")))
        assert client.get_variant("exp-1", "u-1") == "treatment"
        client, _, _ = make_client(transport, on_exposure=raising(RuntimeError("hook boom")))
        assignment = client.get_assignment("exp-1", "u-1")
        assert (assignment.variant_name, assignment.assigned) == ("treatment", True)

    def test_leaves_the_answer_cached_a_second_call_makes_no_request_and_calls_nothing(self, transport):
        hook = mock.Mock(side_effect=RuntimeError("hook boom"))
        client, _, _ = make_client(transport, on_exposure=hook)
        client.get_assignment("exp-1", "u-1")
        assert client.get_assignment("exp-1", "u-1").variant_name == "treatment"
        assert assign_requests(transport) == 1
        assert hook.call_count == 1

    def test_is_reported_to_on_error_once_with_exposure_and_an_experimentation_error_whose_code_is_hook_error(
        self, transport
    ):
        thrown = RuntimeError("hook boom")
        client, _, errors = make_client(transport, on_exposure=raising(thrown))
        client.get_assignment("exp-1", "u-1")
        assert len(errors) == 1
        operation, error = errors[0]
        assert operation == "exposure"
        assert isinstance(error, ExperimentationError)
        assert error.code == "HOOK_ERROR"
        assert error.message.startswith("on_exposure failed: ")
        assert error.message == "on_exposure failed: hook boom"
        assert str(error) == "on_exposure failed: hook boom"
        assert error.__cause__ is thrown
        assert (error.status, error.body) == (None, None)

    def test_is_logged_at_error_with_its_traceback_on_the_controlvar_client_logger(self, transport, caplog):
        thrown = RuntimeError("hook boom")
        client = ExperimentationClient("http://test", "key", transport=transport, on_exposure=raising(thrown))
        with caplog.at_level(logging.WARNING):
            assert client.get_variant("exp-1", "u-1") == "treatment"
        records = [(r.name, r.levelname, r.getMessage(), r.exc_info and r.exc_info[1]) for r in caplog.records]
        assert records == [("controlvar.client", "ERROR", "on_exposure failed: hook boom", thrown)]
        assert "raise exc" in caplog.text  # the hook's own traceback

    def test_an_exception_with_no_message_is_reported_by_its_type(self, transport):
        client, _, errors = make_client(transport, on_exposure=raising(ValueError()))
        client.get_assignment("exp-1", "u-1")
        assert [(operation, error.message) for operation, error in errors] == [
            ("exposure", "on_exposure failed: ValueError")
        ]

    def test_an_on_error_that_raises_too_leaves_the_answer_unchanged(self, transport, caplog):
        on_error = mock.Mock(side_effect=RuntimeError("handler boom"))
        client, _, _ = make_client(transport, on_exposure=raising(RuntimeError("hook boom")), on_error=on_error)
        with caplog.at_level(logging.WARNING):
            assert client.get_variant("exp-1", "u-1") == "treatment"
        assert on_error.call_count == 1
        assert [r.getMessage() for r in caplog.records] == ["on_exposure failed: hook boom", "on_error handler raised"]

    @pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit], ids=["KeyboardInterrupt", "SystemExit"])
    def test_keyboard_interrupt_and_system_exit_are_not_caught(self, transport, exc):
        client, _, errors = make_client(transport, on_exposure=raising(exc()))
        with pytest.raises(exc):
            client.get_assignment("exp-1", "u-1")
        assert errors == []


# ---------------------------------------------------------------- async: refused, or closed


class TestAsync:
    def test_a_coroutine_from_an_async_call_is_not_run_and_is_reported(self, transport, caplog):
        ran: List[Exposure] = []

        class AsyncHook:
            async def __call__(self, exposure: Exposure) -> None:
                ran.append(exposure)

        client, _, errors = make_client(transport, on_exposure=AsyncHook())
        with caplog.at_level(logging.WARNING):
            assert client.get_variant("exp-1", "u-1") == "treatment"
        assert ran == []
        assert [(operation, error.code, error.message) for operation, error in errors] == [
            ("exposure", "HOOK_ERROR", COROUTINE_CLOSED)
        ]
        assert isinstance(errors[0][1].__cause__, TypeError)
        assert [(r.levelname, r.getMessage()) for r in caplog.records] == [("ERROR", COROUTINE_CLOSED)]

    def test_a_returned_coroutine_is_closed_so_it_never_runs_and_never_warns_that_it_was_not_awaited(self):
        ran: List[Exposure] = []
        returned: List[Any] = []

        async def send(exposure: Exposure) -> None:
            ran.append(exposure)

        def hook(exposure: Exposure) -> Any:
            returned.append(send(exposure))
            return returned[-1]

        report = mock.Mock()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            call_exposure_hook(hook, EXPOSURE, report)
            assert inspect.getcoroutinestate(returned[0]) == inspect.CORO_CLOSED
            returned.clear()
            report.reset_mock()  # drops the reported error, whose traceback holds the coroutine
            gc.collect()
        assert ran == []
        assert [str(w.message) for w in caught if "never awaited" in str(w.message)] == []

    def test_a_task_the_hook_schedules_on_a_running_loop_is_left_alone_runs_and_is_not_reported(
        self, transport, caplog
    ):
        ran: List[Exposure] = []
        tasks: List["asyncio.Task[None]"] = []

        async def send(exposure: Exposure) -> None:
            ran.append(exposure)

        async def main() -> None:
            loop = asyncio.get_running_loop()

            def hook(exposure: Exposure) -> "asyncio.Task[None]":
                task = loop.create_task(send(exposure))
                tasks.append(task)
                return task

            client, _, errors = make_client(transport, on_exposure=hook)
            with caplog.at_level(logging.WARNING):
                assert client.get_variant("exp-1", "u-1") == "treatment"
            assert [(operation, error.message) for operation, error in errors] == []
            await asyncio.gather(*tasks)

        asyncio.run(main())
        assert len(tasks) == 1
        assert ran == [EXPOSURE]
        assert caplog.records == []


# --------------------------------------------------------------------------- the guard itself


class TestCallExposureHook:
    def test_passes_the_hook_the_exposure_only_and_reports_nothing_for_a_value(self):
        hook = mock.Mock(return_value="ignored")
        report = mock.Mock()
        call_exposure_hook(hook, EXPOSURE, report)
        assert hook.call_args_list == [mock.call(EXPOSURE)]
        assert report.call_count == 0

    def test_reports_what_the_hook_raises_at_once(self):
        thrown = RuntimeError("sync boom")
        report = mock.Mock()
        call_exposure_hook(raising(thrown), EXPOSURE, report)
        assert report.call_args_list == [mock.call(thrown)]

    def test_drops_what_report_itself_raises(self):
        report = mock.Mock(side_effect=RuntimeError("report boom"))
        call_exposure_hook(raising(RuntimeError("hook boom")), EXPOSURE, report)
        assert report.call_count == 1


# ------------------------------------------------------------------ the store that decides it


class TestUserKeyCacheSetNew:
    def test_true_for_an_empty_slot_false_for_a_live_one_and_true_for_an_expired_one(self):
        cache: UserKeyCache[str] = UserKeyCache(60)
        with mock.patch("controlvar.cache._now") as now:
            now.return_value = 1000.0
            assert cache.set_new("u-1", "exp-1", "a") is True
            now.return_value = 1059.5
            assert cache.set_new("u-1", "exp-1", "b") is False
            assert cache.get("u-1", "exp-1") == "b"
            # "b" was stored at 1059.5, so it lives until 1119.5.
            now.return_value = 1119.5
            assert cache.set_new("u-1", "exp-1", "c") is True
            assert cache.set_new("u-1", "exp-2", "d") is True
            assert cache.get("u-1", "exp-1") == "c"
