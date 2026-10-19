"""Video 5, "Plan a test, bring your team" (plan v2.2, T178): its storyboard, its
numbers and the tool changes it brings.

The numbers the video states are pinned to the code that makes them: the power
calculator's sample sizes to the server's own service (which the dashboard's
calculator is a transcription of, pinned there by power.parity.test.ts) and to
the textbook formula gate 10 uses, and the runtime to the page's own
formatting. Each contract the video adds is shown refusing its planted defect:
the invite capture and its positive count and the mask (C4 of T149, S6 of plan
v2.2, plan v2's condition 11), the "masked value" departure (S7), gate 10/5
(S8c: the invitees and the calculator), and a slider moved on its own steps.
No browser, Docker, network or database: stand-in pages, contexts and APIs.
"""

from __future__ import annotations

import json
import re
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import get_args

import pytest
import yaml
from showcase import contract
from showcase.capture import config, director, gates, guard, narration, run, storyboard

from backend.app.schemas.auth import RoleName
from backend.app.services.power_calculator_service import PowerCalculatorService

pytestmark = [pytest.mark.unit]

REPO = Path(__file__).resolve().parents[4]
ACCEPTANCE = REPO / "tests" / "acceptance"
V5 = ACCEPTANCE / "showcase" / "storyboards" / "05-plan-and-team.yaml"
V1 = ACCEPTANCE / "showcase" / "storyboards" / "01-getting-started.yaml"
FRONTEND = REPO / "frontend" / "src"
CALCULATOR = FRONTEND / "pages" / "power-calculator.tsx"
INVITE_MODAL = FRONTEND / "components" / "admin" / "users" / "InviteUserModal.tsx"
ADMIN_SERVICE = FRONTEND / "services" / "admin.ts"
ADMIN_TYPES = FRONTEND / "types" / "admin.ts"
SCENE = "plan-and-team"
DASH = "http://127.0.0.1:28401"
API = "http://127.0.0.1:28400"
#: A made-up one-time password, in the dialog's alphabet (never a real one).
PASSWORD = "Qm4tRw7xKp2vHs9e"


def board() -> storyboard.Storyboard:
    return storyboard.load(V5)


def v5_data() -> dict:
    return yaml.safe_load(V5.read_text(encoding="utf-8"))


def beats():
    (scene,) = board().scenes
    return scene.beats


def load_refusal(raw: dict) -> str:
    with pytest.raises(storyboard.StoryboardError) as caught:
        storyboard.parse(raw)
    return str(caught.value)


# --- The storyboard -----------------------------------------------------------------


def test_the_storyboard_loads_and_its_captions_are_fixed_and_fit_the_video():
    made = board()
    for cue in storyboard.compile_cues(made):
        assert contract.caption_problems(cue["text"]) == []
    assert storyboard.cue_list_bytes(made) == storyboard.cue_list_bytes(
        storyboard.parse(v5_data())
    )
    length = (
        storyboard.estimate_ms(made) + contract.TITLE_CARD_MS + contract.END_CARD_MS
    )
    assert contract.MIN_VIDEO_MS + 5_000 <= length <= contract.MAX_VIDEO_MS - 5_000


def test_video_5_registers_in_the_tool_on_the_demo_seed_with_no_app_or_override():
    assert config.VIDEOS["05-plan-and-team"] == "storyboards/05-plan-and-team.yaml"
    assert list(config.VIDEOS).index("05-plan-and-team") == 3
    made = board()
    assert made.seed == "demo" and made.apps == []
    from showcase.capture import overrides as override_lists

    listed = override_lists.load()
    assert listed["05-plan-and-team"] == {}
    assert (
        override_lists.override_list(made, run.Ports(28400, 28401, 28402), listed) == []
    )


def test_the_sign_in_is_filmed_and_nothing_else_is_done_off_camera():
    """As video 1: the demo admin signs in on camera, so no scene is signed in
    off camera, warmed, seeded or set up, and the only departure is the mask."""
    made = board()
    assert made.setup is None and made.traffic is None
    for scene in made.scenes:
        assert not scene.signed_in and not scene.warm_results and not scene.off_camera
    typed = [
        step
        for beat in beats()
        for step in beat.steps
        if isinstance(step, storyboard.Type) and step.secret
    ]
    assert [step.text for step in typed] == [config.DEMO_ADMIN[1]]


def test_no_choose_or_slide_is_filmed_in_a_cut_or_an_exit():
    """The loader refuses one in a cut; one in an exit is held for 0 ms by the
    narrated hold and counted by the projection (e0 follow-ups, UX item 4)."""
    for beat in beats():
        for step in [*beat.before, *beat.exit]:
            assert not isinstance(step, (storyboard.Choose, storyboard.Slide))


# --- D84 and S7: the masked value -----------------------------------------------------


def test_video_5_requires_and_states_exactly_the_masked_value():
    made = board()
    assert storyboard.required_departures(made) == [(SCENE, "masked value")]
    assert storyboard.departure_problems(made) == []
    (scene,) = made.scenes
    assert scene.masks == [config.INVITE_PASSWORD_TESTID]
    # Said where the mask is on screen: in the caption of the beat that shows it.
    (stated,) = scene.departures
    shows = next(b for b in scene.beats if b.focus.testid == "invite-created")
    assert stated.says in shows.cue and stated.says == config.MASK_TEXT


