"""The walkthrough transcripts on controlvar.com quote their videos (#1290, G7).

Each video the site lists (``frontend/src/content/videos.json``) has a
descriptive transcript (``frontend/src/content/walkthroughs/<slug>.json``),
shown under its player. It is the only text form of what a video shows but
does not say, so it must be the video's own words. This test holds it to the
storyboard the video was rendered from
(``tests/acceptance/showcase/storyboards/<slug>.yaml``) and to the render's
title and end cards (``showcase.render.templates``). The spoken lines are held
to the pinned captions separately, over the downloaded bytes, by
``frontend/scripts/check-site-media.mjs`` in CI.

THE MATCHING RULE. A transcript is a list of items, each exactly one of
``on_screen`` (a card or a headline, as a list of fragments), ``said`` (the
sentences spoken over one headline) or ``code`` (a code card's lines). The
expected list is built from the storyboard, in its order:

1. the title card: ``PRODUCT_NAME``, the storyboard's ``title``, ``TAGLINE``;
2. per scene, its card, if it has one: one ``on_screen`` item of ``label``,
   then ``command`` or ``headline``, then ``note``; a code card is three
   items, ``on_screen`` [label], ``code`` [its ``lines``, verbatim, blank
   ones included], ``on_screen`` [note];
3. per beat: ``on_screen`` [its ``cue``], then ``said`` [its ``say`` lines];
4. the end card: ``PRODUCT_NAME``, ``END_LINE``, ``SITE``, ``REPOSITORY``,
   ``RECORDED_LINE``.

Item for item, each fragment must equal its source string verbatim. An
``on_screen`` item joins several fields or template constants; each is its own
fragment and is matched on its own. The page writes an item as one "On
screen:" line, adding a full stop to a fragment that has none (``onScreenLine``
in ``frontend/src/components/Walkthroughs.tsx``); that is presentation, and
this test reads the fragments. Within a card the order is the order
``capture.director.card_html`` draws it (label, body, note), checked once by a
person against the video.

Exactly two placeholders may stand in a source string. Each is matched as a
pattern, and its value is checked by hand against the video's render record
when the entry is reviewed:

* ``{version}``, only in ``RECORDED_LINE``: ``v<major>.<minor>.<patch>``;
* ``{traffic_minutes}``, only in a card's ``headline`` or ``note``:
  ``<n> minute(s)``, as the capture measured it (``capture.run.minutes_text``).

Any other ``{name}`` in a string a transcript quotes fails: a new placeholder
needs its rule written here first.

When this fails, the storyboard or the templates no longer say what the
listed video shows. Re-render the video (a new file name, so a new manifest
entry) or revert the storyboard or template change. Never edit the transcript
of an unchanged mp4.

C8 and C9 of the plan are here as well: each title is its storyboard's
``title:``, and each description's claims are facts of its storyboard
(``DESCRIPTION_FACTS``). Reads files only (no git, no network), so it runs the
same in core-build's copy of the tree.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from showcase.render import templates

pytestmark = [pytest.mark.unit]

REPO = Path(__file__).resolve().parents[4]
CONTENT = REPO / "frontend" / "src" / "content"
MANIFEST = CONTENT / "videos.json"
TRANSCRIPTS = CONTENT / "walkthroughs"
BOARDS = REPO / "tests" / "acceptance" / "showcase" / "storyboards"

PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
#: Each placeholder a transcript may fill, and the pattern its value matches.
PLACEHOLDERS = {
    "version": r"v[0-9]+\.[0-9]+\.[0-9]+",
    "traffic_minutes": r"[1-9][0-9]* minutes?",
}

FIX = (
    "Re-render the video (a new file name, so a new manifest entry) or revert the "
    "storyboard or template change; never edit the transcript of an unchanged mp4."
)


@dataclass(frozen=True)
class Source:
    """One string a transcript fragment quotes, where it comes from, and what it may fill."""

    text: str
    where: str
    placeholders: frozenset[str] = frozenset()

    def pattern(self) -> re.Pattern[str]:
        parts = PLACEHOLDER.split(self.text)
        out = []
        for i, part in enumerate(parts):
            if i % 2 == 0:
                out.append(re.escape(part))
            elif part in self.placeholders:
                out.append(f"(?:{PLACEHOLDERS[part]})")
            else:
                raise AssertionError(
                    f"{self.where} has the placeholder {{{part}}}, which a transcript "
                    "may not fill there (the rule allows {version} in RECORDED_LINE "
                    "and {traffic_minutes} in a card's headline or note). Write its "
                    "rule in this test's docstring before a transcript quotes it."
                )
        return re.compile("".join(out))


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def board(slug: str) -> dict:
    return yaml.safe_load((BOARDS / f"{slug}.yaml").read_text(encoding="utf-8"))


def expected_items(storyboard: dict) -> list[tuple[str, list[Source]]]:
    """What the video shows and says, in order, as transcript items of sources."""
    slug = storyboard["slug"]
    items = [
        (
            "on_screen",
            [
                Source(templates.PRODUCT_NAME, "templates.PRODUCT_NAME"),
                Source(storyboard["title"], f"{slug} title"),
                Source(templates.TAGLINE, "templates.TAGLINE"),
            ],
        )
    ]
    for scene in storyboard["scenes"]:
        card = scene.get("card")
        where = f"{slug} scene {scene['id']} card"
        if card:
            label = Source(card["label"], f"{where} label")
            note = Source(card["note"], f"{where} note", frozenset({"traffic_minutes"}))
            if "command" in card:
                items.append(
                    (
                        "on_screen",
                        [label, Source(card["command"], f"{where} command"), note],
                    )
                )
            elif "headline" in card:
                headline = Source(
                    card["headline"],
                    f"{where} headline",
                    frozenset({"traffic_minutes"}),
                )
                items.append(("on_screen", [label, headline, note]))
            elif "code" in card:
                lines = card["code"]["lines"]
                items.append(("on_screen", [label]))
                items.append(
                    (
                        "code",
                        [
                            Source(line, f"{where} code line {i + 1}")
                            for i, line in enumerate(lines)
                        ],
                    )
                )
                items.append(("on_screen", [note]))
            else:
                raise AssertionError(f"{where} has no command, code or headline")
        for number, beat in enumerate(scene["beats"], start=1):
            at = f"{slug} scene {scene['id']} beat {number}"
            items.append(("on_screen", [Source(beat["cue"], f"{at} cue")]))
            items.append(
                (
                    "said",
                    [
                        Source(line, f"{at} say line {i + 1}")
                        for i, line in enumerate(beat["say"])
                    ],
                )
            )
    items.append(
        (
            "on_screen",
            [
                Source(templates.PRODUCT_NAME, "templates.PRODUCT_NAME"),
                Source(templates.END_LINE, "templates.END_LINE"),
                Source(templates.SITE, "templates.SITE"),
                Source(templates.REPOSITORY, "templates.REPOSITORY"),
                Source(
                    templates.RECORDED_LINE,
                    "templates.RECORDED_LINE",
                    frozenset({"version"}),
                ),
            ],
        )
    )
    return items


def transcript_problems(items: list[dict], storyboard: dict) -> list[str]:
    """Every way ``items`` differs from what the storyboard shows and says."""
    expected = expected_items(storyboard)
    problems = []
    for i, (kind, sources) in enumerate(expected):
        if i >= len(items):
            problems.append(f"item {i + 1} is missing: want {kind} {sources[0].where}")
            break
        item = items[i]
        if len(item) != 1 or kind not in item:
            problems.append(
                f"item {i + 1} is {sorted(item)}, want {kind} {sources[0].where}"
            )
            continue
        lines = item[kind]
        if len(lines) != len(sources):
            problems.append(
                f"item {i + 1} ({kind}) has {len(lines)} fragments, the storyboard "
                f"{len(sources)} ({', '.join(s.where for s in sources)})"
            )
            continue
        for line, source in zip(lines, sources):
            if not source.pattern().fullmatch(line):
                problems.append(
                    f"item {i + 1} ({kind}): {line!r} is not {source.where}: {source.text!r}"
                )
    if len(items) > len(expected):
        problems.append(f"{len(items) - len(expected)} item(s) after the end card")
    return problems


MANIFEST_ENTRIES = load_json(MANIFEST)["videos"]
SLUGS = [entry["slug"] for entry in MANIFEST_ENTRIES]


@pytest.mark.parametrize("entry", MANIFEST_ENTRIES, ids=SLUGS)
def test_the_transcript_quotes_its_video(entry):
    transcript = load_json(TRANSCRIPTS / f"{entry['slug']}.json")
    assert transcript["slug"] == entry["slug"]
    assert transcript["mp4"] == entry["mp4"]["name"], (
        f"the transcript is of {transcript['mp4']}, the manifest lists "
        f"{entry['mp4']['name']}: assemble it from that video's approved review page"
    )
    problems = transcript_problems(transcript["items"], board(entry["slug"]))
    assert not problems, "\n".join([*problems[:12], FIX])


@pytest.mark.parametrize("entry", MANIFEST_ENTRIES, ids=SLUGS)
def test_the_title_is_the_storyboards(entry):
    # C8: the section's h3 (the manifest title) is the video's own title card.
    assert entry["title"] == board(entry["slug"])["title"]


# --- C9: what each description claims, as facts of its storyboard -------------------

#: One row per listed video. ``description`` is the manifest's exact text, so
#: changing the copy fails until its facts are written again. Facts:
#:   seed          the storyboard's ``seed``
#:   card_command  a card's ``command``, verbatim
#:   actions       [verb, locator] pairs that occur in this order among the
#:                 beats' ``before``, ``steps`` and ``exit`` (other actions may
#:                 come between them); the locator is compared whole
#:   said          lines the narration says, verbatim
#:   departures    departure kinds the storyboard states
#:   traffic       the storyboard sends simulated users (a ``traffic`` block)
#:   end_state     a part of the storyboard's ``end_state`` the capture checks
DESCRIPTION_FACTS: dict[str, dict[str, Any]] = {
    "01-getting-started": {
        "description": (
            "The Quick Start's command, then a tour of the dashboard: a completed "
            "experiment's results and a flag's rollout schedule."
        ),
        "seed": "demo",
        # "The Quick Start's command": the card's own label says so (shown, not
        # run), and test_showcase_quickstart.py pins both the card's command and
        # that the recording's stack is started with it.
        "card_command": "docker compose up -d --wait",
        # "a completed experiment's results": the one it opens is the one the
        # narration calls completed, then its results; "a flag's rollout
        # schedule": the flags page, then the schedule.
        "actions": [
            ["click", {"role": "link", "name": "Homepage Hero Copy Test"}],
            ["click", {"role": "link", "name": "View results"}],
            ["click", {"testid": "nav-feature-flags"}],
            ["look", {"text": "Rollout schedule", "exact": True}],
        ],
        "said": ["Open the completed one."],
    },
    "02-first-experiment": {
        "description": (
            "Create an experiment in the guided setup, work out how many users it "
            "needs, start it, and read its result once simulated shoppers have "
            "arrived."
        ),
        "seed": "demo",
        # The guided setup (New Experiment), the size estimate, Start; the Live
        # tab as shoppers arrive, then the recommendation.
        "actions": [
            ["click", {"testid": "new-experiment-btn"}],
            ["click", {"testid": "estimate-calculate"}],
            ["click", {"testid": "action-start"}],
            ["click", {"role": "tab", "name": "Live"}],
            ["look", {"testid": "recommendation-reason"}],
        ],
        # "simulated shoppers have arrived": a traffic block, stated on screen.
        "departures": ["simulated traffic"],
        "traffic": True,
        "end_state": {"experiment": {"status": "active", "winner": "Sticky bar"}},
    },
    "03-flag-rollout": {
        "description": (
            "Create a flag, target a segment, set a rollout percentage for everyone "
            "else, switch it on and off, and find each change in the audit log."
        ),
        "seed": "demo",
        # New Flag; the segment condition; the slider for everyone else; Create;
        # the toggle on, then off; the audit log.
        "actions": [
            ["click", {"testid": "new-flag-btn"}],
            ["choose", {"testid": "condition-segment"}],
            ["slide", {"testid": "rollout-percentage"}],
            ["click", {"testid": "submit-flag"}],
            ["click", {"testid": "flag-toggle"}],
            ["click", {"testid": "flag-toggle"}],
            ["click", {"testid": "nav-audit-log"}],
        ],
        # "each change in the audit log": the four changes the capture checks
        # the log for, in order.
        "end_state": {
            "flag": {
                "segment": "Beta testers",
                "audit": [
                    "feature_flag_create",
                    "toggle_enable",
                    "feature_flag_update",
                    "toggle_disable",
                ],
            }
        },
    },
}

VERBS = ("click", "look", "type", "choose", "slide", "move")


def actions(storyboard: dict) -> list[tuple[str, Any]]:
    out = []
    for scene in storyboard["scenes"]:
        for beat in scene["beats"]:
            for part in ("before", "steps", "exit"):
                for step in beat.get(part) or []:
                    out.extend((verb, step[verb]) for verb in VERBS if verb in step)
    return out


def contains(have: Any, want: Any) -> bool:
    if isinstance(want, dict):
        return isinstance(have, dict) and all(
            k in have and contains(have[k], v) for k, v in want.items()
        )
    return have == want


def fact_problems(storyboard: dict, facts: dict[str, Any]) -> list[str]:
    problems = []
    if "seed" in facts and storyboard.get("seed") != facts["seed"]:
        problems.append(f"seed is {storyboard.get('seed')!r}, not {facts['seed']!r}")
    cards = [s["card"] for s in storyboard["scenes"] if s.get("card")]
    if "card_command" in facts and facts["card_command"] not in [
        c.get("command") for c in cards
    ]:
        problems.append(f"no card shows the command {facts['card_command']!r}")
    done = iter(actions(storyboard))
    for verb, target in facts.get("actions", []):
        if not any(v == verb and t == target for v, t in done):
            problems.append(f"no {verb} {target} (in this order)")
    said = [
        line
        for scene in storyboard["scenes"]
        for beat in scene["beats"]
        for line in beat["say"]
    ]
    problems += [
        f"nothing says {line!r}" for line in facts.get("said", []) if line not in said
    ]
    kinds = {d["kind"] for s in storyboard["scenes"] for d in s.get("departures") or []}
    problems += [
        f"no departure of kind {kind!r}"
        for kind in facts.get("departures", [])
        if kind not in kinds
    ]
    if facts.get("traffic") and not storyboard.get("traffic"):
        problems.append("no traffic block: no simulated users arrive")
    if "end_state" in facts and not contains(
        storyboard.get("end_state"), facts["end_state"]
    ):
        problems.append(f"end_state does not hold {facts['end_state']}")
    return problems


def test_every_listed_video_has_its_facts_and_no_other_does():
    assert sorted(DESCRIPTION_FACTS) == sorted(SLUGS)


@pytest.mark.parametrize("entry", MANIFEST_ENTRIES, ids=SLUGS)
def test_the_description_is_true_of_the_storyboard(entry):
    facts = DESCRIPTION_FACTS[entry["slug"]]
    assert entry["description"] == facts["description"], (
        "the description changed: write its facts again in DESCRIPTION_FACTS"
    )
    assert not fact_problems(board(entry["slug"]), facts)


# --- the rule itself, on a made-up storyboard -----------------------------------------
#
# PE 6's planted defects, kept: each must fail, or the comparison has stopped
# comparing. The storyboard is made up so the rule is pinned whatever the site
# lists.

TOY_BOARD = {
    "slug": "09-toy",
    "title": "A toy",
    "scenes": [
        {
            "id": "card",
            "card": {
                "label": "Off camera",
                "headline": "63,000 shoppers later",
                "note": "Sent by the tool in {traffic_minutes} (measured).",
            },
            "beats": [{"cue": "Flags roll out in stages.", "say": ["They do."]}],
        },
        {
            "id": "result",
            "beats": [
                {
                    "cue": "Its results, computed off camera first: lift, p-value, winner.",
                    "say": ["Its results show the lift.", "And a winner."],
                },
                {"cue": "Each stage raises the share of users.", "say": ["It does."]},
            ],
        },
    ],
}

TOY_TRANSCRIPT = [
    {
        "on_screen": [
            "Controlvar",
            "A toy",
            "A/B testing and feature flags you run yourself.",
        ]
    },
    {
        "on_screen": [
            "Off camera",
            "63,000 shoppers later",
            "Sent by the tool in 13 minutes (measured).",
        ]
    },
    {"on_screen": ["Flags roll out in stages."]},
    {"said": ["They do."]},
    {"on_screen": ["Its results, computed off camera first: lift, p-value, winner."]},
    {"said": ["Its results show the lift.", "And a winner."]},
    {"on_screen": ["Each stage raises the share of users."]},
    {"said": ["It does."]},
    {
        "on_screen": [
            "Controlvar",
            "Open source, Apache-2.0, runs on your own infrastructure",
            "controlvar.com",
            "github.com/Controlvar/controlvar",
            "Recorded on Controlvar v0.36.0, a local stack with demo data",
        ]
    },
]


def planted(change) -> list[str]:
    items = copy.deepcopy(TOY_TRANSCRIPT)
    storyboard = copy.deepcopy(TOY_BOARD)
    change(items, storyboard)
    return transcript_problems(items, storyboard)


def test_the_toy_transcript_matches():
    assert planted(lambda items, sb: None) == []


def test_deleting_computed_off_camera_first_fails():
    def change(items, sb):
        items[4]["on_screen"][0] = "Its results: lift, p-value, winner."

    assert planted(change)


def test_swapping_two_headlines_fails():
    def change(items, sb):
        items[4], items[6] = items[6], items[4]

    assert len(planted(change)) == 2


def test_changing_one_word_of_a_headline_fails():
    def change(items, sb):
        items[2]["on_screen"][0] = "Flags roll out in steps."

    assert planted(change)


def test_a_dropped_sentence_or_an_extra_item_fails():
    def drop(items, sb):
        items[5]["said"].pop()

    def extra(items, sb):
        items.append({"said": ["One more thing."]})

    assert planted(drop) and planted(extra)


def test_an_unknown_placeholder_fails():
    def change(items, sb):
        sb["scenes"][0]["beats"][0]["cue"] = "Flags roll out in {stages}."

    with pytest.raises(AssertionError, match=r"\{stages\}"):
        planted(change)


def test_a_known_placeholder_outside_its_place_fails():
    def change(items, sb):
        sb["scenes"][0]["beats"][0]["cue"] = "Done in {traffic_minutes}."
        items[2]["on_screen"][0] = "Done in 13 minutes."

    with pytest.raises(AssertionError, match=r"\{traffic_minutes\}"):
        planted(change)


@pytest.mark.parametrize(
    ("filled", "matches"),
    [
        ("13 minutes", True),
        ("1 minute", True),
        ("minutes", False),
        ("13 mins", False),
        ("0 minutes", False),
    ],
)
def test_traffic_minutes_matches_only_what_the_capture_writes(filled, matches):
    def change(items, sb):
        items[1]["on_screen"][2] = f"Sent by the tool in {filled} (measured)."

    assert (planted(change) == []) is matches


@pytest.mark.parametrize(
    ("line", "matches"),
    [
        ("Recorded on Controlvar v0.36.0, a local stack with demo data", True),
        ("Recorded on Controlvar v1.2.10, a local stack with demo data", True),
        ("Recorded on Controlvar 0.36.0, a local stack with demo data", False),
        ("Recorded on Controlvar v0.36, a local stack with demo data", False),
    ],
)
def test_version_matches_a_release_tag_only(line, matches):
    def change(items, sb):
        items[-1]["on_screen"][4] = line

    assert (planted(change) == []) is matches


def test_a_wrong_fact_fails():
    facts = dict(DESCRIPTION_FACTS[SLUGS[0]])
    facts["actions"] = [
        *facts.get("actions", []),
        ["click", {"testid": "no-such-button"}],
    ]
    assert fact_problems(board(SLUGS[0]), facts)
