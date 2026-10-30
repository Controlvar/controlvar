"""What video 9 makes off camera, through the API (#1442, D176): the call
helper (``offcamera.send``) and the storyboard's exclusion group (``setup``).

The call goes to the take's own stack only, GET, POST or PUT, with no redirect
followed, and answers exactly the status declared or is refused naming the one
it got. The setup makes "Checkout tests" after gate 14's count, adds the
seeded Checkout Button Color to it, and refuses any other answer; when no
sender is given it sends through ``offcamera.send``. Each property is shown
holding, then refused on one planted defect. No browser, Docker, network (but
one loopback server for the redirect test) or database.
"""

from __future__ import annotations

import copy
import http.server
import json
import re
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

import pytest
import yaml
from showcase.capture import offcamera, run, storyboard

pytestmark = [pytest.mark.unit]

SHOWCASE = Path(__file__).resolve().parents[4] / "tests" / "acceptance" / "showcase"
V9 = SHOWCASE / "storyboards" / "09-design-in-depth.yaml"
SOURCE = "09-design-in-depth.yaml"

EXPERIMENT = "0b6b4f1e-0000-4000-8000-0000000000e9"
GROUP = "8c2e1d4a-1111-4222-8333-0000000000a1"
#: The run's admin token, as the API makes one: a needle, never drawn.
TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI2YjFmMGMxZSIsImV4cCI6MTc5MzMxMDk1OX0"
    ".q8wJ3n2Vb7XkT0mPz5LrYc9AuEeHs4GdNiOf1KtWjQo"
)


def board() -> storyboard.Storyboard:
    return storyboard.load(V9)


def raw() -> Dict[str, Any]:
    return yaml.safe_load(V9.read_text(encoding="utf-8"))


def refused(change: Callable[[Dict[str, Any]], Any], reason: str) -> str:
    data = raw()
    change(data)
    with pytest.raises(storyboard.StoryboardError) as raised:
        storyboard.parse(copy.deepcopy(data), source=SOURCE)
    assert reason in str(raised.value), str(raised.value)
    return str(raised.value)


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
    got = offcamera.send(
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
    with pytest.raises(offcamera.CallRefused, match=re.escape(words)):
        offcamera.send(
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


#: Why an origin is refused before any request.
NOT_THE_STACK = (
    "is not plain http on 127.0.0.1, with no user, at a port that is not reserved"
)


@pytest.mark.parametrize(
    "api_url, method, path, words",
    [
        ("http://127.0.0.1:8000", "GET", "/api/v1/holdout", NOT_THE_STACK),
        ("https://127.0.0.1:28400", "GET", "/api/v1/holdout", NOT_THE_STACK),
        ("http://user@127.0.0.1:28400", "GET", "/api/v1/holdout", NOT_THE_STACK),
        ("http://user:pw@127.0.0.1:28400", "GET", "/api/v1/holdout", NOT_THE_STACK),
        ("http://localhost:28400", "GET", "/api/v1/holdout", NOT_THE_STACK),
        ("http://[::1]:28400", "GET", "/api/v1/holdout", NOT_THE_STACK),
        ("http://127.0.0.2:28400", "GET", "/api/v1/holdout", NOT_THE_STACK),
        ("http://127.0.0.1", "GET", "/api/v1/holdout", NOT_THE_STACK),
        ("http://127.0.0.1:5432", "GET", "/api/v1/holdout", NOT_THE_STACK),
        ("http://127.0.0.1:08000", "GET", "/api/v1/holdout", NOT_THE_STACK),
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
    with pytest.raises(offcamera.CallRefused, match=re.escape(words)):
        offcamera.send(
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
        with pytest.raises(offcamera.CallRefused, match="was answered 307, not 200"):
            offcamera.send(url, TOKEN, "GET", "/api/v1/holdout", None, 200)
    finally:
        server.shutdown()
        server.server_close()
    assert Redirecting.hits == ["/api/v1/holdout"]


# ---------------------------------------------------------------------------
# The setup's exclusion group, as the storyboard declares it
# ---------------------------------------------------------------------------


def test_the_setup_makes_one_group_with_the_seeded_experiment_at_full_traffic():
    setup = storyboard.load(V9).setup
    assert setup.segments == []
    assert [g.model_dump() for g in setup.exclusion_groups] == [
        {
            "name": "Checkout tests",
            "traffic_allocation": 1.0,
            "experiments": ["Checkout Button Color"],
        }
    ]


def test_a_setup_that_makes_nothing_is_refused():
    refused(
        lambda d: d["setup"].update(exclusion_groups=[]),
        "a setup makes at least one segment or exclusion group",
    )


@pytest.mark.parametrize("allocation", [0, 1.5])
def test_a_group_allocation_outside_0_to_1_is_refused(allocation):
    refused(
        lambda d: d["setup"]["exclusion_groups"][0].update(
            traffic_allocation=allocation
        ),
        "traffic_allocation",
    )


# ---------------------------------------------------------------------------
# The setup's exclusion group, made after gate 14
# ---------------------------------------------------------------------------


def setup_sender(answers: Dict[str, Tuple[int, Any]], seen: List[Any]):
    def send(api_url, token, method, path, body, status):
        seen.append((method, path, body, status))
        got, answer = answers[path]
        if got != status:
            raise offcamera.CallRefused(
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
#: What the setup sends for the real board, in order: the group, then its
#: seeded experiment (``run.experiment_id`` stood in by "cbc"). The test of the
#: default sender compares with it.
SETUP_SENT = [
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


def test_the_setup_sends_through_offcamera_send_when_no_sender_is_given(
    monkeypatch,
):
    """PE 3 of the video 9 re-plan: the capture calls ``make_setup`` with no
    ``send``, so the default sender is what makes the group on every take.
    Every other setup test hands its own sender in, and would pass whatever
    the default were."""
    monkeypatch.setattr(run, "experiment_id", lambda api, token, name: "cbc")
    seen: List[Any] = []
    monkeypatch.setattr(
        offcamera, "send", setup_sender(SETUP_ANSWERS, seen), raising=True
    )
    answers: List[Any] = []
    made = run.make_setup(board(), "http://127.0.0.1:28400", TOKEN, answers=answers)
    assert seen == SETUP_SENT
    assert [g["id"] for g in made["exclusion_groups"]] == [GROUP]
    assert len(answers) == 2


def capture_source(name: str) -> str:
    source = Path(run.__file__).read_text(encoding="utf-8")
    return source.split(f"def {name}(", 1)[1].split("\ndef ", 1)[0]


def test_the_setup_runs_after_gate_14s_count():
    body = capture_source("capture_video")
    assert body.index('gates["14"] = gate(counts == wanted') < body.index(
        "setup = make_setup("
    )
    assert "answers=setup_answers" in body and "setup_answers=setup_answers" in body