@pytest.mark.regression
def test_a_scene_that_masks_must_state_the_masked_value():
    """S7: a mask is a departure the scene states on screen (D84)."""
    raw = v5_data()
    raw["scenes"][0]["departures"] = []
    assert storyboard.departure_problems(storyboard.parse(raw)) == [
        f"scene {SCENE}: masked value is not stated on screen (departures)"
    ]


def test_the_masked_value_statement_taken_off_the_screen_is_refused():
    raw = v5_data()
    for beat in raw["scenes"][0]["beats"]:
        beat["cue"] = beat["cue"].replace("; hidden for this video", ".")
    assert storyboard.departure_problems(storyboard.parse(raw)) == [
        f"scene {SCENE}: the masked value statement 'hidden for this video' is not"
        " in a caption or the card"
    ]


@pytest.mark.regression
def test_a_mask_is_one_of_a_closed_list():
    """The tool hides credentials only, never other product text (D84 rule 3)."""
    raw = v5_data()
    raw["scenes"][0]["masks"] = ["invite-copy-password-button"]
    assert "masks" in load_refusal(raw)


@pytest.mark.regression
def test_a_storyboard_that_invites_and_masks_nothing_is_refused():
    """The dialog always shows the password: a team with no mask would film it."""
    raw = v5_data()
    raw["scenes"][0]["masks"] = []
    raw["scenes"][0]["departures"] = []
    assert "no scene masks invite-temp-password" in load_refusal(raw)


@pytest.mark.parametrize(
    "change, words",
    [
        (lambda t: t["invitees"].append(dict(t["invitees"][0])), "listed twice"),
        (lambda t: t.update(shown="nowhere"), "'nowhere', which no beat records"),
        (lambda t: t["invitees"][0].update(email="priya@gmail.com"), "email"),
        (lambda t: t["invitees"][0].update(role="OWNER"), "role"),
        (lambda t: t.update(invitees=[]), "invitees"),
    ],
)
def test_a_team_end_state_the_video_cannot_check_is_refused(change, words):
    raw = v5_data()
    change(raw["end_state"]["team"])
    assert words in load_refusal(raw)


def test_a_calculation_no_beat_records_is_refused():
    raw = v5_data()
    raw["end_state"]["calculations"][1]["shown"] = "nowhere"
    assert "'nowhere', which no beat records" in load_refusal(raw)


def test_the_masked_testid_is_the_dialogs():
    """One name in three places: the storyboard's closed list, the tool's
    constant, and the element InviteUserModal.tsx draws the password in."""
    assert get_args(storyboard.MaskedValue) == (config.INVITE_PASSWORD_TESTID,)
    source = INVITE_MODAL.read_text(encoding="utf-8")
    assert re.search(
        r'<code\s+data-testid="invite-temp-password"[^>]*>\s*\{created\.password\}',
        source,
    )


def test_the_invite_is_the_dashboards_post_with_the_password_in_its_body():
    """The capture keys on these: ``AdminService.createUser`` posts to
    /api/v1/users/, and the dialog puts the password it made up in the body."""
    service = ADMIN_SERVICE.read_text(encoding="utf-8")
    assert re.search(
        r"createUser\(data: CreateUserRequest\)[^{]*\{\s*return apiFetch<CreatedUser>"
        r"\('/api/v1/users/', \{ method: 'POST', json: data \}\)",
        service,
    )
    assert config.INVITE_PATH == "/api/v1/users"
    modal = INVITE_MODAL.read_text(encoding="utf-8")
    assert "const password = generateTemporaryPassword();" in modal
    assert re.search(r"AdminService\.createUser\(\{[^}]*\n\s*password,\n", modal)


# --- The calculator's numbers, pinned ------------------------------------------------


def calculator_source() -> str:
    return CALCULATOR.read_text(encoding="utf-8")


def test_the_calculator_opens_on_the_guides_example():
    """A 5% baseline, a 10% lift, 95% and 80%, two variants and all traffic:
    the beats point at the first two and set only the traffic."""
    source = calculator_source()
    for line in (
        "useState<number>(0.05);",
        "useState<number>(0.10);",
        "const [alpha, setAlpha] = useState<number>(0.05);",
        "const [power, setPower] = useState<number>(0.80);",
        "const [nVariants, setNVariants] = useState<number>(2);",
        "const [dailyTraffic, setDailyTraffic] = useState<string>('');",
        "const [trafficAllocation, setTrafficAllocation] = useState<number>(1.0);",
    ):
        assert line in source


def page_runtime(days: float) -> str:
    """The calculator's ``fmtDays`` (power-calculator.tsx; pinned below)."""
    if days < 1:
        return "< 1 day"
    if days < 7:
        return f"{days:.1f} days"
    return f"{days / 7:.1f} weeks ({round(days)} days)"


def test_the_runtime_formatting_is_the_pages():
    source = calculator_source()
    assert "if (d < 7) return `${d.toFixed(1)} days`;" in source
    assert "const weeks = d / 7;" in source
    assert "return `${weeks.toFixed(1)} weeks (${Math.round(d)} days)`;" in source


