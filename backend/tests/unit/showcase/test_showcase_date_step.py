"""The storyboard's ``date`` step and a board's pinned ``time_zone`` and
``locale`` (video 9's start date, D182, EM-5 of the video 9 re-plan).

* What the loader refuses: ``days_ahead`` outside 1 to 7, ``at`` outside 06:00
  to 21:59, a date in a beat's ``before`` or ``exit``, a reading named twice,
  a date on a board that pins no zone, and a zone or a locale half declared or
  unknown.
* Gate 12's judgement of a filled date (``gates.date_as_set``): the page's own
  value at the declared time, the field reading it back, and the page reading
  that local time back as itself (a time a daylight saving change skips
  refuses).
* The capture's browser context: a board that pins no zone gets exactly the
  arguments it always had; video 9 adds its zone and its locale.

The browser half (the page computing the value, the fill, the read back) runs
only in a take. No browser, Docker, network or database.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, Callable, Dict

import pytest
import yaml
from showcase.capture import config, gates, run, storyboard

pytestmark = [pytest.mark.unit]

SHOWCASE = Path(__file__).resolve().parents[4] / "tests" / "acceptance" / "showcase"
V9 = SHOWCASE / "storyboards" / "09-design-in-depth.yaml"
#: Video 9's schedule beat, counted from 1.
SCHEDULE = 13


def raw() -> Dict[str, Any]:
    return yaml.safe_load(V9.read_text(encoding="utf-8"))


def beat(data: Dict[str, Any], number: int) -> Dict[str, Any]:
    return data["scenes"][0]["beats"][number - 1]


def date_step(data: Dict[str, Any]) -> Dict[str, Any]:
    return next(step for step in beat(data, SCHEDULE)["steps"] if "date" in step)


def refused(change: Callable[[Dict[str, Any]], Any], words: str) -> str:
    data = raw()
    change(data)
    with pytest.raises(storyboard.StoryboardError) as raised:
        storyboard.parse(copy.deepcopy(data), source="09-design-in-depth.yaml")
    assert re.search(words, str(raised.value)), str(raised.value)
    return str(raised.value)


# ---------------------------------------------------------------------------
# The real board
# ---------------------------------------------------------------------------


def test_video_9_sets_its_start_two_days_ahead_at_9_in_los_angeles():
    made = storyboard.load(V9)
    assert (made.time_zone, made.locale) == ("America/Los_Angeles", "en-US")
    dates = [
        (number, step)
        for number, b in enumerate(made.scenes[0].beats, start=1)
        for step in b.steps
        if isinstance(step, storyboard.Date)
    ]
    assert len(dates) == 1
    number, step = dates[0]
    assert number == SCHEDULE
    assert (step.date.testid, step.days_ahead, step.at, step.record) == (
        "schedule-start",
        2,
        "09:00",
        "scheduled-start",
    )
    assert not any(
        isinstance(s, storyboard.Date)
        for b in made.scenes[0].beats
        for s in [*b.before, *b.exit]
    )


def test_only_video_9_pins_a_zone():
    pinned = {
        slug: (board.time_zone, board.locale)
        for slug, relative in config.VIDEOS.items()
        for board in [storyboard.load(SHOWCASE / relative)]
        if board.time_zone is not None or board.locale is not None
    }
    assert pinned == {"09-design-in-depth": ("America/Los_Angeles", "en-US")}


def test_a_date_is_estimated_as_a_choose_and_enters_data():
    step = storyboard.Date.model_validate(
        {"date": {"testid": "x"}, "days_ahead": 2, "at": "09:00", "record": "r"}
    )
    choose = storyboard.Choose.model_validate(
        {"choose": {"testid": "x"}, "option": "o"}
    )
    assert storyboard.step_ms(step) == storyboard.step_ms(choose)
    assert storyboard.Date in storyboard.ENTERS_DATA


# ---------------------------------------------------------------------------
# What the loader refuses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("days", [0, 8, -1])
def test_days_ahead_outside_1_to_7_is_refused(days):
    refused(lambda d: date_step(d).update(days_ahead=days), r"days_ahead")


@pytest.mark.parametrize("at", ["05:59", "22:00", "23:30", "00:00", "9:00", "09:60"])
def test_a_time_outside_6_to_22_or_not_hh_mm_is_refused(at):
    refused(lambda d: date_step(d).update(at=at), r"\bat\b")


@pytest.mark.parametrize("at", ["06:00", "21:59", "12:30"])
def test_a_time_from_6_to_21_59_loads(at):
    data = raw()
    date_step(data).update(at=at)
    made = storyboard.parse(data)
    step = next(
        s
        for s in made.scenes[0].beats[SCHEDULE - 1].steps
        if isinstance(s, storyboard.Date)
    )
    assert step.at == at


@pytest.mark.parametrize("part", ["before", "exit"])
def test_a_date_outside_the_beats_steps_is_refused(part):
    def moved(data):
        b = beat(data, SCHEDULE)
        step = date_step(data)
        b["steps"].remove(step)
        b.setdefault(part, []).insert(0, step)

    refused(moved, rf"beat {SCHEDULE}: a date is set in the beat's steps, on camera")


def test_a_date_reading_named_twice_is_refused():
    def two_dates(data):
        b = beat(data, SCHEDULE)
        b["steps"].insert(2, copy.deepcopy(date_step(data)))

    refused(two_dates, r"the date step's reading 'scheduled-start' is named twice")

    def as_a_record(data):
        date_step(data).update(record="analysis")

    refused(as_a_record, r"the date step's reading 'analysis' is named twice")


def test_a_date_on_a_board_that_pins_no_zone_is_refused():
    def unpinned(data):
        del data["time_zone"]
        del data["locale"]

    refused(unpinned, r"a storyboard with a date step pins the browser's time_zone")


@pytest.mark.parametrize(
    "change, words",
    [
        (lambda d: d.pop("locale"), r"time_zone and locale together"),
        (lambda d: d.pop("time_zone"), r"time_zone and locale together"),
        (
            lambda d: d.update(time_zone="Mars/Olympus_Mons"),
            r"'Mars/Olympus_Mons' is not a zone the zone database knows",
        ),
        (
            lambda d: d.update(time_zone="../../etc/passwd"),
            r"is not a zone the zone database knows",
        ),
        (lambda d: d.update(locale="english"), r"locale"),
    ],
    ids=["no-locale", "no-zone", "unknown-zone", "a-path", "not-a-locale"],
)
def test_a_zone_or_locale_half_declared_or_unknown_is_refused(change, words):
    refused(change, words)


# ---------------------------------------------------------------------------
# Gate 12's judgement of a filled date
# ---------------------------------------------------------------------------


def test_a_date_filled_and_read_back_as_set_passes():
    ok, numbers, why = gates.date_as_set(
        "09:00", "2026-11-03T09:00", "2026-11-03T09:00", "2026-11-03T09:00"
    )
    assert ok, why
    assert numbers == {
        "at": "09:00",
        "value": "2026-11-03T09:00",
        "read": "2026-11-03T09:00",
        "read_back": "2026-11-03T09:00",
    }


@pytest.mark.parametrize(
    "at, value, read, back, words",
    [
        (
            "09:00",
            "2026-11-03T09:00",
            "",
            "2026-11-03T09:00",
            "the field reads '' after '2026-11-03T09:00' was filled",
        ),
        (
            "09:00",
            "2026-11-03T09:00",
            "2026-11-03T09:30",
            "2026-11-03T09:30",
            "the field reads '2026-11-03T09:30'",
        ),
        (
            "02:30",
            "2027-03-14T02:30",
            "2027-03-14T02:30",
            "2027-03-14T03:30",
            "back as '2027-03-14T03:30': that local time does not exist",
        ),
        (
            "09:00",
            "2026-11-03T10:00",
            "2026-11-03T10:00",
            "2026-11-03T10:00",
            "the page computed '2026-11-03T10:00', which is not a day at 09:00",
        ),
        ("09:00", "", "", "", "the page computed '', which is not a day at 09:00"),
    ],
    ids=["not-filled", "another-time", "a-skipped-hour", "computed-off", "nothing"],
)
def test_a_date_not_as_set_is_refused(at, value, read, back, words):
    ok, _numbers, why = gates.date_as_set(at, value, read, back)
    assert not ok and words in why, why


# ---------------------------------------------------------------------------
# The browser context: unchanged for every board but the one that pins a zone
# ---------------------------------------------------------------------------

#: The capture's one ``new_context`` call, as it was before boards could pin a
#: zone, with the zone's arguments added last (written out by hand).
NEW_CONTEXT = """            context = browser.new_context(
                viewport=dict(config.VIEWPORT),
                device_scale_factor=1,
                service_workers="block",
                **context_zone(board),
            )"""


def test_a_board_that_pins_no_zone_gets_exactly_the_context_it_always_had():
    boards = {
        slug: storyboard.load(SHOWCASE / relative)
        for slug, relative in config.VIDEOS.items()
    }
    unpinned = [slug for slug, board in boards.items() if board.time_zone is None]
    assert len(unpinned) == len(boards) - 1
    for slug in unpinned:
        assert run.context_zone(boards[slug]) == {}, slug


def test_video_9s_context_adds_its_zone_and_locale():
    assert run.context_zone(storyboard.load(V9)) == {
        "timezone_id": "America/Los_Angeles",
        "locale": "en-US",
    }


def test_the_capture_makes_its_one_context_with_the_zone_added_last():
    source = Path(run.__file__).read_text(encoding="utf-8")
    assert source.count("new_context(") == 1
    assert NEW_CONTEXT in source
