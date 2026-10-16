"""The narration in the storyboards (#1066 voiceover): ``say:``, ``claims:`` and their rules.

G1 (what a sentence may say) and G3 (the headline's numbers are spoken) at
load; G2's comparison and tally (the page half runs at capture); the
pronunciation table; the slot and length arithmetic the voice step projects
with; and the pins that keep the silent path as it is: a storyboard without
narration loads as before, and narration changes nothing the capture records.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from showcase import contract
from showcase.capture import config, gates, narration, storyboard

pytestmark = [pytest.mark.unit]

REPO = Path(__file__).resolve().parents[4]
BOARDS = REPO / "tests" / "acceptance" / "showcase" / "storyboards"
V1 = BOARDS / "01-getting-started.yaml"
V2 = BOARDS / "02-first-experiment.yaml"


def data(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def without_narration(raw: dict) -> dict:
    raw = json.loads(json.dumps(raw))
    for scene in raw["scenes"]:
        for beat in scene["beats"]:
            beat.pop("say", None)
            beat.pop("claims", None)
    return raw


def refusal(raw: dict) -> str:
    with pytest.raises(storyboard.StoryboardError) as caught:
        storyboard.parse(raw, source="test.yaml")
    return str(caught.value)


# --- The two narrated storyboards ------------------------------------------------------------


@pytest.mark.parametrize("path", [V1, V2], ids=["v1", "v2"])
def test_every_beat_is_narrated_and_the_lints_report_nothing(path):
    board = storyboard.load(path)
    assert all(beat.say for scene in board.scenes for beat in scene.beats)
    assert narration.board_problems(board) == []
    for *_where, sentence in storyboard.narration_lines(board):
        assert storyboard.caption_problem(sentence) is None, sentence
        assert len(sentence.split()) <= contract.MAX_CUE_WORDS


#: Every number the narration says, by beat, and the element that shows it at
#: the beat's start (PR body table; the capture's G2 checks it on the page).
SPOKEN_NUMBERS = {
    "01-getting-started": {
        ("stack-card", 2, 2): ["3", "2"],
        ("dashboard", 5, 2): ["15%"],
        ("dashboard", 8, 1): ["10%", "25%", "50%"],
    },
    "02-first-experiment": {
        ("setup", 6, 1): ["31,234", "13"],
        ("later", 1, 1): ["63,000", "2"],
        ("result", 1, 1): ["12.1%", "95%"],
    },
}


@pytest.mark.parametrize("path", [V1, V2], ids=["v1", "v2"])
def test_the_spoken_numbers_are_the_ones_listed(path):
    board = storyboard.load(path)
    said = {
        (scene, beat, line): storyboard.numbers_in(text)
        for scene, beat, line, text in storyboard.narration_lines(board)
        if storyboard.numbers_in(text)
    }
    assert said == SPOKEN_NUMBERS[board.slug]


def test_the_cards_state_the_numbers_their_narration_says_in_digits():
    v1 = storyboard.load(V1)
    note = v1.scenes[0].card.note
    assert "3 experiments, 2 flags" in note
    v2 = storyboard.load(V2)
    later = next(s for s in v2.scenes if s.id == "later")
    assert later.card.note.startswith("About 2 weeks of traffic")
    assert later.beats[0].claims == []
    for number in ("63,000", "2"):
        assert number in storyboard.numbers_in(
            later.card.headline + " " + later.card.note
        )


@pytest.mark.parametrize("path", [V1, V2], ids=["v1", "v2"])
def test_narration_changes_nothing_the_capture_records(path):
    """EM condition 5: the silent path stays as it is. The cue list, and so the
    capture directory and its gate 4 meaning, are the same with or without ``say``."""
    narrated = storyboard.parse(data(path))
    silent = storyboard.parse(without_narration(data(path)))
    assert storyboard.cue_list_bytes(narrated) == storyboard.cue_list_bytes(silent)
    assert storyboard.estimate_ms(narrated) == storyboard.estimate_ms(silent)
    for cue in storyboard.compile_cues(narrated):
        assert set(cue) == {"scene", "beat", "text", "focus", "min_ms", "payoff"}
    assert contract.CUE_KEYS == {"start_ms", "end_ms", "text", "focus"}
    assert storyboard.narration_lines(silent) == []


def test_a_storyboard_without_narration_loads_unchanged():
    board = storyboard.parse(without_narration(data(V2)))
    assert narration.board_problems(board) == []
    assert all(
        beat.say == [] and beat.claims == [] for s in board.scenes for beat in s.beats
    )


# --- Narrate every beat or none ----------------------------------------------------------------


def test_a_narrated_storyboard_narrates_every_beat():
    raw = data(V2)
    del raw["scenes"][0]["beats"][2]["say"]
    assert (
        "- scene setup beat 3: says nothing; a narrated storyboard narrates every beat"
        in refusal(raw)
    )


def test_at_most_four_sentences_a_beat():
    raw = data(V2)
    raw["scenes"][0]["beats"][0]["say"] = ["One more line."] * 5
    assert "List should have at most 4 items" in refusal(raw)


def test_claims_need_something_said_and_a_page_of_the_docs():
    raw = without_narration(data(V2))
    raw["scenes"][0]["beats"][0]["claims"] = [
        {"says": "two weeks", "source": "docs/getting-started/quick-start.md:44"}
    ]
    assert "- scene setup beat 1: claims with nothing said" in refusal(raw)
    raw = data(V2)
    raw["scenes"][0]["beats"][0]["claims"] = [
        {"says": "two weeks", "source": "the page"}
    ]
    assert "String should match pattern" in refusal(raw)


# --- G1, sentence by sentence -------------------------------------------------------------------


def narrated(sentence: str, beat: int = 0) -> dict:
    raw = data(V2)
    raw["scenes"][0]["beats"][beat]["say"] = [sentence]
    return raw


@pytest.mark.parametrize(
    "sentence, reason",
    [
        ("The password is Demo1234!", "holds a secret value (static needle 1 of 4)"),
        ("The password is demo 1234!", "holds a secret value (static needle 1 of 4)"),
        ("The password is Demo1234.", "holds a secret value (static needle 1 of 4)"),
        (
            "Type Demo1234 as the password.",
            "holds a secret value (static needle 1 of 4)",
        ),
        ("Open localhost 3100 in your browser.", "names a host or a port"),
        ("The API answers on port 8000.", "names a host or a port"),
        ("It answers on port 9100.", "names a host or a port"),
        ("Open 127.0.0.1 in your browser.", "names a host or a port"),
        ("Open localhost in your browser.", "names a host or a port"),
        ("Its API is on 28400 here.", "names a host or a port"),
        ("Open the dashboard at localhost:3100 now.", "names a host or a port"),
        ("About three weeks of shoppers.", "says 'three' as a word"),
        ("Thirty-one thousand users per variant.", "says 'Thirty' as a word"),
        ("Half of them see the bar.", "says 'Half' as a word"),
        ("Hundreds of shoppers arrive.", "says 'Hundreds' as a word"),
        ("Results update live as shoppers arrive.", "says 'live'"),
        (
            "It shows new users in real time.",
            "says 'real time', which the narration never says",
        ),
        (
            "The numbers change instantly.",
            "says 'instantly', which the narration never says",
        ),
        ("Shoppers show up as they happen.", "says 'as they happen'"),
        ("It is simply the best way.", "says 'simply', which the narration never says"),
        ("Unlike LaunchDarkly, it runs here.", "says 'LaunchDarkly'"),
        (
            "Visit example.com to read more.",
            "names an address other than controlvar.com",
        ),
        ("See https: for the rest.", "names an address other than controlvar.com"),
        ("Mail admin@demo.com about it.", "names an email address"),
        ("Paste eptk_demo here.", "holds an API key's prefix"),
        ("Fill in {name} here.", "a caption is fixed text: no template"),
        (
            "A Docs journey starts here.",
            "says 'Docs journey', which the video must never show",
        ),
        ("Assign a user from your app now.", "says 'Assign a user from your app'"),
        ("It works. And it is fast.", "holds more than one sentence"),
        ("No end punctuation here", "does not end with '.', '?' or '!'"),
        (
            "One two three four five six seven eight nine ten eleven twelve thirteen.",
            "the caption has 13 words; at most 12",
        ),
    ],
)
def test_a_sentence_the_rules_refuse(sentence, reason):
    text = refusal(narrated(sentence))
    assert "test.yaml: the narration is refused:" in text
    assert f"- scene setup beat 1 say 1: {reason}" in text, text


@pytest.mark.parametrize(
    "sentence",
    [
        "Open the completed one.",
        "Try Controlvar at controlvar.com.",
        "Choose the Live tab, then Refresh.",
        "Each stage raises the share: 10%, then 25%, now 50%.",
        "Sign in as the demo admin.",
    ],
)
def test_sentences_the_rules_accept(sentence):
    raw = narrated(sentence, beat=0)
    # The Live tab is said where the beat or the next clicks Refresh.
    raw["scenes"][0]["beats"][0]["steps"].append(
        {"click": {"testid": "refresh-button"}}
    )
    storyboard.parse(raw)


def test_the_live_tab_needs_a_refresh_in_its_beat_or_the_next():
    v2 = data(V2)
    arriving = next(i for i, s in enumerate(v2["scenes"]) if s["id"] == "arriving")
    # Video 2 says it on the beat before the one that clicks Refresh: accepted.
    assert "the Live tab" in v2["scenes"][arriving]["beats"][0]["say"][0]
    storyboard.parse(v2)
    v2["scenes"][arriving]["beats"][1]["steps"] = [
        s for s in v2["scenes"][arriving]["beats"][1]["steps"] if "click" not in s
    ]
    assert (
        "- scene arriving beat 1 say 1: names the Live tab, but neither this beat nor"
        " the next clicks Refresh"
    ) in refusal(v2)


def test_a_claimed_number_word_is_accepted_and_still_owes_its_digits():
    raw = data(V2)
    later = next(s for s in raw["scenes"] if s["id"] == "later")
    later["beats"][0]["say"] = ["63,000 shoppers later: about two weeks of traffic."]
    later["beats"][0]["claims"] = [
        {"says": "about two weeks", "source": "docs/getting-started/quick-start.md"}
    ]
    storyboard.parse(raw)
    later["beats"][0]["claims"] = [{"says": "about three weeks", "source": "docs/x.md"}]
    text = refusal(raw)
    assert "- scene later beat 1 say 1: says 'two' as a word" in text
    assert "- scene later beat 1: the claim 'about three weeks' is not said" in text
    later["beats"][0]["claims"] = [{"says": "63,000 shoppers", "source": "docs/x.md"}]
    assert "the claim '63,000 shoppers' holds no number word" in refusal(raw)
    # A claim never stands in for a digit: the headline's number must be spoken.
    later["beats"][0]["cue"] = "About 2 weeks of shoppers, simulated in minutes."
    later["beats"][0]["claims"] = [
        {"says": "about two weeks", "source": "docs/getting-started/quick-start.md"}
    ]
    assert (
        "- scene later beat 1: the headline says 2; the narration does not"
        in refusal(raw)
    )


# --- G3: the headline's numbers are spoken ---------------------------------------------------------


def test_narration_without_the_headlines_number_is_refused():
    raw = data(V2)
    raw["scenes"][0]["beats"][5]["say"] = [
        "That's enough users per variant: about 13 days."
    ]
    assert (
        "- scene setup beat 6: the headline says 31,234; the narration does not"
        in refusal(raw)
    )


def test_the_headline_and_the_narration_compare_numbers_by_value():
    assert narration.headline_problems("A 15.0% lift.", ["A 15% lift."]) == []
    assert narration.headline_problems("31,234 users.", ["That's 31234 users."]) == []
    assert narration.headline_problems("12.1% lift.", ["A 12% lift."]) == [
        "the headline says 12.1%; the narration does not"
    ]


@pytest.mark.parametrize(
    "written, key",
    [
        ("15.0%", "15%"),
        ("15%", "15%"),
        ("31,234", "31234"),
        ("31234", "31234"),
        ("10.00", "10"),
        ("12.1%", "12.1%"),
        ("12.10%", "12.10%"),
        ("0.0007", "0.0007"),
        ("1,2", "1,2"),
        ("63,000", "63000"),
    ],
)
def test_numbers_compare_as_written_with_zero_fractions_and_grouping_ignored(
    written, key
):
    assert narration.number_key(written) == key


# --- G2: the function and the tally (the page half runs at capture) ---------------------------------


def test_a_number_on_the_page_at_the_start_or_before_the_exit_passes():
    ok, numbers, why = gates.spoken_numbers(
        "That's 31,234 users per variant: about 13 days.",
        "31,234 users per variant About 13 days (about 2 weeks).",
        "",
    )
    assert (ok, why) == (True, "")
    assert numbers == {
        "said": ["31,234", "13"],
        "shown_at": {"31,234": "start", "13": "start"},
    }
    ok, numbers, _ = gates.spoken_numbers(
        "A 15% lift.", "", "new_copy shows 15.0% improvement"
    )
    assert ok and numbers["shown_at"] == {"15%": "end"}
    ok, numbers, _ = gates.spoken_numbers("2 flags.", "2 flags", "2 flags")
    assert ok and numbers["shown_at"] == {"2": "start and end"}


def test_a_number_the_page_does_not_show_is_refused():
    ok, numbers, why = gates.spoken_numbers(
        "That's 31,000 users per variant.", "31,234 users per variant", "31,234"
    )
    assert not ok
    assert why == (
        "the narration says 31,000; the page shows 31,234 at the beat's start and"
        " 31,234 before its exit"
    )
    # 13 is not found inside 31,234.
    assert not gates.spoken_numbers("About 13 days.", "31,234 users", "31,234 users")[0]


def test_the_tally_is_one_check_per_sentence_exactly():
    assert gates.narration_tally(18, 18) == (
        True,
        {"narration_checks": 18, "sentences": 18},
        "",
    )
    assert (
        gates.narration_tally(17, 18)[2]
        == "17 narration number checks for 18 sentences"
    )
    assert not gates.narration_tally(19, 18)[0]


# --- The pronunciation table ------------------------------------------------------------------------


def test_the_table_is_the_plans_and_keeps_letters_and_digits():
    assert contract.SPOKEN == (
        ("SDKs", "S.D.K.s"),
        ("SDK", "S.D.K."),
        ("controlvar.com", "controlvar dot com"),
    )
    assert contract.spoken_problems() == []


@pytest.mark.parametrize(
    "entry, problem",
    [
        (("31,234", "31,000"), "changes its digits"),
        (("SDK", "S.K.D."), "drops or reorders a letter"),
        (("SDK", "kit"), "drops or reorders a letter"),
        (("SDK", "<sub>S.D.K.</sub>"), "holds markup"),
    ],
)
def test_a_table_entry_that_changes_what_is_said_is_refused(entry, problem):
    problems = contract.spoken_problems((entry,))
    assert any(problem in p for p in problems), problems


@pytest.mark.parametrize(
    "written, sent",
    [
        (
            "In your app, the JavaScript SDK asks.",
            "In your app, the JavaScript S.D.K. asks.",
        ),
        ("Both SDKs do it.", "Both S.D.K.s do it."),
        (
            "Try Controlvar at controlvar.com.",
            "Try Controlvar at controlvar dot com.",
        ),
        ("The SDKfoo stays.", "The SDKfoo stays."),
        ("No change here.", "No change here."),
    ],
)
def test_the_table_applies_to_whole_words_in_the_text_sent_only(written, sent):
    assert contract.polly_text(written) == sent


def test_video_2_sends_the_sdk_as_letters_and_shows_it_as_written():
    board = storyboard.load(V2)
    sdk = next(t for *_w, t in storyboard.narration_lines(board) if "SDK" in t)
    assert sdk == "In your app, the JavaScript SDK asks for each shopper's variant."
    assert (
        contract.polly_text(sdk)
        == "In your app, the JavaScript S.D.K. asks for each shopper's variant."
    )


# --- Slots and the projected length ------------------------------------------------------------------


def test_a_slot_is_the_longer_of_the_clip_and_the_reading_time_plus_the_guard():
    assert contract.SAMPLES_PER_FRAME == 1600 and contract.VOICE_GUARD_MS == 50
    short = "No winner yet."  # reading time 2.0 s
    assert contract.sentence_slot_ms(short, 48_000) == 2050
    assert contract.sentence_slot_ms(short, 144_000) == 3050
    assert contract.sentence_slot_ms(short, 144_001) == 3051
    assert contract.clip_ms(48_001) == 1001


CARD_BOARD = {
    "slug": "09-voice-test",
    "title": "Voice test",
    "seed": "demo",
    "seed_counts": {"experiments": 3, "feature_flags": 2, "users": 4},
    "end_state": {"signed_in_as": "admin@demo.com", "experiments_listed": 3},
    "scenes": [
        {
            "id": "page",
            "open": "/experiments",
            "beats": [
                {
                    "cue": "Open it.",
                    "focus": {"testid": "x"},
                    "say": ["Open it now."],
                    "exit": [{"click": {"testid": "x"}}],
                },
                {
                    "cue": "The result.",
                    "focus": {"testid": "y"},
                    "payoff": True,
                    "say": ["The result."],
                    "exit": [{"click": {"testid": "y"}}],
                },
            ],
        }
    ],
}


def test_the_projection_holds_each_beat_for_its_narration_over_the_exit():
    board = storyboard.parse(CARD_BOARD)
    click = (
        config.POINTER_MS
        + config.POINTER_REST_MS
        + config.CLICK_RING_MS
        + storyboard.STEP_OVERHEAD_MS
    )
    assert click == 1130
    # Clips of 1 s: a sentence's slot is its reading time, 2.05 s. Beat 1 is
    # held for its headline (2.1 s); the payoff beat for its slot and margin.
    short = {("page", 1, 1): 48_000, ("page", 2, 1): 48_000}
    assert storyboard.min_cue_ms("Open it.") == 2100
    expected = (
        storyboard.SCENE_GAP_MS
        + 2100
        + click
        + config.CUE_GAP_MS
        + (2050 + config.CUE_MARGIN_MS)
        + config.PAYOFF_MS
        + click
        + config.CUE_GAP_MS
    )
    assert storyboard.voiced_estimate_ms(board, short) == expected
    assert (
        storyboard.voiced_estimate_ms(board, short)
        == storyboard.estimate_ms(board) + 50
    )
    # A 5 s clip: beat 1's narration runs on over its exit click; the payoff
    # beat's ends before its silent hold.
    long = {("page", 1, 1): 240_000, ("page", 2, 1): 240_000}
    slot = 5000 + contract.VOICE_GUARD_MS
    expected = (
        storyboard.SCENE_GAP_MS
        + (slot + config.CUE_MARGIN_MS - click)
        + click
        + config.CUE_GAP_MS
        + (slot + config.CUE_MARGIN_MS)
        + config.PAYOFF_MS
        + click
        + config.CUE_GAP_MS
    )
    assert storyboard.voiced_estimate_ms(board, long) == expected


# --- The plan's numbers ------------------------------------------------------------------------------


def test_the_voice_contract_is_the_plans():
    assert contract.VOICES == ("Ruth",)
    assert (contract.VOICE_ENGINE, contract.VOICE_FORMAT, contract.POLLY_RATE) == (
        "generative",
        "mp3",
        "24000",
    )
    assert (contract.TEMPO, contract.VOICE_RATE, contract.POLLY_REGION) == (
        "0.9",
        48_000,
        "us-west-2",
    )
    assert (contract.STRETCH_SHORT_MS, contract.STRETCH_LONG_MS) == (45, 5)
    assert contract.MAX_SAY_SENTENCES == 4
    assert contract.PROJECTION_SHARE == 0.97
    assert contract.MAX_VIDEO_MS == 90_000


@pytest.mark.parametrize(
    "raw_ms, stretched_samples, ok",
    [
        (2064, None, True),  # one 0.9 stretch, 27 ms short (Ruth: 24-31 ms)
        (2064, "exact", True),
        (2064, "-45ms", True),
        (2064, "-46ms", False),
        (2064, "+5ms", True),
        (2064, "+6ms", False),
        (2064, "twice", False),
        (2064, "none", False),
        (2064, "0.81", False),
    ],
)
def test_the_stretch_window_is_absolute(raw_ms, stretched_samples, ok):
    raw = raw_ms * 48
    exact = raw / 0.9
    samples = {
        None: round(exact) - 1300,
        "exact": round(exact),
        "-45ms": int(exact) - 2160 + 1,
        "-46ms": round(exact) - 2208,
        "+5ms": int(exact) + 240,
        "+6ms": round(exact) + 288,
        "twice": round(exact / 0.9) - 2600,
        "none": raw,
        "0.81": round(raw / 0.81),
    }[stretched_samples]
    assert (contract.stretch_problem(raw, samples) is None) == ok


@pytest.mark.parametrize(
    "clips, problems",
    [
        ([("a", -23.4, -10.5), ("b", -23.6, -6.8)], 0),
        ([("a", -23.4, -5.8)], 0),  # -1.3 exactly
        ([("a", -23.4, -5.7)], 1),
        ([("a", -24.2, -10.0)], 0),  # -19.7 exactly
        ([("a", -24.3, -10.0)], 1),
        ([("a", -22.8, -10.0)], 0),  # -18.3 exactly
        ([("a", -22.7, -10.0)], 1),
        ([("a", -23.4, -1.5), ("b", -33.0, -20.0)], 2),
    ],
)
def test_the_loudness_precheck_boundaries(clips, problems):
    assert len(contract.loudness_problems(clips)) == problems