@pytest.mark.parametrize(
    "variants, per_variant, total, runtime",
    [
        (2, 31_234, 62_468, "1.8 weeks (12 days)"),
        (3, 37_824, 113_472, "3.2 weeks (23 days)"),
    ],
)
def test_the_sample_sizes_are_the_servers_and_the_formulas(
    variants, per_variant, total, runtime
):
    """What the page draws for 10,000 users a day, 50% in the test: the
    server's service (the calculator transcribes it) and gate 10's formula."""
    made = PowerCalculatorService().compute_sample_size(
        0.05, 0.10, n_variants=variants, daily_traffic=10_000, traffic_allocation=0.5
    )
    assert (made.per_variant, made.total) == (per_variant, total)
    assert page_runtime(made.runtime_days) == runtime
    assert f"{made.per_variant:,}" == f"{per_variant:,}"
    alpha = gates.PLAN_ALPHA / (variants - 1)
    assert gates.planned_sample_oracle(0.05, 0.10, alpha=alpha) == per_variant


def test_the_storyboard_states_the_numbers_the_calculator_draws():
    made = board()
    by_shown = {c.shown: c for c in made.end_state.calculations}
    assert {name: c.per_variant for name, c in by_shown.items()} == {
        "two-variants": 31_234,
        "three-variants": 37_824,
    }
    for c in by_shown.values():
        assert (c.baseline, c.mde) == (0.05, 0.10)
    assert by_shown["two-variants"].variants == 2
    assert by_shown["three-variants"].variants == 3


def test_the_traffic_the_beats_set_is_the_guides_and_on_the_sliders_steps():
    source = calculator_source()
    assert re.search(
        r'id="power-allocation"[^/]*?min="0\.05"\s+max="1\.00"\s+step="0\.05"', source
    )
    steps = [s for b in beats() for s in b.steps]
    slides = [s for s in steps if isinstance(s, storyboard.Slide)]
    assert [(s.slide.css, s.value) for s in slides] == [("#power-allocation", 0.5)]
    assert director.on_step(0.5, 0.05, director.range_step("0.05")[0])
    typed = [s.text for s in steps if isinstance(s, storyboard.Type)]
    assert "10000" in typed
    chosen = [s.option for s in steps if isinstance(s, storyboard.Choose)]
    assert chosen == ["3 (A/B/C test)", "Developer", "Analyst"]
    assert "<option value={3}>3 (A/B/C test)</option>" in source


#: Every number the narration says, the beat that says it, and the page text
#: that draws it (G2 checks each on the page at the beat's start or end).
SPOKEN = {
    "5%": ("baseline", "{(baselineRate * 100).toFixed(1)}%"),
    "10%": ("lift", "{(mde * 100).toFixed(0)}%"),
    "10,000": ("traffic", 'placeholder="e.g. 10000"'),
    "50%": ("traffic", "{(trafficAllocation * 100).toFixed(0)}%"),
    "31,234": ("result", "fmtNumber(result.per_variant)"),
    "12": ("result", "${Math.round(d)} days"),
    "37,824": ("variants", "fmtNumber(result.per_variant)"),
}


def test_the_spoken_numbers_are_the_ones_listed_and_drawn_by_the_page():
    said = [
        number
        for beat in beats()
        for sentence in beat.say
        for number in storyboard.numbers_in(sentence)
    ]
    assert said == ["5%", "10%", "10,000", "50%", "31,234", "12", "37,824"]
    source = calculator_source()
    for number, (_where, drawn) in SPOKEN.items():
        assert drawn in source, number
    # 5% is drawn as "5.0%", which G2 reads as the same number.
    assert narration.number_key("5.0%") == narration.number_key("5%")
    assert narration.number_key("10000") == narration.number_key("10,000")


def test_every_beat_is_narrated_and_the_lints_report_nothing():
    made = board()
    assert narration.board_problems(made) == []
    assert all(beat.say for beat in beats())
    assert len(storyboard.narration_lines(made)) == 14


def test_two_sentences_are_video_1s_word_for_word():
    """Their clips are the ones video 1's narration already made."""
    v1 = {
        sentence
        for scene in storyboard.load(V1).scenes
        for beat in scene.beats
        for sentence in beat.say
    }
    mine = [sentence for beat in beats() for sentence in beat.say]
    shared = [sentence for sentence in mine if sentence in v1]
    assert shared == [
        "Sign in as the demo admin.",
        "The email and password are in the Quick Start.",
    ]


def test_the_roles_the_video_names_are_the_dialogs_and_the_apis():
    modal = INVITE_MODAL.read_text(encoding="utf-8")
    assert (
        "const ROLES: UserRole[] = ['ADMIN', 'DEVELOPER', 'ANALYST', 'VIEWER'];"
        in modal
    )
    assert get_args(storyboard.Role) == get_args(RoleName)
    labels = re.search(
        r"USER_ROLE_LABELS: Record<UserRole, string> = \{([^}]*)\}",
        ADMIN_TYPES.read_text(encoding="utf-8"),
    ).group(1)
    assert dict(re.findall(r"(\w+): '([^']*)'", labels)) == gates.ROLE_LABELS
    # The options the beats choose are the dialog's labels: only Admin is
    # drawn differently, "Admin (superuser)".
    assert "${USER_ROLE_LABELS[r]} (superuser)` : USER_ROLE_LABELS[r]" in modal


# --- Gate 10/5 (S8c): the calculator ----------------------------------------------------


def calc(**changes):
    made = storyboard.Calculation(
        shown="two-variants", baseline=0.05, mde=0.10, variants=2, per_variant=31_234
    )
    return made.model_copy(update=changes)


