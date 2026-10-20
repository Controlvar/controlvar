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

ANIMATIONS (T178 condition 2). A transcript that says ``"kind": "animation"`` is
a silent animation with captions of its own text. It has no storyboard and no
title or end card, so the rule above does not apply to it. Instead this test
refuses:

* a storyboard at ``tests/acceptance/showcase/storyboards/<slug>.yaml``. A stub
  storyboard would pass the recording rule for the wrong reason;
* any item that is not ``on_screen`` with exactly one fragment;
* ``{version}`` or "Recorded on" anywhere in the transcript;
* a description whose facts (``in_transcript``) are not each found, verbatim,
  in the transcript's lines.

Any other ``kind`` is refused, and no ``kind`` is a recording.

What else CI checks for an animation: ``frontend/scripts/check-site-media.mjs``
holds its mp4 to having no audio track and its transcript to the pinned
captions, one caption per cue, in order. CI does not compare those captions
with the animation's source. The reviewer does that by re-running the
extractor that wrote them (T178 condition 2(g)).
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


ANIMATION = "animation"
#: What an animation's transcript may not say anywhere (T178 condition 2(d)).
NOT_IN_AN_ANIMATION = (re.compile(r"recorded on", re.I), re.compile(r"\{version\}"))


def kind_of(transcript: dict) -> str:
    """``recording`` when the transcript declares no ``kind``; ``animation``; nothing else."""
    if "kind" not in transcript:
        return "recording"
    if transcript["kind"] == ANIMATION:
        return ANIMATION
    raise AssertionError(
        f"{transcript.get('slug')}: transcript kind {transcript['kind']!r}; the "
        "only kind is 'animation', and a recording declares none"
    )


def animation_problems(transcript: dict, boards: Path = BOARDS) -> list[str]:
    """Every way an animation's transcript breaks T178 condition 2(d)."""
    slug = transcript["slug"]
    problems = []
    if (boards / f"{slug}.yaml").exists():
        problems.append(
            f"{slug} is an animation but has a storyboard ({slug}.yaml): an animation "
            "is not recorded, and a stub storyboard passes the recording rule for "
            "the wrong reason"
        )
    for i, item in enumerate(transcript["items"], start=1):
        if list(item) != ["on_screen"] or len(item["on_screen"]) != 1:
            problems.append(
                f"item {i} is {item!r}: an animation's transcript is on-screen items "
                "of one caption each"
            )
    text = json.dumps(transcript["items"])
    problems += [
        f"the transcript says {pattern.pattern!r}, which no animation may"
        for pattern in NOT_IN_AN_ANIMATION
        if pattern.search(text)
    ]
    return problems


MANIFEST_ENTRIES = load_json(MANIFEST)["videos"]
SLUGS = [entry["slug"] for entry in MANIFEST_ENTRIES]


def transcript_of(entry: dict) -> dict:
    return load_json(TRANSCRIPTS / f"{entry['slug']}.json")


@pytest.mark.parametrize("entry", MANIFEST_ENTRIES, ids=SLUGS)
def test_the_transcript_quotes_its_video(entry):
    transcript = transcript_of(entry)
    assert transcript["slug"] == entry["slug"]
    assert transcript["mp4"] == entry["mp4"]["name"], (
        f"the transcript is of {transcript['mp4']}, the manifest lists "
        f"{entry['mp4']['name']}: assemble it from that video's approved review page"
    )
    if kind_of(transcript) == ANIMATION:
        problems = animation_problems(transcript)
        assert not problems, "\n".join(problems)
        return
    problems = transcript_problems(transcript["items"], board(entry["slug"]))
    assert not problems, "\n".join([*problems[:12], FIX])


