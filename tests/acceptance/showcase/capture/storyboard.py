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
  state (gate 10) to compare with the API.

A scene opens a page (``open``), an experiment's results page by the
experiment's name (``results_of``, its id looked up off camera), or draws a
card: one documented command, lines quoted from a page of the docs (``code``,
checked against the recorded tree), or one headline. ``traffic`` starts or
finishes the storyboard's simulated users off camera before the scene.

Refused at load (``StoryboardError``), before any stack starts: an unknown key,
a cue with no focus, a caption ``contract.caption_problems`` refuses, a caption
holding a template (``{``) or anything but plain text, a ``type`` of a
credential into a step not marked secret, an on-camera step that opens the API
keys page, a locator naming nothing, a scene with no beats, a code line that
names a host and port, and traffic finished before it starts. Caption text
never comes from the page or the run: ``compile_cues`` is a pure function of
the file, so the cue list is the same on every run (gate 13a).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Union

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


Step = Union[Click, Move, Type, Look, ScrollTo, WaitFor]


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
    command: Optional[str] = None
    code: Optional[Code] = None
    headline: Optional[str] = Field(default=None, max_length=40)
    note: str

    @model_validator(mode="after")
    def _one_body(self) -> "Card":
        bodies = [b for b in (self.command, self.code, self.headline) if b]
        if len(bodies) != 1:
            raise ValueError("a card shows exactly one of command, code or headline")
        return self


class Scene(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,40}$")
    open: Optional[str] = Field(default=None, pattern=r"^/[A-Za-z0-9/_?=&.-]*$")
    #: Open this experiment's results page; the id is looked up off camera.
    results_of: Optional[str] = None
    card: Optional[Card] = None
    #: Sign in as the demo admin off camera before the scene.
    signed_in: bool = False
    #: Results to load off camera first, so no skeleton is filmed (by experiment name).
    warm_results: List[str] = Field(default_factory=list)
    #: Off camera, before the scene: start the storyboard's simulated users
    #: (and wait for the first of them), or wait for the last.
    traffic: Optional[Literal["start", "finish"]] = None
    beats: List[Beat] = Field(min_length=1)

    @model_validator(mode="after")
    def _page_or_card(self) -> "Scene":
        if sum(x is not None for x in (self.open, self.results_of, self.card)) != 1:
            raise ValueError(
                "a scene opens a page, opens an experiment's results or draws a card:"
                " exactly one, not both"
            )
        return self


class SeedCounts(_Strict):
    experiments: int
    feature_flags: int
    users: int


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


class EndState(_Strict):
    """Gate 10: what is true after the last scene, checked through the API."""

    signed_in_as: str
    #: The experiments page's rows the video showed == the API's experiments.
    experiments_listed: Optional[int] = None
    experiment: Optional[ExperimentEnd] = None

    @model_validator(mode="after")
    def _something(self) -> "EndState":
        if self.experiments_listed is None and self.experiment is None:
            raise ValueError("an end state names experiments_listed or an experiment")
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
    scenes: List[Scene] = Field(min_length=1)

    @model_validator(mode="after")
    def _traffic_in_order(self) -> "Storyboard":
        moves = [s.traffic for s in self.scenes if s.traffic]
        if moves and self.traffic is None:
            raise ValueError("a scene moves traffic, but the storyboard has none")
        if self.traffic is not None and moves != ["start", "finish"]:
            raise ValueError(
                f"traffic is started once and then finished once, not {moves}"
            )
        if self.end_state.experiment is not None and self.traffic is None:
            raise ValueError("an experiment's end state needs the storyboard's traffic")
        return self


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
    for key in ("click", "move", "type", "look", "scroll_to", "wait_for"):
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


def numbers_in(text: str) -> List[str]:
    """The numbers a text states, as written: ``31,234``, ``12.1%``, ``95%``."""
    return re.findall(r"(?<![\w.])[0-9](?:[0-9,]*[0-9])?(?:\.[0-9]+)?%?", text)


def cue_list_bytes(board: Storyboard) -> bytes:
    return json.dumps(compile_cues(board), sort_keys=True, ensure_ascii=False).encode()