def test_gate_10_holds_each_calculation_to_the_formula_and_the_page():
    ok, numbers, why = gates.calculation_end(calc(), shown="31,234")
    assert ok and why == "" and numbers["oracle"] == 31_234
    three = calc(shown="three-variants", variants=3, per_variant=37_824)
    assert gates.calculation_end(three, shown=" 37,824 ")[0]


@pytest.mark.regression
@pytest.mark.parametrize(
    "made, shown, words",
    [
        (calc(per_variant=31_235), "31,235", "the formula gives 31,234"),
        (calc(variants=3), "31,234", "the formula gives 37,824"),
        (calc(), "31,000", "the calculator drew '31,000'"),
        (calc(), "...", "the calculator drew '...'"),
        (calc(), None, "the calculator drew ''"),
    ],
)
def test_a_calculation_the_page_or_the_formula_disagrees_with_is_refused(
    made, shown, words
):
    ok, _numbers, why = gates.calculation_end(made, shown=shown)
    assert not ok and words in why


# --- Gate 10/5 (S8c): the team ----------------------------------------------------------

SEED_USERS = [
    {
        "email": "admin@demo.com",
        "role": "ADMIN",
        "is_superuser": True,
        "is_active": True,
    },
    {
        "email": "dev@demo.com",
        "role": "DEVELOPER",
        "is_superuser": False,
        "is_active": True,
    },
    {
        "email": "analyst@demo.com",
        "role": "ANALYST",
        "is_superuser": False,
        "is_active": True,
    },
    {
        "email": "viewer@demo.com",
        "role": "VIEWER",
        "is_superuser": False,
        "is_active": True,
    },
]
PRIYA = {
    "email": "priya@example.com",
    "role": "DEVELOPER",
    "is_superuser": False,
    "is_active": True,
}
MARCUS = {
    "email": "marcus@example.com",
    "role": "ANALYST",
    "is_superuser": False,
    "is_active": True,
}
#: The users table's body as Chromium's innerText gives it: newest first.
TABLE = (
    "marcus\tmarcus@example.com\tAnalyst\tNo\tActive\tEditDelete\n"
    "priya\tpriya@example.com\tDeveloper\tNo\tActive\tEditDelete\n"
    "admin\tadmin@demo.com\tAdmin\tSuperuser\tActive\tEditDelete\n"
)


def team() -> storyboard.Team:
    made = board().end_state.team
    assert made is not None
    return made


def team_check(after=None, shown=TABLE, before=None):
    return gates.team_end(
        team(),
        before=SEED_USERS if before is None else before,
        after=[MARCUS, PRIYA, *SEED_USERS] if after is None else after,
        shown=shown,
    )


def test_gate_10_holds_the_team_to_exactly_these_two_invitees():
    ok, numbers, why = team_check()
    assert ok and why == ""
    assert numbers["added"] == ["marcus@example.com", "priya@example.com"]
    assert numbers["shown_rows"] == [
        ["marcus@example.com", "Analyst"],
        ["priya@example.com", "Developer"],
    ]


@pytest.mark.regression
@pytest.mark.parametrize(
    "after, shown, words",
    [
        # One invite instead of two (plan v1's tamper).
        ([PRIYA, *SEED_USERS], TABLE, "the video added ['priya@example.com']"),
        # An account the storyboard does not invite.
        (
            [{**MARCUS, "email": "eve@example.com"}, MARCUS, PRIYA, *SEED_USERS],
            TABLE,
            "the storyboard invites",
        ),
        # The wrong role, or an Admin's superuser flag on a Developer.
        (
            [{**MARCUS, "role": "VIEWER"}, PRIYA, *SEED_USERS],
            TABLE,
            "marcus@example.com is 'VIEWER'",
        ),
        (
            [MARCUS, {**PRIYA, "is_superuser": True}, *SEED_USERS],
            TABLE,
            "superuser True",
        ),
        ([MARCUS, {**PRIYA, "is_active": False}, *SEED_USERS], TABLE, "active False"),
        # A seed account changed or gone.
        ([MARCUS, PRIYA, *SEED_USERS[:3]], TABLE, "changed: ['viewer@demo.com']"),
        (
            [
                MARCUS,
                PRIYA,
                {**SEED_USERS[1], "role": "ADMIN"},
                SEED_USERS[0],
                *SEED_USERS[2:],
            ],
            TABLE,
            "changed: ['dev@demo.com']",
        ),
        # The page did not show them as the API lists them.
        (
            None,
            TABLE.replace("\tAnalyst\t", "\tViewer\t", 1),
            "the users table's top rows",
        ),
        (None, "\n".join(TABLE.splitlines()[1:]), "the users table's top rows"),
        (None, None, "the users table's top rows"),
    ],
)
def test_a_team_the_api_or_the_page_disagrees_with_is_refused(after, shown, words):
    ok, _numbers, why = team_check(after=after, shown=shown)
    assert not ok and words in why


# --- C4 (S6): the capture, the positive count and the mask ------------------------------


@pytest.fixture
def page_text_script(monkeypatch):
    """docs_runner.execute (which holds gate 6's page-text script) imports
    Playwright; stand in for it, so these run in the CI unit job too."""
    stand_in = types.ModuleType("docs_runner.execute")
    stand_in.DRAWN_JS = "read the page's text and fields"
    monkeypatch.setitem(sys.modules, "docs_runner.execute", stand_in)
    return stand_in.DRAWN_JS


