"""The demo apps in the showcase tool (E0): ports, CORS, child environments,
keys, the whole-context listeners, the app builds and the machine lock.

Each guard is shown refusing its planted defect. No browser, Docker, network
or database: stand-in pages and contexts, and a stand-in ``docker`` on
``PATH`` that writes every call it gets to a log, where a run has to start.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml
from showcase.capture import build, config, director, gates, guard, run, storyboard
from showcase.capture.procs import Children, python_argv
from showcase.capture.stack import Ports, Stack, StackError, read_key

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
ACCEPTANCE = REPO_ROOT / "tests" / "acceptance"
PORTS = Ports(28400, 28401, 28402, 28403, 28404)
SERVER_KEY = "eptk_" + "s" * 40


# --- Ports (PE 8) -------------------------------------------------------------------


def test_the_demo_apps_developer_ports_are_reserved():
    assert {3200, 3300} <= set(config.RESERVED_PORTS)
    assert guard.reserved_in("http://localhost:3200")
    assert guard.reserved_in("next start -p 3300")
    with pytest.raises(guard.Refused, match="ShopLab port 3200 is reserved"):
        guard.refuse_ports(
            {**PORTS.as_dict(), "ShopLab": 3200}, in_use=lambda _port: False
        )


def test_the_tools_own_app_children_name_no_reserved_port(tmp_path):
    """The API's and dashboard's settings, the app's build and start, and a seed."""
    api = guard.api_settings(
        api_port=28400,
        dashboard_port=28401,
        postgres_port=28402,
        profile="core",
        app_ports=(28403, 28404),
    )
    guard.refuse_reserved_in([], api, what="api")
    guard.refuse_reserved_in(
        [], guard.dashboard_settings(api_port=28400, profile="core"), what="web"
    )
    for app in config.APPS:
        env = guard.app_settings(app=app, api_port=28400, dashboard_port=28401)
        guard.refuse_reserved_in(
            build.app_start_argv(tmp_path, app, PORTS.app(app)), env, what=app
        )
        guard.refuse_reserved_in(build.app_build_argv(tmp_path, app), env, what=app)
        guard.refuse_reserved_in(
            python_argv(tmp_path, config.APP_SEED[app][0], ["--no-history"]),
            guard.seed_settings(app, api, tmp_path / "keys"),
            what=app,
        )


# --- CORS: the dashboard and this video's apps, exactly ------------------------------


def test_cors_allows_the_dashboard_and_the_videos_apps_only():
    def cors(app_ports):
        return guard.api_settings(
            api_port=28400,
            dashboard_port=28401,
            postgres_port=28402,
            profile="core",
            app_ports=app_ports,
        )["CORS_ORIGINS"]

    assert cors(()) == "http://127.0.0.1:28401"
    assert cors((28404,)) == "http://127.0.0.1:28401,http://127.0.0.1:28404"


def test_the_stack_gives_the_api_the_origins_of_the_apps_it_records(tmp_path):
    work = guard.WorkRoot(tmp_path / "work")
    stack = Stack(
        tree=tmp_path,
        ports=PORTS,
        profile="core",
        run_id="t",
        children=Children(work, {}),
        logs=tmp_path / "logs",
        parent={"PATH": "/usr/bin"},
        apps={"streampulse": tmp_path / "app"},
    )
    assert stack.api_env["CORS_ORIGINS"] == (
        "http://127.0.0.1:28401,http://127.0.0.1:28404"
    )
    assert set(stack.app_envs) == {"streampulse"}


# --- Each kind of child has its own allow-list (P3) ------------------------------------

PARENT = {"PATH": "/usr/bin:/bin", "HOME": "/Users/someone", "TMPDIR": "/tmp/x"}


def _children(tmp_path) -> Children:
    return Children(guard.WorkRoot(tmp_path / "work"), PARENT)


@pytest.mark.regression
def test_an_app_key_in_the_dashboard_build_is_refused(tmp_path):
    """The allow-list was the union of every child's, so SHOPLAB_API_KEY in the
    dashboard build's environment passed."""
    env = guard.child_env(
        {
            **guard.dashboard_settings(api_port=28400, profile="core"),
            "SHOPLAB_API_KEY": config.APP_STATIC_KEY["shoplab"],
        },
        parent=PARENT,
    )
    with pytest.raises(
        guard.Refused, match="npm-build .dashboard. was started with SHOPLAB_API_KEY"
    ):
        _children(tmp_path)._record(
            "npm-build", ["npm", "run", "build"], env, "dashboard"
        )


def test_each_kind_is_given_exactly_its_own_keys(tmp_path):
    api = guard.api_settings(
        api_port=28400, dashboard_port=28401, postgres_port=28402, profile="core"
    )
    children = _children(tmp_path)
    # The API and the demo seed carry no app key at all.
    assert not {"SHOPLAB_API_KEY", "STREAMPULSE_API_KEY"} & set(
        guard.allowed_env_keys("api")
    )
    children._record("api", ["uvicorn"], guard.child_env(api, parent=PARENT), "api")
    shop = guard.child_env(
        guard.seed_settings("shoplab", api, tmp_path / "keys"), parent=PARENT
    )
    children._record("seed-shoplab", ["python"], shop, "seed-shoplab")
    with pytest.raises(guard.Refused, match="STREAMPULSE_API_KEY"):
        children._record(
            "seed-shoplab",
            ["python"],
            {**shop, "STREAMPULSE_API_KEY": "x"},
            "seed-shoplab",
        )
    with pytest.raises(guard.Refused, match="SHOPLAB_API_KEY"):
        children._record("api", ["uvicorn"], shop, "api")
    app = guard.child_env(
        guard.app_settings(app="shoplab", api_port=28400, dashboard_port=28401),
        parent=PARENT,
    )
    children._record("app-shoplab", ["node"], app, "app")
    with pytest.raises(guard.Refused, match="POSTGRES_PASSWORD"):
        children._record(
            "app-shoplab", ["node"], {**app, "POSTGRES_PASSWORD": "p"}, "app"
        )
    with pytest.raises(guard.Refused, match="no child kind"):
        guard.allowed_env_keys("everything")


