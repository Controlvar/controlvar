"""The showcase's code cards quote the docs, line for line (#1066, video 2).

Video 2's SDK card shows lines of the JavaScript SDK's quick start
(docs/sdk/javascript.md). The capture checks them against the recorded
release's page; these tests check them against this tree's, so a change to the
quick start that the card no longer matches fails on the pull request that
makes it, docs-only or not (test_docs_only_gate.py's DOCS_TESTS).

Video 9 (D182) says what the dashboard does as it does it. Every sentence it
says has a row of ``CLAIMS`` (QA 2a): what the video draws while it is said,
checked against the storyboard (its caption, a test id its beat names, or the
product's fixed copy in frontend/src), and the pages that state it, read from
this tree (by their words, never by line number). No sentence of it says "API".

Video 10 (#1512) has the same: every sentence it says has a row of
``CLAIMS_10``, and its card's note says what docs/sdk/go.md says about
installing the Go SDK. Its other checks are in test_showcase_flag_values.py;
these are here because they read docs/, so they run on a docs-only change too.

Video 11 (#1889) too: ``CLAIMS_11``, each sentence held to the flag list's
own words in frontend/src and to docs/feature-flags/stale-flags.md, whose
reasons table is the one the board's end state is derived from. Its other
checks are in test_showcase_stale_flags.py.

Video 12 (#1890) has ``CLAIMS_12``, one row per sentence, quoting the merged
dashboard copy and docs/guides/balance-within-groups.md; its card quotes that
guide's "Assign users" block (``test_every_code_card_quotes_its_page_of_the_docs``),
and the key typed on camera is the block's. Its other checks are in
test_showcase_balance_within.py.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Set, Tuple

import pytest
import yaml
from showcase.capture import config, storyboard

pytestmark = [pytest.mark.unit]

REPO = Path(__file__).resolve().parents[4]
STORYBOARDS = REPO / "tests" / "acceptance" / "showcase"
JS_SDK_DOC = REPO / "docs" / "sdk" / "javascript.md"


def code_cards():
    for relative in config.VIDEOS.values():
        board = storyboard.load(STORYBOARDS / relative)
        for scene in board.scenes:
            if scene.card is not None and scene.card.code is not None:
                yield board, scene.card.code


def test_every_code_card_quotes_its_page_of_the_docs():
    cards = list(code_cards())
    assert cards, "no storyboard has a code card: the check found nothing"
    for board, code in cards:
        page = REPO / code.doc
        assert storyboard.quoted_lines(page.read_text(encoding="utf-8"), code) == [], (
            board.slug
        )
        assert not any(storyboard.HOST_PORT.search(line) for line in code.lines)


def first_experiment_card() -> storyboard.Code:
    return next(
        code for board, code in code_cards() if board.slug == "02-first-experiment"
    )


def test_a_code_line_the_docs_do_not_have_is_refused():
    code = first_experiment_card()
    page = JS_SDK_DOC.read_text(encoding="utf-8")
    changed = code.model_copy(update={"lines": [code.lines[0].replace("123", "124")]})
    assert "has no line" in storyboard.quoted_lines(page, changed)[0]
    swapped = code.model_copy(update={"lines": [code.lines[-1], code.lines[0]]})
    assert "after the one before it" in storyboard.quoted_lines(page, swapped)[0]
    elsewhere = code.model_copy(update={"section": "Installation"})
    assert storyboard.quoted_lines(page, elsewhere)


@pytest.mark.regression
def test_a_code_card_the_recorded_release_does_not_have_is_refused(tmp_path):
    from showcase.capture import director, run

    board = storyboard.load(STORYBOARDS / config.VIDEOS["02-first-experiment"])
    page = tmp_path / "docs" / "sdk" / "javascript.md"
    page.parent.mkdir(parents=True)
    page.write_text(JS_SDK_DOC.read_text(encoding="utf-8"), encoding="utf-8")
    run.refuse_unquoted_code(board, tmp_path)
    text = page.read_text(encoding="utf-8")
    page.write_text(text.replace("getAssignment(", "assign("), encoding="utf-8")
    with pytest.raises(director.CaptureFailed, match="C10 copy: scene sdk"):
        run.refuse_unquoted_code(board, tmp_path)
    page.unlink()
    with pytest.raises(director.CaptureFailed, match="not in the recorded tree"):
        run.refuse_unquoted_code(board, tmp_path)


# ---------------------------------------------------------------------------
# CLAIMS: each sentence, what is drawn while it is said, and the pages that
# state it
# ---------------------------------------------------------------------------

DESIGN_SLUG = "09-design-in-depth"


class Drawn(NamedTuple):
    """Something the video draws while a sentence is said (QA 2b).

    ``kind`` is where: ``cue`` (the beat's caption holds ``what``), ``testid``
    (the beat's focus, a step or a record names the element ``what``), or
    ``copy`` (the product's fixed copy ``what``, in the frontend file
    ``source``, under frontend/src)."""

    kind: str
    what: str
    source: str = ""


class Claim(NamedTuple):
    scene: str
    beat: int
    drawn: Tuple[Drawn, ...]
    #: ``(file under the repository, words it holds)``, whitespace aside.
    pages: Tuple[Tuple[str, str], ...] = ()


def squeezed(text: str) -> str:
    return " ".join(text.split())


def read_page(relative: str) -> str:
    return (REPO / relative).read_text(encoding="utf-8")


#: A test id a CSS locator names (``[data-testid=x]``).
CSS_TESTID = re.compile(r"data-testid=['\"]?([a-z0-9-]+)")


def beat_testids(beat: storyboard.Beat) -> Set[str]:
    """Every test id the beat's focus, steps (before, on camera and exit) and
    records name, by ``testid`` or inside a CSS selector."""
    locators = [beat.focus, *beat.record.values()]
    for step in [*beat.before, *beat.steps, *beat.exit]:
        locators += storyboard._locators(step)
    found: Set[str] = set()
    for locator in locators:
        if locator.testid:
            found.add(locator.testid)
        found.update(CSS_TESTID.findall(locator.css or ""))
    return found


def drawn_problem(
    scene: storyboard.Scene,
    number: int,
    item: Drawn,
    read: Callable[[str], str] = read_page,
) -> Optional[str]:
    """Why *item* is not drawn at beat *number* of *scene*, or None."""
    beat = scene.beats[number - 1]
    if item.kind == "cue":
        ok = item.what in beat.cue
    elif item.kind == "testid":
        ok = item.what in beat_testids(beat)
    elif item.kind == "copy":
        try:
            ok = squeezed(item.what) in squeezed(read(f"frontend/src/{item.source}"))
        except OSError:
            return f"has no frontend file {item.source!r}"
    else:
        return f"has an unknown kind {item.kind!r}"
    return None if ok else f"does not draw {item.kind} {item.what!r}"


def claims_problems(
    board: storyboard.Storyboard,
    claims: Dict[str, Claim],
    read: Callable[[str], str] = read_page,
) -> List[str]:
    """Every sentence of every scene has its row; each row's sentence is said
    where it says, everything it names is drawn there, and every page it names
    holds its words."""
    problems: List[str] = []
    scenes = {scene.id: scene for scene in board.scenes}
    said = {
        (scene.id, number, sentence)
        for scene in board.scenes
        for number, beat in enumerate(scene.beats, start=1)
        for sentence in beat.say
    }
    for scene_id, number, sentence in sorted(said):
        if sentence not in claims:
            problems.append(
                f"scene {scene_id} beat {number} says {sentence!r}, which has no"
                " CLAIMS row"
            )
    for sentence, claim in claims.items():
        where = f"scene {claim.scene} beat {claim.beat}"
        if (claim.scene, claim.beat, sentence) not in said:
            problems.append(f"{where} does not say {sentence!r}")
            continue
        for item in claim.drawn:
            problem = drawn_problem(scenes[claim.scene], claim.beat, item, read)
            if problem:
                problems.append(f"{where} {problem} while it says {sentence!r}")
        for relative, words in claim.pages:
            try:
                text = read(relative)
            except OSError:
                problems.append(f"{relative} is gone ({sentence!r})")
                continue
            if squeezed(words) not in squeezed(text):
                problems.append(f"{relative} no longer says {words!r} ({sentence!r})")
    return problems


WIZARD_GUIDE = "docs/guides/experiment-wizard.md"
GUIDE = "docs/guides/dashboard-and-api.md"
USER_GUIDE = "docs/guides/user-guide.md"
CONCEPTS = "docs/getting-started/concepts.md"
BAYESIAN = "docs/api/bayesian.md"
SEQUENTIAL = "docs/api/sequential-testing.md"
MEGS = "docs/api/mutual-exclusion-groups.md"
SETTINGS = "components/experiments/new/AnalysisSettingsFields.tsx"
SCHEDULE = "components/experiments/ExperimentScheduleSection.tsx"
HOLDOUTS = "utils/holdouts.ts"
D = "design"

CLAIMS: Dict[str, Claim] = {
    "Choose New Experiment.": Claim(
        D,
        1,
        (Drawn("testid", "new-experiment-btn"),),
        ((WIZARD_GUIDE, "**Experiments → + New Experiment** opens guided setup"),),
    ),
    "Name it, and the key your code uses fills itself in.": Claim(
        D,
        2,
        (Drawn("testid", "experiment-name"), Drawn("testid", "experiment-key")),
        ((WIZARD_GUIDE, "Name, key, description, hypothesis"),),
    ),
    "Make purchases the primary metric: the one you decide on.": Claim(
        D,
        3,
        (Drawn("testid", "metric-name-0"), Drawn("cue", "the one you decide on")),
        ((WIZARD_GUIDE, "one of them primary"),),
    ),
    "Add a second metric to track alongside it: add to cart.": Claim(
        D,
        4,
        (Drawn("testid", "add-metric"), Drawn("testid", "metric-name-1")),
        ((WIZARD_GUIDE, "the metrics (each with its own name)"),),
    ),
    "Add a third version, and split the traffic so it reaches 100%.": Claim(
        D,
        5,
        (Drawn("testid", "add-variant"), Drawn("testid", "variant-allocation-2")),
        ((WIZARD_GUIDE, "their traffic split (it must add up to exactly 100%)"),),
    ),
    "Choose who can join from what your app sends: US or Canada.": Claim(
        D,
        6,
        (Drawn("testid", "condition-attribute"), Drawn("testid", "condition-value")),
        (
            (WIZARD_GUIDE, "and optional targeting rules"),
            (
                CONCEPTS,
                "Targeting rules define which users are eligible for an experiment",
            ),
        ),
    ),
    "Everyone else gets the control, and isn't enrolled.": Claim(
        D,
        6,
        (Drawn("cue", "Choose who can join"),),
        (
            (
                CONCEPTS,
                "a new user who does not match the rules is not assigned: the API"
                ' answers with the control variant and `"assigned": false`, records'
                " no assignment",
            ),
        ),
    ),
    "Next, a confidence level and a correction, which lock once it starts.": Claim(
        D,
        7,
        (Drawn("testid", "analysis-settings"), Drawn("cue", "lock once it starts")),
        ((WIZARD_GUIDE, "cannot be changed once it leaves draft"),),
    ),
    "With several versions, one can look like a winner by luck.": Claim(
        D,
        8,
        (
            Drawn("cue", "Correct for lucky winners"),
            Drawn("copy", "look like a winner by luck", SETTINGS),
        ),
        ((WIZARD_GUIDE, "the correction for several variants"),),
    ),
    "Benjamini-Hochberg, the default, keeps false winners to about 5% of all winners.": Claim(
        D,
        8,
        (Drawn("copy", "at about 5% (at 95% confidence)", SETTINGS),),
        ((WIZARD_GUIDE, "(Benjamini-Hochberg by default, Bonferroni or None)"),),
    ),
    "Turn on Bayesian to also see each version's chance of being best.": Claim(
        D,
        9,
        (
            Drawn("testid", "analysis-bayesian"),
            Drawn(
                "copy",
                "for the primary metric, the probability that each variant is the",
                SETTINGS,
            ),
        ),
        ((BAYESIAN, "**Probability to be best**"),),
    ),
    "Turn on sequential testing, in beta.": Claim(
        D,
        10,
        (
            Drawn("testid", "analysis-sequential"),
            Drawn("cue", "sequential testing (beta)"),
            Drawn("copy", "Also test the primary metric sequentially (beta)", SETTINGS),
        ),
        (
            (SEQUENTIAL, "## Turning it on"),
            (SEQUENTIAL, "tick **Also test the primary metric sequentially (beta)**"),
            (
                GUIDE,
                "| Turn on sequential testing (beta) when creating an experiment |"
                " Dashboard |",
            ),
        ),
    ),
    "Check results whenever you like, and stop once the evidence is strong.": Claim(
        D,
        10,
        (
            Drawn(
                "copy",
                "Check results as often as you like, and stop early once the evidence"
                " is strong.",
                SETTINGS,
            ),
        ),
        (
            (
                SEQUENTIAL,
                "Sequential testing lets you continuously monitor experiment results"
                " and stop as soon as you have enough evidence",
            ),
            (SEQUENTIAL, "stays valid however often you read the results"),
        ),
    ),
    "Review your choices, then create it.": Claim(
        D,
        11,
        (Drawn("testid", "review-variants"), Drawn("testid", "wizard-create")),
        ((WIZARD_GUIDE, "Everything as it will be sent"),),
    ),
    "It's saved as a draft, with its versions, metrics and analysis.": Claim(
        D,
        12,
        (
            Drawn("testid", "experiment-analysis"),
            Drawn("testid", "variants-table"),
            Drawn("testid", "metrics-list"),
        ),
        ((WIZARD_GUIDE, "**Create Experiment** creates it as a draft"),),
    ),
    "Give it a start date, and it starts on its own.": Claim(
        D,
        13,
        (
            Drawn("testid", "schedule-start"),
            Drawn("cue", "it starts on its own"),
            Drawn("copy", "It starts by itself at this time.", SCHEDULE),
        ),
        (
            (USER_GUIDE, "The experiment activates at `start_date`"),
            (
                GUIDE,
                "| Schedule a draft experiment's start and end dates | Dashboard |",
            ),
        ),
    ),
    "Until then, it stays a draft.": Claim(
        D,
        13,
        (Drawn("copy", "Until then it stays a draft", SCHEDULE),),
        ((USER_GUIDE, "stays a draft until it"),),
    ),
    # Narration review condition 1: what the docs say (the mutual-exclusion-groups
    # page's "A global holdout keeps..." line, the guide's row)
    # and the route the page is at; nothing the docs do not contain.
    "A holdout covers all your experiments, so it has its own page.": Claim(
        D,
        14,
        (
            Drawn("testid", "holdout-scope-note"),
            Drawn("copy", "out of EVERY experiment, not one", HOLDOUTS),
        ),
        (
            (MEGS, "A global holdout keeps a percentage of new users out of **all**"),
            (MEGS, "experiments platform-wide"),
            (
                GUIDE,
                "| Global holdouts: create and activate; read their results (beta) |"
                " Dashboard |",
            ),
            (
                "frontend/src/pages/experiments/index.tsx",
                'href="/experiments/holdouts"',
            ),
            (
                "frontend/src/pages/experiments/holdouts.tsx",
                '<PageTitle title="Global holdouts" />',
            ),
        ),
    ),
    "A global holdout keeps 5% of new users out of every experiment.": Claim(
        D,
        15,
        (
            Drawn("testid", "holdout-confirm"),
            Drawn("copy", "% of new users out of every experiment.`", HOLDOUTS),
            Drawn("copy", "export const DEFAULT_SIZE = 5;", HOLDOUTS),
        ),
        (
            (
                MEGS,
                "A global holdout keeps a percentage of new users out of every experiment",
            ),
            (MEGS, "a new holdout's size starts at 5% in the dashboard"),
        ),
    ),
    "That includes this one.": Claim(
        D,
        15,
        (Drawn("copy", "out of EVERY experiment, not one", HOLDOUTS),),
        ((MEGS, "out of **all** experiments platform-wide"),),
    ),
    "It's active now, and its size is fixed.": Claim(
        D,
        16,
        (
            Drawn("testid", "holdout-active-card"),
            Drawn("testid", "holdout-edit"),
            Drawn("testid", "holdout-size-locked"),
            Drawn("copy", "Fixed once a holdout has been active.", HOLDOUTS),
        ),
        ((MEGS, "The percentage cannot change once the holdout has been active"),),
    ),
    "Experiments in the same exclusion group never share a user.": Claim(
        D,
        17,
        (Drawn("testid", "exclusion-group-row"),),
        (
            (MEGS, "Users are assigned to at most one experiment within the group."),
            (
                MEGS,
                "A user enrolled in one experiment of a group is never enrolled in"
                " another experiment of that group",
            ),
        ),
    ),
    "Add this draft to the group with Checkout Button Color.": Claim(
        D,
        18,
        (
            Drawn("testid", "exclusion-group-add-select"),
            Drawn("testid", "exclusion-group-member-list"),
        ),
        (
            (MEGS, "A group's page adds and removes its experiments"),
            (GUIDE, "| Mutual exclusion groups | Dashboard |"),
        ),
    ),
    "Read it back: it's still a draft, in its group.": Claim(
        D,
        19,
        (
            Drawn("testid", "experiment-status"),
            Drawn("testid", "experiment-exclusion-group"),
        ),
        ((GUIDE, "| See on the experiment page its exclusion group | Dashboard |"),),
    ),
    "It has its start date, and sequential testing is on.": Claim(
        D,
        19,
        (
            Drawn("testid", "experiment-start-date"),
            Drawn("testid", "experiment-sequential"),
        ),
        (
            (
                GUIDE,
                "| See on the experiment page whether sequential testing (beta) is on |"
                " Dashboard |",
            ),
            (USER_GUIDE, 'as "Starts" and "Ends"'),
        ),
    ),
    "And the holdout is active.": Claim(
        D,
        19,
        (Drawn("testid", "experiment-holdout"),),
        (
            (
                GUIDE,
                "| See on the experiment page whether a global holdout is active |"
                " Dashboard |",
            ),
        ),
    ),
}


def design_board() -> storyboard.Storyboard:
    return storyboard.load(STORYBOARDS / config.VIDEOS[DESIGN_SLUG])


def design_raw():
    """Video 9's storyboard as written, to change one thing and parse again."""
    return yaml.safe_load(
        (STORYBOARDS / config.VIDEOS[DESIGN_SLUG]).read_text(encoding="utf-8")
    )


def test_every_sentence_of_video_9_has_its_claims_row_and_it_holds():
    made = design_board()
    sentences = [text for _s, _b, _l, text in storyboard.narration_lines(made)]
    assert len(CLAIMS) == len(sentences) == 26
    assert sorted(CLAIMS) == sorted(sentences)
    assert claims_problems(made, CLAIMS) == []


def test_a_sentence_with_no_claims_row_is_refused():
    raw = design_raw()
    raw["scenes"][0]["beats"][12]["say"].append("The scheduler checks it often.")
    assert claims_problems(storyboard.parse(raw), CLAIMS) == [
        "scene design beat 13 says 'The scheduler checks it often.', which has no"
        " CLAIMS row"
    ]


def test_a_claim_whose_drawing_is_gone_is_refused():
    raw = design_raw()
    steps = raw["scenes"][0]["beats"][15]["steps"]
    raw["scenes"][0]["beats"][15]["steps"] = [
        s
        for s in steps
        if "look" not in s or s["look"].get("testid") != "holdout-size-locked"
    ]
    assert claims_problems(storyboard.parse(raw), CLAIMS) == [
        "scene design beat 16 does not draw testid 'holdout-size-locked' while it"
        ' says "It\'s active now, and its size is fixed."'
    ]


def test_a_claim_whose_product_copy_is_gone_is_refused():
    def changed(relative: str) -> str:
        text = read_page(relative)
        if relative == f"frontend/src/{HOLDOUTS}":
            return text.replace("Fixed once a holdout has been active.", "Locked.")
        return text

    assert claims_problems(design_board(), CLAIMS, read=changed) == [
        "scene design beat 16 does not draw copy 'Fixed once a holdout has been"
        " active.' while it says \"It's active now, and its size is fixed.\""
    ]


def test_a_claim_whose_page_no_longer_says_it_is_refused():
    def changed(relative: str) -> str:
        text = read_page(relative)
        if relative == MEGS:
            return text.replace("**all** experiments", "most experiments")
        return text

    assert claims_problems(design_board(), CLAIMS, read=changed) == [
        f"{MEGS} no longer says 'A global holdout keeps a percentage of new users out"
        " of **all**' ('A holdout covers all your experiments, so it has its own"
        " page.')",
        f"{MEGS} no longer says 'out of **all** experiments platform-wide' ('That"
        " includes this one.')",
    ]


def test_the_holdout_pages_line_cites_only_what_the_docs_and_the_route_hold():
    """Narration review condition 1: the mutual-exclusion-groups page's line that
    opens "A global holdout keeps a percentage of new users out of **all**", the
    guide's global-holdouts row, and the /experiments/holdouts route; no row
    claims the docs say the holdout has its own page.

    The line is found by its words, not by its number, so rewrapping the
    paragraphs above it does not move what this test reads."""
    claim = CLAIMS["A holdout covers all your experiments, so it has its own page."]
    cited = [words for _page, words in claim.pages]
    assert not [w for w in cited if "own page" in w]
    lines = [
        line
        for line in read_page(MEGS).splitlines()
        if squeezed(cited[0]) in squeezed(line)
    ]
    assert len(lines) == 1, f"{MEGS}: {len(lines)} lines hold {cited[0]!r}"
    assert squeezed(cited[1]) in squeezed(lines[0])
    assert (REPO / "frontend/src/pages/experiments/holdouts.tsx").is_file()


#: A word video 9 never says or captions (QA 2c): everything it shows is in
#: the dashboard (D182).
API_WORD = re.compile(r"\bAPI\b")


def api_said(made: storyboard.Storyboard) -> List[str]:
    found = []
    for scene in made.scenes:
        for text in storyboard.scene_screen_text(scene):
            if API_WORD.search(text):
                found.append(f"scene {scene.id} says {text!r}")
        for beat in scene.beats:
            found += [
                f"scene {scene.id} says {s!r}" for s in beat.say if API_WORD.search(s)
            ]
    return found


def test_no_sentence_or_caption_of_video_9_says_api():
    assert api_said(design_board()) == []


def test_saying_the_old_api_line_is_refused():
    raw = design_raw()
    raw["scenes"][0]["beats"][11]["say"].append(
        "Guided setup ends here: you do the rest through the API."
    )
    assert api_said(storyboard.parse(raw)) == [
        "scene design says 'Guided setup ends here: you do the rest through the API.'"
    ]


# ---------------------------------------------------------------------------
# Video 10: CLAIMS_10, and the card's note
# ---------------------------------------------------------------------------

VALUES_SLUG = "10-serve-a-value"
CREATE = "docs/feature-flags/create.md"
GO = "docs/sdk/go.md"
FLAG_VALUES = "utils/flagValues.ts"
NEW_FLAG = "pages/feature-flags/new.tsx"
F = "flag"

CLAIMS_10: Dict[str, Claim] = {
    "A flag can serve a value instead of on or off.": Claim(
        F,
        1,
        (Drawn("testid", "new-flag-btn"), Drawn("cue", "A flag can serve a value")),
        (
            (CREATE, "### Optional: serve a value instead of on or off"),
            (
                CREATE,
                "Choose **String**, **Number** or **JSON** to serve a value instead",
            ),
        ),
    ),
    "Choose New Flag.": Claim(
        F,
        1,
        (Drawn("testid", "new-flag-btn"),),
        ((CREATE, "then click **+ New Flag**"),),
    ),
    "Name it, and the key your code uses fills itself in.": Claim(
        F,
        2,
        (Drawn("testid", "flag-name-input"), Drawn("testid", "flag-key-input")),
        (
            (
                CREATE,
                "The identifier your code passes to the SDK. It's filled in from the name",
            ),
        ),
    ),
    "You can change it: here, to the key the docs use.": Claim(
        F,
        2,
        (Drawn("testid", "flag-key-input"), Drawn("cue", "you can change it")),
        (
            (
                CREATE,
                "and you can edit it. Use lowercase letters, digits, `-` and `_`.",
            ),
            (GO, 'client.EvaluateFlag(ctx, "checkout-button-label", user)'),
        ),
    ),
    "Choose its type: a string, a number, or JSON.": Claim(
        F,
        3,
        (
            Drawn("testid", "flag-value-type-choice"),
            Drawn("copy", "['boolean', 'string', 'number', 'json']", FLAG_VALUES),
        ),
        (
            (
                CREATE,
                "Choose **String**, **Number** or **JSON** to serve a value instead",
            ),
        ),
    ),
    "Here, a string: the checkout button label.": Claim(
        F,
        3,
        (
            Drawn("cue", "String"),
            Drawn("copy", "string: 'Text, such as a button label.',", FLAG_VALUES),
        ),
        ((GO, '"checkout-button-label", user) // a string flag'),),
    ),
    "Users it's on for get the on value: Complete purchase.": Claim(
        F,
        4,
        (
            Drawn("testid", "flag-value-fields"),
            Drawn("copy", "on_value: 'On value',", FLAG_VALUES),
            Drawn(
                "copy",
                "'What users the flag is on for get: users who match",
                FLAG_VALUES,
            ),
        ),
        (
            (
                CREATE,
                "| **On value** | What users the flag is on for get: users who match a"
                " targeting rule, and the rollout percentage of everyone else. |",
            ),
        ),
    ),
    "Everyone else gets the off value: Buy now.": Claim(
        F,
        4,
        (
            Drawn("testid", "flag-value-fields"),
            Drawn("copy", "off_value: 'Off value',", FLAG_VALUES),
            Drawn(
                "copy",
                "off_value: 'What everyone else gets, and what everyone gets while the"
                " flag is off.',",
                FLAG_VALUES,
            ),
        ),
        (
            (
                CREATE,
                "| **Off value** | What everyone else gets, and what everyone gets while"
                " the flag is off. |",
            ),
        ),
    ),
    "Add a rule: users on the pro plan get the on value.": Claim(
        F,
        5,
        (
            Drawn("testid", "add-group"),
            Drawn("testid", "condition-attribute"),
            Drawn("testid", "condition-value"),
        ),
        (
            (CREATE, "If a user matches a rule, the flag is on for them."),
            (CREATE, "users who match a targeting rule"),
        ),
    ),
    "With the rollout at 0%, no one else gets the on value.": Claim(
        F,
        6,
        (
            Drawn("testid", "rollout-value-hint"),
            Drawn("cue", "Rollout at 0%"),
            Drawn(
                "copy",
                "'The share of users who match no targeting rule that get the on value."
                " Everyone else gets the off value.'",
                FLAG_VALUES,
            ),
            Drawn(
                "copy",
                "const [rolloutPercentage, setRolloutPercentage] = useState(0);",
                NEW_FLAG,
            ),
        ),
        ((CREATE, "| `0%` | Only users who match a targeting rule get the flag |"),),
    ),
    "Choose Create Feature Flag.": Claim(
        F,
        6,
        (
            Drawn("testid", "submit-flag"),
            Drawn(
                "copy",
                "{isSubmitting ? 'Creating...' : 'Create Feature Flag'}",
                NEW_FLAG,
            ),
        ),
        ((CREATE, "Click **Create Feature Flag**."),),
    ),
    "It's created switched off, so everyone gets the off value.": Claim(
        F,
        7,
        (
            Drawn("cue", "everyone gets the off value"),
            Drawn(
                "copy",
                "VALUE_FLAG_STATUS_OFF = 'Everyone gets the off value';",
                FLAG_VALUES,
            ),
            Drawn("copy", "is_active: false,", NEW_FLAG),
        ),
        (
            (
                CREATE,
                "A new flag starts off, so everyone gets its off value until you turn it on.",
            ),
            (CREATE, "reads *Everyone gets the off value* while it is off"),
        ),
    ),
    "Switch it on: rules and rollout decide who gets the on value.": Claim(
        F,
        8,
        (
            Drawn("testid", "flag-toggle"),
            Drawn(
                "copy",
                "VALUE_FLAG_STATUS_ON = 'Rules and rollout decide who gets the on value';",
                FLAG_VALUES,
            ),
        ),
        ((CREATE, "*Rules and rollout decide who gets the on value* while it is on"),),
    ),
    "In your app, the Go SDK reads the value for each user.": Claim(
        "sdk",
        1,
        (Drawn("cue", "Your app reads the value"),),
        (
            (GO, "`EvalResult.Value` is what the flag serves this user"),
            (GO, "Read the value with the method for its type:"),
        ),
    ),
    "Without an answer, it returns the default you give it.": Claim(
        "sdk",
        1,
        (Drawn("cue", "with a default"),),
        (
            (GO, "`StringValue` and `NumberValue` return your default"),
            (GO, "a nil result, a failed evaluation"),
            (
                GO,
                "Against a server that sends no `value`, `StringValue` returns your default.",
            ),
        ),
    ),
}


def values_board() -> storyboard.Storyboard:
    return storyboard.load(STORYBOARDS / config.VIDEOS[VALUES_SLUG])


def values_raw():
    return yaml.safe_load(
        (STORYBOARDS / config.VIDEOS[VALUES_SLUG]).read_text(encoding="utf-8")
    )


def test_every_sentence_of_video_10_has_its_claims_row_and_it_holds():
    made = values_board()
    sentences = [text for _s, _b, _l, text in storyboard.narration_lines(made)]
    assert len(CLAIMS_10) == len(sentences) == 15
    assert sorted(CLAIMS_10) == sorted(sentences)
    assert claims_problems(made, CLAIMS_10) == []


def test_a_video_10_sentence_with_no_claims_row_is_refused():
    raw = values_raw()
    raw["scenes"][0]["beats"][5]["say"][0] = (
        "With the rollout at 0%, nobody else gets it."
    )
    assert claims_problems(storyboard.parse(raw), CLAIMS_10) == [
        "scene flag beat 6 says 'With the rollout at 0%, nobody else gets it.', which"
        " has no CLAIMS row",
        "scene flag beat 6 does not say 'With the rollout at 0%, no one else gets the on"
        " value.'",
    ]


def test_a_video_10_claim_whose_drawing_is_gone_is_refused():
    raw = values_raw()
    raw["scenes"][0]["beats"][5]["steps"] = [{"look": {"testid": "rollout-percentage"}}]
    assert claims_problems(storyboard.parse(raw), CLAIMS_10) == [
        "scene flag beat 6 does not draw testid 'rollout-value-hint' while it says"
        " 'With the rollout at 0%, no one else gets the on value.'"
    ]


def test_a_video_10_claim_whose_page_no_longer_says_it_is_refused():
    def changed(relative: str) -> str:
        text = read_page(relative)
        if relative == GO:
            return text.replace("return your default", "panic")
        return text

    assert claims_problems(values_board(), CLAIMS_10, read=changed) == [
        f"{GO} no longer says '`StringValue` and `NumberValue` return your default'"
        " ('Without an answer, it returns the default you give it.')"
    ]


def test_a_video_10_claim_whose_product_copy_is_gone_is_refused():
    def changed(relative: str) -> str:
        text = read_page(relative)
        if relative == f"frontend/src/{FLAG_VALUES}":
            return text.replace("'Everyone gets the off value'", "'Off for everyone'")
        return text

    assert claims_problems(values_board(), CLAIMS_10, read=changed) == [
        "scene flag beat 7 does not draw copy \"VALUE_FLAG_STATUS_OFF = 'Everyone gets"
        " the off value';\" while it says \"It's created switched off, so everyone gets"
        ' the off value."'
    ]


#: What the card's note says about installing, and the words of go.md it rests on.
GO_INSTALL = (
    "latest release",
    "`go get` resolves the module's latest release and records it in your `go.mod`",
)


def test_video_10s_card_note_says_what_go_md_says_about_installing():
    card = values_board().scenes[1].card
    assert card.code.doc == GO
    assert GO_INSTALL[0] in card.note
    assert squeezed(GO_INSTALL[1]) in squeezed(read_page(GO))


def test_an_untagged_go_module_makes_the_note_false():
    """The plant: go.md says go get takes the head of main, as it did before
    the module's first tag; the note no longer holds."""
    page = read_page(GO).replace(
        "resolves the module's latest release", "resolves the head of the `main` branch"
    )
    assert page != read_page(GO)
    assert squeezed(GO_INSTALL[1]) not in squeezed(page)


# ---------------------------------------------------------------------------
# Video 11: CLAIMS_11
# ---------------------------------------------------------------------------

STALE_SLUG = "11-stale-flags"
STALE_DOC = "docs/feature-flags/stale-flags.md"
CAPABILITIES = "docs/capabilities.md"
QUICK_START = "docs/getting-started/quick-start.md"
FLAG_LIST = "pages/feature-flags/index.tsx"
FL = "flags"
#: The list draws N as the API's stale_days, never a constant of its own.
N_FROM_THE_API = Drawn("copy", "setStaleDays(data.stale_days);", FLAG_LIST)

CLAIMS_11: Dict[str, Claim] = {
    "The flag list marks flags that look ready to remove.": Claim(
        FL,
        1,
        (
            Drawn("testid", "flags-table"),
            Drawn("copy", 'data-testid="flag-stale-pill"', FLAG_LIST),
            Drawn("copy", "{flag.stale === true && (", FLAG_LIST),
        ),
        (
            (
                CAPABILITIES,
                "the flag list marks a flag that looks ready to remove, and says why",
            ),
        ),
    ),
    "A flag must be at least 30 days old to be stale.": Claim(
        FL,
        1,
        (Drawn("testid", "flags-table"), N_FROM_THE_API),
        (
            (STALE_DOC, "A flag is stale when it was created at least N days ago"),
            (STALE_DOC, "N is 30 days by default"),
        ),
    ),
    "The demo's own flags are new, so they are not stale.": Claim(
        FL,
        1,
        (Drawn("testid", "flags-table"), Drawn("testid", "flag-row")),
        (
            (QUICK_START, "applies the `demo` seed (three experiments, two flags)"),
            (STALE_DOC, "A flag is stale when it was created at least N days ago"),
        ),
    ),
    "Each stale flag says why, under its Stale pill.": Claim(
        FL,
        1,
        (
            Drawn("copy", 'data-testid="flag-stale-reasons"', FLAG_LIST),
            Drawn("copy", "staleReasonText(reason, staleDays)", FLAG_LIST),
        ),
        ((STALE_DOC, "The flag list marks such a flag as stale and says why."),),
    ),
    "Legacy signup form is on for everyone, unchanged for 30 days.": Claim(
        FL,
        2,
        (
            Drawn("testid", "flag-stale-reasons"),
            Drawn("cue", "on for everyone, unchanged for 30 days"),
            Drawn(
                "copy", "return `On for everyone, unchanged for ${n} days`;", FLAG_LIST
            ),
            N_FROM_THE_API,
        ),
        (
            (
                STALE_DOC,
                "| `on_for_everyone` | The flag is on at 100% with no targeting rules,"
                " and unchanged for N days. |",
            ),
        ),
    ),
    "Everyone gets one answer, so it's usually ready to come out.": Claim(
        FL,
        2,
        (Drawn("testid", "flag-row"),),
        (
            (
                STALE_DOC,
                "A flag that has served everyone the same answer for weeks, or that"
                " nothing has asked about, is usually ready to come out of your code.",
            ),
        ),
    ),
    "Holiday banner is off for everyone, unchanged for 30 days.": Claim(
        FL,
        3,
        (
            Drawn("testid", "flag-stale-reasons"),
            Drawn("cue", "left unchanged"),
            Drawn(
                "copy", "return `Off for everyone, unchanged for ${n} days`;", FLAG_LIST
            ),
            N_FROM_THE_API,
        ),
        (
            (
                STALE_DOC,
                "| `off_for_everyone` | The flag is off, or on at 0% with no targeting"
                " rules, and unchanged for N days. |",
            ),
        ),
    ),
    "The count starts at its last change, its Updated date.": Claim(
        FL,
        3,
        (
            Drawn("testid", "flag-row"),
            Drawn("copy", ">Updated</th>", FLAG_LIST),
            Drawn("copy", "{formatDate(flag.updated_at)}", FLAG_LIST),
        ),
        (
            (STALE_DOC, "The count starts from the flag's `updated_at`."),
            (STALE_DOC, "every change to a flag sets it"),
        ),
    ),
    "Old search ranking: not evaluated in 30 days, while others were.": Claim(
        FL,
        4,
        (
            Drawn("testid", "flag-stale-reasons"),
            Drawn("cue", "not evaluated in 30 days"),
            Drawn("copy", "return `Not evaluated in ${n} days`;", FLAG_LIST),
            N_FROM_THE_API,
        ),
        (
            (
                STALE_DOC,
                "| `not_evaluated` | No evaluation of the flag was recorded in the last N"
                " days, while evaluations of other flags were. |",
            ),
        ),
    ),
    "That means no evaluation was recorded, not that no code uses it.": Claim(
        FL,
        4,
        (Drawn("testid", "flag-stale-reasons"),),
        (
            (
                STALE_DOC,
                "Not evaluated means no evaluation was recorded, not that no code uses"
                " the flag.",
            ),
        ),
    ),
    "Press Stale to list only the flags marked stale.": Claim(
        FL,
        5,
        (
            Drawn("testid", "flag-stale-filter"),
            Drawn("copy", "{ label: 'Stale', value: 'stale' },", FLAG_LIST),
            Drawn(
                "copy",
                "...(staleFilter === 'stale' ? { stale: true } : {}),",
                FLAG_LIST,
            ),
        ),
        (
            (
                STALE_DOC,
                "`GET /api/v1/feature-flags/?stale=true` lists only the stale flags",
            ),
        ),
    ),
    "Stale is a suggestion: nothing is changed or archived for you.": Claim(
        FL,
        6,
        (
            Drawn("testid", "flag-stale-hint"),
            Drawn("cue", "nothing is changed or archived for you"),
            Drawn(
                "copy",
                "Stale is a suggestion: nothing is changed or archived for you.",
                FLAG_LIST,
            ),
        ),
        (
            (
                STALE_DOC,
                "It is a suggestion: listing changes no flag, and nothing is archived or"
                " turned off for you.",
            ),
        ),
    ),
    "A flag tagged permanent is never stale.": Claim(
        FL,
        6,
        (
            Drawn("testid", "flag-stale-hint"),
            Drawn("copy", "A flag tagged permanent is never stale;", FLAG_LIST),
        ),
        ((STALE_DOC, "A flag tagged `permanent` is never stale."),),
    ),
    "Tags are set through the API.": Claim(
        FL,
        6,
        (
            Drawn("testid", "flag-stale-hint"),
            Drawn("copy", "tags are set through the API.", FLAG_LIST),
        ),
        ((STALE_DOC, "Tags are set through the API"),),
    ),
    "Listing the stale flags changes none of them.": Claim(
        FL,
        6,
        (
            Drawn("testid", "flags-table"),
            Drawn("copy", "nothing is changed or archived for you", FLAG_LIST),
        ),
        (
            (STALE_DOC, "Listing changes no flag."),
            (CAPABILITIES, "listing changes no flag"),
        ),
    ),
}


def stale_board() -> storyboard.Storyboard:
    return storyboard.load(STORYBOARDS / config.VIDEOS[STALE_SLUG])


def stale_raw():
    return yaml.safe_load(
        (STORYBOARDS / config.VIDEOS[STALE_SLUG]).read_text(encoding="utf-8")
    )


def test_every_sentence_of_video_11_has_its_claims_row_and_it_holds():
    made = stale_board()
    sentences = [text for _s, _b, _l, text in storyboard.narration_lines(made)]
    assert len(CLAIMS_11) == len(sentences) == 15
    assert sorted(CLAIMS_11) == sorted(sentences)
    assert claims_problems(made, CLAIMS_11) == []


def test_a_video_11_sentence_with_no_claims_row_is_refused():
    raw = stale_raw()
    raw["scenes"][0]["beats"][3]["say"].append("Nothing will ever ask for it again.")
    assert claims_problems(storyboard.parse(raw), CLAIMS_11) == [
        "scene flags beat 4 says 'Nothing will ever ask for it again.', which has no"
        " CLAIMS row"
    ]


def test_a_video_11_claim_whose_drawing_is_gone_is_refused():
    raw = stale_raw()
    raw["scenes"][0]["beats"][4]["focus"] = {
        "role": "button",
        "name": "Stale",
        "exact": True,
    }
    assert claims_problems(storyboard.parse(raw), CLAIMS_11) == [
        "scene flags beat 5 does not draw testid 'flag-stale-filter' while it says"
        " 'Press Stale to list only the flags marked stale.'"
    ]


def test_a_video_11_claim_whose_page_no_longer_says_it_is_refused():
    def changed(relative: str) -> str:
        text = read_page(relative)
        if relative == STALE_DOC:
            return text.replace("`permanent` is never stale.", "`keep` is never stale.")
        return text

    assert claims_problems(stale_board(), CLAIMS_11, read=changed) == [
        f"{STALE_DOC} no longer says 'A flag tagged `permanent` is never stale.'"
        " ('A flag tagged permanent is never stale.')"
    ]


def test_a_video_11_claim_whose_product_copy_is_gone_is_refused():
    def changed(relative: str) -> str:
        text = read_page(relative)
        if relative == f"frontend/src/{FLAG_LIST}":
            return text.replace(
                "return `Not evaluated in ${n} days`;", "return `Unused for ${n} days`;"
            )
        return text

    assert claims_problems(stale_board(), CLAIMS_11, read=changed) == [
        "scene flags beat 4 does not draw copy 'return `Not evaluated in ${n} days`;'"
        " while it says 'Old search ranking: not evaluated in 30 days, while others"
        " were.'"
    ]


def test_the_stale_guides_reasons_table_is_the_one_the_board_derives_from():
    """``storyboard.derived_stale`` applies the guide's table; each row is
    here, word for word, with the order the reasons are listed in and the
    permanent tag's rule, so a docs-only change to the table meets the board
    (test_showcase_stale_flags.py holds the derivation to the server's)."""
    page = squeezed(read_page(STALE_DOC))
    for words in (
        "| Reason | When |",
        "`stale_reasons` lists every reason that applies, in that order.",
        "A flag tagged `permanent` is never stale. The tag must be exactly"
        " permanent; case and surrounding spaces are ignored.",
        "N is 30 days by default; `STALE_FLAG_DAYS` sets it",
    ):
        assert squeezed(words) in page, words
    order = re.findall(r"^\| `([a-z_]+)` \|", read_page(STALE_DOC), re.MULTILINE)
    assert tuple(order) == storyboard.STALE_REASONS


# ---------------------------------------------------------------------------
# Video 12: CLAIMS_12, and the card's key
# ---------------------------------------------------------------------------

BALANCE_SLUG = "12-balance-within"
BALANCE_GUIDE = "docs/guides/balance-within-groups.md"
BALANCE_WORDS = "components/experiments/new/balanceWithin.ts"
BALANCE_FIELDS = "components/experiments/new/BalanceWithinFields.tsx"
BASIC_FIELDS = "components/experiments/new/BasicInfoFields.tsx"
FORM_STATE = "components/experiments/new/formState.ts"
WIZARD = "components/experiments/new/Wizard.tsx"
DETAIL = "pages/experiments/[id].tsx"
B = "design"

CLAIMS_12: Dict[str, Claim] = {
    "An experiment can balance its split within groups of users.": Claim(
        B,
        1,
        (
            Drawn("cue", "Balance a split within groups"),
            Drawn("testid", "new-experiment-btn"),
        ),
        (
            (
                BALANCE_GUIDE,
                "An experiment that balances its split within groups gives each group"
                " of users the same split as the whole experiment",
            ),
        ),
    ),
    "Choose New Experiment.": Claim(
        B,
        1,
        (Drawn("testid", "new-experiment-btn"),),
        ((WIZARD_GUIDE, "**Experiments → + New Experiment** opens guided setup"),),
    ),
    "Name it, and the key your code uses fills itself in.": Claim(
        B,
        2,
        (
            Drawn("testid", "experiment-name"),
            Drawn("testid", "experiment-key"),
            Drawn("copy", "Auto-generated from the name.", BASIC_FIELDS),
        ),
        ((WIZARD_GUIDE, "Name, key, description, hypothesis"),),
    ),
    "You can change it: here, to the key the docs use.": Claim(
        B,
        2,
        (Drawn("testid", "experiment-key"), Drawn("cue", "you can change it")),
        ((BALANCE_GUIDE, '{"experiment_key": "pricing-test", "user_id": "u-123"'),),
    ),
    "Make purchases the primary metric: the one you decide on.": Claim(
        B,
        3,
        (Drawn("testid", "metric-name-0"), Drawn("cue", "the one you decide on")),
        ((WIZARD_GUIDE, "one of them primary"),),
    ),
    "At 50/50, turn on Balance the split within groups.": Claim(
        B,
        4,
        (
            Drawn("testid", "balance-switch"),
            Drawn("cue", "Turn on Balance the split within groups"),
            Drawn(
                "copy",
                "export const BALANCE_SWITCH_LABEL = 'Balance the split within groups';",
                BALANCE_WORDS,
            ),
            Drawn(
                "copy",
                "{ name: 'Control', description: '', traffic_allocation: 50,"
                " is_control: true },",
                FORM_STATE,
            ),
            Drawn(
                "copy",
                "{ name: 'Treatment', description: '', traffic_allocation: 50,"
                " is_control: false },",
                FORM_STATE,
            ),
        ),
        (
            (
                BALANCE_GUIDE,
                "The **Variants** step of guided setup and the single-page form have"
                " **Balance the split within groups**, off until you turn it on.",
            ),
            (BALANCE_GUIDE, "(50/50, 20/80, 35/35/30 and 40/30/30 are allowed)"),
        ),
    ),
    "Each group gets the same split as the whole experiment.": Claim(
        B,
        4,
        (
            Drawn("testid", "balance-switch"),
            Drawn(
                "copy",
                "export const BALANCE_HELPER = 'Each group gets the same split as the"
                " whole experiment.';",
                BALANCE_WORDS,
            ),
        ),
        (
            (
                BALANCE_GUIDE,
                "gives each group of users the same split as the whole experiment",
            ),
        ),
    ),
    "Group users by another attribute: tier.": Claim(
        B,
        5,
        (
            Drawn("testid", "balance-group-by"),
            Drawn("testid", "balance-custom-key"),
            Drawn(
                "copy",
                "export const BALANCE_GROUP_BY_LABEL = 'Group users by';",
                BALANCE_WORDS,
            ),
            Drawn(
                "copy",
                '<option value="custom">Another attribute…</option>',
                BALANCE_FIELDS,
            ),
        ),
        (
            (
                BALANCE_GUIDE,
                "**Group users by** offers Platform, Country and User Tier, **Another"
                " attribute…** (type its name)",
            ),
            (
                BALANCE_GUIDE,
                "`attribute` is a top-level key of the assign `context`, such as `tier`",
            ),
        ),
    ),
    "Then add the values to keep apart, one at a time.": Claim(
        B,
        5,
        (
            Drawn("testid", "balance-value-input"),
            Drawn("testid", "balance-value-add"),
            Drawn("testid", "balance-values"),
            Drawn(
                "copy",
                "export const BALANCE_VALUES_LABEL = 'Values to keep apart (up to 20)';",
                BALANCE_WORDS,
            ),
        ),
        (
            (
                BALANCE_GUIDE,
                '{"balance_within": {"attribute": "tier", "values": ["high_value",'
                ' "churn_risk", "new"]}}',
            ),
            (
                BALANCE_GUIDE,
                "**Values to keep apart (up to 20)** takes one chip per value",
            ),
        ),
    ),
    "Review shows what the split is balanced within, then create it.": Claim(
        B,
        6,
        (
            Drawn("testid", "review-balance"),
            Drawn("testid", "wizard-create"),
            Drawn(
                "copy",
                "Balance: {balanceSummary(payload.balance_within ?? null, segmentNames)}",
                WIZARD,
            ),
        ),
        (
            (
                WIZARD_GUIDE,
                "the Review step's **Variants** row ends **Balance: within**",
            ),
            (WIZARD_GUIDE, "**Create Experiment** creates it as a draft"),
        ),
    ),
    "It's saved as a draft, balanced within tier and its values.": Claim(
        B,
        7,
        (
            Drawn("testid", "experiment-status"),
            Drawn("testid", "experiment-balance"),
            Drawn("cue", "Saved as a draft, balanced within tier"),
            Drawn(
                "copy",
                '<dd data-testid="experiment-balance">{balanceSummary(setting ?? null,'
                " names)}</dd>",
                DETAIL,
            ),
        ),
        (
            (WIZARD_GUIDE, "**Create Experiment** creates it as a draft"),
            (
                BALANCE_GUIDE,
                "The experiment page's **Balance** row says what the split is balanced"
                " within, such as `within Platform (ios, android, web)`",
            ),
        ),
    ),
    "In your app, send tier at the top level of the context.": Claim(
        "assign",
        1,
        (Drawn("cue", "Send each user's tier"),),
        (
            (
                BALANCE_GUIDE,
                "Put the attribute at the top level of `context` in every assign request",
            ),
        ),
    ),
    "A user without it is balanced as unknown.": Claim(
        "assign",
        1,
        (
            Drawn("cue", "or they are balanced as unknown"),
            Drawn("copy", "and users without the attribute as unknown.", BALANCE_WORDS),
        ),
        (
            (BALANCE_GUIDE, "and a user without the key in `unknown`"),
            (BALANCE_GUIDE, "so that user is balanced in `unknown`"),
        ),
    ),
}


def balance_board() -> storyboard.Storyboard:
    return storyboard.load(STORYBOARDS / config.VIDEOS[BALANCE_SLUG])


def balance_raw():
    return yaml.safe_load(
        (STORYBOARDS / config.VIDEOS[BALANCE_SLUG]).read_text(encoding="utf-8")
    )


def test_every_sentence_of_video_12_has_its_claims_row_and_it_holds():
    made = balance_board()
    sentences = [text for _s, _b, _l, text in storyboard.narration_lines(made)]
    assert len(CLAIMS_12) == len(sentences) == 13
    assert sorted(CLAIMS_12) == sorted(sentences)
    assert claims_problems(made, CLAIMS_12) == []


def test_a_video_12_sentence_with_no_claims_row_is_refused():
    raw = balance_raw()
    raw["scenes"][0]["beats"][3]["say"].append("Small groups are merged.")
    assert claims_problems(storyboard.parse(raw), CLAIMS_12) == [
        "scene design beat 4 says 'Small groups are merged.', which has no CLAIMS row"
    ]


def test_a_video_12_claim_whose_drawing_is_gone_is_refused():
    """The Review beat made to look only at the variants: its focus, its look
    and its record of the balance row gone (and the end state's comparison of
    it, which the loader would otherwise refuse)."""
    raw = balance_raw()
    review = raw["scenes"][0]["beats"][5]
    review["focus"] = {"css": "div.py-3:has([data-testid=review-variants])"}
    review["steps"] = [{"look": {"testid": "review-variants"}, "ms": 1000}]
    review["record"].pop("review-balance")
    raw["end_state"]["design"]["shown"].pop("review_balance")
    assert claims_problems(storyboard.parse(raw), CLAIMS_12) == [
        "scene design beat 6 does not draw testid 'review-balance' while it says"
        " 'Review shows what the split is balanced within, then create it.'"
    ]


def test_a_video_12_claim_whose_page_no_longer_says_it_is_refused():
    def changed(relative: str) -> str:
        text = read_page(relative)
        if relative == BALANCE_GUIDE:
            return text.replace(
                "in every assign request", "in the first assign request"
            )
        return text

    assert claims_problems(balance_board(), CLAIMS_12, read=changed) == [
        f"{BALANCE_GUIDE} no longer says 'Put the attribute at the top level of"
        " `context` in every assign request' ('In your app, send tier at the top level"
        " of the context.')"
    ]


def test_a_video_12_claim_whose_product_copy_is_gone_is_refused():
    def changed(relative: str) -> str:
        text = read_page(relative)
        if relative == f"frontend/src/{BALANCE_WORDS}":
            return text.replace(
                "'Balance the split within groups'", "'Balance within groups'"
            )
        return text

    assert claims_problems(balance_board(), CLAIMS_12, read=changed) == [
        'scene design beat 4 does not draw copy "export const BALANCE_SWITCH_LABEL ='
        " 'Balance the split within groups';\" while it says 'At 50/50, turn on"
        " Balance the split within groups.'"
    ]


#: The key in the card's line: the guide's, the one typed on camera.
EXPERIMENT_KEY = re.compile(r'"experiment_key": "([^"]+)"')


def test_video_12s_card_is_the_guides_assign_line_with_the_key_typed_on_camera():
    made = balance_board()
    card = made.scenes[1].card
    assert (card.label, card.code.doc, card.code.section) == (
        "From the guide",
        BALANCE_GUIDE,
        "Assign users",
    )
    (line,) = card.code.lines
    assert EXPERIMENT_KEY.findall(line) == [made.end_state.design.key]
    assert '"context": {"tier": "high_value"}' in line
    assert made.end_state.design.balance_within.attribute == "tier"
    assert storyboard.quoted_lines(read_page(BALANCE_GUIDE), card.code) == []


def test_a_guide_whose_assign_line_names_another_key_refuses_the_card():
    """The plant: the guide's block keyed otherwise; the card no longer quotes it."""
    card = balance_board().scenes[1].card
    page = read_page(BALANCE_GUIDE).replace(
        '{"experiment_key": "pricing-test", "user_id"',
        '{"experiment_key": "price-test", "user_id"',
    )
    assert page != read_page(BALANCE_GUIDE)
    assert "has no line" in storyboard.quoted_lines(page, card.code)[0]
