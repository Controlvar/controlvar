"""A video's storyboard: the YAML file, its refusals, and the cue list it compiles to.

A storyboard is a list of scenes. A scene is either a page (opened off camera
at ``open``, then recorded) or a card (``card``: an HTML card drawn by the same
browser and recorded like a page). A scene is a list of beats; a beat is

* ``before``: waits for the page the last beat's click opened. Recording
  pauses while they run (a cut: no spinner is filmed, and the capture
  directory allows at most a second without a caption), so a beat's clicks go
  in ``exit``, not here;
* ``cue``: the caption, and ``focus``: the element it is about, which must be
  inside the page area when the cue starts and when it ends (gate 9);
* ``steps``: run while the caption is on screen. None may remove the focus;
* the hold: the cue stays until its reading time (``contract.reading_time_ms``)
  has passed, and a ``payoff`` beat then holds ``config.PAYOFF_MS`` more with
  nothing moving (U5);
* ``exit``: run after the cue's end check, with the caption still up: the
  click that leaves the page;
* ``record``: elements whose text is read at the cue's start, for the end
  state (gate 10) to compare with the API;
* ``say``: the narration, one to four sentences, each a caption of its own
  (``showcase.capture.narration`` says what a sentence may say), and
  ``claims``: any number it says as a word, with the page of the docs that
  states it. A storyboard narrates every beat or none, and the cue list
  (``compile_cues``) is the same either way: the voice step (``python -m
  showcase.voice``) makes the clips, and a narrated capture holds each beat
  for its clips as well (``showcase.capture.voiced``).

A scene opens a page (``open``), an experiment's results page by the
experiment's name (``results_of``, its id looked up off camera), or draws a
card: one documented command, lines quoted from a page of the docs (``code``,
checked against the recorded tree), or one headline. ``traffic`` starts or
finishes the storyboard's simulated users off camera before the scene, or
``run``s them, start to finish, before it (one card that both follows them and
says how long they took).
``setup`` makes what the video uses but does not film being made (a segment),
through the API, off camera, after gate 14 and before the first scene.

A scene is recorded in the dashboard or in a demo app (``app``: ``shoplab``,
``streampulse``); ``open`` is a path on that app's origin. ``off_camera`` is a
closed list of what runs before the scene is opened: ``seed``, an app's own
seed (``history`` off unless asked for), and ``visitor``, the app's own
localStorage keys the scene is filmed with (T178 condition 5: one browser
context, so the visitor is set, never a second context made). An app's scenes
come after its seed, which runs once. An app's first ``visitor`` is set by an
init script before the app first loads (so its first load, the off-camera
arming, is that visitor's); a later one is a declared off-camera switch,
which clears the origin's storage and sets its keys before the scene's page
opens, and so must follow a scene of the same app (the page is on its origin).
In a storyboard whose end state counts an experiment's users, every app with
a visitor key (``config.APP_VISITOR_KEY``) declares its visitors, from its
first scene: no page of it loads under an id the storyboard does not name.

Video 4's incident adds three steps to that closed list (S4 of plan v2.2),
each once and in this order after the StreamPulse seed: ``story: 2``, the
demo's own rollout script, its step 2 only; ``crashes``, the tool's fixed
crash sender, started right after a safety run completes; and, on the card
after B9 (a dashboard scene filmed before the monitor's next run),
``wait_for_rollback``, which waits for the safety monitor to roll the flag
back by itself. That card says the gap the cut hides, measured
(``{rollback_minutes}``, nowhere else), and the end state's ``rollback`` names
what gate 10/4 holds the platform and the page to. A dashboard scene may open
a flag's page by the flag's name (``flag_of``), its id looked up off camera.

Three steps work a form control the way a person would be seen to: ``choose``
changes a native <select> to one of its options without opening it (a
screencast never draws a native list, so an open one would film a select
changing by itself; C9), ``slide`` moves a range input to a value on its
own steps (the power calculator's are 0.05 apart), the drawn cursor on its
thumb, and ``date`` sets a ``datetime-local`` input, its picker never opened
(as ``choose``), to a day a few days after the take at a fixed time of day,
in the browser's zone: the page computes the value, so the take is right on
any day it is filmed, and its reading keeps the value, its instant and how
the page draws it. A storyboard with a ``date`` step pins the browser's
``time_zone`` and ``locale``, so the take is the same on any machine; one
without names neither, and its browser is as it always was.

A scene that films a credential the product shows (the invite dialog's
one-time password) names it under ``masks``, a closed list: the capture hides
it before it is painted (C4 of T149, S6 of plan v2.2) and the scene states the
"masked value" departure (D84). An end state may name the power calculator's
sample sizes the video shows (``calculations``) and the teammates it invites
(``team``), each held by gate 10 (S8c).

Refused at load (``StoryboardError``), before any stack starts: an unknown key,
a cue with no focus, a caption ``contract.caption_problems`` refuses, a caption
holding a template (``{``) or anything but plain text, a ``type`` of a
credential into a step not marked secret, an on-camera step that opens the API
keys page, a locator naming nothing, a scene with no beats, a code line that
names a host and port, traffic finished before it starts, a ``choose`` or
``slide`` in ``before`` (cut, so never seen), a ``date`` anywhere but a beat's
steps, ``days_ahead`` outside 1 to 7, ``at`` other than 06:00 to 21:59, a
``date`` reading named twice or as another beat's ``record``, a ``date`` on a
storyboard that does not pin its ``time_zone`` and ``locale``, a zone the
zone database does not know, a flag's end state naming a
segment the setup does not make or a reading no beat records, an incident step
other than ``story: 2`` or out of order, or ``{rollback_minutes}`` anywhere but
the card after the wait, and a narration ``narration.board_problems`` refuses
(every problem at once). Caption text
never comes from the page or the run: ``compile_cues`` is a pure function of
the file, so the cue list is the same on every run (gate 13a).
"""

from __future__ import annotations

import json
import re
import zoneinfo
from pathlib import Path
from typing import Any, Dict, List, Literal, Mapping, Optional, Tuple, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from showcase import contract
from showcase.capture import config


class StoryboardError(ValueError):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Locator(_Strict):
    """How a step or a cue finds its element. Exactly one way, plus ``nth``."""

    role: Optional[str] = None
    name: Optional[str] = None
    exact: bool = False
    text: Optional[str] = None
    label: Optional[str] = None
    testid: Optional[str] = None
    css: Optional[str] = None
    nth: int = 0

    @model_validator(mode="after")
    def _one_way(self) -> "Locator":
        ways = [
            w for w in (self.role, self.text, self.label, self.testid, self.css) if w
        ]
        if len(ways) != 1:
            raise ValueError(
                "a locator names exactly one of role, text, label, testid or css"
            )
        if self.name is not None and self.role is None:
            raise ValueError("name is a role's accessible name; give role too")
        return self

    def describe(self) -> str:
        if self.role:
            return f"{self.role} {self.name!r}" if self.name else str(self.role)
        for key in ("text", "label", "testid", "css"):
            value = getattr(self, key)
            if value:
                return f"{key} {value!r}"
        return "?"


class Click(_Strict):
    click: Locator


class Move(_Strict):
    move: Locator


class Type(_Strict):
    type: Locator
    text: str
    #: The field must be a password field, drawn as dots.
    secret: bool = False
    #: Empty the field first (it holds a default the storyboard replaces).
    clear: bool = False
    #: Select the field's text, then type over it, as a person replaces a value.
    #: For a number box: emptied first (``clear``), it can draw "034" for 34.
    select: bool = False

    @model_validator(mode="after")
    def _one_way_to_replace(self) -> "Type":
        if self.select and self.clear:
            raise ValueError(
                "a type step replaces a value one way: select or clear, not both"
            )
        if self.select and self.secret:
            raise ValueError(
                "a secret field starts empty: there is nothing to select in it"
            )
        return self


class Look(_Strict):
    """A ring around an element, held ``ms`` (at most 2.5 s, one ring at a time)."""

    look: Locator
    ms: int = Field(default=1500, ge=300, le=2500)


class ScrollTo(_Strict):
    scroll_to: Locator


class WaitFor(_Strict):
    """Wait, off the clock of any cue, for an element or an address."""

    wait_for: Optional[Locator] = None
    url: Optional[str] = None
    timeout_ms: int = Field(default=15000, ge=100, le=60000)

    @model_validator(mode="after")
    def _one(self) -> "WaitFor":
        if (self.wait_for is None) == (self.url is None):
            raise ValueError("wait_for names an element or a url, not both")
        return self


class Choose(_Strict):
    """A native <select> set to the option whose text is ``option``, never opened.

    The cursor goes to the select and presses it; the value changes; the
    closed select is held on its new text (C9).
    """

    choose: Locator
    option: str = Field(min_length=1, max_length=80)