def test_the_env_gate_names_the_kind_it_judged_against():
    ok, numbers, why = gates.child_environments(
        [{"name": "next-build-shoplab", "kind": "app", "env_keys": ["SECRET_KEY"]}]
    )
    assert not ok and why == "next-build-shoplab (app) was started with SECRET_KEY"
    assert set(gates.allowed_by_kind()) == set(guard.KINDS)


# --- The server key (PE 5) ---------------------------------------------------------------


def test_a_key_file_is_read_stripped(tmp_path):
    path = tmp_path / ".api_key_server"
    path.write_text(SERVER_KEY + "\n", encoding="utf-8")
    assert read_key(path) == SERVER_KEY
    path.write_text("one two\n", encoding="utf-8")
    with pytest.raises(StackError, match="does not hold one key"):
        read_key(path)
    path.write_text("\n", encoding="utf-8")
    with pytest.raises(StackError, match="does not hold one key"):
        read_key(path)
    with pytest.raises(StackError, match="was not written by the seed"):
        read_key(tmp_path / "missing")


def test_the_server_key_is_never_given_to_a_child(tmp_path):
    children = _children(tmp_path)
    run.keep_private(SERVER_KEY, director.redaction.Redactor(), children, [])
    env = guard.child_env({}, parent=PARENT)
    with pytest.raises(guard.Refused, match="a private key is on its argv"):
        children._record("sender", ["curl", f"-H X-API-Key: {SERVER_KEY}"], env, "api")
    with pytest.raises(guard.Refused, match="a private key is in its environment"):
        children._record("sender", ["curl"], {**env, "PATH": SERVER_KEY}, "api")
    assert children.started == []


def test_gate_6_counts_the_server_key_exactly_as_read():
    ok, numbers, _ = gates.needle_count(
        config.STATIC_NEEDLES, ["a-token-value-123", SERVER_KEY], [SERVER_KEY]
    )
    assert ok and numbers == {"static": 4, "kept": 2, "total": 6, "required": 1}
    ok, _, why = gates.needle_count(
        config.STATIC_NEEDLES,
        ["a-token-value-123", SERVER_KEY + "\n"],
        [SERVER_KEY + "\n"],
    )
    assert not ok and why == "a key read from a file was kept with whitespace"
    ok, _, why = gates.needle_count(
        config.STATIC_NEEDLES, ["a-token-value-123"], [SERVER_KEY]
    )
    assert not ok and why == "a key read from a file is not a needle"


# --- The whole context's listeners (PE 6) -----------------------------------------------


class Request:
    def __init__(self, url, headers=None, failure=None):
        self.url = url
        self.headers = headers or {}
        self.failure = failure
        self.post_data_json = None


class Response:
    def __init__(self, url, status, request=None):
        self.url = url
        self.status = status
        self.request = request
        self.headers = {"content-type": "text/html"}


class Clock:
    ms = 0.0


@pytest.fixture(autouse=True)
def _stand_in_clock(monkeypatch):
    """The director's clock advances only when a page waits (no real sleeping)."""
    Clock.ms = 0.0
    monkeypatch.setattr(director, "_now_ms", lambda: Clock.ms)


def answer(page, url, status):
    """A request from the page, and its response."""
    request = Request(url)
    page.fire("request", request)
    page.fire("response", Response(url, status, request))
    page.fire("requestfinished", request)
    return request


class Page:
    def __init__(self):
        self.handlers = {}
        self.visits = []
        #: Run when the page opens a URL: the requests a page load makes.
        self.on_goto = None

    def goto(self, url):
        self.visits.append(url)
        if self.on_goto is not None:
            self.on_goto(url)

    def wait_for_timeout(self, ms):
        Clock.ms += ms

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def fire(self, event, value):
        for handler in self.handlers.get(event, []):
            handler(value)

    def wait_for_load_state(self, *args, **kwargs):
        pass


class Context(Page):
    def route(self, *args):
        pass

    def route_web_socket(self, *args):
        pass

    def add_init_script(self, script):
        self.script = script


API = "http://127.0.0.1:28400"
DASH = "http://127.0.0.1:28401"
PULSE = "http://127.0.0.1:28404"


def make(tmp_path):
    page, context = Page(), Context()
    made = director.Director(
        context=context,
        page=page,
        frames=tmp_path / "work" / "v" / "capture" / "frames",
        dashboard=DASH,
        api_port=28400,
        dashboard_port=28401,
        redactor=director.redaction.Redactor(),
        forbidden=(),
        work=guard.WorkRoot(tmp_path / "work"),
        apps={"streampulse": 28404},
    )
    made.install()
    made.tally.apps["streampulse"] = {"answers_400": [], "failed": [], "unanswered": []}
    made.where = "scene player beat 1"
    return made, page, context


@pytest.mark.regression
def test_a_server_error_from_the_api_origin_fails_the_video(tmp_path):
    """Only a 5xx from the dashboard's origin was counted; an app calls the API's."""
    made, page, _ = make(tmp_path)
    page.fire("response", Response(f"{API}/api/v1/feature-flags/evaluate/x", 502))
    with pytest.raises(
        director.CaptureFailed, match="gate 11: 502 from /api/v1/feature-flags"
    ):
        made.check_events()


@pytest.mark.regression
def test_a_page_opened_outside_the_storyboard_fails_the_video(tmp_path):
    """The listeners were the first page's: a second page was not watched at all."""
    made, _, context = make(tmp_path)
    second = Page()
    context.fire("page", second)
    assert "response" in second.handlers and "requestfailed" in second.handlers
    second.fire("response", Response(f"{API}/api/v1/x", 500))
    assert made.tally.server_errors
    with pytest.raises(
        director.CaptureFailed, match="P2: a page opened outside the storyboard"
    ):
        made.check_events()