class Context:
    def __init__(self):
        self.scripts = []
        self.handlers = {}

    def route(self, pattern, handler):
        pass

    def route_web_socket(self, pattern, handler):
        pass

    def add_init_script(self, script):
        self.scripts.append(script)

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)


def masked_testids(scripts, origin):
    """What the browser would mask on *origin*: the testids each installed
    ``director.MASK_INIT`` script hides there, read from the script's own text."""
    found = set()
    for script in scripts:
        where = re.search(r"location\.origin !== (\"[^\"]*\")\) return;", script)
        css = re.search(r"const css = (\".*?\");\n", script)
        if not (where and css and json.loads(where.group(1)) == origin):
            continue
        for testid in re.findall(
            r'\[data-testid="([^"]+)"\]\{visibility:hidden', json.loads(css.group(1))
        ):
            found.add(testid)
    return found


class Page:
    """The users page with the invite dialog open, as gate 6 reads it: the
    password in the page's text unless a mask for its element is installed."""

    def __init__(self, context, drawn_js):
        self.context = context
        self.drawn_js = drawn_js
        self.url = DASH + "/admin/users"
        self.handlers = {}
        self.created = None

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def fire(self, event, value):
        for handler in self.handlers.get(event, []):
            handler(value)

    def evaluate(self, script, arg=None):
        assert script == self.drawn_js, str(script)[:40]
        text = "User Management\nInvite User\nUser created\n"
        if self.created is not None:
            text += (
                "priya@example.com can sign in as priya with this temporary password.\n"
            )
            if config.INVITE_PASSWORD_TESTID not in masked_testids(
                self.context.scripts, DASH
            ):
                text += self.created + "\n"
            text += "Copy\nDone\n"
        return [text, []]


class Request:
    def __init__(self, url, body, method="POST"):
        self.url = url
        self.method = method
        self.headers = {"content-type": "application/json"}
        self.post_data_json = body


class Response:
    def __init__(self, request, status=201, body=None):
        self.url = request.url
        self.request = request
        self.status = status
        self.headers = {"content-type": "application/json"}
        self._body = body if body is not None else {"email": "priya@example.com"}

    def json(self):
        return self._body


def a_director(
    tmp_path, page_text_script, *, invites=1, masks=(config.INVITE_PASSWORD_TESTID,)
):
    context = Context()
    page = Page(context, page_text_script)
    made = director.Director(
        context=context,
        page=page,
        frames=tmp_path / "work" / "v5" / "capture" / "frames",
        dashboard=DASH,
        api_port=28400,
        dashboard_port=28401,
        redactor=director.redaction.Redactor(),
        forbidden=(),
        work=guard.WorkRoot(tmp_path / "work"),
        invites=invites,
        masks=masks,
    )
    made.install()
    return made, page, context


def invite_body(password=PASSWORD):
    body = {
        "email": "priya@example.com",
        "username": "priya",
        "full_name": "Priya Shah",
        "role": "DEVELOPER",
        "is_active": True,
        "is_superuser": False,
    }
    if password is not None:
        body["password"] = password
    return body


def invite(page, password=PASSWORD, url=DASH + "/api/v1/users/", answered=True):
    """The dialog's Create User: the request, then (if *answered*) its answer."""
    request = Request(url, invite_body(password))
    page.fire("request", request)
    if answered:
        page.fire("response", Response(request))
    return request


def user_created(made, page, password=PASSWORD):
    """The dialog draws the password; the director is at the beat that shows it."""
    page.created = password
    made.where = f"scene {SCENE} beat 10"


def test_an_invites_password_is_a_needle_before_its_answer_is_read(
    tmp_path, page_text_script
):
    made, page, _context = a_director(tmp_path, page_text_script)
    made.where = f"scene {SCENE} beat 9 exit"
    request = Request(DASH + "/api/v1/users/", invite_body())
    page.fire("request", request)
    # Captured as the request is seen: before any answer.
    assert PASSWORD in made.redactor.values
    assert made.tally.invites == [
        {"where": f"scene {SCENE} beat 9 exit", "captured": True}
    ]
    page.fire("response", Response(request))
    made.check_events()
    assert made.check_invites() == {
        "invites": 1,
        "requests": 1,
        "passwords_captured": 1,
        "masks": [config.INVITE_PASSWORD_TESTID],
    }


def test_with_the_mask_and_the_capture_the_user_created_view_passes(
    tmp_path, page_text_script
):
    made, page, _context = a_director(tmp_path, page_text_script)
    invite(page)
    user_created(made, page)
    made.check_needles()
    assert made.check_invites()["passwords_captured"] == 1


@pytest.mark.regression
def test_with_the_mask_removed_and_the_capture_kept_gate_6_refuses_at_user_created(
    tmp_path, page_text_script
):
    """Plan v2's condition 11, the first case."""
    made, page, _context = a_director(tmp_path, page_text_script, masks=())
    invite(page)
    user_created(made, page)
    with pytest.raises(
        director.CaptureFailed,
        match=f"gate 6: scene {SCENE} beat 10: a kept value is drawn in the page's"
        " text; recording dropped",
    ):
        made.check_needles()


