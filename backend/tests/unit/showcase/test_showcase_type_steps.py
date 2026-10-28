"""A ``type`` step replaces a value by selecting it, and every field reads what
was typed into it (video 9's share boxes, #1066).

* ``select: true`` selects the field's text and types over it, as a person
  replaces a value. A number box emptied first (``clear: true``) can draw
  "034" while 34 is saved: React keeps the "0" it wrote into the emptied box
  and the keys land after it. ``select`` is refused with ``clear`` (one way to
  replace a value) and with ``secret`` (a password field starts empty).
* Gate 12, typed as shown: after its keys, every ``type`` step reads its field
  back, and the take refuses unless it reads the typed text (replaced) or the
  value before the step followed by it. Before this check a frame showing
  "033" passed every gate.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
import yaml
from showcase.capture import config, director, gates, guard, storyboard

pytestmark = [pytest.mark.unit]

SHOWCASE = Path(__file__).resolve().parents[4] / "tests" / "acceptance" / "showcase"
DRAWN_JS = "the stand-in's page text and fields"
TYPE_KIND_JS = "el => String(el.type || '').toLowerCase()"


# --- Loading ------------------------------------------------------------------------------


def boards():
    for slug, relative in {**config.VIDEOS, **config.PROBES}.items():
        yield slug, SHOWCASE / relative


def type_steps(board):
    for scene in board.scenes:
        for beat in scene.beats:
            for step in [*beat.before, *beat.steps, *beat.exit]:
                if isinstance(step, storyboard.Type):
                    yield step


@pytest.mark.parametrize("slug, path", list(boards()), ids=[s for s, _ in boards()])
def test_every_board_still_loads_and_none_selects(slug, path):
    board = storyboard.load(path)
    steps = list(type_steps(board))
    assert all(step.select is False for step in steps), slug


def board_with(slug, change):
    """The registered board *slug* as data, with *change* made to the first type
    step it picks (``change`` returns True on the step it changed)."""
    data = yaml.safe_load((SHOWCASE / config.VIDEOS[slug]).read_text(encoding="utf-8"))
    for scene in data["scenes"]:
        for beat in scene["beats"]:
            for key in ("before", "steps", "exit"):
                for step in beat.get(key) or []:
                    if "type" in step and change(step):
                        return data
    raise AssertionError(f"{slug} has no type step to change")


def select_too(step):
    if not step.get("clear"):
        return False
    step["select"] = True
    return True


def select_a_secret(step):
    if not step.get("secret"):
        return False
    step["select"] = True
    return True


def test_select_with_clear_is_refused_at_load_naming_the_step():
    data = board_with("02-first-experiment", select_too)
    with pytest.raises(storyboard.StoryboardError, match=r"beat \d+ step \d+: ") as e:
        storyboard.parse(data, source="02")
    assert "select or clear, not both" in str(e.value)


def test_select_on_a_secret_field_is_refused_at_load_naming_the_step():
    data = board_with("01-getting-started", select_a_secret)
    with pytest.raises(storyboard.StoryboardError, match=r"beat \d+ step \d+: ") as e:
        storyboard.parse(data, source="01")
    assert "a secret field starts empty" in str(e.value)


def test_select_alone_loads():
    def select_instead(step):
        if not step.get("clear"):
            return False
        del step["clear"]
        step["select"] = True
        return True

    board = storyboard.parse(board_with("02-first-experiment", select_instead))
    assert any(step.select and not step.clear for step in type_steps(board))


# --- The judge ----------------------------------------------------------------------------


def judge(text, before, after, *, replaced=False, secret=False, kept=()):
    return gates.typed_as_shown(
        text, before, after, replaced=replaced, secret=secret, kept=kept
    )


@pytest.mark.parametrize(
    "text, before, after, replaced",
    [
        ("34", "50", "34", True),  # selected (or emptied) and typed over
        ("Delivery promise", "", "Delivery promise", False),  # an empty field
        ("def", "abc", "abcdef", False),  # appended to a value, caret at the end
    ],
)
def test_a_field_that_reads_what_was_typed_passes(text, before, after, replaced):
    ok, numbers, why = judge(text, before, after, replaced=replaced)
    assert ok and why == ""
    assert numbers["after"] == after and numbers["values_shown"] is True


@pytest.mark.parametrize(
    "text, before, after, replaced, words",
    [
        (
            "33",
            "50",
            "033",
            True,
            "reads '033' after '33' was typed; it should read '33'",
        ),
        ("34", "50", "5034", True, "reads '5034' after '34' was typed"),
        ("34", "5", "534", True, "reads '534' after '34' was typed"),
        ("def", "abc", "adefbc", False, "it should read 'abcdef'"),
    ],
)
def test_a_field_that_reads_anything_else_is_refused(
    text, before, after, replaced, words
):
    ok, numbers, why = judge(text, before, after, replaced=replaced)
    assert not ok and words in why
    assert numbers["after_chars"] == len(after)


def test_a_secret_step_shows_lengths_never_values():
    ok, numbers, why = judge("pass-word-1", "", "pass-word-", secret=True)
    assert not ok
    assert why == (
        "the field reads 10 characters after 11 were typed; it should read 11"
        " (values not shown)"
    )
    assert not {"typed", "before", "after"} & set(numbers)
    assert numbers["values_shown"] is False


def test_a_value_holding_a_kept_value_is_not_printed():
    kept = config.STATIC_NEEDLES[0]
    ok, numbers, why = judge("x", "", kept + "y", kept=config.STATIC_NEEDLES)
    # Judged as booleans first, so a failure here never prints the value.
    printed = kept in why or any(kept in str(v) for v in numbers.values())
    said_hidden = "values not shown" in why
    assert not printed, "a kept value was printed in the refusal or its numbers"
    assert not ok and said_hidden


# --- Through the director, on a stand-in page ------------------------------------------------


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
    def __init__(self):
        self.clicks = []

    def move(self, x, y):
        pass

    def click(self, x, y):
        self.clicks.append((x, y))


class Field:
    """A text or number box. Emptied by ``fill("")``, a number box redraws "0"
    and keeps it (React's number-input rule with ``Number(value)``), so keys
    typed next land after it; a selection is replaced by the next keys."""

    box = {"x": 400.0, "y": 300.0, "width": 120.0, "height": 32.0}

    def __init__(self, value="", *, kind="text"):
        self.value = value
        self.kind = kind
        self.selected = False
        self.calls = []

    def wait_for(self, state, timeout):
        pass

    def bounding_box(self):
        return dict(self.box)

    def nth(self, index):
        return self

    def evaluate(self, script, *args):
        if script == TYPE_KIND_JS:
            return self.kind
        raise AssertionError(f"an unexpected element script: {script[:40]}")

    def input_value(self):
        self.calls.append("read")
        return self.value

    def fill(self, text):
        self.calls.append(f"fill {text!r}")
        self.value = "0" if (self.kind == "number" and text == "") else text

    def select_text(self):
        self.calls.append("select")
        self.selected = True

    def press_sequentially(self, text, delay):
        self.calls.append(f"keys {text!r}")
        self.value = text if self.selected else self.value + text
        self.selected = False


class StandIn:
    def __init__(self, clock, elements):
        self.clock = clock
        self.elements = elements
        self.mouse = Mouse()
        self.screencast = Screencast()

    def wait_for_timeout(self, ms):
        self.clock.ms += ms

    def get_by_test_id(self, testid):
        return self.elements[testid]

    def evaluate(self, script, *args):
        if script == DRAWN_JS:
            return "", []
        raise AssertionError(f"an unexpected page script: {script[:40]}")


@pytest.fixture
def stage(tmp_path, monkeypatch):
    """A director on a stand-in page and clock, and a function to give it fields."""
    clock = Clock()
    monkeypatch.setattr(director, "_now_ms", clock.now)
    stand_in = types.ModuleType("docs_runner.execute")
    stand_in.DRAWN_JS = DRAWN_JS
    monkeypatch.setitem(sys.modules, "docs_runner.execute", stand_in)

    def make(elements):
        page = StandIn(clock, elements)
        made = director.Director(
            context=object(),
            page=page,
            frames=tmp_path / "work" / "video" / "capture" / "frames",
            dashboard="http://127.0.0.1:28401",
            api_port=28400,
            dashboard_port=28401,
            redactor=director.redaction.Redactor(),
            forbidden=(),
            work=guard.WorkRoot(tmp_path / "work"),
        )
        made.recorder.t0 = clock.ms  # recording
        made.where = "scene design beat 5"
        return made, page

    return make


def share(testid, text, **flags):
    return storyboard.Type(type=storyboard.Locator(testid=testid), text=text, **flags)


@pytest.mark.regression
def test_a_share_box_drawing_033_after_33_is_refused(stage):
    """QA R2: an emptied number box draws "033" for 33 while 33 is saved, and
    before gate 12 read the field back that frame passed every gate."""
    box = Field("50", kind="number")
    made, _page = stage({"variant-allocation-1": box})
    with pytest.raises(
        director.CaptureFailed,
        match=(
            r"^gate 12: scene design beat 5: testid 'variant-allocation-1': the field"
            r" reads '033' after '33' was typed; it should read '33'$"
        ),
    ):
        made.step(share("variant-allocation-1", "33", clear=True))
    assert box.value == "033"
    (checked,) = made.tally.typed
    assert (
        checked["ok"] is False and checked["field"] == "testid 'variant-allocation-1'"
    )


def test_select_types_over_the_value_and_never_empties_the_box(stage):
    box = Field("50", kind="number")
    made, _page = stage({"variant-allocation-0": box})
    made.step(share("variant-allocation-0", "34", select=True))
    assert box.calls == ["read", "select", "keys '34'", "read"]
    assert box.value == "34"
    (checked,) = made.tally.typed
    assert checked == {
        "where": "scene design beat 5",
        "field": "testid 'variant-allocation-0'",
        "ok": True,
        "replaced": True,
        "typed_chars": 2,
        "after_chars": 2,
    }


def test_clear_still_empties_a_text_field_and_selects_nothing(stage):
    name = Field("Treatment")
    made, _page = stage({"variant-name-1": name})
    made.step(share("variant-name-1", "One page", clear=True))
    assert name.calls == ["read", "fill ''", "keys 'One page'", "read"]
    assert name.value == "One page" and made.tally.typed[0]["ok"]


def test_typing_into_an_empty_field_appends(stage):
    name = Field("")
    made, _page = stage({"experiment-name": name})
    made.step(share("experiment-name", "Delivery promise"))
    assert name.calls == ["read", "keys 'Delivery promise'", "read"]
    assert made.tally.typed[0]["ok"]


def test_a_secret_field_read_back_wrong_is_refused_without_its_value(stage):
    class Dropping(Field):
        def press_sequentially(self, text, delay):
            super().press_sequentially(text[:-1], delay)

    field = Dropping("", kind="password")
    made, _page = stage({"password": field})
    with pytest.raises(director.CaptureFailed, match="values not shown") as e:
        made.step(share("password", "pass-word-1", secret=True))
    assert "pass-word" not in str(e.value)