def test_an_armed_app_scene_counts_answers_failures_and_unanswered(tmp_path):
    made, page, _ = make(tmp_path)
    made.counting = "streampulse"
    answer(page, f"{API}/api/v1/tracking/assign", 404)
    with pytest.raises(
        director.CaptureFailed,
        match="P2: streampulse: answers 400: 404 from /api/v1/tracking/assign",
    ):
        made.check_events()

    made, page, _ = make(tmp_path)
    made.counting = "streampulse"
    failed = Request(
        f"{API}/api/v1/feature-flags/evaluate/x", failure="net::ERR_FAILED"
    )
    page.fire("request", failed)
    page.fire("requestfailed", failed)
    with pytest.raises(
        director.CaptureFailed,
        match="P2: streampulse: failed: /api/v1/feature-flags/evaluate/x: net::ERR_FAILED",
    ):
        made.check_events()

    made, page, _ = make(tmp_path)
    made.counting = "streampulse"
    answered = Request(f"{PULSE}/_next/static/a.js")
    page.fire("request", answered)
    page.fire("requestfinished", answered)
    page.fire("request", Request(f"{API}/api/v1/feature-flags/evaluate/y"))
    made.drain("streampulse")
    with pytest.raises(
        director.CaptureFailed,
        match="P2: streampulse: unanswered: /api/v1/feature-flags/evaluate/y",
    ):
        made.check_events()


@pytest.mark.regression
def test_a_dashboard_scene_fails_on_a_4xx_or_a_request_with_no_answer(tmp_path):
    """#1251: only a 5xx from the dashboard's origin was counted; a 4xx, or a
    request that never answered, passed while a scene was being recorded."""
    made, page, _ = make(tmp_path)
    made.tally.apps["dashboard"] = {"answers_400": [], "failed": [], "unanswered": []}
    made.counting = "dashboard"
    answer(page, f"{DASH}/api/v1/experiments/", 403)
    with pytest.raises(
        director.CaptureFailed,
        match="P2: dashboard: answers 400: 403 from /api/v1/experiments/",
    ):
        made.check_events()

    made, page, _ = make(tmp_path)
    made.tally.apps["dashboard"] = {"answers_400": [], "failed": [], "unanswered": []}
    made.counting = "dashboard"
    page.fire("request", Request(f"{DASH}/api/v1/results/abc"))
    made.drain("dashboard")
    with pytest.raises(
        director.CaptureFailed,
        match="P2: dashboard: unanswered: /api/v1/results/abc",
    ):
        made.check_events()


def test_a_request_is_judged_by_the_scene_it_started_in(tmp_path):
    """Started while nothing was counted, answered 404 inside a counted scene:
    not that scene's. Started in a counted scene, answered after it ended:
    still that scene's."""
    made, page, _ = make(tmp_path)
    early = Request(f"{API}/api/v1/feature-flags/evaluate/x")
    page.fire("request", early)
    made.counting = "streampulse"
    page.fire("response", Response(early.url, 404, early))
    made.check_events()
    late = Request(f"{API}/api/v1/tracking/assign")
    page.fire("request", late)
    made.counting = None
    made.where = "scene experiments beat 1"
    page.fire("response", Response(late.url, 404, late))
    with pytest.raises(
        director.CaptureFailed,
        match=r"streampulse: answers 400: 404 from /api/v1/tracking/assign"
        r" \(started scene player beat 1\)",
    ):
        made.check_events()


def test_the_drain_waits_for_a_late_answer_and_reports_what_is_left(tmp_path):
    made, page, _ = make(tmp_path)
    made.counting = "streampulse"
    slow = Request(f"{API}/api/v1/feature-flags/evaluate/slow")
    page.fire("request", slow)
    stuck = Request(f"{API}/api/v1/feature-flags/evaluate/stuck")
    page.fire("request", stuck)
    page.fire("response", Response(stuck.url, 200, stuck))
    waited = page.wait_for_timeout

    def answer_slow_after_3_s(ms):
        waited(ms)
        if Clock.ms >= 3000:
            page.fire("requestfinished", slow)

    page.wait_for_timeout = answer_slow_after_3_s
    made.drain("streampulse")
    assert Clock.ms >= director.DRAIN_MS
    assert made.tally.apps["streampulse"]["unanswered"] == [
        "/api/v1/feature-flags/evaluate/stuck: answered 200, body unfinished"
        " after 10.0 s (started scene player beat 1)"
    ]


# --- #1251 through Director.scene() -------------------------------------------------------


def scene(**fields):
    data = {
        "id": "list",
        "open": "/experiments",
        "beats": [{"cue": "Your experiments.", "focus": {"css": "table"}}],
    }
    data.update(fields)
    return storyboard.Scene.model_validate(data, strict=False)


def stage(tmp_path, on_goto=None, on_beat=None):
    """A director whose scene() runs for real; only the browser's drawing and
    the needle reads are stood in for."""
    made, page, context = make(tmp_path)
    made.tally.apps.clear()
    made.app_ready = lambda app: {"experiments": 1, "feature_flags": 1}
    made.check_needles = lambda: None
    made.draw_cursor = lambda: None
    made.ring = lambda box: None
    made.recorder.start = lambda: None
    made.recorder.stop = lambda: None
    made.measure_zoom = lambda wanted=None: {"body": wanted}
    made.self_test_overlay = lambda spec: {"offset_px": 0}
    made.beat = lambda sc, index, beat: on_beat(page) if on_beat else None
    page.on_goto = (lambda url: on_goto(page, url)) if on_goto else None
    armed = []
    real_arm = made.arm
    made.arm = lambda app: armed.append(app) or real_arm(app)
    return made, page, armed


@pytest.mark.regression
@pytest.mark.parametrize("status", [403, 404])
def test_a_dashboard_scenes_4xx_fails_it_through_scene(tmp_path, status):
    made, _, _ = stage(
        tmp_path,
        on_beat=lambda page: answer(page, f"{DASH}/api/v1/experiments/", status),
    )
    with pytest.raises(
        director.CaptureFailed, match=f"P2: dashboard: answers 400: {status} from"
    ):
        made.scene(scene())


