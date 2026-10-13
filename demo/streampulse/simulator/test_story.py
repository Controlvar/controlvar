"""Offline tests for the rollout story (no network: StoryClient is replaced by an in-memory fake)."""
from __future__ import annotations

import copy
import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rollout_story as story  # noqa: E402
from traffic import PLAYER_FLAG  # noqa: E402

FLAG_ID = "11111111-1111-1111-1111-111111111111"
EMPLOYEE_GROUP = {
    "id": "grp-employees",
    "logical_operator": "AND",
    "conditions": [{"id": "cond-employee", "attribute": "employee", "operator": "equals", "value": True}],
}


class FakeStoryClient:
    """In-memory platform: flag, 4-stage schedule (rollout-service semantics), safety config/settings."""

    def __init__(self, api_url: str = "", token: str = "dev", timeout: float = 15.0):
        self.api_url = api_url
        self.token = token
        self.flag = {
            "id": FLAG_ID,
            "key": PLAYER_FLAG,
            "status": "active",
            "rollout_percentage": 5,
            "targeting_rules": {"logical_operator": "OR", "groups": [copy.deepcopy(EMPLOYEE_GROUP)]},
        }
        self.schedule = {
            "id": "sched-1",
            "name": "Player v2 rollout",
            "status": "active",
            "feature_flag_id": FLAG_ID,
            "stages": [
                {"id": "st-1", "stage_order": 1, "name": "Internal + 5%", "target_percentage": 5, "status": "in_progress", "trigger_type": "time_based"},
                {"id": "st-2", "stage_order": 2, "name": "25%", "target_percentage": 25, "status": "pending", "trigger_type": "time_based"},
                {"id": "st-3", "stage_order": 3, "name": "50%", "target_percentage": 50, "status": "pending", "trigger_type": "time_based"},
                {"id": "st-4", "stage_order": 4, "name": "100%", "target_percentage": 100, "status": "pending", "trigger_type": "time_based"},
            ],
        }
        self.settings = {"enable_automatic_rollbacks": True}
        self.config = {
            "feature_flag_id": FLAG_ID,
            "enabled": True,
            "rollback_percentage": 5,
            "metrics": {"error_rate": {"warning_threshold": 0.02, "critical_threshold": 0.05, "comparison_type": "greater_than"}},
        }
        self.error_rate = 0.0
        self.healthy_after_checks: int | None = None  # becomes healthy after N safety checks
        self.scheduler_after_polls: int | None = None  # the "safety scheduler" rolls back after N get_flag calls
        self.calls: list[tuple[str, ...]] = []
        self.rollbacks: list[tuple[int, str]] = []
        self.flag_updates: list[dict] = []
        self.stage_updates: list[tuple[str, dict]] = []
        self.checks = 0
        self.polls = 0

    # --- flags ---
    def flag_by_key(self, key):
        self.calls.append(("flag_by_key", key))
        assert key == PLAYER_FLAG
        return {"id": FLAG_ID, "key": key, "rollout_percentage": self.flag["rollout_percentage"], "targeting_rules": self.flag["targeting_rules"]}

    def get_flag(self, flag_id):
        self.calls.append(("get_flag", flag_id))
        self.polls += 1
        if (
            self.scheduler_after_polls is not None
            and self.polls > self.scheduler_after_polls
            and self.flag["rollout_percentage"] > self.config["rollback_percentage"]
        ):
            self._roll_back(self.config["rollback_percentage"])
        return copy.deepcopy(self.flag)

    def _roll_back(self, percentage):
        """What SafetyService.execute_rollback does (#629): a target of 0 turns the flag off,
        any other target lowers the global %; either way the active schedule is paused."""
        if percentage == 0:
            self.flag["status"] = "inactive"
        self.flag["rollout_percentage"] = percentage
        if self.schedule["status"] == "active":
            self.schedule["status"] = "paused"

    def update_flag(self, flag_id, body):
        self.calls.append(("update_flag", flag_id))
        self.flag_updates.append(copy.deepcopy(body))
        if "targeting_rules" in body:
            self.flag["targeting_rules"] = copy.deepcopy(body["targeting_rules"])
        if "rollout_percentage" in body:
            self.flag["rollout_percentage"] = body["rollout_percentage"]
        return copy.deepcopy(self.flag)

    # --- schedule (mirrors RolloutService.manually_advance_stage) ---
    def schedule_for_flag(self, flag_id):
        self.calls.append(("schedule_for_flag", flag_id))
        return copy.deepcopy(self.schedule)

    def _stage(self, stage_id):
        return next(s for s in self.schedule["stages"] if s["id"] == stage_id)

    def update_stage(self, stage_id, body):
        self.calls.append(("update_stage", stage_id))
        self.stage_updates.append((stage_id, dict(body)))
        self._stage(stage_id).update(body)
        return copy.deepcopy(self._stage(stage_id))

    def activate_schedule(self, schedule_id):
        """Mirrors RolloutService.activate_rollout_schedule: DRAFT or PAUSED -> ACTIVE."""
        self.calls.append(("activate_schedule", schedule_id))
        assert schedule_id == self.schedule["id"]
        if self.schedule["status"] not in ("draft", "paused"):
            raise story.ApiError(400, f"Cannot activate schedule in {self.schedule['status']} status")
        self.schedule["status"] = "active"
        return copy.deepcopy(self.schedule)

    def advance_stage(self, stage_id):
        self.calls.append(("advance_stage", stage_id))
        stage = self._stage(stage_id)
        if stage["trigger_type"] != "manual":
            raise story.ApiError(400, "Can only manually advance stages with manual trigger type")
        if self.schedule["status"] != "active":
            raise story.ApiError(400, f"Cannot advance stage for schedule in {self.schedule['status']} status")
        if stage["status"] == "pending":
            stage["status"] = "in_progress"
            self.flag["rollout_percentage"] = stage["target_percentage"]
        elif stage["status"] == "in_progress":
            stage["status"] = "completed"
            nxt = next((s for s in self.schedule["stages"] if s["stage_order"] > stage["stage_order"] and s["status"] == "pending"), None)
            if nxt:
                nxt["status"] = "in_progress"
                self.flag["rollout_percentage"] = nxt["target_percentage"]
            else:
                self.schedule["status"] = "completed"
        else:
            raise story.ApiError(400, f"Cannot advance stage in {stage['status']} status")
        return copy.deepcopy(stage)

    # --- safety ---
    def safety_settings(self):
        self.calls.append(("safety_settings",))
        return dict(self.settings)

    def enable_automatic_rollbacks(self):
        self.settings["enable_automatic_rollbacks"] = True
        return dict(self.settings)

    def safety_config(self, flag_id):
        self.calls.append(("safety_config", flag_id))
        return copy.deepcopy(self.config)

    def safety_check(self, flag_id):
        self.calls.append(("safety_check", flag_id))
        self.checks += 1
        if self.healthy_after_checks is not None and self.checks > self.healthy_after_checks:
            self.error_rate = 0.0
        critical = self.error_rate > 0.05
        return {
            "feature_flag_id": flag_id,
            "is_healthy": not critical,
            "metrics": [{"name": "error_rate", "current_value": self.error_rate, "threshold": 0.05, "is_healthy": not critical,
                         "details": {"warning_threshold": 0.02, "critical_threshold": 0.05, "warning": self.error_rate > 0.02}}],
        }

    def rollback(self, flag_id, percentage, reason):
        self.calls.append(("rollback", flag_id, str(percentage)))
        self.rollbacks.append((percentage, reason))
        previous = self.flag["rollout_percentage"]
        self._roll_back(percentage)
        return {"success": True, "feature_flag_id": flag_id, "message": f"rolled back from {previous}% to {percentage}%"}

    # --- helpers for assertions ---
    def stage_status(self, order):
        return next(s["status"] for s in self.schedule["stages"] if s["stage_order"] == order)

    def writes(self):
        return [c for c in self.calls if c[0] in ("update_flag", "update_stage", "advance_stage", "activate_schedule", "rollback")]