class Slide(_Strict):
    """A range input moved to ``value``, the drawn cursor on its thumb.

    The value is the input's own (the power calculator's allocation runs from
    0.05 to 1.00 in steps of 0.05), and must be one of its steps.
    """

    slide: Locator
    value: float = Field(ge=0, le=100_000)


#: A ``date`` step's time of day, "HH:MM": 06:00 to 21:59, clear of midnight
#: and of the night hours a change of daylight saving time skips or repeats.
DATE_AT = r"^(?:0[6-9]|1[0-9]|2[01]):[0-5][0-9]$"
#: The name a ``date`` step keeps its reading under.
READING_NAME = r"^[a-z][a-z0-9-]{0,40}$"


class Date(_Strict):
    """A ``datetime-local`` input set to the day ``days_ahead`` days after the
    page's own today, at ``at``, in the browser's zone (the storyboard's
    ``time_zone``), its picker never opened (C9, as ``choose``).

    The cursor goes to the field and presses it; the page computes the value
    and the field is filled with it; gate 12 reads it back (``gates.date_as_set``).
    ``record`` keeps the value, its instant (``toISOString``) and how the page
    draws it (``toLocaleString``)."""

    date: Locator
    days_ahead: int = Field(ge=1, le=7)
    at: str = Field(pattern=DATE_AT)
    record: str = Field(pattern=READING_NAME)


Step = Union[Click, Move, Type, Look, ScrollTo, WaitFor, Choose, Slide, Date]
#: The steps that enter data. In a cut (a beat's ``before``) what they enter is
#: entered off camera, so the scene states off-camera setup (D84,
#: ``required_departures``). A click is not one: video 2 clicks its Live tab in
#: a cut, the cursor on the tab before it. The load refuses a choose, a slide
#: or a date in a cut outright (``parse``), so today the row is met by a type;
#: the other three are in it for the day that refusal is lifted.
ENTERS_DATA = (Type, Choose, Slide, Date)


class Claim(_Strict):
    """A number the narration says as a word, and the page of the docs that
    states it. A person checks it (U6): no page check can see a number word."""

    says: str = Field(min_length=1, max_length=80)
    source: str = Field(pattern=r"^docs/[A-Za-z0-9/_.-]+\.md(?::[0-9]{1,5})?$")


class Beat(_Strict):
    before: List[Step] = Field(default_factory=list)
    cue: str
    focus: Locator
    steps: List[Step] = Field(default_factory=list)
    exit: List[Step] = Field(default_factory=list)
    payoff: bool = False
    #: Role=alert elements this beat may show (none by default; gate 11).
    alerts: List[str] = Field(default_factory=list)
    #: Elements whose text is kept at the cue's start, by name, for gate 10.
    record: Dict[str, Locator] = Field(default_factory=dict)
    #: The narration: up to four sentences, spoken in order (none: a silent beat).
    say: List[str] = Field(default_factory=list, max_length=contract.MAX_SAY_SENTENCES)
    #: Number words the narration says, each with its page of the docs.
    claims: List[Claim] = Field(default_factory=list)


#: The longest line a code card shows (it is drawn 1,600 px wide).
CODE_LINE_CHARS = 110
#: A host and port in a line of code (``localhost:8000``, ``127.0.0.1:3000``).
HOST_PORT = re.compile(
    r"(?:localhost|[0-9]{1,3}(?:\.[0-9]{1,3}){3}|\w+\.\w+):[0-9]{2,5}"
)


class Code(_Strict):
    """Lines quoted from a fenced block under a heading of a page of the docs.

    Each line is the page's own, in the page's order (``quoted_lines``
    checks it against the recorded tree, and a unit test against this one).
    """

    doc: str = Field(pattern=r"^docs/[A-Za-z0-9/_.-]+\.md$")
    section: str = Field(min_length=1)
    lines: List[str] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def _no_host_port(self) -> "Code":
        for line in self.lines:
            if HOST_PORT.search(line):
                raise ValueError(f"a code line names a host and port: {line!r}")
            if len(line) > CODE_LINE_CHARS:
                raise ValueError(
                    f"a code line is over {CODE_LINE_CHARS} characters: {line!r}"
                )
        return self


class Card(_Strict):
    """A card drawn by the browser: a label, then one documented command, a few
    quoted lines of code, or a headline, and one sentence."""

    label: str
    command: Optional[str] = Field(default=None, min_length=1)
    code: Optional[Code] = None
    headline: Optional[str] = Field(default=None, min_length=1, max_length=40)
    note: str

    @model_validator(mode="after")
    def _one_body(self) -> "Card":
        # Counted as ``card_html`` chooses what to draw: by presence, not by
        # truthiness. A second layer: ``min_length=1`` on command and headline
        # already refuses an empty one, so this alone cannot be seen failing.
        bodies = [b for b in (self.command, self.code, self.headline) if b is not None]
        if len(bodies) != 1:
            raise ValueError("a card shows exactly one of command, code or headline")
        return self

    def drawn_text(self) -> List[str]:
        """What ``card_html`` draws: the label, the one body, and the note."""
        if self.command is not None:
            body = [self.command]
        elif self.code is not None:
            body = list(self.code.lines)
        else:
            body = [self.headline or ""]
        return [self.label, *body, self.note]


def card_texts(card: Optional[Card]) -> List[str]:
    """Everything a card draws (``Card.drawn_text``), or nothing for no card."""
    return [] if card is None else [text for text in card.drawn_text() if text]


App = Literal["dashboard", "shoplab", "streampulse"]


class SeedApp(_Strict):
    """Off camera: a demo app's seed (``backend.scripts.seed_<app>``)."""

    seed: Literal["shoplab", "streampulse"]
    #: The seed's days of history (its default); off is ``--no-history``.
    history: bool = False