@pytest.mark.regression
def test_with_the_mask_removed_and_the_capture_dropped_gate_6_passes_and_the_count_refuses(
    tmp_path, page_text_script
):
    """Plan v2's condition 11, the second case, and S6's silent mode: gate 6
    cannot know a password it was never given, so it passes one drawn in
    clear; the positive count is what refuses."""
    made, page, _context = a_director(tmp_path, page_text_script, masks=())
    made.capture_request = lambda body: None  # the request capture, dropped
    invite(page)
    user_created(made, page)
    made.check_needles()  # gate 6 alone passes
    assert made.tally.needle_checks == 1
    with pytest.raises(
        director.CaptureFailed,
        match=r"C4: 0 of 1 invite password\(s\) were captured as needles before"
        " their answers: a password drawn on screen would pass gate 6; recording"
        " dropped",
    ):
        made.check_invites()


@pytest.mark.regression
def test_an_invite_request_without_a_password_field_is_refused(
    tmp_path, page_text_script
):
    made, page, _context = a_director(tmp_path, page_text_script)
    made.where = f"scene {SCENE} beat 9 exit"
    invite(page, password=None)
    assert made.tally.invites == []
    with pytest.raises(
        director.CaptureFailed,
        match=f"C4: an invite request \\(scene {SCENE} beat 9 exit\\) has no password"
        " field",
    ):
        made.check_events()


@pytest.mark.regression
def test_an_invite_answered_before_its_request_was_seen_is_refused(
    tmp_path, page_text_script
):
    """S6: the password is registered from the request before its answer is
    read; an answer to an invite no request listener saw means it never was."""
    made, page, _context = a_director(tmp_path, page_text_script)
    unseen = Request(DASH + "/api/v1/users/", invite_body())
    page.fire("response", Response(unseen))
    with pytest.raises(
        director.CaptureFailed,
        match="C4: an invite was answered .* before its request was seen",
    ):
        made.check_events()


@pytest.mark.regression
def test_fewer_invites_than_the_storyboard_makes_are_refused(
    tmp_path, page_text_script
):
    made, page, _context = a_director(tmp_path, page_text_script, invites=2)
    invite(page)
    with pytest.raises(
        director.CaptureFailed,
        match=r"C4: the dashboard sent 1 invite\(s\); the storyboard invites 2",
    ):
        made.check_invites()


def test_more_invites_than_the_storyboard_makes_are_refused(tmp_path, page_text_script):
    made, page, _context = a_director(tmp_path, page_text_script, invites=1)
    invite(page)
    invite(page, password="Zx8cVb3nMq6wEr2t")
    with pytest.raises(director.CaptureFailed, match="sent 2 invite"):
        made.check_invites()


def test_a_video_that_invites_no_one_refuses_an_invite(tmp_path, page_text_script):
    """Videos 1-3 and 6 make none: one would be an undeclared, unmasked password."""
    made, page, _context = a_director(tmp_path, page_text_script, invites=0, masks=())
    assert made.check_invites()["requests"] == 0
    invite(page)
    with pytest.raises(director.CaptureFailed, match="the storyboard invites 0"):
        made.check_invites()


@pytest.mark.parametrize(
    "url, method, invite_",
    [
        (DASH + "/api/v1/users/", "POST", True),
        (API + "/api/v1/users", "POST", True),
        (DASH + "/api/v1/users/", "GET", False),
        (DASH + "/api/v1/users/me", "POST", False),
        (DASH + "/api/v1/auth/login", "POST", False),
        ("http://127.0.0.1:28999/api/v1/users/", "POST", False),
    ],
)
def test_only_a_post_to_the_stacks_users_route_is_an_invite(
    tmp_path, page_text_script, url, method, invite_
):
    made, _page, _context = a_director(tmp_path, page_text_script)
    assert made.is_invite(Request(url, {}, method=method)) is invite_


def test_the_mask_is_installed_before_the_first_navigation_on_the_dashboard_only(
    tmp_path, page_text_script
):
    made, _page, context = a_director(tmp_path, page_text_script)
    script = context.scripts[-1]
    assert script == director.mask_init(DASH, (config.INVITE_PASSWORD_TESTID,))
    assert masked_testids(context.scripts, DASH) == {config.INVITE_PASSWORD_TESTID}
    assert masked_testids(context.scripts, "http://127.0.0.1:28403") == set()
    # Into <head> once it exists, as the zoom is: never appended to <html>.
    assert "document.head.appendChild(style)" in script
    assert "documentElement.appendChild" not in script
    # A video with no mask installs none.
    _made, _page, bare = a_director(tmp_path / "bare", page_text_script, masks=())
    assert not any("showcase-mask" in s for s in bare.scripts)


def test_the_mask_hides_the_text_and_draws_a_solid_block_with_its_words():
    css = director.mask_css((config.INVITE_PASSWORD_TESTID,))
    selector = '[data-testid="invite-temp-password"]'
    assert f"{selector}{{visibility:hidden !important;" in css
    after = css.split(f"{selector}::after{{", 1)[1]
    assert f'content:"{config.MASK_TEXT}"' in after
    assert "visibility:visible" in after and "inset:0" in after
    assert f"background:{config.MASK_FILL}" in after
    assert "blur" not in css and "#FF00FF" not in css.upper()
    assert config.MASK_FILL == "#E2E8F0"


def test_the_capture_masks_and_counts_what_the_storyboard_declares():
    made = board()
    assert run.masked(made) == (config.INVITE_PASSWORD_TESTID,)
    assert run.masked(storyboard.load(V1)) == ()
    assert len(made.end_state.team.invitees) == 2