def make_story(client: FakeStoryClient, **kw) -> tuple[story.Story, io.StringIO]:
    """Story with a fake clock: sleeping advances time instantly."""
    out = io.StringIO()
    sleeps: list[float] = []
    kw.setdefault("sleep", sleeps.append)
    kw.setdefault("clock", lambda: sum(sleeps))
    kw.setdefault("run_traffic", lambda args: 0)
    kw.setdefault("api_key", "eptk_test")
    kw.setdefault("poll_interval", 1)
    kw.setdefault("rollback_wait", 5)
    kw.setdefault("healthy_wait", 5)
    s = story.Story(client=client, out=out, **kw)
    s.sleeps = sleeps  # type: ignore[attr-defined]
    return s, out


# --------------------------------------------------------------------------------------


def test_step_1_is_read_only_and_describes_the_starting_state():
    client = FakeStoryClient()
    s, out = make_story(client)
    assert s.step_1() is True
    text = out.getvalue()
    assert "Step 1/7" in text
    assert "rollout 5%" in text
    assert 'employee equals true' in text
    assert "▶ stage 1" in text and "· stage 2" in text
    assert "rollback to 5%" in text and "automatic rollbacks globally ON" in text
    assert "HEALTHY" in text
    assert "Internal tester" in text and "targeting_rule" in text
    assert client.writes() == []