@pytest.mark.regression
def test_a_dashboard_scenes_unanswered_request_fails_it_through_scene(tmp_path):
    made, _, _ = stage(
        tmp_path,
        on_goto=lambda page, url: page.fire(
            "request", Request(f"{DASH}/api/v1/experiments/")
        ),
    )
    with pytest.raises(
        director.CaptureFailed,
        match="P2: dashboard: unanswered: /api/v1/experiments/: no response",
    ):
        made.scene(scene())


def test_an_app_is_armed_once_and_its_scene_fails_on_a_4xx(tmp_path):
    made, page, armed = stage(tmp_path)
    app_scene = scene(id="player", app="streampulse", open="/")
    made.scene(app_scene)
    made.scene(app_scene)
    assert armed == ["streampulse"]
    assert page.visits == [f"{PULSE}/", f"{PULSE}/", f"{PULSE}/"]
    made.beat = lambda sc, index, beat: answer(
        page, f"{API}/api/v1/feature-flags/evaluate/x", 404
    )
    with pytest.raises(
        director.CaptureFailed, match="P2: streampulse: answers 400: 404"
    ):
        made.scene(app_scene)


def test_an_app_scenes_unanswered_request_fails_it_through_scene(tmp_path):
    made, page, _ = stage(tmp_path)
    made.beat = lambda sc, index, beat: page.fire(
        "request", Request(f"{API}/api/v1/tracking/assign")
    )
    with pytest.raises(
        director.CaptureFailed,
        match="P2: streampulse: unanswered: /api/v1/tracking/assign",
    ):
        made.scene(scene(id="player", app="streampulse", open="/"))


def test_a_clean_dashboard_scene_passes_through_scene(tmp_path):
    made, _, _ = stage(
        tmp_path,
        on_beat=lambda page: answer(page, f"{DASH}/api/v1/experiments/", 200),
    )
    made.scene(scene())
    assert made.tally.apps["dashboard"] == {
        "answers_400": [],
        "failed": [],
        "unanswered": [],
    }


def test_outside_an_armed_app_scene_a_4xx_and_a_failure_are_only_recorded(tmp_path):
    made, page, _ = make(tmp_path)
    answer(page, f"{DASH}/api/v1/auth/me", 401)
    failed = Request(f"{DASH}/_next/data/x.json", failure="net::ERR_ABORTED")
    page.fire("requestfailed", failed)
    made.check_events()
    assert made.tally.failed_events == [
        "/_next/data/x.json: net::ERR_ABORTED (started scene player beat 1)"
    ]


def test_a_request_with_a_key_that_is_not_a_needle_fails_the_video(tmp_path):
    made, page, _ = make(tmp_path)
    page.fire(
        "request",
        Request(f"{API}/x", {"X-API-Key": config.APP_STATIC_KEY["streampulse"]}),
    )
    made.check_events()
    page.fire("request", Request(f"{API}/x", {"x-api-key": "eptk_" + "u" * 40}))
    with pytest.raises(
        director.CaptureFailed,
        match="gate 6: an app request carried an X-API-Key that is not a needle",
    ):
        made.check_events()


def test_the_server_key_kept_with_its_newline_is_not_a_needle_a_header_carries(
    tmp_path,
):
    """PE 5's tamper: registered with the "\\n" kept, a header (which cannot carry
    one) does not match it, and the header check refuses."""
    made, page, _ = make(tmp_path)
    made.redactor.add(SERVER_KEY + "\n")
    page.fire(
        "request", Request(f"{API}/api/v1/tracking/errors", {"x-api-key": SERVER_KEY})
    )
    with pytest.raises(director.CaptureFailed, match="X-API-Key that is not a needle"):
        made.check_events()
    made, page, _ = make(tmp_path)
    run.keep_private(SERVER_KEY, made.redactor, _children(tmp_path), [])
    page.fire(
        "request", Request(f"{API}/api/v1/tracking/errors", {"x-api-key": SERVER_KEY})
    )
    made.check_events()


def test_an_app_is_not_armed_without_an_api_assertion(tmp_path):
    made, _, _ = make(tmp_path)
    with pytest.raises(
        director.CaptureFailed, match="nothing asserts streampulse is ready"
    ):
        made.arm("streampulse")
    with pytest.raises(
        director.CaptureFailed, match="shoplab is not one of the video's apps"
    ):
        made.arm("shoplab")


def test_each_origin_gets_its_own_zoom(tmp_path):
    _, _, context = make(tmp_path)
    assert '"http://127.0.0.1:28404": 1.07' in context.script
    assert '"http://127.0.0.1:28401": 1.33333' in context.script
    ok, numbers, why = gates.zoom(
        {"html": 1.0, "body": 4 / 3}, config.ORIGIN_ZOOM["streampulse"]
    )
    assert not ok and "not 1.070000" in why
    assert gates.zoom({"html": 1.0, "body": 1.07}, 1.07)[0]


# --- Arming asks the API (off camera) -----------------------------------------------------


def test_an_app_is_ready_only_when_all_its_experiments_and_flags_are_active(
    monkeypatch,
):
    lists = {
        "experiments": [
            {"key": "streampulse_wrapped", "status": "active"},
            {"key": "homepage_hero", "status": "draft"},
        ],
        "feature-flags": [{"key": "streampulse_player_v2", "status": "ACTIVE"}],
    }

    def answer(url, token=None, body=None):
        items = lists["experiments" if "/experiments/" in url else "feature-flags"]
        return {"items": items, "total": len(items)}

    monkeypatch.setattr(run, "_api", answer)
    assert run.app_ready(API, "t", "streampulse") == {
        "experiments": 1,
        "feature_flags": 1,
    }
    lists["experiments"][0]["status"] = "draft"
    with pytest.raises(
        StackError, match="experiments not active before its first load"
    ):
        run.app_ready(API, "t", "streampulse")
    with pytest.raises(StackError, match="no experiments keyed shoplab_"):
        run.app_ready(API, "t", "shoplab")


# --- The storyboard ----------------------------------------------------------------------