@pytest.mark.parametrize("entry", MANIFEST_ENTRIES, ids=SLUGS)
def test_the_title_is_the_storyboards(entry):
    if kind_of(transcript_of(entry)) == ANIMATION:
        # An animation has no storyboard, so no title card to equal.
        assert not (BOARDS / f"{entry['slug']}.yaml").exists()
        return
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
#: An animation has no storyboard. Its row has one fact kind, the only one it may
#: have:
#:   in_transcript phrases each found, verbatim, in the transcript's lines (its
#:                 captions)
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
    "08-warehouse-explainer": {
        "description": (
            "Your own SQL selects users in your warehouse, a script you write sends "
            "them to the batch assign API (beta), targeting admits some, and a "
            "deterministic hash splits them."
        ),
        # An animation: each clause is one of its own captions, verbatim.
        "in_transcript": [
            "Your users already live in Snowflake or BigQuery",
            "Your own SQL query picks the eligible users",
            "A short sync script you write sends them",
            "POST /api/v1/tracking/assign/batch (beta)",
            "The experiment's targeting rules admit some users",
            "Admitted users are split by a deterministic hash",
        ],
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


def animation_fact_problems(transcript: dict, facts: dict[str, Any]) -> list[str]:
    """An animation's row may hold only ``in_transcript``, each phrase found in a line."""
    problems = [
        f"an animation's facts are in_transcript only, not {key!r}"
        for key in facts
        if key not in ("description", "in_transcript")
    ]
    if not facts.get("in_transcript"):
        problems.append("no in_transcript facts")
    lines = [line for item in transcript["items"] for line in item.get("on_screen", [])]
    problems += [
        f"no line of the transcript says {phrase!r}"
        for phrase in facts.get("in_transcript", [])
        if not any(phrase in line for line in lines)
    ]
    return problems


def test_every_listed_video_has_its_facts_and_no_other_does():
    assert sorted(DESCRIPTION_FACTS) == sorted(SLUGS)


@pytest.mark.parametrize("entry", MANIFEST_ENTRIES, ids=SLUGS)
def test_the_description_is_true_of_the_storyboard(entry):
    facts = DESCRIPTION_FACTS[entry["slug"]]
    assert entry["description"] == facts["description"], (
        "the description changed: write its facts again in DESCRIPTION_FACTS"
    )
    transcript = transcript_of(entry)
    if kind_of(transcript) == ANIMATION:
        assert not animation_fact_problems(transcript, facts)
        return
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


# --- the animation rule, on a made-up animation (T178 condition 2(f)) -----------------

TOY_ANIMATION = {
    "slug": "09-toy-animation",
    "kind": "animation",
    "mp4": "09-toy-animation-000000000000.mp4",
    "items": [
        {"on_screen": ["Rows go out in chunks."]},
        {"on_screen": ["Then a hash."]},
    ],
}


def toy_animation(change=None) -> dict:
    transcript = copy.deepcopy(TOY_ANIMATION)
    if change:
        change(transcript)
    return transcript


def test_the_toy_animation_passes(tmp_path):
    assert animation_problems(toy_animation(), boards=tmp_path) == []
    assert kind_of(toy_animation()) == ANIMATION
    assert kind_of({"slug": "x"}) == "recording"


def test_an_animation_with_a_storyboard_fails(tmp_path):
    (tmp_path / "09-toy-animation.yaml").write_text("title: A stub\n", encoding="utf-8")
    assert any(
        "has a storyboard" in p
        for p in animation_problems(toy_animation(), boards=tmp_path)
    )


def test_an_animation_with_a_said_item_or_two_captions_in_one_item_fails(tmp_path):
    def said(t):
        t["items"][1] = {"said": ["Then a hash."]}

    def joined(t):
        t["items"] = [{"on_screen": ["Rows go out in chunks.", "Then a hash."]}]

    for change in (said, joined):
        assert any(
            "on-screen items of one caption each" in p
            for p in animation_problems(toy_animation(change), boards=tmp_path)
        )


def test_an_animation_saying_recorded_on_or_holding_a_version_fails(tmp_path):
    def recorded(t):
        t["items"].append(
            {
                "on_screen": [
                    "Recorded on Controlvar v0.36.0, a local stack with demo data"
                ]
            }
        )

    def version(t):
        t["items"].append({"on_screen": ["Made with Controlvar {version}"]})

    assert any(
        "recorded on" in p
        for p in animation_problems(toy_animation(recorded), boards=tmp_path)
    )
    assert any(
        "version" in p
        for p in animation_problems(toy_animation(version), boards=tmp_path)
    )


def test_a_kind_other_than_animation_fails():
    with pytest.raises(AssertionError, match="transcript kind 'silent'"):
        kind_of({**TOY_ANIMATION, "kind": "silent"})


def test_an_animation_fact_not_in_its_captions_fails():
    facts = {"description": "A toy.", "in_transcript": ["Rows go out in chunks"]}
    assert animation_fact_problems(toy_animation(), facts) == []
    missing = {**facts, "in_transcript": ["Rows go out in batches"]}
    assert animation_fact_problems(toy_animation(), missing) == [
        "no line of the transcript says 'Rows go out in batches'"
    ]
    storyboard_fact = {**facts, "seed": "demo"}
    assert animation_fact_problems(toy_animation(), storyboard_fact) == [
        "an animation's facts are in_transcript only, not 'seed'"
    ]