def test_step_2_advances_the_schedule_to_25_and_is_idempotent():
    client = FakeStoryClient()
    s, out = make_story(client)
    assert s.step_2() is True
    assert client.flag["rollout_percentage"] == 25
    assert client.stage_status(1) == "completed" and client.stage_status(2) == "in_progress" and client.stage_status(3) == "pending"
    # the time-based stage 1 was switched to a manual trigger before the advance call
    assert client.stage_updates == [("st-1", {"trigger_type": "manual"})]
    assert ("advance_stage", "st-1") in client.calls
    text = out.getvalue()
    assert "Rollout 5% → 25%" in text and "switched from a time-based to a manual trigger" in text

    writes_before = len(client.writes())
    assert s.step_2() is True
    assert len(client.writes()) == writes_before, "re-running step 2 must not write"
    assert "Already at stage 2" in out.getvalue()
    assert client.flag["rollout_percentage"] == 25


def test_step_3_runs_the_incident_traffic_subprocess_and_checks_safety():
    client = FakeStoryClient()
    client.flag["rollout_percentage"] = 25
    runs: list[list[str]] = []

    def run_traffic(args):
        runs.append(args)
        client.error_rate = 0.12  # the incident traffic pushed the error rate past 5%
        return 0

    s, out = make_story(client, run_traffic=run_traffic, api_key="eptk_secret", api_url="http://api:8000")
    assert s.step_3() is True
    assert len(runs) == 1
    args = runs[0]
    assert args[: 2] == ["--incident", "android12"]
    assert args[args.index("--rate") + 1] == "6"
    assert args[args.index("--duration") + 1] == "90"
    assert args[args.index("--api-key") + 1] == "eptk_secret"
    assert args[args.index("--api-url") + 1] == "http://api:8000"
    assert ("safety_check", FLAG_ID) in client.calls
    text = out.getvalue()
    assert "CRITICAL" in text and "12.0%" in text and "/admin/safety" in text
    assert client.writes() == []


def test_step_3_fails_loudly_when_traffic_exits_non_zero():
    client = FakeStoryClient()
    client.flag["rollout_percentage"] = 25
    s, _ = make_story(client, run_traffic=lambda args: 3)
    with pytest.raises(story.StoryError, match="code 3"):
        s.step_3()


def test_step_3_says_the_crashes_go_out_with_the_server_key_and_names_its_file_on_exit_2():
    client = FakeStoryClient()
    client.flag["rollout_percentage"] = 25

    def run_traffic(args):
        client.error_rate = 0.12
        return 0

    s, out = make_story(client, run_traffic=run_traffic)
    assert s.step_3() is True
    assert "the way the StreamPulse server would forward them, with its server-side key" in out.getvalue()

    s, _ = make_story(client, run_traffic=lambda args: 2)
    with pytest.raises(story.StoryError, match=r"code 2 .*demo/streampulse/\.api_key_server.*seed_streampulse\.py"):
        s.step_3()