def board(**changes):
    data = {
        "slug": "90-app-probe",
        "title": "Probe",
        "seed": "demo",
        "seed_counts": {"experiments": 3, "feature_flags": 2, "users": 4},
        "end_state": {"signed_in_as": "admin@demo.com", "experiments_listed": 3},
        "scenes": [
            {
                "id": "player",
                "app": "streampulse",
                "off_camera": [{"seed": "streampulse"}],
                "departures": [
                    {"kind": "off-camera setup", "says": "seeded off camera"}
                ],
                "open": "/",
                "beats": [
                    {
                        "cue": "Its flags were seeded off camera.",
                        "focus": {"role": "heading", "name": "Player"},
                        "payoff": True,
                    }
                ],
            }
        ],
    }
    data["scenes"][0].update(changes)
    return data


def test_an_app_scene_comes_after_its_seed():
    assert storyboard.parse(board()).apps == ["streampulse"]
    with pytest.raises(storyboard.StoryboardError, match="before its seed ran"):
        storyboard.parse(board(off_camera=[]))
    with pytest.raises(storyboard.StoryboardError, match="seed runs twice"):
        storyboard.parse(board(off_camera=[{"seed": "streampulse"}] * 2))


@pytest.mark.parametrize(
    "change",
    [
        {"off_camera": [{"story": 2}]},
        {"off_camera": [{"seed": "demo"}]},
        {"off_camera": [{"seed": "streampulse", "reset": True}]},
        {"signed_in": True},
        {"open": None, "results_of": "Some experiment"},
        {"app": "elsewhere"},
    ],
)
def test_what_an_app_scene_may_not_do_is_refused(change):
    with pytest.raises(storyboard.StoryboardError):
        storyboard.parse(board(**change))


def test_the_probe_is_capture_only_and_not_a_video(tmp_path):
    assert "90-app-probe" not in config.VIDEOS
    assert storyboard.load(run.board_path("90-app-probe")).apps == [
        "shoplab",
        "streampulse",
    ]
    options = run.Options(
        videos=["90-app-probe"],
        ref="v0.0.0",
        repo=tmp_path,
        work_dir=tmp_path / "work",
        profile="core",
        ports=PORTS,
        out=tmp_path / "videos",
    )
    with pytest.raises(guard.Refused, match="never a video: give --capture-only"):
        run.preflight(options, {"PATH": "/usr/bin", "HOME": str(tmp_path / "home")})


# --- The app builds ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["node", "NEXT", "dev", "-p", "28404", "-H", "127.0.0.1"],
        ["npm", "start"],
        ["node", "NEXT", "start", "-p", "3300", "-H", "127.0.0.1"],
        ["node", "NEXT", "start", "-p", "28404"],
    ],
)
def test_an_app_is_served_only_by_next_start_of_its_built_copy(tmp_path, argv):
    real = str(build.next_bin(tmp_path, "streampulse"))
    argv = [real if a == "NEXT" else a for a in argv]
    with pytest.raises(guard.Refused, match="is served by start -p 28404 -H 127.0.0.1"):
        build.refuse_app_argv(argv, tmp_path, "streampulse", 28404)
    build.refuse_app_argv(
        build.app_start_argv(tmp_path, "streampulse", 28404),
        tmp_path,
        "streampulse",
        28404,
    )


def test_an_env_file_in_the_build_tree_is_refused(tmp_path):
    app = build.app_dir(tmp_path, "shoplab")
    app.mkdir(parents=True)
    build.refuse_env_files(tmp_path, "shoplab")
    (app / ".env.local").write_text("NEXT_PUBLIC_CONTROLVAR_API_KEY=x\n")
    with pytest.raises(
        build.BuildError, match="demo/shoplab/.env.local is in the build tree"
    ):
        build.refuse_env_files(tmp_path, "shoplab")


def test_the_bundle_names_the_tools_api_and_no_developer_address(tmp_path):
    chunks = build.app_dir(tmp_path, "shoplab") / ".next" / "static" / "chunks"
    chunks.mkdir(parents=True)
    (chunks / "a.js").write_text('const u="http://127.0.0.1:28400";')
    assert "names http://127.0.0.1:28400" in build.app_bundle_check(
        tmp_path, "shoplab", API
    )
    (chunks / "b.js").write_text('const d="http://localhost:8000";')
    with pytest.raises(build.BuildError, match="names localhost:8000"):
        build.app_bundle_check(tmp_path, "shoplab", API)
    (chunks / "b.js").unlink()
    (chunks / "a.js").write_text("const u=process.env.X;")
    with pytest.raises(build.BuildError, match="does not name the tool's API"):
        build.app_bundle_check(tmp_path, "shoplab", API)


def test_an_app_without_installed_modules_is_refused_naming_npm_ci(tmp_path):
    with pytest.raises(guard.Refused, match="run `npm ci` in demo/streampulse"):
        build.clone_app_node_modules(
            tmp_path / "checkout", tmp_path / "tree", "streampulse"
        )


def test_the_app_tree_links_its_modules_at_the_root(tmp_path):
    checkout = tmp_path / "checkout"
    (checkout / "demo" / "shoplab" / "node_modules" / "next").mkdir(parents=True)
    (
        checkout / "demo" / "shoplab" / "node_modules" / "next" / "package.json"
    ).write_text("{}")
    tree = tmp_path / "tree"
    build.app_dir(tree, "shoplab").mkdir(parents=True)
    build.clone_app_node_modules(checkout, tree, "shoplab")
    assert os.readlink(tree / "node_modules") == "demo/shoplab/node_modules"
    assert (tree / "node_modules" / "next" / "package.json").is_file()


# --- One run per machine (PE 7) -------------------------------------------------------------