#: A visitor's storage: at most this many keys, each value at most this long.
VISITOR_KEYS_MAX = 4
VISITOR_VALUE_MAX = 200
#: ShopLab's own test for a visitor id (demo/shoplab/src/lib/visitor.ts,
#: ``UUID_V4``): an id it fails is silently replaced by a random one, so the
#: page would load as a visitor nobody declared. A unit test keeps the two equal.
#: Applied with ``fullmatch``: Python's ``$`` also matches before a trailing
#: newline, which the JavaScript one (no ``m`` flag) does not.
UUID_V4 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class Visitor(_Strict):
    """Off camera: the localStorage a demo app's scene is filmed with (T178
    condition 5). Its keys are the app's own (``<app>.``), and one of them is
    the app's visitor id (``config.APP_VISITOR_KEY``), a UUID v4."""

    visitor: Dict[str, str] = Field(min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> "Visitor":
        if len(self.visitor) > VISITOR_KEYS_MAX:
            raise ValueError(
                f"a visitor sets at most {VISITOR_KEYS_MAX} keys, not {len(self.visitor)}"
            )
        for key, value in self.visitor.items():
            if "token" in key.lower():
                raise ValueError(f"a visitor never sets a token ({key!r})")
            if len(value) > VISITOR_VALUE_MAX:
                raise ValueError(
                    f"the visitor's {key!r} is over {VISITOR_VALUE_MAX} characters"
                )
        return self


class Story(_Strict):
    """Off camera: the StreamPulse demo's own rollout script, its step 2 only
    (``rollout_story.py --step 2``: the Player v2 schedule's stage 2, 25%),
    run from the recorded tree against the tool's stack (S4 of plan v2.2).
    Any other step is refused at load: step 3 sends incident traffic of its
    own, and step 4 rolls the flag back by hand when the monitor is late. The
    video's incident is the tool's own fixed sender (``Crashes``)."""

    story: Literal[2]


class CrashPlan(_Strict):
    """How many times Android 12 devices ask for the flag, and how many crashes
    they report. Each crashing device runs one loop (``config.CRASH_LOOP``: it
    asks for the flag and crashes, again and again) as one of
    ``config.CRASH_DEVICES``; every other ask is a device that does not crash."""

    evaluations: int = Field(ge=1, le=1000)
    reports: int = Field(ge=1, le=200)

    @model_validator(mode="after")
    def _loops(self) -> "CrashPlan":
        if self.reports % config.CRASH_LOOP:
            raise ValueError(
                f"crash reports come {config.CRASH_LOOP} a crashing device;"
                f" {self.reports} is not a whole number of devices"
            )
        if self.reports // config.CRASH_LOOP > len(config.CRASH_DEVICES):
            raise ValueError(
                f"{self.reports} crash reports need more than the"
                f" {len(config.CRASH_DEVICES)} crashing devices the tool has"
            )
        if self.evaluations < self.reports:
            raise ValueError(
                f"each crash follows an ask for the flag: {self.evaluations} asks"
                f" cannot carry {self.reports} crashes"
            )
        return self


class Crashes(_Strict):
    """Off camera: the tool's fixed crash sender (S4, T156 C15). Simulated
    Android 12 devices ask for ``config.CRASH_FLAG`` through the API with the
    app's key, and report their crashes with the StreamPulse server key, held
    in this process only. It starts right after a safety run completes (B9's
    anchor), and every answer must be 2xx."""

    crashes: CrashPlan


class RollbackWait(_Strict):
    """What ``wait_for_rollback`` waits for: the safety monitor's own rollback
    of *flag* to *percentage*, within *within_s* seconds."""

    flag: str = Field(pattern=r"^[a-z0-9][a-z0-9_]{0,63}$")
    percentage: int = Field(ge=0, le=100)
    within_s: int = Field(ge=30, le=600)


class WaitForRollback(_Strict):
    """Off camera: wait for the safety monitor to roll the flag back by itself.
    The tool reads the flag and its audit log; nothing ever asks for a
    rollback (``POST .../rollback``)."""

    wait_for_rollback: RollbackWait


#: What may run off camera before a scene: a closed list.
OffCamera = Union[SeedApp, Visitor, Story, Crashes, WaitForRollback]
#: Video 4's incident, off camera, by each step's name in the storyboard.
INCIDENT_KINDS: Dict[type, str] = {
    Story: "story",
    Crashes: "crashes",
    WaitForRollback: "wait_for_rollback",
}
#: In the card right after ``wait_for_rollback``, and nowhere else: the gap the
#: cut hides, measured on the wall clock from B9's last frame (S5 of plan v2.2).
ROLLBACK_MINUTES = "{rollback_minutes}"
#: In a card that finishes the traffic: how long it took, measured (run.py).
TRAFFIC_MINUTES = "{traffic_minutes}"
#: The measured values a card may name, each filled in by the run.
MEASURED = (TRAFFIC_MINUTES, ROLLBACK_MINUTES)

#: A departure from a new user's path (D84), each stated on screen where it
#: happens: users the tool simulates, a scheduler interval shortened, a time
#: skip, something made off camera, a value masked by the tool.
DepartureKind = Literal[
    "simulated traffic",
    "shortened interval",
    "time skip",
    "off-camera setup",
    "masked value",
]


class Departure(_Strict):
    """A departure, and the words on screen that state it (D84).

    ``says`` must be drawn in the scene: in one of its captions, or in its
    card's label, headline, note or command (``departure_problems``).
    """

    kind: DepartureKind
    says: str = Field(min_length=8, max_length=200)


#: What a scene may mask (C4 of T149, S6 of plan v2.2), by the element's
#: ``data-testid``: a closed list of credentials the product draws, so no other
#: product text is ever hidden by the tool (D84 rule 3). The invite dialog's
#: one-time password (frontend/src/components/admin/users/InviteUserModal.tsx;
#: a unit test keeps the two equal, and ``config.INVITE_PASSWORD_TESTID``).
MaskedValue = Literal["invite-temp-password"]


class Scene(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,40}$")
    #: The origin the scene is recorded on: the dashboard, or a demo app.
    app: App = "dashboard"
    #: Run, in order, before the scene is opened.
    off_camera: List[OffCamera] = Field(default_factory=list)
    #: Every departure from a new user's path in this scene, stated on screen (D84).
    departures: List[Departure] = Field(default_factory=list)
    #: Credentials this scene films, each hidden by the tool before it is
    #: painted (C4, S6): the scene states the "masked value" departure.
    masks: List[MaskedValue] = Field(default_factory=list)
    open: Optional[str] = Field(default=None, pattern=r"^/[A-Za-z0-9/_?=&.-]*$")
    #: Open this experiment's results page; the id is looked up off camera.
    results_of: Optional[str] = None
    #: Open this feature flag's page, by the flag's name; the id is looked up
    #: off camera, as ``results_of`` does.
    flag_of: Optional[str] = Field(default=None, min_length=1, max_length=128)
    card: Optional[Card] = None
    #: Sign in as the demo admin off camera before the scene.
    signed_in: bool = False
    #: Results to load off camera first, so no skeleton is filmed (by experiment name).
    warm_results: List[str] = Field(default_factory=list)
    #: Off camera, before the scene: start the storyboard's simulated users
    #: (and wait for the first of them), wait for the last, or both (``run``).
    traffic: Optional[Literal["start", "finish", "run"]] = None
    beats: List[Beat] = Field(min_length=1)

    @model_validator(mode="after")
    def _page_or_card(self) -> "Scene":
        openers = (self.open, self.results_of, self.flag_of, self.card)
        if sum(x is not None for x in openers) != 1:
            raise ValueError(
                "a scene opens a page, opens an experiment's results or a flag's"
                " page, or draws a card: exactly one, not both"
            )
        if self.app != "dashboard" and (
            self.open is None or self.signed_in or self.warm_results
        ):
            raise ValueError(
                f"a {self.app} scene opens a page of the app; results, cards,"
                " signing in and warm results are the dashboard's"
            )
        visitors = [item for item in self.off_camera if isinstance(item, Visitor)]
        if visitors:
            visitor_problem(self.app, visitors)
        return self

    @property
    def visitor(self) -> Optional[Dict[str, str]]:
        """The storage this scene is filmed with (its ``visitor``), or None."""
        found = [item.visitor for item in self.off_camera if isinstance(item, Visitor)]
        return found[0] if found else None


def visitor_problem(app: str, visitors: List[Visitor]) -> None:
    """Raises (ValueError) when a scene's ``visitor`` items are not one visitor
    of an app that has visitors, with the app's own keys and a UUID v4 id."""
    key = config.APP_VISITOR_KEY.get(app)
    if key is None:
        raise ValueError(f"a visitor is set only in an app with visitors, not {app}")
    if len(visitors) != 1:
        raise ValueError("a scene is filmed with one visitor")
    storage = visitors[0].visitor
    foreign = [k for k in storage if not k.startswith(f"{app}.")]
    if foreign:
        raise ValueError(
            f"a {app} visitor sets only {app}'s own keys, not {foreign[0]!r}"
        )
    if key not in storage:
        raise ValueError(f"a {app} visitor names its id ({key})")
    if not UUID_V4.fullmatch(storage[key]):
        raise ValueError(
            f"{key} {storage[key]!r} is not a UUID v4: {app} would replace it with"
            " a random one, and the scene would be filmed as a visitor nobody declared"
        )


class SeedCounts(_Strict):
    experiments: int
    feature_flags: int
    users: int


class Shopper(_Strict):
    """A visitor filmed in a demo app, counted in the experiment (S8b of plan
    v2.2): the arm the storyboard declares for them, the testid the page draws
    for that arm, and whether they buy on camera."""

    #: The visitor id a scene's ``visitor`` sets.
    visitor: str
    #: The variant's name, as the API names it.
    arm: str = Field(min_length=1)
    #: The ``data-testid`` the page draws for that arm (never read from the app).
    drawn: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,60}$")
    #: The shopper sends the experiment's event (the traffic's) on camera.
    buys: bool


class ExperimentEnd(_Strict):
    """Gate 10 for an experiment the video made: it exists once, as made, its
    users and conversions are the traffic's, and the page showed the API's result."""

    name: str
    key: str
    status: Literal["active"]
    #: The winning variant's name, as the API names it.
    winner: str
    #: The ``record`` name of the element that showed the recommendation.
    shown: str
    #: The minimum detectable effect typed in the wizard: the Planned sample
    #: card plans with it, as the effect saved with the experiment (#1257).
    mde: float = Field(gt=0, lt=1)
    #: The ``record`` names of the Planned sample card and of its effect line.
    planned: str
    effect: str
    #: The visitors filmed in a demo app, each counted with the traffic (S8b):
    #: every visitor a scene declares, and no other.
    on_camera_shoppers: List[Shopper] = Field(default_factory=list)


#: The audit action types a flag's end state may name.
FlagAction = Literal[
    "feature_flag_create", "feature_flag_update", "toggle_enable", "toggle_disable"
]


class FlagEnd(_Strict):
    """Gate 10 for a flag the video made: it exists once, with its key, its one
    rule naming the setup's segment, at the rollout and status the video left
    it, and the audit log holds the video's changes to it, each by the
    signed-in user, as the page showed them."""

    name: str
    key: str
    #: The segment the flag's only rule (``is in segment``) names.
    segment: str
    rollout_percentage: int = Field(ge=0, le=100)
    status: Literal["active", "inactive"]
    #: The audit entries on the flag, oldest first: all of them, exactly.
    audit: List[FlagAction] = Field(min_length=1)
    #: The ``record`` name of the audit log's rows as the page showed them.
    shown: str


#: The audit action types a rolled-back flag's end state may name.
RollbackAction = Literal[
    "feature_flag_update", "toggle_enable", "toggle_disable", "safety_rollback"
]


class Agreement(_Strict):
    """Gate 10/4 at one beat's start, after the rollback (plan v2 C1): the app's
    panel row for the flag, its phone and the API's answer for the device, read
    off camera with the app's key and the context the app sends, all say the
    same: on or off (``enabled``)."""

    scene: str
    beat: int = Field(ge=1)
    #: The StreamPulse device the app is filmed as (``config.STREAMPULSE_DEVICES``).
    device: str
    #: The ``record`` names of the panel's reason for the flag, and of the phone's screen.
    panel: str
    phone: str
    enabled: bool