def test_step_4_waits_for_the_safety_scheduler_when_automatic_rollbacks_are_on():
    client = FakeStoryClient()
    client.flag["rollout_percentage"] = 25
    client.error_rate = 0.12
    client.scheduler_after_polls = 3
    s, out = make_story(client)
    assert s.step_4() is True
    assert client.flag["rollout_percentage"] == 5
    assert client.rollbacks == [], "the scheduler did it; no manual rollback"
    assert s.sleeps, "polled with sleeps in between"
    text = out.getvalue()
    assert "Automatic rollbacks are ON" in text and "Rollout 25% → 5%" in text
    assert "paused the rollout schedule" in text and client.schedule["status"] == "paused"


def test_step_4_rolls_back_by_hand_when_automatic_rollbacks_are_off():
    client = FakeStoryClient()
    client.flag["rollout_percentage"] = 25
    client.error_rate = 0.12
    client.settings["enable_automatic_rollbacks"] = False
    s, out = make_story(client)
    assert s.step_4() is True
    assert client.rollbacks and client.rollbacks[0][0] == 5
    assert "Android 12" in client.rollbacks[0][1]
    assert client.flag["rollout_percentage"] == 5
    assert "Automatic rollbacks are OFF" in out.getvalue()
    assert s.sleeps == []


def test_step_4_falls_back_to_a_manual_rollback_when_the_scheduler_never_acts():
    client = FakeStoryClient()
    client.flag["rollout_percentage"] = 25
    client.error_rate = 0.12
    s, out = make_story(client)  # scheduler_after_polls stays None: the flag never moves on its own
    assert s.step_4() is True
    assert client.rollbacks == [(5, client.rollbacks[0][1])]
    assert client.flag["rollout_percentage"] == 5
    assert "did not act in time" in out.getvalue()


def test_step_4_is_idempotent_and_refuses_when_there_is_nothing_to_roll_back():
    client = FakeStoryClient()  # already at 5%
    s, out = make_story(client)
    assert s.step_4() is True
    assert "already rolled back" in out.getvalue() and client.writes() == []

    client = FakeStoryClient()
    client.flag["rollout_percentage"] = 25  # healthy at 25%: nothing to roll back
    s, _ = make_story(client)
    with pytest.raises(story.StoryError, match="healthy"):
        s.step_4()
    assert client.writes() == []


def test_step_5_adds_the_fixed_build_rule_next_to_the_employee_group():
    client = FakeStoryClient()
    s, out = make_story(client)
    assert s.step_5() is True
    assert len(client.flag_updates) == 1
    rules = client.flag_updates[0]["targeting_rules"]
    assert rules["logical_operator"] == "OR"
    assert rules["groups"][0] == EMPLOYEE_GROUP
    fix = rules["groups"][1]["conditions"]
    assert fix == [{"id": "cond-app-version", "attribute": "app_version", "operator": "semver_gte", "value": "3.2.1"}]
    assert set(client.flag_updates[0]) == {"targeting_rules"}, "PUT touches nothing else"
    assert "semver_gte" in out.getvalue()

    assert s.step_5() is True
    assert len(client.flag_updates) == 1, "second run is a no-op"
    assert "already there" in out.getvalue()


def test_step_6_waits_until_healthy_resumes_the_paused_schedule_then_advances_to_50():
    client = FakeStoryClient()
    # state after steps 2-5: stage 2 in progress, flag rolled back to 5% and the schedule
    # paused by the rollback (#629), still critical for a while
    client.schedule["stages"][0].update({"status": "completed", "trigger_type": "manual"})
    client.schedule["stages"][1]["status"] = "in_progress"
    client.flag["rollout_percentage"] = 5
    client.schedule["status"] = "paused"
    client.error_rate = 0.12
    client.healthy_after_checks = 2
    s, out = make_story(client, healthy_wait=120)
    assert s.step_6() is True
    assert client.flag["rollout_percentage"] == 50
    assert client.schedule["status"] == "active"
    assert client.stage_status(2) == "completed" and client.stage_status(3) == "in_progress"
    resumed = client.calls.index(("activate_schedule", "sched-1"))
    first_advance = client.calls.index(next(c for c in client.calls if c[0] == "advance_stage"))
    assert resumed < first_advance, "the schedule is resumed before any stage is advanced"
    text = out.getvalue()
    assert "error rate still 12.0%" in text and "HEALTHY" in text and "Rollout 5% → 50%" in text
    assert "Resumed the rollout schedule" in text
    assert len(s.sleeps) >= 1

    assert s.step_6() is True
    assert "Already at stage 3" in out.getvalue()


