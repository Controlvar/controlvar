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
    # Narration review condition 1: what the docs say (md:189, the guide's row)
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
    """Narration review condition 1: md:189, the guide's global-holdouts row, and
    the /experiments/holdouts route; no row claims the docs say the holdout has
    its own page."""
    claim = CLAIMS["A holdout covers all your experiments, so it has its own page."]
    cited = [words for _page, words in claim.pages]
    assert not [w for w in cited if "own page" in w]
    line = read_page(MEGS).splitlines()[188]
    assert squeezed(cited[0]) in squeezed(line) and squeezed(cited[1]) in squeezed(line)
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