class KillSwitch(_Strict):
    """A flag the video switches off on camera: its exact audit entries."""

    name: str
    key: str
    audit: List[FlagAction] = Field(min_length=1)


class RollbackEnd(_Strict):
    """Gate 10/4 (S8a of plan v2.2): the safety monitor rolled the flag back by
    itself, and every reading of it agrees.

    * the flag is at ``percentage``, still active, and its schedule is paused;
    * its audit entries, oldest first, are exactly ``audit``, every one by the
      signed-in user but the last, one ``safety_rollback`` by the safety
      monitor (``system:safety-monitor``), an automatic one;
    * the API's log holds that one rollback, automatic, and no POST asked for
      a rollback, from the page or from the tool;
    * the page drew it: the safety badge at B9, the schedule's badge and the
      rollout after the rollback, the audit log's top row and the rollback's
      reason, and the agreement at ``agree``;
    * the kill switch the video turned off is off, with its exact entries.
    """

    name: str
    key: str
    percentage: int = Field(ge=0, le=100)
    status: Literal["active"]
    schedule: Literal["paused"]
    audit: List[RollbackAction] = Field(min_length=1)
    #: The ``record`` names of what the page drew: the audit log's rows and the
    #: rollback's details, the safety badge at B9, and the schedule's badge and
    #: the rollout percentage after the rollback.
    shown: str
    reason: str
    unhealthy: str
    paused: str
    rolled_back: str
    agree: Agreement
    kill_switch: KillSwitch

    @model_validator(mode="after")
    def _one_rollback_last(self) -> "RollbackEnd":
        if self.audit.count("safety_rollback") != 1 or self.audit[-1] != (
            "safety_rollback"
        ):
            raise ValueError(
                "a rolled-back flag's audit ends in one safety_rollback, and holds no other"
            )
        return self


class Calculation(_Strict):
    """Gate 10/5: a sample size the power calculator drew, with its inputs.

    The inputs are the calculator's: ``baseline`` and ``mde`` as fractions, the
    number of variants (control included), and its own 95% confidence and 80%
    power. ``per_variant`` is what the page draws, and must be the textbook
    formula's for those inputs (``gates.calculation_end``)."""

    #: The ``record`` name of the element that drew it.
    shown: str
    baseline: float = Field(gt=0, lt=1)
    mde: float = Field(gt=0, lt=1)
    variants: int = Field(ge=2, le=5)
    per_variant: int = Field(gt=0)


#: The roles the invite dialog offers, as the API names them.
Role = Literal["ADMIN", "DEVELOPER", "ANALYST", "VIEWER"]


class Invitee(_Strict):
    """A teammate the video invites (S8c of plan v2.2)."""

    #: At example.com, never a real address (the UX rules).
    email: str = Field(pattern=r"^[a-z][a-z0-9._-]{0,40}@example\.com$")
    role: Role


class Team(_Strict):
    """Gate 10/5 (S8c): the teammates the video invites, in the order it
    invites them, and the users table that shows them."""

    invitees: List[Invitee] = Field(min_length=1)
    #: The ``record`` name of the users table's body as the page showed it.
    shown: str


#: The most a variant's configuration may be in the guided setup, in UTF-8
#: bytes (frontend/src/components/experiments/new/formState.ts
#: ``CONFIGURATION_MAX_BYTES``; a unit test keeps them equal). It is the
#: dashboard's own limit: the API accepts larger ones.
CONFIGURATION_MAX_BYTES = 16_384

#: The confidence levels and corrections the guided setup offers
#: (frontend/src/components/results/shared/analysisSettings.ts
#: ``CONFIDENCE_OPTIONS`` and ``CORRECTION_OPTIONS``; a unit test keeps them
#: equal): a storyboard cannot claim a setting the dashboard cannot make.
CONFIDENCE_OPTIONS: Tuple[float, ...] = (0.9, 0.95, 0.99)
CORRECTION_OPTIONS: Tuple[str, ...] = ("benjamini_hochberg", "bonferroni", "none")

#: The operators the targeting builder offers (frontend/src/types/targeting.ts
#: ``OperatorType``; a unit test keeps them equal), less the two segment
#: operators: a segment condition's value is an id made at run time, which a
#: design end state cannot declare.
BUILDER_OPERATORS: Tuple[str, ...] = (
    "equals",
    "not_equals",
    "contains",
    "not_contains",
    "starts_with",
    "ends_with",
    "greater_than",
    "less_than",
    "greater_than_or_equal",
    "less_than_or_equal",
    "in",
    "not_in",
    "regex",
    "is_null",
    "is_not_null",
    "semver_eq",
    "semver_gt",
    "semver_lt",
    "semver_gte",
    "semver_lte",
    "geo_within_radius",
    "time_window",
    "array_contains",
    "array_intersects",
)
SEGMENT_OPERATORS: Tuple[str, ...] = ("in_segment", "not_in_segment")
#: The operators whose row has no value box: the builder sends ``null``.
NO_VALUE_OPERATORS: Tuple[str, ...] = ("is_null", "is_not_null")

#: What a design end state may compare with a recorded reading: the Review
#: step's rows, and the experiment page's analysis and Bayesian rows.
DesignReading = Literal[
    "review_variants",
    "review_metrics",
    "review_targeting",
    "review_analysis",
    "detail_analysis",
    "detail_bayesian",
]


class DesignVariant(_Strict):
    """A variant of the experiment the video designs, as the wizard saves it."""

    name: str = Field(min_length=1, max_length=100)
    allocation: int = Field(ge=0, le=100)
    control: bool = False
    #: Its JSON configuration; absent means none was typed.
    configuration: Optional[Dict[str, Any]] = None

    @model_validator(mode="after")
    def _configuration_fits(self) -> "DesignVariant":
        if self.configuration is not None:
            # The compact form is the least a person could type: over the
            # limit in it, the wizard refuses it in every form.
            text = json.dumps(self.configuration, separators=(",", ":"))
            size = len(text.encode("utf-8"))
            if size > CONFIGURATION_MAX_BYTES:
                raise ValueError(
                    f"variant {self.name!r}'s configuration is {size} bytes; the"
                    f" guided setup takes at most {CONFIGURATION_MAX_BYTES}"
                )
        return self


class DesignMetric(_Strict):
    """A metric of the experiment the video designs, as the wizard saves it."""

    name: str = Field(min_length=1, max_length=100)
    event: str = Field(min_length=1)
    type: Literal["conversion", "revenue", "count", "duration", "custom"]
    primary: bool = False
    #: The minimum detectable effect typed on the Estimate step: the wizard
    #: saves it on the primary metric only. Absent when none was typed.
    mde: Optional[float] = Field(default=None, gt=0, lt=1)
    lower_is_better: bool = False

    @model_validator(mode="after")
    def _effect_on_primary(self) -> "DesignMetric":
        if self.mde is not None and not self.primary:
            raise ValueError(
                f"metric {self.name!r} has an mde, and is not the primary metric:"
                " the wizard saves the typed effect on the primary only"
            )
        return self


class DesignCondition(_Strict):
    """One condition of a targeting rule, as the builder sends it (less its id)."""

    attribute: str = Field(min_length=1)
    operator: str
    value: Any = None

    @model_validator(mode="after")
    def _offered(self) -> "DesignCondition":
        if self.operator in SEGMENT_OPERATORS:
            raise ValueError(
                f"operator {self.operator!r}: a segment's id is made at run time,"
                " so a design end state does not declare one"
            )
        if self.operator not in BUILDER_OPERATORS:
            raise ValueError(
                f"operator {self.operator!r} is not one the targeting builder offers"
            )
        if self.operator in NO_VALUE_OPERATORS:
            if self.value is not None:
                raise ValueError(
                    f"operator {self.operator!r} has no value box: the builder"
                    f" sends null, not {self.value!r}"
                )
        elif not isinstance(self.value, str):
            raise ValueError(
                f"the builder sends the value box's text, a string, not {self.value!r}"
            )
        return self


class DesignGroup(_Strict):
    logical_operator: Literal["AND", "OR"]
    conditions: List[DesignCondition] = Field(min_length=1)


class DesignTargeting(_Strict):
    """Who can join, as the builder sends it, without the ids it adds to each
    group and condition (they are made in the browser)."""

    logical_operator: Literal["AND", "OR"]
    groups: List[DesignGroup] = Field(min_length=1)


class DesignAnalysis(_Strict):
    confidence: float
    correction: str
    bayesian: bool

    @model_validator(mode="after")
    def _offered(self) -> "DesignAnalysis":
        if self.confidence not in CONFIDENCE_OPTIONS:
            raise ValueError(
                f"confidence {self.confidence} is not one the guided setup offers"
                f" {list(CONFIDENCE_OPTIONS)}"
            )
        if self.correction not in CORRECTION_OPTIONS:
            raise ValueError(
                f"correction {self.correction!r} is not one the guided setup"
                f" offers {list(CORRECTION_OPTIONS)}"
            )
        return self