def test_step_7_reaches_100_completes_the_schedule_and_removes_the_rules():
    client = FakeStoryClient()
    for stage in client.schedule["stages"][:2]:
        stage.update({"status": "completed", "trigger_type": "manual"})
    client.schedule["stages"][2].update({"status": "in_progress", "trigger_type": "manual"})
    client.flag["rollout_percentage"] = 50
    s, out = make_story(client)
    assert s.step_7() is True
    assert client.flag["rollout_percentage"] == 100
    assert client.stage_status(4) == "completed" and client.schedule["status"] == "completed"
    assert client.flag["targeting_rules"] == {}
    assert client.flag_updates[-1] == {"targeting_rules": {}}
    text = out.getvalue()
    assert "Rollout is now 100%" in text and "Removed the targeting rules" in text and "Targeting: none" in text

    writes_before = len(client.writes())
    assert s.step_7() is True
    assert len(client.writes()) == writes_before


def test_auto_runs_all_seven_steps_in_order_through_main():
    client = FakeStoryClient()
    made = []

    def factory(url, token):
        made.append((url, token))
        client.scheduler_after_polls = 10 ** 6  # never; step 4 falls back to the manual rollback
        return client

    def run_traffic(args):
        client.error_rate = 0.12
        return 0

    sleeps: list[float] = []
    out = io.StringIO()
    rc = story.main(
        ["--auto", "--pace", "0", "--token", "t0k", "--api-url", "http://api:8000", "--no-wait", "--rollback-wait", "0"],
        client_factory=factory,
        sleep=sleeps.append,
        run_traffic=run_traffic,
        out=out,
    )
    assert rc == 0
    assert made == [("http://api:8000", "t0k")]
    text = out.getvalue()
    assert [f"Step {i}/7" in text for i in range(1, 8)] == [True] * 7
    assert text.index("Step 1/7") < text.index("Step 4/7") < text.index("Step 7/7")
    assert client.flag["rollout_percentage"] == 100 and client.schedule["status"] == "completed"
    assert client.rollbacks and client.rollbacks[0][0] == 5
    assert ("activate_schedule", "sched-1") in client.calls, "step 6 resumed the schedule the rollback paused"
    assert client.flag["targeting_rules"] == {}


def test_main_reports_precondition_and_auth_failures():
    client = FakeStoryClient()
    client.flag["rollout_percentage"] = 25  # healthy: step 4 has nothing to do
    rc = story.main(["--step", "4", "--token", "t0k"], client_factory=lambda u, t: client, out=io.StringIO())
    assert rc == 1

    class Unauthorized(FakeStoryClient):
        def flag_by_key(self, key):
            raise story.ApiError(401, "no", "http://x")

    assert story.main(["--step", "1", "--token", "t0k"], client_factory=lambda u, t: Unauthorized(), out=io.StringIO()) == 2

    class Down(FakeStoryClient):
        def flag_by_key(self, key):
            raise story.ApiError(0, "connection refused", "http://x")

    assert story.main(["--step", "1", "--token", "t0k"], client_factory=lambda u, t: Down(), out=io.StringIO()) == 4

    with pytest.raises(SystemExit):
        story.main([])  # --step or --auto is required