def test_record_hands_the_director_the_invites_and_masks_and_counts_before_gate_10():
    """The wiring in ``run.record``: the Director gets the storyboard's invites
    and masks, and the positive count runs after the scenes, before gate 10."""
    source = (ACCEPTANCE / "showcase" / "capture" / "run.py").read_text(
        encoding="utf-8"
    )
    body = source.split("def record(", 1)[1].split("\ndef ", 1)[0]
    assert "masks=masked(board)," in body
    assert re.search(r"invites=len\(board\.end_state\.team\.invitees\)", body)
    count = body.index("invites = director.check_invites()")
    assert body.index("director.scene(") < count < body.index('gates["10"] = gate(')
    assert '"C4": (True, invites, "")' in body
    # Gate 10/5: the users read before the first scene, and both judgements made.
    assert body.index("team_before = (") < body.index("for scene in board.scenes:")
    tenth = body[
        body.index("# Gate 10: the end state") : body.index('gates["10"] = gate(')
    ]
    assert "judge.calculation_end(" in tenth and "judge.team_end(" in tenth
    assert "before=team_before," in tenth


@pytest.mark.regression
def test_a_c4_refusal_drops_the_recording(tmp_path):
    """A C4 refusal means a password may be in a frame, as gate 6's does."""
    work = guard.WorkRoot(tmp_path / "work")
    frames = tmp_path / "work" / "v5" / "capture" / "frames"
    frames.mkdir(parents=True)
    (frames / "raw-000001.jpg").write_bytes(b"\xff\xd8")
    run.drop_recording(director.CaptureFailed("C4", "0 of 2 captured"), frames, work)
    assert not frames.exists()
    frames.mkdir(parents=True)
    run.drop_recording(director.CaptureFailed("gate 10", "a count"), frames, work)
    assert frames.exists()


# --- A slider moved on its own steps ------------------------------------------------------


class Clock:
    def __init__(self):
        self.ms = 1_000_000.0

    def now(self):
        return self.ms


class Overlay:
    def __exit__(self, *args):
        pass


class Screencast:
    def show_overlay(self, html):
        return Overlay()


class Mouse:
    def move(self, x, y):
        pass

    def click(self, x, y):
        pass


class Allocation:
    """The calculator's allocation slider: 0.05-1.00 in steps of 0.05, at 1.00."""

    box = {"x": 400.0, "y": 300.0, "width": 960.0, "height": 24.0}

    def __init__(self, value=1.0, step="0.05", sticks=False):
        self.value = value
        self.step = step
        self.sticks = sticks
        self.filled = []

    def wait_for(self, state, timeout):
        pass

    def bounding_box(self):
        return dict(self.box)

    def nth(self, index):
        return self

    def fill(self, text):
        self.filled.append(text)
        if not self.sticks:
            self.value = float(text)

    def evaluate(self, script, *args):
        if script == director.RANGE_JS:
            return ["INPUT", "range", 0.05, 1.0, self.value, self.step]
        assert script == "el => Number(el.value)"
        return self.value


class StandIn:
    def __init__(self, clock, element):
        self.clock = clock
        self.element = element
        self.mouse = Mouse()
        self.screencast = Screencast()

    def wait_for_timeout(self, ms):
        self.clock.ms += ms

    def locator(self, css):
        return self.element