class DesignEnd(_Strict):
    """Gate 10 for an experiment the video designs and leaves a draft
    (``gates.design_end``): it exists once, exactly as declared, with nothing
    guided setup cannot set, and the readings named in ``shown`` said what was
    saved, in the product's words."""

    name: str = Field(min_length=1, max_length=100)
    key: str = Field(min_length=1)
    status: Literal["draft"]
    type: Literal["a_b", "mv"]
    #: Every variant, in the order the wizard holds them.
    variants: List[DesignVariant] = Field(min_length=2)
    #: Every metric, in the order the wizard holds them.
    metrics: List[DesignMetric] = Field(min_length=1)
    #: Absent: everyone can join.
    targeting: Optional[DesignTargeting] = None
    analysis: DesignAnalysis
    #: Each reading compared, by the ``record`` name of the beat that read it.
    shown: Dict[DesignReading, str] = Field(min_length=1)

    @model_validator(mode="after")
    def _as_the_wizard_saves(self) -> "DesignEnd":
        names = [variant.name for variant in self.variants]
        if len(set(names)) != len(names):
            raise ValueError(f"a variant name repeats: {names}")
        total = sum(variant.allocation for variant in self.variants)
        if total != 100:
            raise ValueError(f"the variants' allocations add up to {total}, not 100")
        controls = [variant.name for variant in self.variants if variant.control]
        if len(controls) != 1:
            raise ValueError(f"exactly one variant is the control, not {controls}")
        names = [metric.name for metric in self.metrics]
        if len(set(names)) != len(names):
            raise ValueError(f"a metric name repeats: {names}")
        primary = [metric.name for metric in self.metrics if metric.primary]
        if len(primary) != 1:
            raise ValueError(f"exactly one metric is primary, not {primary}")
        if not any(kind.startswith("review_") for kind in self.shown):
            raise ValueError(
                "a design end state compares at least one Review row (review_*)"
            )
        return self


class EndState(_Strict):
    """Gate 10: what is true after the last scene, checked through the API."""

    signed_in_as: str
    #: The experiments page's rows the video showed == the API's experiments.
    experiments_listed: Optional[int] = None
    experiment: Optional[ExperimentEnd] = None
    flag: Optional[FlagEnd] = None
    #: The power calculator's sample sizes the video showed.
    calculations: List[Calculation] = Field(default_factory=list)
    #: The teammates the video invited (their one-time passwords masked).
    team: Optional[Team] = None
    #: An experiment the video designed and left a draft.
    design: Optional[DesignEnd] = None
    #: A flag the safety monitor rolled back by itself (video 4).
    rollback: Optional[RollbackEnd] = None

    @model_validator(mode="after")
    def _something(self) -> "EndState":
        if (
            self.experiments_listed is None
            and self.experiment is None
            and self.flag is None
            and not self.calculations
            and self.team is None
            and self.design is None
            and self.rollback is None
        ):
            raise ValueError(
                "an end state names experiments_listed, an experiment, a flag,"
                " calculations or a team, or a design, or a rollback"
            )
        if self.design is not None and (
            self.experiment is not None or self.flag is not None
        ):
            raise ValueError(
                "a design end state stands alone: not with an experiment or a flag"
            )
        return self


class SegmentSetup(_Strict):
    """A rules segment, made through ``POST /api/v1/segments``."""

    name: str = Field(min_length=2, max_length=128)
    description: str = Field(max_length=512)
    rules: Dict[str, Any]


class GroupSetup(_Strict):
    """A mutual exclusion group, made through ``POST
    /api/v1/mutual-exclusion-groups``, and the seeded experiments then added
    to it, each by its name (its id looked up off camera)."""

    name: str = Field(min_length=1, max_length=200)
    traffic_allocation: float = Field(gt=0, le=1)
    experiments: List[str] = Field(min_length=1)


class Setup(_Strict):
    """What the video uses but does not film being made, made through the API
    off camera, after gate 14 (the seed's counts) and before the first scene."""

    segments: List[SegmentSetup] = Field(default_factory=list)
    exclusion_groups: List[GroupSetup] = Field(default_factory=list)

    @model_validator(mode="after")
    def _something(self) -> "Setup":
        if not self.segments and not self.exclusion_groups:
            raise ValueError("a setup makes at least one segment or exclusion group")
        return self


class Traffic(_Strict):
    """Simulated users for one experiment, sent through the tracking API off camera.

    The users and their conversions are ``backend.tests.realistic.data_generator``'s
    (#1064) at ``seed``: the same on every run. Each user is assigned by
    ``POST /api/v1/tracking/assign``, as the JavaScript SDK does, and converts
    or not by the variant the server answered.
    """

    experiment: str
    users: int = Field(ge=100, le=200_000)
    seed: int
    control_rate: float = Field(gt=0, lt=1)
    treatment_rate: float = Field(gt=0, lt=1)
    #: The metric's event, sent as the SDK sends it (``event_type`` and ``event_name``).
    event: str = Field(pattern=r"^[a-z0-9_]{1,40}$")
    #: Users sent before ``traffic: start`` lets its scene be filmed.
    first: int = Field(default=500, ge=1)
    #: Requests in flight at once.
    workers: int = Field(default=2, ge=1, le=8)


