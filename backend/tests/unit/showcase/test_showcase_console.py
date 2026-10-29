"""Video 9's console scene (#1442): the page, the calls, gate api, gates 10a
and 10b, and the setup's exclusion group.

The director films the console scene on a stand-in page and a stand-in API,
in the answers' own field order (the API's response models at this tree);
each property is shown holding, then refused on one planted defect. No
browser, Docker, network (but one loopback server for the redirect test) or
database.
"""

from __future__ import annotations

import copy
import datetime as dt
import http.server
import json
import re
import sys
import threading
import types
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
import yaml
from showcase import contract
from showcase.capture import config, console, director, gates, guard, run, storyboard

from backend.app.schemas.experiment import ExperimentDetailResponse, ExperimentResponse
from backend.app.schemas.global_holdout import GlobalHoldoutResponse

pytestmark = [pytest.mark.unit]

SHOWCASE = Path(__file__).resolve().parents[4] / "tests" / "acceptance" / "showcase"
V9 = SHOWCASE / "storyboards" / "09-design-in-depth.yaml"

EXPERIMENT = "0b6b4f1e-0000-4000-8000-0000000000e9"
GROUP = "8c2e1d4a-1111-4222-8333-0000000000a1"
HOLDOUT = "5d1f9a7b-2222-4333-8444-0000000000b2"
OWNER = "6b1f0c1e-0000-4000-8000-000000000001"
#: The run's admin token, as the API makes one: a needle, never drawn.
TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI2YjFmMGMxZSIsImV4cCI6MTc5MzMxMDk1OX0"
    ".q8wJ3n2Vb7XkT0mPz5LrYc9AuEeHs4GdNiOf1KtWjQo"
)
NOW = dt.datetime(2026, 10, 29, 22, 40, tzinfo=dt.timezone.utc)
START = "2026-10-30T09:00:00Z"


def board() -> storyboard.Storyboard:
    return storyboard.load(V9)


def api_scene(made: storyboard.Storyboard) -> storyboard.Scene:
    return next(s for s in made.scenes if s.console is not None)


# ---------------------------------------------------------------------------
# The stand-in API: answers in the response models' own field order
# ---------------------------------------------------------------------------


def experiment_answer(model: Any, **set_: Any) -> Dict[str, Any]:
    """An experiment as the API answers it, every field in the model's order."""
    values: Dict[str, Any] = {
        "id": EXPERIMENT,
        "name": "Delivery promise",
        "key": "delivery_promise",
        "status": "draft",
        "owner_id": OWNER,
        "experiment_type": "a_b",
        "sequential_testing_enabled": False,
        "mutual_exclusion_group_id": None,
        "start_date": None,
        "owner_name": "Demo Admin",
    }
    values.update(set_)
    return {name: values.get(name) for name in model.model_fields}


def holdout_answer(**set_: Any) -> Dict[str, Any]:
    values: Dict[str, Any] = {
        "id": HOLDOUT,
        "name": "Q4 holdout",
        "holdout_percentage": 5,
        "is_active": True,
        "owner_id": OWNER,
        "created_at": "2026-10-29T22:41:00",
        "updated_at": "2026-10-29T22:41:00",
        "activated_at": "2026-10-29T22:41:00",
    }
    values.update(set_)
    return {name: values.get(name) for name in GlobalHoldoutResponse.model_fields}


class StandInApi:
    """The six calls, answered as the API at this tree answers them."""

    def __init__(self):
        self.sent: List[Tuple[str, str, Any, int]] = []
        self.answers: Dict[Tuple[str, str], Tuple[int, Any]] = {}
        self.start: Optional[str] = None

    def __call__(self, method, path, body, status):
        self.sent.append((method, path, copy.deepcopy(body), status))
        if (method, path) in self.answers:
            got, answer = self.answers[(method, path)]
            if got != status:
                raise console.CallRefused(
                    f"{method} {path} was answered {got}, not {status}"
                )
            return got, copy.deepcopy(answer)
        if path == f"/api/v1/mutual-exclusion-groups/{GROUP}/experiments":
            return 200, {"status": "ok", "experiment_id": EXPERIMENT, "group_id": GROUP}
        if (method, path) == ("POST", "/api/v1/holdout"):
            return 201, holdout_answer()
        if path == f"/api/v1/experiments/{EXPERIMENT}/schedule":
            self.start = body["start_date"]
            return 200, experiment_answer(
                ExperimentResponse,
                start_date=self.start.rstrip("Z"),
                mutual_exclusion_group_id=GROUP,
            )
        if (method, path) == ("PUT", f"/api/v1/experiments/{EXPERIMENT}"):
            return 200, experiment_answer(
                ExperimentResponse,
                start_date=self.start.rstrip("Z"),
                mutual_exclusion_group_id=GROUP,
                sequential_testing_enabled=True,
            )
        if (method, path) == ("GET", f"/api/v1/experiments/{EXPERIMENT}"):
            return 200, experiment_answer(
                ExperimentDetailResponse,
                start_date=self.start.rstrip("Z"),
                mutual_exclusion_group_id=GROUP,
                sequential_testing_enabled=True,
            )
        if (method, path) == ("GET", "/api/v1/holdout"):
            return 200, holdout_answer()
        raise AssertionError(f"an unexpected call: {method} {path}")