@pytest.fixture
def stage(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(director, "_now_ms", clock.now)

    def make(element):
        made = director.Director(
            context=object(),
            page=StandIn(clock, element),
            frames=tmp_path / "work" / "video" / "capture" / "frames",
            dashboard=DASH,
            api_port=28400,
            dashboard_port=28401,
            redactor=director.redaction.Redactor(),
            forbidden=(),
            work=guard.WorkRoot(tmp_path / "work"),
        )
        made.recorder.t0 = clock.ms
        made.where = f"scene {SCENE} beat 3"
        return made

    return make


def slide_to(made, element, value):
    _found, box = made.box(storyboard.Locator(css="#power-allocation"))
    made.slide(element, box, value, "css '#power-allocation'")


def test_the_allocation_slides_on_its_own_steps_to_50_percent(stage):
    allocation = Allocation()
    made = stage(allocation)
    slide_to(made, allocation, 0.5)
    assert allocation.value == 0.5 and allocation.filled[-1] == "0.5"
    values = [float(text) for text in allocation.filled]
    assert values == sorted(values, reverse=True) and len(values) > 5
    for text in allocation.filled:
        # Written as the browser writes a range's value, or fill refuses it.
        assert re.fullmatch(r"0\.[0-9]?[1-9]", text), text
        assert director.on_step(float(text), 0.05, 0.05), text
    # The cursor ends on the thumb at 0.50: 0.45 of 0.95 along the track.
    thumb = config.RANGE_THUMB_CSS_PX * config.ZOOM
    assert made.cursor[0] == pytest.approx(
        400 + thumb / 2 + (960 - thumb) * 0.45 / 0.95
    )


@pytest.mark.regression
@pytest.mark.parametrize("value", [0.52, 0.071])
def test_a_value_off_the_sliders_steps_is_refused(stage, value):
    allocation = Allocation()
    made = stage(allocation)
    with pytest.raises(
        director.CaptureFailed, match="moves in steps of 0.05 from 0.05"
    ):
        slide_to(made, allocation, value)
    assert allocation.filled == []


def test_a_slider_that_does_not_land_is_refused(stage):
    allocation = Allocation(sticks=True)
    made = stage(allocation)
    with pytest.raises(director.CaptureFailed, match="stopped at 1, not 0.5"):
        slide_to(made, allocation, 0.5)


class FlagSlider(Allocation):
    """Video 3's slider: 0-100 with no step attribute; each fill timed."""

    clock = None
    times: list = []

    def fill(self, text):
        self.times = [*self.times, self.clock.ms]
        super().fill(text)

    def evaluate(self, script, *args):
        if script == director.RANGE_JS:
            return ["INPUT", "range", 0, 100, self.value, ""]
        assert script == "el => Number(el.value)"
        return self.value


def test_video_3s_slider_fills_the_whole_numbers_it_always_did(stage):
    """No step attribute moves by 1, typed as whole numbers: the same values,
    at the same moments, as the slide video 3 was recorded with."""
    flag = FlagSlider(value=10, step="")
    made = stage(flag)
    flag.clock = made.page.clock
    slide_to(made, flag, 50)
    assert flag.filled[-1] == "50" and flag.value == 50
    # Before stepped sliders: int(round(start + (value - start) * ease(t))),
    # filled when it changed, every 15 ms of the slide's clock.
    before, at, t_ms, current = [], [], 0.0, 10
    while True:
        t = min(1.0, t_ms / config.SLIDE_MS)
        now = int(round(10 + 40 * director.ease(t)))
        if now != current:
            before.append(str(now))
            at.append(t_ms)
            current = now
        if t >= 1.0:
            break
        t_ms += 15
    assert flag.filled == before
    assert [ms - flag.times[0] for ms in flag.times] == [ms - at[0] for ms in at]


@pytest.mark.parametrize(
    "written, step, places",
    [
        ("", 1.0, 0),
        ("1", 1.0, 0),
        ("0.05", 0.05, 2),
        ("0.5", 0.5, 1),
        ("any", 1.0, 0),
        ("-1", 1.0, 0),
    ],
)
def test_a_sliders_step_is_read_as_written_and_1_without_one(written, step, places):
    assert director.range_step(written) == (step, places)


@pytest.mark.regression
@pytest.mark.parametrize(
    "value, places, text",
    [
        (0.9, 2, "0.9"),
        (0.5000000000000001, 2, "0.5"),
        (1.0, 2, "1"),
        (0.95, 2, "0.95"),
        (0.05, 2, "0.05"),
        (50, 0, "50"),
        (10.0, 0, "10"),
    ],
)
def test_a_range_value_is_typed_as_the_browser_writes_it(value, places, text):
    """Chromium keeps a range's value as "0.9", never "0.90", and Playwright's
    fill refuses a value that reads back different ("Malformed value",
    measured in the dress rehearsal on the calculator's allocation)."""
    assert director.range_text(value, places) == text


def test_a_slide_value_may_be_fractional_in_the_storyboard():
    step = storyboard.Slide.model_validate(
        {"slide": {"css": "#power-allocation"}, "value": 0.5}, strict=False
    )
    assert step.value == 0.5
    whole = storyboard.Slide.model_validate(
        {"slide": {"testid": "rollout-percentage"}, "value": 10}, strict=False
    )
    assert whole.value == 10


# --- The end state as a whole -------------------------------------------------------------


def test_the_end_state_is_the_calculations_and_the_team():
    end = board().end_state
    assert end.signed_in_as == config.DEMO_ADMIN[0]
    assert (
        end.experiments_listed is None and end.experiment is None and end.flag is None
    )
    assert [(i.email, i.role) for i in end.team.invitees] == [
        ("priya@example.com", "DEVELOPER"),
        ("marcus@example.com", "ANALYST"),
    ]
    recorded = {name for beat in beats() for name in beat.record}
    assert recorded == {"two-variants", "three-variants", "team"}


def test_each_invite_beat_types_and_chooses_the_invitee_the_end_state_names():
    """The beats invite the end state's teammates, in its order: each one's
    email typed, and its role's label chosen in the dialog."""
    invited = []
    for beat in beats():
        steps = beat.steps
        emails = [
            s.text
            for s in steps
            if isinstance(s, storyboard.Type) and s.type.testid == "invite-email-input"
        ]
        roles = [
            s.option
            for s in steps
            if isinstance(s, storyboard.Choose)
            and s.choose.testid == "invite-role-select"
        ]
        if emails or roles:
            assert len(emails) == len(roles) == 1, beat.cue
            invited.append((emails[0], roles[0]))
    assert invited == [
        (i.email, gates.ROLE_LABELS[i.role]) for i in board().end_state.team.invitees
    ]


def test_an_end_state_with_only_a_team_or_calculations_is_one():
    only = SimpleNamespace(
        signed_in_as="admin@demo.com",
        calculations=[
            {
                "shown": "x",
                "baseline": 0.05,
                "mde": 0.1,
                "variants": 2,
                "per_variant": 31234,
            }
        ],
    )
    made = storyboard.EndState.model_validate(vars(only), strict=False)
    assert len(made.calculations) == 1
    with pytest.raises(ValueError, match="calculations or a team"):
        storyboard.EndState.model_validate(
            {"signed_in_as": "admin@demo.com"}, strict=False
        )