class Storyboard(_Strict):
    slug: str = Field(pattern=r"^[0-9]{2}-[a-z0-9-]+$")
    title: str = Field(min_length=1, max_length=40)
    seed: Literal["demo"]
    seed_counts: SeedCounts
    end_state: EndState
    #: Text this video must never show, on top of ``config.FORBIDDEN_TEXT`` (U4).
    never_show: List[str] = Field(default_factory=list)
    traffic: Optional[Traffic] = None
    setup: Optional[Setup] = None
    #: The browser's zone (an IANA name) and locale (``en-US``), pinned for a
    #: storyboard that draws times: both or neither. Neither: the browser takes
    #: the machine's, as it always has.
    time_zone: Optional[str] = Field(default=None, min_length=1, max_length=64)
    locale: Optional[str] = Field(default=None, pattern=r"^[a-z]{2,3}-[A-Z]{2}$")
    scenes: List[Scene] = Field(min_length=1)

    @model_validator(mode="after")
    def _dates_and_their_zone(self) -> "Storyboard":
        """The browser's ``time_zone`` and ``locale``, both or neither, the zone
        one the zone database knows; and each ``date`` step's reading named
        once, never as a beat's ``record``, on a storyboard that pins both."""
        if (self.time_zone is None) != (self.locale is None):
            raise ValueError(
                "a storyboard pins the browser's time_zone and locale together,"
                " or neither"
            )
        if self.time_zone is not None:
            try:
                zoneinfo.ZoneInfo(self.time_zone)
            except (zoneinfo.ZoneInfoNotFoundError, ValueError):
                raise ValueError(
                    f"time_zone {self.time_zone!r} is not a zone the zone database knows"
                ) from None
        recorded = [name for s in self.scenes for b in s.beats for name in b.record]
        dated = [
            step.record
            for scene in self.scenes
            for beat in scene.beats
            for step in beat.steps
            if isinstance(step, Date)
        ]
        for name in dated:
            if name in recorded or dated.count(name) > 1:
                raise ValueError(f"the date step's reading {name!r} is named twice")
        if dated and self.time_zone is None:
            raise ValueError(
                "a storyboard with a date step pins the browser's time_zone and"
                " locale, so the take is the same on any machine"
            )
        return self

    @model_validator(mode="after")
    def _traffic_in_order(self) -> "Storyboard":
        moves = [s.traffic for s in self.scenes if s.traffic]
        if moves and self.traffic is None:
            raise ValueError("a scene moves traffic, but the storyboard has none")
        if self.traffic is not None and moves not in (["start", "finish"], ["run"]):
            raise ValueError(
                "traffic is started once and then finished once, or run once,"
                f" not {moves}"
            )
        self._visitors_in_order()
        seeded: List[str] = []
        for scene in self.scenes:
            for item in scene.off_camera:
                if not isinstance(item, SeedApp):
                    continue
                if item.seed in seeded:
                    raise ValueError(f"the {item.seed} seed runs twice")
                seeded.append(item.seed)
            if scene.app != "dashboard" and scene.app not in seeded:
                raise ValueError(
                    f"scene {scene.id} is recorded in {scene.app} before its seed"
                    " ran (off_camera: seed)"
                )
        if self.end_state.experiment is not None and self.traffic is None:
            raise ValueError("an experiment's end state needs the storyboard's traffic")
        recorded = [name for s in self.scenes for b in s.beats for name in b.record]
        made = self.end_state.experiment
        if made is not None:
            for name in (made.shown, made.planned, made.effect):
                if name not in recorded:
                    raise ValueError(
                        f"the experiment's end state compares {name!r}, which no beat"
                        " records"
                    )
        flag = self.end_state.flag
        if flag is not None:
            made = [s.name for s in self.setup.segments] if self.setup else []
            if flag.segment not in made:
                raise ValueError(
                    f"the flag's rule names {flag.segment!r}, which the setup does not make"
                )
            if flag.shown not in recorded:
                raise ValueError(
                    f"the flag's end state compares {flag.shown!r}, which no beat records"
                )
        design = self.end_state.design
        if design is not None:
            if self.traffic is not None:
                raise ValueError(
                    "a designed experiment is left a draft: its storyboard sends no"
                    " traffic"
                )
            for kind, name in design.shown.items():
                if name not in recorded:
                    raise ValueError(
                        f"the design's end state compares {kind} with {name!r},"
                        " which no beat records"
                    )
        return self

    @model_validator(mode="after")
    def _team_and_calculations(self) -> "Storyboard":
        """Gate 10/5's readings are recorded by some beat; each invitee is
        invited once; and a storyboard that invites anyone masks the invite's
        one-time password in some scene (C4, S6): the dialog always shows it."""
        recorded = [name for s in self.scenes for b in s.beats for name in b.record]
        for made in self.end_state.calculations:
            if made.shown not in recorded:
                raise ValueError(
                    f"the calculation's end state compares {made.shown!r}, which no"
                    " beat records"
                )
        team = self.end_state.team
        if team is None:
            return self
        if team.shown not in recorded:
            raise ValueError(
                f"the team's end state compares {team.shown!r}, which no beat records"
            )
        emails = [invitee.email for invitee in team.invitees]
        if len(set(emails)) != len(emails):
            raise ValueError(f"an invitee is listed twice: {emails}")
        if not any(config.INVITE_PASSWORD_TESTID in s.masks for s in self.scenes):
            raise ValueError(
                f"the video invites {len(emails)} teammates, and no scene masks"
                f" {config.INVITE_PASSWORD_TESTID}: each one-time password would be"
                " filmed (masks)"
            )
        return self

    @model_validator(mode="after")
    def _incident_in_order(self) -> "Storyboard":
        """Video 4's incident (S4, S5, S8a of plan v2.2), refused at load unless
        it is whole and in order: the StreamPulse seed, then ``story: 2``, then
        ``crashes``, then at least one scene (the last of them B9: a dashboard
        page, filmed before the monitor's next run), then ``wait_for_rollback``
        on a card that says the gap it hides (``{rollback_minutes}``), each
        once, with the rollback's end state for that flag and percentage, and
        every reading that end state names recorded where it is true."""
        found: Dict[str, int] = {}
        #: Each step's place, scene and then its place in the scene's list.
        placed: Dict[str, Tuple[int, int]] = {}
        seeded: Optional[Tuple[int, int]] = None
        for index, scene in enumerate(self.scenes):
            for position, item in enumerate(scene.off_camera):
                if isinstance(item, SeedApp) and item.seed == "streampulse":
                    seeded = (index, position)
                kind = INCIDENT_KINDS.get(type(item))
                if kind is None:
                    continue
                if kind in found:
                    raise ValueError(f"{kind} runs twice")
                found[kind] = index
                placed[kind] = (index, position)
        wait_at = found.get("wait_for_rollback")
        for index, scene in enumerate(self.scenes):
            if index != wait_at and ROLLBACK_MINUTES in " ".join(
                card_texts(scene.card)
            ):
                raise ValueError(
                    f"scene {scene.id} names {ROLLBACK_MINUTES}: only the card"
                    " right after wait_for_rollback says the gap the cut hides"
                )
        end = self.end_state.rollback
        if not found and end is None:
            return self
        if sorted(found) != sorted(INCIDENT_KINDS.values()) or end is None:
            raise ValueError(
                "the incident is story, crashes, wait_for_rollback and the"
                f" rollback's end state, each once; this storyboard has"
                f" {sorted(found) or 'none of them'}"
                f"{'' if end is not None else ' and no rollback end state'}"
            )
        if (
            seeded is None
            or not seeded < placed["story"]
            or not found["story"] < found["crashes"] < wait_at - 1
        ):
            raise ValueError(
                "off camera, the StreamPulse seed runs first, then story: 2, then"
                " crashes, then at least one scene before wait_for_rollback"
                " (B9, filmed before the monitor acts)"
            )
        b9 = self.scenes[wait_at - 1]
        if b9.app != "dashboard" or b9.card is not None:
            raise ValueError(
                f"scene {b9.id} is B9, the scene before wait_for_rollback: it is"
                " a dashboard page that shows the flag's safety check"
            )
        wait = next(
            item.wait_for_rollback
            for item in self.scenes[wait_at].off_camera
            if isinstance(item, WaitForRollback)
        )
        if wait.flag != config.CRASH_FLAG or end.key != config.CRASH_FLAG:
            raise ValueError(
                f"the crashes are sent against {config.CRASH_FLAG}: the wait and the"
                f" end state name it, not {wait.flag} and {end.key}"
            )
        if wait.percentage != end.percentage:
            raise ValueError(
                f"the wait is for {wait.percentage}%; the end state says"
                f" {end.percentage}%"
            )
        card = self.scenes[wait_at].card
        if card is None or ROLLBACK_MINUTES not in " ".join(card_texts(card)):
            raise ValueError(
                f"scene {self.scenes[wait_at].id} follows wait_for_rollback: it is"
                f" a card that says the measured gap the cut hides ({ROLLBACK_MINUTES})"
            )
        where: Dict[str, int] = {}
        for index, scene in enumerate(self.scenes):
            for beat in scene.beats:
                for name in beat.record:
                    where.setdefault(name, index)
        for name, after in (
            (end.unhealthy, None),
            (end.paused, wait_at),
            (end.rolled_back, wait_at),
            (end.shown, wait_at),
            (end.reason, wait_at),
        ):
            if name not in where:
                raise ValueError(
                    f"the rollback's end state compares {name!r}, which no beat records"
                )
            if after is None and where[name] != wait_at - 1:
                raise ValueError(
                    f"{name!r} is read in B9 ({b9.id}), before the monitor acts"
                )
            if after is not None and where[name] <= after:
                raise ValueError(
                    f"{name!r} is read after the rollback, not before"
                    f" {self.scenes[after].id}"
                )
        agree = end.agree
        scene = next((s for s in self.scenes if s.id == agree.scene), None)
        if scene is None or not 1 <= agree.beat <= len(scene.beats):
            raise ValueError(
                f"the agreement is read at scene {agree.scene} beat {agree.beat},"
                " which the storyboard does not have"
            )
        if self.scenes.index(scene) <= wait_at or scene.app != "streampulse":
            raise ValueError(
                f"the agreement is read in StreamPulse after the rollback, not in"
                f" {scene.id}"
            )
        recorded = scene.beats[agree.beat - 1].record
        if agree.panel not in recorded or agree.phone not in recorded:
            raise ValueError(
                f"scene {scene.id} beat {agree.beat} records the panel's"
                f" {agree.panel!r} and the phone's {agree.phone!r} at its start"
            )
        if agree.device not in config.STREAMPULSE_DEVICES:
            raise ValueError(
                f"{agree.device} is not a StreamPulse device the tool knows"
            )
        return self

    def _visitors_in_order(self) -> None:
        """T178 condition 5 and PE condition 3: the visitors an app is filmed as.

        An app with visitors declares one on its first scene (its init-script
        visitor, under whom it first loads) or on none; a later one switches,
        right after a scene of the same app; each id once. A storyboard that
        counts an experiment's users declares a visitor for every app it
        records that has visitors, and its shoppers are exactly those visitors.
        """
        first: Dict[str, str] = {}
        seen: List[str] = []
        previous: Optional[Scene] = None
        for scene in self.scenes:
            storage = scene.visitor
            if scene.app != "dashboard" and scene.app not in first:
                first[scene.app] = scene.id
                if storage is None and any(
                    s.app == scene.app and s.visitor is not None for s in self.scenes
                ):
                    raise ValueError(
                        f"scene {scene.id} is {scene.app}'s first, and a later one"
                        " sets a visitor: the first sets one too, or the app first"
                        " loads as a visitor nobody declared"
                    )
            elif storage is not None and (
                previous is None or previous.app != scene.app
            ):
                raise ValueError(
                    f"scene {scene.id} switches the {scene.app} visitor off camera,"
                    f" so it follows a {scene.app} scene (the page is on its origin)"
                )
            if storage is not None:
                ident = storage[config.APP_VISITOR_KEY[scene.app]]
                if ident in seen:
                    raise ValueError(f"the visitor {ident} is set twice")
                seen.append(ident)
            previous = scene
        made = self.end_state.experiment
        if made is None:
            return
        for app in self.apps:
            if app in config.APP_VISITOR_KEY and not any(
                s.app == app and s.visitor is not None for s in self.scenes
            ):
                raise ValueError(
                    f"the experiment's users are counted, and {app} would load as a"
                    " random visitor: declare its visitors (off_camera: visitor)"
                )
        shoppers = [shopper.visitor for shopper in made.on_camera_shoppers]
        if sorted(shoppers) != sorted(seen) or len(set(shoppers)) != len(shoppers):
            raise ValueError(
                f"the experiment's on-camera shoppers {shoppers} are not, each once,"
                f" the visitors the scenes set {seen}"
            )

    @property
    def apps(self) -> List[str]:
        """The demo apps the video records, in the order it first opens them."""
        found: List[str] = []
        for scene in self.scenes:
            if scene.app != "dashboard" and scene.app not in found:
                found.append(scene.app)
        return found


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