def test_story_client_uses_bearer_auth_and_the_documented_endpoints(monkeypatch):
    captured: list[dict] = []

    class Resp:
        def __init__(self, body: bytes):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.body

    def fake_urlopen(req, timeout):
        captured.append({"url": req.full_url, "method": req.get_method(), "headers": {k.lower(): v for k, v in req.header_items()}, "body": req.data})
        if "/feature-flags/?" in req.full_url:
            return Resp(b'{"items": [{"id": "f-1", "key": "streampulse_player_v2"}], "total": 1}')
        if "/rollout-schedules/?" in req.full_url:
            return Resp(b'{"items": [{"id": "s-1", "status": "active", "stages": []}], "total": 1}')
        return Resp(b'{"ok": true}')

    monkeypatch.setattr(story.urllib.request, "urlopen", fake_urlopen)
    client = story.StoryClient("http://localhost:8000/", "dev")
    assert client.flag_by_key(PLAYER_FLAG)["id"] == "f-1"
    assert captured[0]["headers"]["authorization"] == "Bearer dev"
    assert captured[0]["url"].startswith("http://localhost:8000/api/v1/feature-flags/?")
    client.advance_stage("st-2")
    assert captured[-1] == {**captured[-1], "url": "http://localhost:8000/api/v1/rollout-schedules/stages/st-2/advance", "method": "POST"}
    client.update_stage("st-2", {"trigger_type": "manual"})
    assert captured[-1]["url"].endswith("/rollout-schedules/stages/st-2") and captured[-1]["method"] == "PUT"
    client.rollback("f-1", 5, "crash spike")
    assert captured[-1]["url"] == "http://localhost:8000/api/v1/safety/feature-flags/f-1/rollback?percentage=5&reason=crash+spike"
    client.activate_schedule("s-1")
    assert captured[-1]["url"] == "http://localhost:8000/api/v1/rollout-schedules/s-1/activate" and captured[-1]["method"] == "POST"
    client.safety_check("f-1")
    assert captured[-1]["url"].endswith("/safety/feature-flags/f-1/check")
    client.update_flag("f-1", {"targeting_rules": {}})
    assert captured[-1]["method"] == "PUT" and captured[-1]["body"] == b'{"targeting_rules": {}}'
    client.schedule_for_flag("f-1")
    assert "feature_flag_id=f-1" in captured[-1]["url"]


# --------------------------------------------------------------------------------------
# Sign-in (#195): the old default token "dev" was accepted only by a backend in its
# development sign-in mode, which neither .env.example nor setup-local.sh turns on, so the
# documented command exited 2 with a 401.
# --------------------------------------------------------------------------------------


@pytest.mark.regression
def test_without_token_the_story_signs_in_as_the_seeded_demo_admin():
    logins: list[tuple[str, str, str]] = []
    made: list[tuple[str, str]] = []

    def login(api_url, email, password):
        logins.append((api_url, email, password))
        return "jwt-from-login"

    def factory(url, token):
        made.append((url, token))
        return FakeStoryClient(url, token)

    rc = story.main(["--step", "1"], client_factory=factory, login=login, out=io.StringIO())
    assert rc == 0
    assert logins == [("http://localhost:8000", "admin@demo.com", "Demo1234!")]
    assert made == [("http://localhost:8000", "jwt-from-login")]


@pytest.mark.regression
def test_the_parser_has_no_dev_token_default():
    args = story.build_parser().parse_args(["--step", "1"])
    assert args.token is None
    assert (args.email, args.password) == ("admin@demo.com", "Demo1234!")


def test_an_explicit_token_skips_the_sign_in():
    def login(*a):
        raise AssertionError("must not sign in when --token is given")

    made: list[str] = []

    def factory(url, token):
        made.append(token)
        return FakeStoryClient(url, token)

    rc = story.main(["--step", "1", "--token", "given"], client_factory=factory, login=login, out=io.StringIO())
    assert rc == 0
    assert made == ["given"]


def test_a_failed_sign_in_exits_2_and_an_unreachable_api_exits_4(capsys):
    def factory(u, t):
        raise AssertionError("no client without a token")

    def refused(*a):
        raise story.ApiError(401, '{"detail":"Incorrect email or password"}', "http://x")

    rc = story.main(
        ["--step", "1", "--email", "a@b.c", "--password", "x"], client_factory=factory, login=refused, out=io.StringIO()
    )
    assert rc == 2
    assert "could not sign in" in capsys.readouterr().err

    def down(*a):
        raise story.ApiError(0, "connection refused", "http://x")

    assert story.main(["--step", "1"], client_factory=factory, login=down, out=io.StringIO()) == 4