# ---------------------------------------------------------------------------
# The stand-in page: draws what DRAW_JS is given, reads it back
# ---------------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.ms = 1_000_000.0

    def now(self):
        return self.ms


class Overlay:
    def __exit__(self, *args):
        pass


class Screencast:
    def __init__(self):
        self.overlays: List[str] = []

    def start(self, **kwargs):
        pass

    def stop(self):
        pass

    def show_overlay(self, html):
        self.overlays.append(html)
        return Overlay()


class Mouse:
    def move(self, x, y):
        pass

    def click(self, x, y):
        pass


class Element:
    def __init__(self, page, selector=""):
        self.page = page
        self.selector = selector

    def nth(self, index):
        return self

    def wait_for(self, state, timeout):
        if not self.page.found(self.selector):
            raise director.PlaywrightError(f"{self.selector} not found")

    def bounding_box(self):
        return {"x": 160.0, "y": 140.0, "width": 1600.0, "height": 660.0}

    def evaluate(self, script, *args):
        if script == director.ON_TOP_JS:
            return True
        raise AssertionError(f"an unexpected element script: {script[:40]}")

    def inner_text(self):
        return self.page.text()


DRAWN_JS = "the stand-in's page text and fields"


class StandIn:
    """A console page: ``DRAW_JS`` fills it, ``READ_JS`` and ``DRAWN_JS`` read it."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self.url = "http://127.0.0.1:28401/experiments/" + EXPERIMENT
        self.mouse = Mouse()
        self.screencast = Screencast()
        self.html = ""
        self.state: Dict[str, Any] = {"request": [], "response": [], "note": ""}
        self.marked: List[Tuple[str, bool]] = []
        #: Plants: a canned response drawn instead, or the panel left as it was.
        self.canned: Optional[List[List[Any]]] = None
        self.frozen_after: Optional[int] = None
        self.draws = 0
        self.extra = ""

    def wait_for_timeout(self, ms):
        self.clock.ms += ms

    def _element(self, *args, **kwargs):
        return Element(self, args[0] if args else "")

    get_by_role = get_by_text = get_by_label = get_by_test_id = locator = _element

    def found(self, selector: str) -> bool:
        field = console.FIELD_SELECTOR.match(selector)
        if field:
            return any(key == field.group(1) for _t, _c, key in self.state["response"])
        return True

    def set_content(self, html):
        self.html = html

    def goto(self, url):
        pass

    def text(self) -> str:
        label = re.search(r'<div id="label">(.*?)</div>', self.html)
        lines = [line[0] for line in [*self.state["request"], *self.state["response"]]]
        return "\n".join(
            [label.group(1) if label else "", *lines, self.state["note"], self.extra]
        )

    def evaluate(self, script, *args):
        if script == console.DRAW_JS:
            self.draws += 1
            if self.frozen_after is not None and self.draws > self.frozen_after:
                return True
            state = copy.deepcopy(args[0])
            if self.canned is not None and state["response"]:
                state["response"] = self.canned
            self.state = state
            return True
        if script == console.READ_JS:
            return {
                "request": [line[0] for line in self.state["request"]],
                "response": [[line[0], line[2]] for line in self.state["response"]],
                "note": self.state["note"],
            }
        if script == console.MARK_JS:
            self.marked.append(tuple(args[0]))
            return True
        if script == DRAWN_JS:
            return self.text(), []
        if script == director.HEADER_JS:
            return {"links": 0, "wrapped": [], "cut": []}
        if script == director.VIEWPORT_TEXT_JS:
            return self.text()
        if script == director.BROKEN_JS:
            return {"overlay": False, "alerts": []}
        raise AssertionError(f"an unexpected page script: {script[:40]}")


@pytest.fixture
def stage(tmp_path, monkeypatch):
    """A director on a stand-in page and clock, the calls on a stand-in API."""
    clock = Clock()
    monkeypatch.setattr(director, "_now_ms", clock.now)
    stand_in = types.ModuleType("docs_runner.execute")
    stand_in.DRAWN_JS = DRAWN_JS
    monkeypatch.setitem(sys.modules, "docs_runner.execute", stand_in)

    def make(api: Optional[StandInApi] = None):
        page = StandIn(clock)
        api = api or StandInApi()
        redactor = director.redaction.Redactor()
        redactor.add(TOKEN)
        made = director.Director(
            context=object(),
            page=page,
            frames=tmp_path / "work" / "09" / "capture" / "frames",
            dashboard="http://127.0.0.1:28401",
            api_port=28400,
            dashboard_port=28401,
            redactor=redactor,
            forbidden=(),
            work=guard.WorkRoot(tmp_path / "work"),
        )
        made.calls = console.Calls(
            api, {"experiment": EXPERIMENT, "group": GROUP}, now=lambda: NOW
        )
        return made, page, api

    return make


def film(made: director.Director, scene: storyboard.Scene) -> None:
    made.scene(scene)


def refused_while_filming(made, scene, gate: str, words: str) -> str:
    with pytest.raises(director.CaptureFailed) as raised:
        film(made, scene)
    assert raised.value.gate == gate, str(raised.value)
    assert words in str(raised.value), str(raised.value)
    return str(raised.value)


# ---------------------------------------------------------------------------
# The page: light, its dark panel at most 60% of it, nothing fetched
# ---------------------------------------------------------------------------


def test_the_console_body_is_light_and_its_dark_panel_covers_at_most_60_percent():
    page = console.console_html("Signed in to the API off camera")
    assert console.BODY == "#F8FAFC" and console.PANEL == "#0F172A"
    body = re.search(r"\nbody \{([^}]*)\}", page).group(1)
    assert f"background:{console.BODY};" in body
    assert f"background:{console.PANEL};" in re.search(
        r"#panel \{([^}]*)\}", page
    ).group(1)
    assert console.dark_share() <= console.DARK_SHARE_MAX
    assert console.PANEL_W <= 1760
    assert console.PANEL_H < contract.PAGE_H


def test_the_console_forces_zoom_1_and_fetches_nothing():
    page = console.console_html("Signed in to the API off camera")
    assert "html body { zoom:1 !important; }" in page
    for fetch in ("src=", "href=", "url(", "@import", "<link", "<script", "http"):
        assert fetch not in page, fetch


def test_the_label_is_text_never_markup():
    page = console.console_html("<b>Signed</b> & in")
    assert "&lt;b&gt;Signed&lt;/b&gt; &amp; in" in page


def test_the_panel_holds_12_lines_and_its_text_is_set_as_text():
    assert console.PANEL_H == int(12 * 28 * 1.5 + 2 * 32 + 2 * 12 + 2)
    assert "textContent" in console.DRAW_JS and "innerHTML" not in console.DRAW_JS


# ---------------------------------------------------------------------------
# The call: (status, body), GET/POST/PUT, no redirect, the exact status
# ---------------------------------------------------------------------------


def opener_answering(status: int, body: Any, seen: List[Any]):
    def opener(request):
        seen.append(request)
        return status, json.dumps(body).encode()

    return opener


def test_a_call_sends_the_token_in_a_header_and_returns_the_status_and_the_answer():
    seen: List[Any] = []
    got = console.send(
        "http://127.0.0.1:28400",
        TOKEN,
        "PUT",
        f"/api/v1/experiments/{EXPERIMENT}",
        {"sequential_testing_enabled": True},
        200,
        opener=opener_answering(200, {"status": "draft"}, seen),
    )
    assert got == (200, {"status": "draft"})
    (request,) = seen
    assert request.get_method() == "PUT"
    assert request.full_url == f"http://127.0.0.1:28400/api/v1/experiments/{EXPERIMENT}"
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"
    assert json.loads(request.data) == {"sequential_testing_enabled": True}


@pytest.mark.parametrize(
    "status, declared, words",
    [
        (307, 200, "was answered 307, not 200"),
        (200, 201, "was answered 200, not 201"),
        (422, 200, "was answered 422, not 200: Start date must be in the future"),
    ],
)
def test_any_status_but_the_declared_one_is_refused_naming_it(status, declared, words):
    seen: List[Any] = []
    with pytest.raises(console.CallRefused, match=re.escape(words)):
        console.send(
            "http://127.0.0.1:28400",
            TOKEN,
            "POST",
            "/api/v1/holdout",
            {"name": "Q4 holdout"},
            declared,
            opener=opener_answering(
                status, {"detail": "Start date must be in the future"}, seen
            ),
        )


@pytest.mark.parametrize(
    "api_url, method, path, words",
    [
        ("http://127.0.0.1:8000", "GET", "/api/v1/holdout", "names a reserved port"),
        ("https://127.0.0.1:28400", "GET", "/api/v1/holdout", "not plain http"),
        (
            "http://127.0.0.1:28400",
            "DELETE",
            "/api/v1/holdout",
            "only GET, POST and PUT",
        ),
        ("http://127.0.0.1:28400", "GET", "/health", "under /api/v1/"),
    ],
)
def test_a_call_is_refused_before_it_is_made(api_url, method, path, words):
    seen: List[Any] = []
    with pytest.raises(console.CallRefused, match=re.escape(words)):
        console.send(
            api_url,
            TOKEN,
            method,
            path,
            None,
            200,
            opener=opener_answering(200, {}, seen),
        )
    assert seen == []


class Redirecting(http.server.BaseHTTPRequestHandler):
    hits: List[str] = []

    def do_GET(self):
        Redirecting.hits.append(self.path)
        if self.path == "/api/v1/holdout":
            self.send_response(307)
            self.send_header("Location", "/api/v1/elsewhere")
            self.end_headers()
        else:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


@pytest.mark.regression
def test_a_redirect_is_the_answer_never_followed():
    """urllib follows a 307 by default: the helper's opener does not, so the
    307 is what the take sees, and refuses."""
    server = http.server.HTTPServer(("127.0.0.1", 0), Redirecting)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    Redirecting.hits = []
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        with pytest.raises(console.CallRefused, match="was answered 307, not 200"):
            console.send(url, TOKEN, "GET", "/api/v1/holdout", None, 200)
    finally:
        server.shutdown()
        server.server_close()
    assert Redirecting.hits == ["/api/v1/holdout"]


# ---------------------------------------------------------------------------
# The exchange's text, in the answer's own order
# ---------------------------------------------------------------------------


def made_exchange(call: storyboard.Call, answer: Dict[str, Any]) -> console.Exchange:
    calls = console.Calls(
        lambda *a: (call.answer.status, answer),
        {"experiment": EXPERIMENT, "group": GROUP, "start": START},
    )
    exchange = calls.begin("api", 4, call)
    calls.make(exchange, call)
    return exchange


def test_the_sequential_answer_is_drawn_in_12_lines_in_its_own_order():
    call = api_scene(board()).beats[3].call
    answer = experiment_answer(
        ExperimentResponse,
        start_date="2026-10-30T09:00:00",
        mutual_exclusion_group_id=GROUP,
        sequential_testing_enabled=True,
    )
    assert console.exchange_text(made_exchange(call, answer)) == [
        f"PUT /api/v1/experiments/{EXPERIMENT[:8]}…",
        '{"sequential_testing_enabled": true}',
        "200 OK",
        "…",
        '"status": "draft"',
        "…",
        '"start_date": "2026-10-30T09:00:00"',
        "…",
        '"sequential_testing_enabled": true',
        "…",
        f'"mutual_exclusion_group_id": "{GROUP[:8]}…"',
        "…",
    ]


# ---------------------------------------------------------------------------
# Filming the scene: gate api holds, then each plant is refused
# ---------------------------------------------------------------------------


def test_the_console_scene_films_its_six_exchanges_and_gate_api_passes(stage):
    made, page, api = stage()
    scene = api_scene(board())
    film(made, scene)
    assert [(m, p) for m, p, _b, _s in api.sent] == [
        ("POST", f"/api/v1/mutual-exclusion-groups/{GROUP}/experiments"),
        ("POST", "/api/v1/holdout"),
        ("PUT", f"/api/v1/experiments/{EXPERIMENT}/schedule"),
        ("PUT", f"/api/v1/experiments/{EXPERIMENT}"),
        ("GET", f"/api/v1/experiments/{EXPERIMENT}"),
        ("GET", "/api/v1/holdout"),
    ]
    assert api.sent[2][2] == {"start_date": START}
    problems, numbers = console.api_problems(
        board(), made.calls, [*config.STATIC_NEEDLES, *sorted(made.redactor.values)]
    )
    assert problems == []
    assert [len(console.exchange_text(e)) for e in made.calls.exchanges] == [
        6,
        9,
        8,
        12,
        11,
        8,
    ]
    assert made.calls.kept_up == [
        {
            "scene": "api",
            "beat": 5,
            "keeps": ["api", 4],
            "at_cue_start": [],
            "at_cue_end": [],
        }
    ]
    assert made.calls.values["holdout"] == HOLDOUT
    # No cursor on the console, and it is not a still span (its holds are not
    # a card's); the rings bar their field as well.
    assert not any("clip-path" in html for html in page.screencast.overlays)
    assert made.still_spans == []
    assert ("group_id", True) in page.marked and ("group_id", False) in page.marked
    assert made.tally.console_secret_checks == 6 * 3 + 2
    written = json.dumps(numbers)
    assert TOKEN not in written and "Bearer" not in written
    assert all(e.record()["omitted"] >= 0 for e in made.calls.exchanges)


def test_a_307_where_200_is_declared_is_refused_and_no_answer_is_drawn(stage):
    api = StandInApi()
    api.answers[("POST", f"/api/v1/mutual-exclusion-groups/{GROUP}/experiments")] = (
        307,
        None,
    )
    made, page, _ = stage(api)
    refused_while_filming(made, api_scene(board()), "api", "was answered 307, not 200")
    assert page.state["response"] == []


def test_a_200_where_201_is_declared_is_refused(stage):
    api = StandInApi()
    api.answers[("POST", "/api/v1/holdout")] = (200, holdout_answer())
    made, _page, _ = stage(api)
    refused_while_filming(made, api_scene(board()), "api", "was answered 200, not 201")


def test_a_past_start_is_refused_with_its_status_and_detail_and_nothing_drawn(stage):
    def past(method, path, body, status):
        if path.endswith("/schedule"):
            raise console.CallRefused(
                f"{method} {path} was answered 422, not 200: Start date must be in the"
                " future"
            )
        return StandInApi()(method, path, body, status)

    made, page, _ = stage()
    made.calls.sender = past
    refused_while_filming(
        made,
        api_scene(board()),
        "api",
        "422, not 200: Start date must be in the future",
    )
    assert page.state["response"] == []


def test_a_constant_answer_drawn_is_not_the_apis(stage):
    made, page, _ = stage()
    page.canned = [
        ["200 OK", "status", None],
        ['"status": "ok"', "", "status"],
        ['"experiment_id": "00000000…"', "", "experiment_id"],
        ['"group_id": "00000000…"', "", "group_id"],
    ]
    refused_while_filming(
        made, api_scene(board()), "api", "the drawn answer is not the API's"
    )


def test_the_previous_beats_panel_left_up_is_refused(stage):
    made, page, _ = stage()
    page.frozen_after = 2
    refused_while_filming(
        made,
        api_scene(board()),
        "api",
        "scene api beat 2: the request drawn is not the one to be sent",
    )


def test_a_body_drawn_that_is_not_the_one_sent_is_refused(stage, monkeypatch):
    made, _page, _ = stage()
    make = console.Calls.make

    def swapped(self, exchange, call):
        if exchange.method == "POST" and exchange.template == "/api/v1/holdout":
            exchange.body = dict(exchange.body, holdout_percentage=10)
        return make(self, exchange, call)

    monkeypatch.setattr(console.Calls, "make", swapped)
    refused_while_filming(
        made,
        api_scene(board()),
        "api",
        "the request sent is not the one drawn at the cue's start",
    )


def test_a_shown_field_missing_from_the_answer_is_refused(stage):
    api = StandInApi()
    answer = holdout_answer()
    del answer["is_active"]
    api.answers[("POST", "/api/v1/holdout")] = (201, answer)
    made, _page, _ = stage(api)
    refused_while_filming(
        made, api_scene(board()), "api", "the answer has no 'is_active'"
    )


@pytest.mark.regression
def test_a_credential_in_an_answer_is_a_needle_from_then_on(stage):
    """The tool's own calls are not a page's: the listener never sees them, so
    each answer goes through register_credentials where it is received."""
    api = StandInApi()
    api.answers[("GET", "/api/v1/holdout")] = (
        200,
        dict(holdout_answer(), api_key="eptk_live_" + "c0ffee" * 6),
    )
    made, _page, _ = stage(api)
    film(made, api_scene(board()))
    assert "eptk_live_" + "c0ffee" * 6 in made.redactor.values


def test_an_exchange_text_that_drops_a_field_fails_the_raw_answer_check(
    stage, monkeypatch
):
    """The raw answer's own value, on its field's own line: built without
    ``exchange_lines``, so a field it drops is still missed (one no ring
    looks for, so that this check alone stands between it and the take)."""
    lines = console.exchange_lines

    def dropping(exchange):
        request, response = lines(exchange)
        return request, [line for line in response if line[2] != "experiment_id"]

    monkeypatch.setattr(console, "exchange_lines", dropping)
    made, _page, _ = stage()
    refused_while_filming(
        made, api_scene(board()), "api", "experiment_id is drawn on 0 lines, not 1"
    )


def test_the_token_cut_short_in_the_panel_is_refused_and_a_holdout_answer_passes():
    holdout = "\n".join(
        console.exchange_text(
            made_exchange(api_scene(board()).beats[1].call, holdout_answer())
        )
    )
    needles = [*config.STATIC_NEEDLES, TOKEN]
    assert console.secret_findings(holdout, needles) == []
    for cut in (22, 40):
        found = console.secret_findings(f'"name": "{TOKEN[:cut]}"', needles)
        assert found, cut


def test_the_token_cut_short_in_a_drawn_answer_refuses_the_take(stage):
    api = StandInApi()
    api.answers[("POST", "/api/v1/holdout")] = (
        201,
        holdout_answer(name=TOKEN[:22]),
    )
    made, _page, _ = stage(api)
    refused_while_filming(
        made, api_scene(board()), "gate 6", "the console's text gives away"
    )


def test_the_token_whole_on_the_console_page_refuses_the_take(stage):
    made, page, _ = stage()
    page.extra = TOKEN
    refused_while_filming(made, api_scene(board()), "gate 6", "")


class FontRequest:
    """What a console page that fetched a font would have asked for."""

    url = "http://127.0.0.1:28401/fonts/mono.woff2"
    headers: Dict[str, str] = {}
    method = "GET"
    post_data_json = None


def test_a_request_while_the_console_is_filmed_is_refused(stage):
    made, page, _ = stage()
    draw = page.evaluate

    def fetching(script, *args):
        if script == console.DRAW_JS:
            made._on_request(FontRequest())
        return draw(script, *args)

    page.evaluate = fetching
    refused_while_filming(
        made,
        api_scene(board()),
        "P2",
        "the console page made 13 request(s), first /fonts/mono.woff2",
    )


# ---------------------------------------------------------------------------
# Gate api, from the records
# ---------------------------------------------------------------------------


def filmed(stage) -> Tuple[director.Director, List[str]]:
    made, _page, _ = stage()
    film(made, api_scene(board()))
    return made, [*config.STATIC_NEEDLES, *sorted(made.redactor.values)]


def test_gate_api_refuses_an_exchange_missing_or_out_of_order(stage):
    made, needles = filmed(stage)
    made.calls.exchanges.pop(2)
    problems, _ = console.api_problems(board(), made.calls, needles)
    assert problems == [
        "5 exchanges made; the board has 6 calls, in another order or number"
    ]


def test_gate_api_refuses_a_value_the_board_does_not_declare(stage):
    made, needles = filmed(stage)
    made.calls.exchanges[5].shown["id"] = GROUP
    problems, _ = console.api_problems(board(), made.calls, needles)
    assert problems == [
        "scene api beat 7 (GET /api/v1/holdout): id is '8c2e1d4a…', not '5d1f9a7b…'"
    ]


def test_gate_api_refuses_a_start_an_hour_off(stage):
    made, needles = filmed(stage)
    made.calls.exchanges[2].shown["start_date"] = "2026-10-30T10:00:00"
    problems, _ = console.api_problems(board(), made.calls, needles)
    assert len(problems) == 1 and "start_date is '2026-10-30T10:00:00'" in problems[0]


def test_gate_api_refuses_a_send_outside_its_beat(stage):
    made, needles = filmed(stage)
    times = made.calls.exchanges[0].times
    times["sent"] = times["cue_end"] + 1
    problems, _ = console.api_problems(board(), made.calls, needles)
    assert "not drawn, sent, answered and drawn in that order" in problems[0]


def test_gate_api_refuses_a_record_holding_the_token(stage):
    made, needles = filmed(stage)
    made.calls.exchanges[0].kept["leak"] = TOKEN
    problems, _ = console.api_problems(board(), made.calls, needles)
    assert "a record holds 'eyJ'" in problems


# ---------------------------------------------------------------------------
# Gates 10a and 10b
# ---------------------------------------------------------------------------


def setup_of() -> storyboard.Setup:
    return board().setup


def group(members=("Checkout Button Color",), allocation=1.0, id_=GROUP):
    return {
        "id": id_,
        "name": "Checkout tests",
        "traffic_allocation": allocation,
        "experiments": [
            {"id": "x", "name": name, "status": "draft"} for name in members
        ],
    }


def before(**changes) -> Tuple[bool, Dict[str, Any], str]:
    reads: Dict[str, Any] = {
        "groups": {"items": [group()], "total": 1},
        "group": group(),
        "holdouts": {"items": [], "total": 0},
        "saved": experiment_answer(ExperimentDetailResponse),
    }
    reads.update(changes)
    return gates.console_before(setup_of(), **reads)


def test_10a_passes_at_the_saved_draft_and_untouched_keeps_its_8_rows():
    ok, numbers, why = before()
    assert ok, why
    assert numbers["group_id"] == GROUP
    assert numbers["sequential_testing_config"] is None
    assert [k for k, _ in gates.UNTOUCHED] == [
        "sequential_testing_enabled",
        "variance_reduction_config",
        "mutual_exclusion_group_id",
        "optimization_type",
        "split_url_config",
        "start_date",
        "end_date",
        "resume_at",
    ]


@pytest.mark.parametrize(
    "changes, words",
    [
        (
            {"groups": {"items": [group(), dict(group(), name="Other")], "total": 2}},
            "the API holds the groups",
        ),
        (
            {"holdouts": {"items": [{}], "total": 1}},
            "holdouts before the video makes one",
        ),
        ({"group": group(allocation=0.5)}, "takes 0.5 of the traffic"),
        (
            {"group": group(members=("Checkout Button Color", "Delivery promise"))},
            "holds ['Checkout Button Color', 'Delivery promise']",
        ),
    ],
    ids=[
        "a-second-group",
        "a-holdout-already",
        "half-the-traffic",
        "the-draft-already-in",
    ],
)
def test_10a_refuses_each_planted_state(changes, words):
    ok, _numbers, why = before(**changes)
    assert not ok and words in why, why


def test_10a_refuses_a_board_whose_group_takes_half_the_traffic():
    data = yaml.safe_load(V9.read_text(encoding="utf-8"))
    data["setup"]["exclusion_groups"][0]["traffic_allocation"] = 0.5
    half = storyboard.parse(data).setup
    ok, _numbers, why = gates.console_before(
        half,
        groups={"items": [group(allocation=0.5)], "total": 1},
        group=group(allocation=0.5),
        holdouts={"items": [], "total": 0},
        saved=experiment_answer(ExperimentDetailResponse),
    )
    assert not ok and "the video's group takes all of it (1.0)" in why


def records_after(stage) -> List[Dict[str, Any]]:
    made, _ = filmed(stage)
    return [e.record() for e in made.calls.exchanges]


def after(stage, saved=None, **changes) -> Tuple[bool, Dict[str, Any], str]:
    reads: Dict[str, Any] = {
        "before": before()[1],
        "group": group(members=("Checkout Button Color", "Delivery promise")),
        "holdouts": {"items": [holdout_answer()], "total": 1},
        "active": holdout_answer(),
        "saved": saved
        or experiment_answer(
            ExperimentDetailResponse,
            start_date="2026-10-30T09:00:00",
            mutual_exclusion_group_id=GROUP,
            sequential_testing_enabled=True,
        ),
        "exchanges": records_after(stage),
        "start": START,
    }
    reads.update(changes)
    return gates.console_after(board().end_state.design, setup_of(), **reads)


def test_10b_passes_when_the_api_holds_what_the_calls_set(stage):
    ok, numbers, why = after(stage)
    assert ok, why
    assert numbers["members"] == ["Checkout Button Color", "Delivery promise"]


@pytest.mark.parametrize(
    "change, words",
    [
        (
            lambda s: {"saved": dict(s, mutual_exclusion_group_id=HOLDOUT)},
            "is in group",
        ),
        (lambda s: {"group": group()}, "the group holds ['Checkout Button Color']"),
        (
            lambda s: {"group": group(members=("Delivery promise",))},
            "the group holds ['Delivery promise']",
        ),
        (
            lambda s: {"active": holdout_answer(is_active=False)},
            "the active holdout is",
        ),
        (
            lambda s: {"holdouts": {"items": [{}, {}], "total": 2}},
            "the API holds 2 holdouts",
        ),
        (
            lambda s: {"saved": dict(s, start_date="2026-10-30T10:00:00")},
            "starts '2026-10-30T10:00:00'",
        ),
        (
            lambda s: {
                "saved": dict(
                    s,
                    sequential_testing_config={
                        "method": "msprt",
                        "alpha": 0.05,
                        "tau_squared": 0.001,
                        "alpha_spending": "obrien_fleming",
                        "max_looks": 10,
                    },
                )
            },
            "sequential_testing_config is",
        ),
        (
            lambda s: {"saved": dict(s, sequential_testing_method="msprt")},
            "sequential_testing_method is 'msprt'",
        ),
    ],
    ids=[
        "another-group",
        "members-checkout-only",
        "members-draft-only",
        "holdout-inactive",
        "two-holdouts",
        "start-an-hour-off",
        "config-defaults-written",
        "a-method-set",
    ],
)
def test_10b_refuses_each_planted_state(stage, change, words):
    saved = experiment_answer(
        ExperimentDetailResponse,
        start_date="2026-10-30T09:00:00",
        mutual_exclusion_group_id=GROUP,
        sequential_testing_enabled=True,
    )
    ok, _numbers, why = after(stage, **change(saved))
    assert not ok and words in why, why


def design_after(**changes) -> Tuple[bool, Dict[str, Any], str]:
    from backend.tests.unit.showcase.test_showcase_design_end import DRAWN, saved

    end = board().end_state.design
    answer = dict(
        saved(end),
        sequential_testing_enabled=True,
        mutual_exclusion_group_id=GROUP,
        start_date="2026-10-30T09:00:00",
    )
    answer.update(changes)
    return gates.design_end(
        end,
        named=[answer],
        total=4,
        seed_experiments=3,
        owner_id=answer["owner_id"],
        readings=DRAWN,
        moved={
            "sequential_testing_enabled": True,
            "mutual_exclusion_group_id": GROUP,
            "start_date": START,
        },
    )


def test_10b_design_checks_pass_with_what_the_calls_moved():
    ok, _numbers, why = design_after()
    assert ok, why


@pytest.mark.parametrize(
    "changes, words",
    [
        ({"status": "active"}, "is 'active', not 'draft'"),
        ({"sequential_testing_enabled": False}, "has sequential_testing_enabled False"),
        ({"bayesian_enabled": False}, "bayesian_enabled is False"),
        ({"start_date": "2026-10-30T10:00:00"}, "has start_date '2026-10-30T10:00:00'"),
        ({"end_date": "2026-12-01T00:00:00"}, "has end_date"),
        ({"optimization_type": "thompson_sampling"}, "has optimization_type"),
    ],
)
def test_10b_design_checks_refuse_anything_else_moved(changes, words):
    ok, _numbers, why = design_after(**changes)
    assert not ok and words in why, why


def test_the_manifest_keeps_gate_10_and_the_render_refuses_one_without_it(tmp_path):
    from backend.tests.unit.showcase.capture_dirs import default_manifest, make_capture

    assert "10" in contract.REQUIRED_CAPTURE_GATES
    assert "api" not in contract.REQUIRED_CAPTURE_GATES
    manifest = default_manifest()
    del manifest["gates"]["10"]
    root = make_capture(tmp_path / "c", manifest=manifest)
    with pytest.raises(
        contract.CaptureRefused, match="capture gate 10 is not in the manifest"
    ):
        contract.validate_capture_dir(root)


def test_10_is_ok_only_when_10a_and_10b_are(stage):
    ok_b, made_b, why_b = after(stage, holdouts={"items": [{}, {}], "total": 2})
    ok_a, made_a, _ = before()
    assert ok_a and not ok_b
    why = [] if ok_b else [f"10b: {why_b}"]
    numbers = {"10a": run.gate(ok_a, **made_a), "10b": run.gate(ok_b, **made_b)}
    ten = run.gate(not why, **numbers)
    assert ten["ok"] is False
    assert ten["numbers"]["10a"]["ok"] is True and ten["numbers"]["10b"]["ok"] is False


def capture_source(name: str) -> str:
    source = Path(run.__file__).read_text(encoding="utf-8")
    return source.split(f"def {name}(", 1)[1].split("\ndef ", 1)[0]


def test_10a_runs_before_the_first_call_and_10b_after_the_scene():
    body = capture_source("record")
    loop = body.split("for scene in board.scenes:", 1)[1]
    assert loop.index("console_before_calls(") < loop.index("director.scene(shown)")
    assert "if scene.console is not None and before_calls is None:" in loop
    ten = body.split("# Gate 10: the end state, through the API.", 1)[1]
    assert ten.index('gates["api"] = gate(') < ten.index("console_after_calls(")
    assert 'numbers["10a"] = gate(before_calls[0], **before_calls[1])' in ten
    assert 'numbers["10b"] = gate(ok, **made)' in ten
    assert 'why.append(f"10b: {reason}")' in ten
    # A board with no console scene: gate 10 exactly as before.
    assert 'numbers["design"] = made' in ten


# ---------------------------------------------------------------------------
# The setup's exclusion group, after gate 14
# ---------------------------------------------------------------------------


def setup_sender(answers: Dict[str, Tuple[int, Any]], seen: List[Any]):
    def send(api_url, token, method, path, body, status):
        seen.append((method, path, body, status))
        got, answer = answers[path]
        if got != status:
            raise console.CallRefused(
                f"{method} {path} was answered {got}, not {status}"
            )
        return got, answer

    return send


SETUP_ANSWERS = {
    "/api/v1/mutual-exclusion-groups": (
        201,
        {"id": GROUP, "name": "Checkout tests", "traffic_allocation": 1.0},
    ),
    f"/api/v1/mutual-exclusion-groups/{GROUP}/experiments": (
        200,
        {"status": "ok", "experiment_id": "cbc", "group_id": GROUP},
    ),
}


def test_the_setup_makes_the_group_and_adds_the_seeded_experiment(monkeypatch):
    monkeypatch.setattr(run, "experiment_id", lambda api, token, name: "cbc")
    seen: List[Any] = []
    answers: List[Any] = []
    made = run.make_setup(
        board(),
        "http://127.0.0.1:28400",
        TOKEN,
        send=setup_sender(SETUP_ANSWERS, seen),
        answers=answers,
    )
    assert made == {
        "segments": [],
        "exclusion_groups": [
            {
                "group": "Checkout tests",
                "id": GROUP,
                "traffic_allocation": 1.0,
                "experiments": ["Checkout Button Color"],
            }
        ],
    }
    assert seen == [
        (
            "POST",
            "/api/v1/mutual-exclusion-groups",
            {"name": "Checkout tests", "traffic_allocation": 1.0},
            201,
        ),
        (
            "POST",
            f"/api/v1/mutual-exclusion-groups/{GROUP}/experiments",
            {"experiment_id": "cbc"},
            200,
        ),
    ]
    assert len(answers) == 2 and TOKEN not in json.dumps(made)


@pytest.mark.parametrize(
    "path, answer, words",
    [
        (
            "/api/v1/mutual-exclusion-groups",
            (200, SETUP_ANSWERS["/api/v1/mutual-exclusion-groups"][1]),
            "was answered 200, not 201",
        ),
        (
            "/api/v1/mutual-exclusion-groups",
            (201, {"id": GROUP, "name": "Checkout tests", "traffic_allocation": 0.5}),
            "was answered with 'Checkout tests'",
        ),
        (
            f"/api/v1/mutual-exclusion-groups/{GROUP}/experiments",
            (400, {"detail": "already in another group"}),
            "was answered 400, not 200",
        ),
    ],
)
def test_the_setup_refuses_any_other_answer(monkeypatch, path, answer, words):
    monkeypatch.setattr(run, "experiment_id", lambda api, token, name: "cbc")
    answers = dict(SETUP_ANSWERS, **{path: answer})
    with pytest.raises(run.StackError, match=re.escape(words)):
        run.make_setup(
            board(), "http://api", TOKEN, send=setup_sender(answers, []), answers=[]
        )


def test_the_setup_runs_after_gate_14s_count():
    body = capture_source("capture_video")
    assert body.index('gates["14"] = gate(counts == wanted') < body.index(
        "setup = make_setup("
    )
    assert "answers=setup_answers" in body and "setup_answers=setup_answers" in body