_PLAIN = re.compile(r"^[A-Za-z0-9 ,.:;'’!?%()/&+-]+$")
_STEP_KEYS = {
    "click": Click,
    "move": Move,
    "type": Type,
    "look": Look,
    "scroll_to": ScrollTo,
    "wait_for": WaitFor,
    "url": WaitFor,
    "choose": Choose,
    "slide": Slide,
    "date": Date,
}


def _steps(raw: Any, where: str) -> List[Any]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise StoryboardError(f"{where}: steps are a list")
    out = []
    for index, step in enumerate(raw):
        if not isinstance(step, dict) or not step:
            raise StoryboardError(f"{where} step {index + 1}: a step is a mapping")
        kinds = [key for key in step if key in _STEP_KEYS]
        if len(kinds) < 1:
            raise StoryboardError(
                f"{where} step {index + 1}: unknown step {sorted(step)}"
            )
        model = _STEP_KEYS[kinds[0]]
        try:
            out.append(model.model_validate(step, strict=False))
        except ValidationError as error:
            raise StoryboardError(f"{where} step {index + 1}: {error}") from None
    return out


def caption_problem(text: str) -> Optional[str]:
    """Why *text* cannot be a caption, or None."""
    if "{" in text or "}" in text:
        return "a caption is fixed text: no template"
    if not _PLAIN.match(text or ""):
        return "a caption is plain text (letters, digits and ordinary punctuation)"
    problems = contract.caption_problems(text)
    if problems:
        return "the caption " + "; ".join(problems)
    return None


def parse(data: Any, *, source: str = "storyboard") -> Storyboard:
    if not isinstance(data, dict):
        raise StoryboardError(f"{source}: the file is a mapping")
    data = json.loads(json.dumps(data))
    for s_index, scene in enumerate(data.get("scenes") or []):
        if not isinstance(scene, dict):
            raise StoryboardError(f"{source}: scene {s_index + 1} is a mapping")
        for b_index, beat in enumerate(scene.get("beats") or []):
            if not isinstance(beat, dict):
                raise StoryboardError(f"{source}: a beat is a mapping")
            where = f"{source} scene {scene.get('id')} beat {b_index + 1}"
            beat["before"] = _steps(beat.get("before"), where + " before")
            beat["steps"] = _steps(beat.get("steps"), where)
            beat["exit"] = _steps(beat.get("exit"), where + " exit")
    try:
        board = Storyboard.model_validate(data, strict=False)
    except ValidationError as error:
        raise StoryboardError(f"{source}: {error}") from None
    for scene in board.scenes:
        for index, beat in enumerate(scene.beats):
            where = f"{source} scene {scene.id} beat {index + 1}"
            problem = caption_problem(beat.cue)
            if problem:
                raise StoryboardError(f"{where}: {problem}")
            for step in [*beat.before, *beat.steps, *beat.exit]:
                _refuse_step(step, where)
            if any(isinstance(step, (Choose, Slide)) for step in beat.before):
                raise StoryboardError(
                    f"{where}: a choose or slide in before is cut from the video"
                )
            if any(isinstance(step, Date) for step in [*beat.before, *beat.exit]):
                raise StoryboardError(
                    f"{where}: a date is set in the beat's steps, on camera, never"
                    " in before or exit"
                )
    # G1 and G3, the narration's rules (the voice step runs them again).
    from showcase.capture import narration

    problems = narration.board_problems(board)
    if problems:
        raise StoryboardError(
            f"{source}: the narration is refused:\n"
            + "\n".join(f"- {p}" for p in problems)
        )
    return board


def _refuse_step(step: Any, where: str) -> None:
    if isinstance(step, Type):
        if step.text in config.STATIC_NEEDLES and not step.secret:
            raise StoryboardError(
                f"{where}: types a credential into a field not marked secret"
            )
        if len(step.text) > 45:
            raise StoryboardError(f"{where}: types more than 45 characters")
    for locator in _locators(step):
        named = " ".join(
            str(v)
            for v in (locator.name, locator.text, locator.css, locator.testid)
            if v
        ).lower()
        if "api key" in named or "api-keys" in named or "api_keys" in named:
            raise StoryboardError(f"{where}: no step opens the API keys page")
    if isinstance(step, WaitFor) and step.url and "api-keys" in step.url:
        raise StoryboardError(f"{where}: no step opens the API keys page")


def _locators(step: Any) -> List[Locator]:
    for key in (
        "click",
        "move",
        "type",
        "look",
        "scroll_to",
        "wait_for",
        "choose",
        "slide",
        "date",
    ):
        value = getattr(step, key, None)
        if isinstance(value, Locator):
            return [value]
    return []


def quoted_lines(page: str, code: Code) -> List[str]:
    """Why *code* is not quoted from *page* (a Markdown page's text), or nothing.

    The lines must come from the first fenced block under the heading
    ``code.section``, each the page's own line, in the page's order (a blank
    line in the card stands for any lines left out).
    """
    lines = page.splitlines()
    heading = re.compile(r"^#{1,6}\s+" + re.escape(code.section) + r"\s*$")
    start = next((i for i, line in enumerate(lines) if heading.match(line)), None)
    if start is None:
        return [f"{code.doc} has no heading {code.section!r}"]
    block: List[str] = []
    inside = False
    for line in lines[start + 1 :]:
        if line.startswith("#") and not inside:
            break
        if line.startswith("```"):
            if inside:
                break
            inside = True
            continue
        if inside:
            block.append(line)
    if not block:
        return [f"{code.doc} has no code block under {code.section!r}"]
    problems = []
    at = 0
    for line in code.lines:
        if not line:
            continue
        try:
            at = block.index(line, at) + 1
        except ValueError:
            problems.append(
                f"{code.doc} ({code.section}) has no line {line!r} after the one before it"
            )
    return problems


def required_departures(board: Storyboard) -> List[Tuple[str, str]]:
    """``(scene id, kind)`` for every departure the tool itself makes, which
    the scene must state on screen (D84):

    * ``off-camera setup``: a scene with ``off_camera`` work before it; the
      first scene of a storyboard with a ``setup``; the scene the director
      signs in before, off camera (the first ``signed_in`` one); a scene
      whose results are loaded off camera first (``warm_results``); a scene
      with a beat whose ``before`` types, chooses or slides (``ENTERS_DATA``;
      a cut: what is entered there is entered off camera, as video 6's second
      shopper's form). A click in a cut is not counted: video 2 opens its
      Live tab that way, the cursor on the tab before the cut;
    * ``simulated traffic``: every scene from the one that starts the
      simulated users to the one that finishes them, both included (one that
      runs them, start to finish, is both);
    * ``time skip``: the scene that finishes them, after the minutes they took
      off camera (its card says how many, measured: ``{traffic_minutes}``);
    * ``masked value``: a scene that masks a credential the product draws
      (``masks``; S7 of plan v2.2): the tool replaces what the page draws.

    Off camera (``off_camera``, S7 of plan v2.2), each step by what it is: an
    app's seed, a visitor and ``story`` (the demo's rollout script, run by the
    tool) are off-camera setup; ``crashes`` is simulated traffic, the devices'
    crashes the tool sends; ``wait_for_rollback`` is a time skip, on the card it
    runs before, which says how long the cut hides. A shortened scheduler
    interval is stated as its override's statement (``overrides``).
    """
    needed: List[Tuple[str, str]] = []
    running = False
    signed_in = False
    for index, scene in enumerate(board.scenes):
        signs_in = scene.signed_in and not signed_in
        signed_in = signed_in or scene.signed_in
        enters_data_in_a_cut = any(
            isinstance(step, ENTERS_DATA)
            for beat in scene.beats
            for step in beat.before
        )
        steps = [OFF_CAMERA_KINDS[type(item)] for item in scene.off_camera]
        if (
            "off-camera setup" in steps
            or (index == 0 and board.setup is not None)
            or signs_in
            or scene.warm_results
            or enters_data_in_a_cut
        ):
            needed.append((scene.id, "off-camera setup"))
        if scene.traffic in ("start", "run"):
            running = True
        if running or "simulated traffic" in steps:
            needed.append((scene.id, "simulated traffic"))
        if scene.traffic in ("finish", "run") or "time skip" in steps:
            needed.append((scene.id, "time skip"))
        if scene.traffic in ("finish", "run"):
            running = False
        if scene.masks:
            needed.append((scene.id, "masked value"))
    return needed