def test_sign_in_posts_the_credentials_and_returns_the_access_token(monkeypatch):
    captured: list[dict] = []

    class Resp:
        def __init__(self, body: bytes):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.body

    bodies = [b'{"access_token": "jwt-1", "token_type": "bearer"}', b'{"detail": "odd"}']

    def fake_urlopen(req, timeout):
        captured.append({"url": req.full_url, "method": req.get_method(), "body": req.data})
        return Resp(bodies.pop(0))

    monkeypatch.setattr(story.urllib.request, "urlopen", fake_urlopen)
    assert story.sign_in("http://localhost:8000/", "admin@demo.com", "Demo1234!") == "jwt-1"
    assert captured[0] == {
        "url": "http://localhost:8000/api/v1/auth/login",
        "method": "POST",
        "body": b'{"email": "admin@demo.com", "password": "Demo1234!"}',
    }
    with pytest.raises(story.ApiError):
        story.sign_in("http://localhost:8000", "admin@demo.com", "Demo1234!")


def test_every_step_but_the_incident_writes_nothing_when_run_again():
    """Each step run a second time, right after it ran: steps 1, 2 and 4-7 print
    the state and write nothing; step 3 runs the incident traffic again."""
    client = FakeStoryClient()
    runs: list[list[str]] = []

    def run_traffic(args):
        runs.append(args)
        client.error_rate = 0.12
        return 0

    s, out = make_story(client, run_traffic=run_traffic, healthy_wait=120)
    for step in range(1, 8):
        if step == 4:
            client.scheduler_after_polls = (
                client.polls
            )  # the monitor acts at the next poll
        if step == 5:
            client.scheduler_after_polls = None  # and once only
        if step == 6:
            client.healthy_after_checks = client.checks + 1
        assert s.run_step(step) is True
        writes, traffic = len(client.writes()), len(runs)
        assert s.run_step(step) is True
        if step == 3:
            assert len(runs) == traffic + 1, "re-running step 3 runs the incident again"
            assert len(client.writes()) == writes
        else:
            assert len(client.writes()) == writes, f"re-running step {step} wrote"
            assert len(runs) == traffic
    assert (
        client.flag["rollout_percentage"] == 100
        and client.schedule["status"] == "completed"
    )
    assert len(runs) == 2


def test_the_story_takes_the_documented_options_with_their_defaults():
    defaults = story.build_parser().parse_args(["--step", "1"])
    assert defaults.token is None and defaults.api_key is None
    assert (defaults.email, defaults.password) == ("admin@demo.com", "Demo1234!")
    assert defaults.api_url == "http://localhost:8000"
    assert defaults.dashboard_url == "http://localhost:3100"
    assert defaults.app_url == "http://localhost:3300"
    assert defaults.pace == 15.0
    assert (defaults.incident_rate, defaults.incident_duration) == (6, 90)
    assert defaults.rollback_wait == 6 * 60 and defaults.no_wait is False
    given = story.build_parser().parse_args(
        [
            "--auto",
            "--pace",
            "20",
            "--api-key",
            "k",
            "--incident-rate",
            "5",
            "--incident-duration",
            "60",
            "--rollback-wait",
            "30",
            "--no-wait",
            "--dashboard-url",
            "http://d",
            "--app-url",
            "http://a",
            "--api-url",
            "http://api",
        ]
    )
    assert given.auto and given.pace == 20.0 and given.no_wait
    with pytest.raises(SystemExit):
        story.build_parser().parse_args(["--step", "8"])


def test_no_wait_advances_a_critical_flag_without_waiting():
    """With --no-wait, step 6 resumes and advances at once while the flag is still
    critical, so the safety monitor rolls it back again."""
    client = FakeStoryClient()
    client.schedule["stages"][0].update(
        {"status": "completed", "trigger_type": "manual"}
    )
    client.schedule["stages"][1]["status"] = "in_progress"
    client.flag["rollout_percentage"] = 5
    client.schedule["status"] = "paused"
    client.error_rate = 0.12
    s, out = make_story(client, healthy_wait=0.0)
    assert s.step_6() is True
    assert s.sleeps == []
    assert client.flag["rollout_percentage"] == 50
    assert "WARNING: still critical after waiting" in out.getvalue()