def test_the_machine_lock_is_under_home_and_outside_any_checkout(tmp_path):
    home = tmp_path / "home"
    path = guard.machine_lock_path({"HOME": str(home)})
    assert (
        path
        == home.resolve()
        / "Library"
        / "Caches"
        / "experimently-showcase"
        / "showcase.lock"
    )
    (home / ".git").mkdir(parents=True)
    with pytest.raises(guard.Refused, match="inside the git checkout"):
        guard.machine_lock_path({"HOME": str(home)})
    with pytest.raises(guard.Refused, match="HOME is not set"):
        guard.machine_lock_path({})
    with pytest.raises(guard.Refused, match="the machine lock .* is inside"):
        guard.refuse_lock_inside(path, home)
    guard.refuse_lock_inside(path, tmp_path / "work")


FAKE_DOCKER = """#!/bin/sh
echo "$@" >> "{log}"
exit 1
"""

#: Run 1, in its own process: main() with its capture (or its render) held open
#: until the test lets it go, so the machine lock is held for that long.
RUN_ONE = r"""
import pathlib, sys, time
sys.path.insert(0, sys.argv[1])
from showcase.capture import run
hold, ready, where = pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3]), sys.argv[4]

def wait():
    ready.write_text(where)
    while not hold.exists():
        time.sleep(0.05)

def capture(options, environ, log):
    if where == "capture":
        wait()
    return {"90-app-probe": pathlib.Path(sys.argv[5])}

def render(capture, out, environ, log):
    wait()
    return 0

run.run = capture
run.render = render
argv = ["90-app-probe", "--ref", "v0.0.0", "--work-dir", sys.argv[6]]
if where == "capture":
    argv.append("--capture-only")
sys.exit(run.main(argv))
"""