#: What each off-camera step is, as a departure from a new user's path (D84;
#: S7 of plan v2.2). A closed table: a step with no row here cannot load.
OFF_CAMERA_KINDS: Dict[type, str] = {
    SeedApp: "off-camera setup",
    Visitor: "off-camera setup",
    Story: "off-camera setup",
    Crashes: "simulated traffic",
    WaitForRollback: "time skip",
}


def incident_at(board: Storyboard) -> Dict[str, int]:
    """Where video 4's incident steps run: each one's scene index, by name."""
    found: Dict[str, int] = {}
    for index, scene in enumerate(board.scenes):
        for item in scene.off_camera:
            kind = INCIDENT_KINDS.get(type(item))
            if kind is not None:
                found[kind] = index
    return found


def b9_scene(board: Storyboard) -> Optional[Scene]:
    """B9: the scene right before ``wait_for_rollback``'s, or None."""
    wait_at = incident_at(board).get("wait_for_rollback")
    return None if wait_at is None else board.scenes[wait_at - 1]


#: EM condition 6 of T166 (S2 of plan v2.2): a storyboard that records a demo
#: app says so, word for word, in a caption or a card of one of its scenes.
OWN_BUILD = "The demo apps run from their own build in this recording"


def own_build_problems(board: Storyboard) -> List[str]:
    """S2: the own-build sentence, drawn in some scene of a storyboard with app
    scenes; nothing for one without (videos 1-3)."""
    if not board.apps:
        return []
    if any(OWN_BUILD in text for s in board.scenes for text in scene_screen_text(s)):
        return []
    return [
        f"the demo apps are recorded, and no scene draws {OWN_BUILD!r} (EM C6 of T166)"
    ]


def own_build_scene(board: Storyboard) -> Optional[str]:
    """The first scene that draws the own-build sentence, or None."""
    for scene in board.scenes:
        if any(OWN_BUILD in text for text in scene_screen_text(scene)):
            return scene.id
    return None


def scene_screen_text(scene: Scene) -> List[str]:
    """What a scene draws that a storyboard controls: its captions and its card."""
    texts = [beat.cue for beat in scene.beats]
    if scene.card is not None:
        texts += [t for t in scene.card.drawn_text() if t]
    return texts


def departure_problems(board: Storyboard) -> List[str]:
    """D84: each departure the tool makes is declared on its scene, every
    declared departure's words are on screen in that scene, and a storyboard
    with app scenes says the apps are the tool's own build (S2). Empty when none."""
    problems: List[str] = own_build_problems(board)
    by_id = {scene.id: scene for scene in board.scenes}
    for scene_id, kind in required_departures(board):
        if not any(d.kind == kind for d in by_id[scene_id].departures):
            problems.append(
                f"scene {scene_id}: {kind} is not stated on screen (departures)"
            )
    for scene in board.scenes:
        shown = scene_screen_text(scene)
        for departure in scene.departures:
            if not any(departure.says in text for text in shown):
                problems.append(
                    f"scene {scene.id}: the {departure.kind} statement"
                    f" {departure.says!r} is not in a caption or the card"
                )
    return problems


def load(path: Path) -> Storyboard:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise StoryboardError(f"{path.name}: {error}") from None
    board = parse(data, source=path.name)
    if path.stem != board.slug:
        raise StoryboardError(
            f"{path.name}: slug {board.slug!r} is not the file's name"
        )
    return board


# ---------------------------------------------------------------------------
# Pace and the cue list
# ---------------------------------------------------------------------------


def min_cue_ms(text: str) -> int:
    """How long the capture holds *text*: the contract's reading time, plus a margin
    so that rounding the cue's ends to frames never takes it under."""
    return contract.reading_time_ms(text) + config.CUE_MARGIN_MS


def compile_cues(board: Storyboard) -> List[Dict[str, Any]]:
    """The cue list in order: scene, beat, text, focus, minimum hold. Pure (gate 13a)."""
    cues = []
    for scene in board.scenes:
        for index, beat in enumerate(scene.beats):
            cues.append(
                {
                    "scene": scene.id,
                    "beat": index + 1,
                    "text": beat.cue,
                    "focus": beat.focus.describe(),
                    "min_ms": min_cue_ms(beat.cue)
                    + (config.PAYOFF_MS if beat.payoff else 0),
                    "payoff": beat.payoff,
                }
            )
    return cues


#: Off-camera work a step's estimate leaves out: the needle and event checks
#: after it, and the browser's own pace (measured on video 1: about 30 ms).
STEP_OVERHEAD_MS = 30
#: Between two scenes: the screencast stopping and starting again.
SCENE_GAP_MS = 35


def step_ms(step: Any) -> int:
    """How long a step keeps the camera rolling, by the pace constants."""
    if isinstance(step, Click):
        ms = config.POINTER_MS + config.POINTER_REST_MS + config.CLICK_RING_MS
    elif isinstance(step, Move):
        ms = config.POINTER_MS
    elif isinstance(step, Type):
        ms = (
            config.POINTER_MS
            + len(step.text) * config.TYPE_DELAY_MS
            + config.FIELD_PAUSE_MS
        )
    elif isinstance(step, Look):
        ms = step.ms
    elif isinstance(step, ScrollTo):
        ms = config.SCROLL_MS
    elif isinstance(step, (Choose, Date)):
        # A date is filled as a choose is chosen: pressed, set, held to settle.
        ms = (
            config.POINTER_MS
            + config.POINTER_REST_MS
            + config.CLICK_RING_MS
            + config.SETTLE_MS
        )
    elif isinstance(step, Slide):
        ms = (
            config.POINTER_MS
            + config.POINTER_REST_MS
            + config.SLIDE_MS
            + config.SETTLE_MS
        )
    else:  # a wait on camera: the element is there, then it paints
        ms = config.PAINT_MS
    return ms + STEP_OVERHEAD_MS


def estimate_ms(board: Storyboard) -> int:
    """The recording's length before it is made: each cue held for the longer of
    its reading time and its steps, then its payoff and its exit, then the gap.

    Waits in ``before`` are cut, and scrolling an element into view is not
    counted, so a real recording runs a little longer (video 1: 54.7 s
    recorded against an estimate within 1% of it).
    """
    total = 0
    for scene in board.scenes:
        total += SCENE_GAP_MS
        for beat in scene.beats:
            held = max(min_cue_ms(beat.cue), sum(step_ms(s) for s in beat.steps))
            total += held + (config.PAYOFF_MS if beat.payoff else 0)
            total += sum(step_ms(s) for s in beat.exit) + config.CUE_GAP_MS
    return total


def narration_lines(board: Storyboard) -> List[Tuple[str, int, int, str]]:
    """Every narration sentence in order: ``(scene id, beat, line, text)``,
    beats and lines counted from 1. Empty for a storyboard without narration."""
    return [
        (scene.id, number, line, sentence)
        for scene in board.scenes
        for number, beat in enumerate(scene.beats, start=1)
        for line, sentence in enumerate(beat.say, start=1)
    ]


def voiced_estimate_ms(
    board: Storyboard, samples: Mapping[Tuple[str, int, int], int]
) -> int:
    """``estimate_ms`` for a narrated storyboard, from each sentence's clip.

    *samples* gives each ``narration_lines`` entry's clip length in samples
    (``contract.VOICE_RATE``). A beat is held for the longest of its headline's
    reading time, its steps, and its sentences' slots
    (``contract.sentence_slot_ms``) plus the cue margin; the narration may run
    on over the beat's exit, so the exit's steps count towards it, except on a
    payoff beat, whose narration ends before its silent hold.
    """
    total = 0
    for scene in board.scenes:
        total += SCENE_GAP_MS
        for number, beat in enumerate(scene.beats, start=1):
            spoken = sum(
                contract.sentence_slot_ms(sentence, samples[(scene.id, number, line)])
                for line, sentence in enumerate(beat.say, start=1)
            )
            steps = sum(step_ms(s) for s in beat.steps)
            exit_ms = sum(step_ms(s) for s in beat.exit)
            overlap = 0 if beat.payoff else exit_ms
            held = max(
                min_cue_ms(beat.cue), steps, spoken + config.CUE_MARGIN_MS - overlap
            )
            total += held + (config.PAYOFF_MS if beat.payoff else 0)
            total += exit_ms + config.CUE_GAP_MS
    return total


def numbers_in(text: str) -> List[str]:
    """The numbers a text states, as written: ``31,234``, ``12.1%``, ``95%``."""
    return re.findall(r"(?<![\w.])[0-9](?:[0-9,]*[0-9])?(?:\.[0-9]+)?%?", text)


def cue_list_bytes(board: Storyboard) -> bytes:
    return json.dumps(compile_cues(board), sort_keys=True, ensure_ascii=False).encode()