@pytest.mark.regression
@pytest.mark.parametrize("where", ["capture", "render"])
def test_a_second_run_on_the_machine_is_refused_before_any_docker_call(tmp_path, where):
    """The lock was per work root and released before the render: a second run
    with another work root and other ports, or one while the first rendered,
    was not stopped by it. Now it refuses at the lock, before preflight, so the
    stand-in docker is never called."""
    log = tmp_path / "docker-calls.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER.format(log=log), encoding="utf-8")
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(home),
        "TMPDIR": str(tmp_path),
    }
    hold, ready = tmp_path / "let-go", tmp_path / "holding"
    first = subprocess.Popen(
        [
            sys.executable,
            "-c",
            RUN_ONE,
            str(ACCEPTANCE),
            str(hold),
            str(ready),
            where,
            str(tmp_path / "capture-1"),
            str(tmp_path / "work-1"),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for _ in range(600):
            if ready.exists() or first.poll() is not None:
                break
            time.sleep(0.05)
        assert ready.read_text() == where, first.communicate()
        second = subprocess.run(
            [
                sys.executable,
                "-m",
                "showcase.capture",
                "90-app-probe",
                "--ref",
                "v0.0.0",
                "--capture-only",
                "--work-dir",
                str(tmp_path / "work-2"),
                "--api-port",
                "28410",
                "--dashboard-port",
                "28411",
                "--postgres-port",
                "28412",
                "--shoplab-port",
                "28413",
                "--streampulse-port",
                "28414",
            ],
            cwd=ACCEPTANCE,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
    finally:
        hold.write_text("go")
        first.wait(timeout=60)
    assert second.returncode == 2, second.stdout + second.stderr
    assert "another showcase run holds" in second.stdout
    assert "experimently-showcase/showcase.lock" in second.stdout
    assert not log.exists() or log.read_text() == ""
    assert first.returncode == 0, first.stderr.read()


# --- D84: every departure from a new user's path is stated on screen ------------------------


def test_a_seeded_scene_states_it_on_screen():
    assert storyboard.departure_problems(storyboard.parse(board())) == []
    assert storyboard.departure_problems(storyboard.parse(board(departures=[]))) == [
        "scene player: off-camera setup is not stated on screen (departures)"
    ]


def test_a_statement_that_is_not_drawn_is_refused():
    unseen = board(departures=[{"kind": "off-camera setup", "says": "made beforehand"}])
    assert storyboard.departure_problems(storyboard.parse(unseen)) == [
        "scene player: the off-camera setup statement 'made beforehand' is not"
        " in a caption or the card"
    ]


def without_departures(slug: str) -> dict:
    """A video's storyboard as a mapping, every ``departures:`` taken out."""
    raw = yaml.safe_load(run.board_path(slug).read_text(encoding="utf-8"))
    for scene in raw["scenes"]:
        scene.pop("departures", None)
    return raw


def test_simulated_traffic_is_stated_in_every_scene_it_runs_through():
    """V2: from the scene that starts the users to the one that finishes them;
    and V2's off-camera sign-in and warmed results."""
    v2 = storyboard.load(run.board_path("02-first-experiment"))
    assert storyboard.required_departures(v2) == [
        ("setup", "off-camera setup"),
        ("sdk", "simulated traffic"),
        ("arriving", "simulated traffic"),
        ("later", "simulated traffic"),
        ("result", "off-camera setup"),
    ]
    assert storyboard.departure_problems(v2) == []
    # Without its statements, every one of them is refused.
    bare = storyboard.parse(without_departures("02-first-experiment"))
    assert storyboard.departure_problems(bare) == [
        "scene setup: off-camera setup is not stated on screen (departures)",
        "scene sdk: simulated traffic is not stated on screen (departures)",
        "scene arriving: simulated traffic is not stated on screen (departures)",
        "scene later: simulated traffic is not stated on screen (departures)",
        "scene result: off-camera setup is not stated on screen (departures)",
    ]


def test_v1s_warmed_results_are_a_departure_it_states():
    v1 = storyboard.load(run.board_path("01-getting-started"))
    assert storyboard.departure_problems(v1) == []
    # Without its statement, the warmed results are refused as before.
    raw = without_departures("01-getting-started")
    assert storyboard.departure_problems(storyboard.parse(raw)) == [
        "scene dashboard: off-camera setup is not stated on screen (departures)"
    ]


def test_setup_made_before_the_first_scene_is_stated_there():
    v3 = storyboard.load(run.board_path("03-flag-rollout"))
    assert storyboard.departure_problems(v3) == []
    bare = storyboard.parse(without_departures("03-flag-rollout"))
    assert storyboard.departure_problems(bare) == [
        "scene flag: off-camera setup is not stated on screen (departures)"
    ]


#: Every departure V2 and V3 state (D84), with the scene that states it.
STATEMENTS = [
    ("02-first-experiment", "setup", "off-camera setup", "Signed in off camera"),
    (
        "02-first-experiment",
        "sdk",
        "simulated traffic",
        "the shoppers in this video are simulated",
    ),
    (
        "02-first-experiment",
        "arriving",
        "simulated traffic",
        "simulated shoppers arrive",
    ),
    ("02-first-experiment", "later", "simulated traffic", "simulated in minutes"),
    (
        "02-first-experiment",
        "later",
        "time skip",
        "so the results page reads Duration 0d",
    ),
    ("02-first-experiment", "result", "off-camera setup", "Computed off camera first"),
    ("03-flag-rollout", "flag", "off-camera setup", "Signed in off camera"),
    (
        "03-flag-rollout",
        "flag",
        "off-camera setup",
        "Beta testers segment, made off camera",
    ),
]


def test_v2_and_v3_state_exactly_these_departures():
    stated = []
    for slug in ("02-first-experiment", "03-flag-rollout"):
        for scene in storyboard.load(run.board_path(slug)).scenes:
            stated += [(slug, scene.id, d.kind, d.says) for d in scene.departures]
    assert stated == STATEMENTS


@pytest.mark.parametrize("slug, scene_id, kind, says", STATEMENTS)
def test_each_statement_taken_off_the_screen_is_refused(slug, scene_id, kind, says):
    """The words of each statement, taken out of the scene's captions and card
    (the declaration kept), fail the D84 check."""
    raw = yaml.safe_load(run.board_path(slug).read_text(encoding="utf-8"))
    scene = next(s for s in raw["scenes"] if s["id"] == scene_id)
    for item in scene["beats"]:
        item["cue"] = item["cue"].replace(says, "Shown here")
    if "card" in scene:
        scene["card"]["note"] = scene["card"]["note"].replace(says, "Shown here")
    assert storyboard.departure_problems(storyboard.parse(raw)) == [
        f"scene {scene_id}: the {kind} statement {says!r} is not in a caption or"
        " the card"
    ]


@pytest.mark.parametrize("slug", ["02-first-experiment", "03-flag-rollout"])
def test_a_video_whose_departure_is_not_stated_is_refused_before_it_starts(
    tmp_path, monkeypatch, slug
):
    options = run.Options(
        videos=[slug],
        ref="v0.0.0",
        repo=tmp_path,
        work_dir=tmp_path / "work",
        profile="core",
        ports=PORTS,
        out=tmp_path / "videos",
    )
    environ = {"PATH": "/usr/bin", "HOME": str(tmp_path / "home")}
    # As committed, D84 lets it through (anything refused later is not D84's).
    try:
        run.preflight(options, environ)
    except guard.Refused as refused:
        assert "D84" not in str(refused)
    # With its statements taken out, it is refused before anything starts.
    bare = tmp_path / "boards" / f"{slug}.yaml"
    bare.parent.mkdir()
    bare.write_text(yaml.safe_dump(without_departures(slug)), encoding="utf-8")
    real = run.board_path
    monkeypatch.setattr(run, "board_path", lambda s: bare if s == slug else real(s))
    with pytest.raises(
        guard.Refused, match=f"{slug}: D84, a departure from a new user"
    ):
        run.preflight(options, environ)


def test_a_card_body_given_empty_is_refused_and_only_the_drawn_body_counts():
    """``card_html`` draws by presence: ``command=""`` drew an empty terminal and
    no headline, while the D84 screen text read the headline."""
    card = {"label": "Off camera", "note": "Sent by the tool."}
    with pytest.raises(Exception, match="at least 1 character"):
        storyboard.Card.model_validate(
            {**card, "command": "", "headline": "simulated in minutes"}, strict=False
        )
    drawn = storyboard.Card.model_validate(
        {**card, "headline": "simulated in minutes"}, strict=False
    )
    assert drawn.drawn_text() == [
        "Off camera",
        "simulated in minutes",
        "Sent by the tool.",
    ]
    assert "simulated in minutes" in director.card_html(drawn)


@pytest.mark.parametrize(
    "change, needed",
    [
        (
            {
                "signed_in": True,
                "off_camera": [],
                "app": "dashboard",
                "open": "/experiments",
            },
            True,
        ),
        (
            {
                "warm_results": ["Some experiment"],
                "off_camera": [],
                "app": "dashboard",
                "open": "/experiments",
            },
            True,
        ),
        ({"off_camera": [], "app": "dashboard", "open": "/experiments"}, False),
    ],
)
def test_an_off_camera_sign_in_or_warm_results_must_be_stated(change, needed):
    data = board(**change, departures=[])
    found = storyboard.departure_problems(storyboard.parse(data))
    assert found == (
        ["scene player: off-camera setup is not stated on screen (departures)"]
        if needed
        else []
    )


def test_only_the_scene_that_signs_in_must_say_so():
    data = board(signed_in=True, off_camera=[], app="dashboard", open="/experiments")
    second = dict(data["scenes"][0], id="again", departures=[])
    data["scenes"].append(second)
    assert storyboard.required_departures(storyboard.parse(data)) == [
        ("player", "off-camera setup")
    ]


def test_the_browser_context_blocks_service_workers():
    """A worker's request is the worker's: no page listener would see it."""
    import ast

    tree = ast.parse((ACCEPTANCE / "showcase" / "capture" / "run.py").read_text())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "new_context"
    ]
    assert len(calls) == 1
    given = {k.arg: getattr(k.value, "value", None) for k in calls[0].keywords}
    assert given.get("service_workers") == "block"


# --- Portable copies: cp -c is macOS's -------------------------------------------------------

#: A ``cp`` that has no ``-c``, as GNU coreutils' (the Linux CI runners'), and
#: otherwise the system's own; every call is logged.
NO_CLONE_CP = """#!/bin/sh
echo "$@" >> "{log}"
case "$1" in
  -cR) echo "cp: invalid option -- 'c'" >&2; exit 1 ;;
esac
exec /bin/cp "$@"
"""


def _stand_in_cp(tmp_path, monkeypatch, script=NO_CLONE_CP):
    log = tmp_path / "cp-calls.log"
    bin_dir = tmp_path / "cp-bin"
    bin_dir.mkdir()
    cp = bin_dir / "cp"
    cp.write_text(script.format(log=log), encoding="utf-8")
    cp.chmod(cp.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    return log


def _checkout_with_next(tmp_path, app):
    checkout = tmp_path / "checkout"
    modules = checkout / "demo" / app / "node_modules" / "next"
    modules.mkdir(parents=True)
    (modules / "package.json").write_text("{}")
    return checkout


@pytest.mark.regression
def test_a_cp_without_clone_falls_back_to_a_plain_copy(tmp_path, monkeypatch):
    """CI (Linux): ``cp: invalid option -- 'c'`` failed the app tree's build."""
    log = _stand_in_cp(tmp_path, monkeypatch)
    checkout = _checkout_with_next(tmp_path, "shoplab")
    tree = tmp_path / "tree"
    build.app_dir(tree, "shoplab").mkdir(parents=True)
    build.clone_app_node_modules(checkout, tree, "shoplab")
    assert (tree / "node_modules" / "next" / "package.json").is_file()
    calls = log.read_text().splitlines()
    assert [c.split()[0] for c in calls] == ["-cR", "-R"]


def test_the_dashboards_modules_fall_back_the_same_way(tmp_path, monkeypatch):
    log = _stand_in_cp(tmp_path, monkeypatch)
    source = tmp_path / "frontend-modules"
    (source / "next").mkdir(parents=True)
    (source / "next" / "package.json").write_text("{}")
    tree = tmp_path / "tree"
    (tree / "frontend").mkdir(parents=True)
    build.clone_node_modules(source, tree)
    assert (tree / "frontend" / "node_modules" / "next" / "package.json").is_file()
    assert [c.split()[0] for c in log.read_text().splitlines()] == ["-cR", "-R"]


def test_any_other_cp_failure_is_not_retried(tmp_path, monkeypatch):
    failing = """#!/bin/sh
echo "$@" >> "{log}"
echo "cp: cannot create directory: Permission denied" >&2
exit 1
"""
    log = _stand_in_cp(tmp_path, monkeypatch, failing)
    checkout = _checkout_with_next(tmp_path, "shoplab")
    tree = tmp_path / "tree"
    build.app_dir(tree, "shoplab").mkdir(parents=True)
    with pytest.raises(build.BuildError, match="Permission denied"):
        build.clone_app_node_modules(checkout, tree, "shoplab")
    assert len(log.read_text().splitlines()) == 1


# --- E0 follow-ups -----------------------------------------------------------------------------


def test_a_failure_is_charged_to_the_scene_the_request_started_in(tmp_path):
    """Started while nothing was counted, failed inside a counted scene: not counted."""
    made, page, _ = make(tmp_path)
    made.tally.apps["dashboard"] = {"answers_400": [], "failed": [], "unanswered": []}
    early = Request(f"{DASH}/api/v1/experiments/", failure="net::ERR_FAILED")
    page.fire("request", early)
    made.counting = "dashboard"
    page.fire("requestfailed", early)
    made.check_events()
    assert made.tally.failed_events == [
        "/api/v1/experiments/: net::ERR_FAILED (started scene player beat 1)"
    ]
    late = Request(f"{DASH}/api/v1/results/x", failure="net::ERR_FAILED")
    page.fire("request", late)
    made.counting = None
    page.fire("requestfailed", late)
    with pytest.raises(
        director.CaptureFailed, match="P2: dashboard: failed: /api/v1/results/x"
    ):
        made.check_events()


def test_a_request_born_while_the_scene_drains_is_not_counted_unanswered(tmp_path):
    made, page, _ = make(tmp_path)
    made.counting = "streampulse"
    first = Request(f"{API}/api/v1/feature-flags/evaluate/a")
    page.fire("request", first)
    waited = page.wait_for_timeout
    born = []

    def poll_then_answer(ms):
        waited(ms)
        if not born:
            born.append(Request(f"{API}/api/v1/feature-flags/evaluate/poll"))
            page.fire("request", born[0])
        else:
            page.fire("requestfinished", first)

    page.wait_for_timeout = poll_then_answer
    made.drain("streampulse")
    assert made.tally.apps["streampulse"]["unanswered"] == []
    assert Clock.ms < director.DRAIN_MS


def test_a_departure_stated_in_a_code_card_line_counts():
    """A code card draws its quoted lines: they are its screen text (V2's SDK card)."""
    v2 = storyboard.load(run.board_path("02-first-experiment"))
    card = next(s.card for s in v2.scenes if s.id == "sdk")
    assert card is not None and card.code is not None
    drawn = card.drawn_text()
    assert drawn[0] == card.label and drawn[-1] == card.note
    assert drawn[1:-1] == list(card.code.lines)
    assert all(
        line
        in storyboard.scene_screen_text(next(s for s in v2.scenes if s.id == "sdk"))
        for line in card.code.lines
        if line
    )


def test_the_app_settings_name_each_setting_twice_and_nothing_else_may():
    """An app is built from an archived ref, which may read only the names
    before the rename: app_settings() gives both. No other child is allowed
    either name."""
    app = guard.app_settings(app="shoplab", api_port=28400, dashboard_port=28401)
    for setting in ("API_URL", "API_KEY", "DASHBOARD_URL"):
        new, old = (
            f"NEXT_PUBLIC_CONTROLVAR_{setting}",
            f"NEXT_PUBLIC_EXPERIMENTLY_{setting}",
        )
        assert app[new] == app[old] and app[new], setting
        for kind in ("docker", "api", "seed-shoplab", "dashboard"):
            assert not {new, old} & guard.allowed_env_keys(kind), kind
