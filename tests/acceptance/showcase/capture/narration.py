"""What a narration may say (#1066 voiceover): gates G1 and G3, and G2's comparison.

A beat's ``say:`` sentences are sent to Amazon Polly and published as the
video's captions and transcript, so they are fixed, checked text. They are
checked when the storyboard loads (``storyboard.parse``) and again by the voice
step before it sends anything (``showcase.voice``), with the same function:

* G1, per sentence: one caption's worth of plain text ending in ``.``, ``?`` or
  ``!``; numbers in digits, as the page prints them (a number word is refused
  unless the beat lists it under ``claims:`` with a page of the docs); no host,
  port, address, ``localhost``, ``127.0.0.1`` or URL other than
  controlvar.com; nothing from ``config.FORBIDDEN_TEXT`` or the
  storyboard's ``never_show``; no secret value (the static needles, compared
  as the OCR scan compares them with punctuation removed as well, and
  ``eptk_``); no template; "live" only as "the
  Live tab", and only with a Refresh click in that beat or the next; never
  "real time", "instantly", "immediately" or "as it happens", nor the words the
  UX rules never say.
* G3: every number in the beat's headline (``cue``) is also spoken.
* G2's comparison (``number_key``): numbers as written, except that a fraction
  of zeros is ignored (``15%`` is ``15.0%``) and commas count only as digit
  grouping (``31,234`` is ``31234``). The page half runs at capture.

A storyboard narrates every beat or none. One that narrates none loads as it
always has, and its capture is unchanged by this module.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, List, Sequence

from showcase import contract
from showcase.capture import config, guard
from showcase.capture.storyboard import (
    HOST_PORT,
    Click,
    Storyboard,
    caption_problem,
    numbers_in,
)

#: Numbers said as words, which no page check can see. "one" is not here: it is
#: a pronoun in ordinary speech ("the completed one"), and the plan accepts the
#: number it can hide.
NUMBER_WORDS = (
    "zero",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "seventy",
    "eighty",
    "ninety",
    "hundred",
    "thousand",
    "million",
    "half",
    "halves",
    "quarter",
    "double",
    "twice",
    "dozen",
)
NUMBER_WORD = re.compile(r"\b(?:" + "|".join(NUMBER_WORDS) + r")s?\b", re.IGNORECASE)

#: What the UX rules never say: claims the video cannot show, and marketing words.
NEVER_SAY = (
    "real time",
    "real-time",
    "realtime",
    "instantly",
    "immediately",
    "as it happens",
    "as they happen",
    "simply",
    "just",
    "easy",
    "easily",
    "seamless",
    "seamlessly",
    "secure",
    "securely",
    "compliant",
    "enterprise",
    "free",
    # The founder's name.
    "ashish",
    "markanday",
    # Other products.
    "optimizely",
    "launchdarkly",
    "statsig",
    "growthbook",
    "eppo",
    "posthog",
    "flagsmith",
    "configcat",
    "unleash",
    "amplitude",
    "split.io",
    "vwo",
    "ab tasty",
    "kameleoon",
    "devcycle",
)
_NEVER_SAY = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in NEVER_SAY) + r")\b", re.IGNORECASE
)

#: "Live" is the Live tab's name, and nothing else: the page shows new users
#: only when Refresh is clicked.
LIVE_TAB = "the Live tab"
_LIVE = re.compile(r"\blive\b", re.IGNORECASE)

#: The tool's own ports, on top of the reserved ones a developer's stack uses.
NAMED_PORTS = config.RESERVED_PORTS + (
    config.DEFAULT_API_PORT,
    config.DEFAULT_DASHBOARD_PORT,
    config.DEFAULT_POSTGRES_PORT,
)
_PORT_WORD = re.compile(r"\bports?\s+[0-9]", re.IGNORECASE)
_LOCALHOST = re.compile(r"\blocalhost\b", re.IGNORECASE)
_URL = re.compile(r"https?:|://|\bwww\.", re.IGNORECASE)
_DOMAIN = re.compile(
    r"\b[a-z0-9-]{1,63}\.(?:com|io|dev|net|org|ai|app|co)\b", re.IGNORECASE
)
#: The one address a narration may say (the end card's, if it is ever spoken).
SITE = "controlvar.com"
_ENDS = re.compile(r"[.?!]$")
_INNER_END = re.compile(r"[.?!]\s")
#: An API key's prefix: no narration says one, even in part.
KEY_PREFIX = "eptk_"

_GROUPED = re.compile(r"^[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?%?$")
_ZERO_FRACTION = re.compile(r"\.0+(?=%?$)")


def number_key(number: str) -> str:
    """A number as G2 and G3 compare it: as written, but ``15.0%`` is ``15%`` and
    ``31,234`` is ``31234`` (commas count only as digit grouping)."""
    text = number.replace(",", "") if _GROUPED.match(number) else number
    return _ZERO_FRACTION.sub("", text)


def _fixed_value_form(text: str) -> str:
    """*text* as the narration check compares fixed values: ``contract.normalise``
    with punctuation removed too, on both sides, so a value said without its
    punctuation ("Demo1234.") is still found."""
    return re.sub(r"[^0-9a-z]", "", contract.normalise(text))


def _words_unclaimed(sentence: str, claimed: Sequence[str]) -> str:
    """*sentence* with every claimed phrase blanked out."""
    for phrase in claimed:
        sentence = sentence.replace(phrase, " " * len(phrase))
    return sentence


def sentence_problems(
    sentence: Any,
    *,
    forbidden: Sequence[str] = config.FORBIDDEN_TEXT,
    claimed: Sequence[str] = (),
    refresh: bool = False,
) -> List[str]:
    """G1 for one sentence: everything wrong with it, or nothing.

    *forbidden* is the text no page of the video may show; *claimed* the beat's
    claimed phrases; *refresh* whether the beat or the next clicks Refresh.
    """
    if not isinstance(sentence, str) or not sentence.strip():
        return ["is empty"]
    problems: List[str] = []
    problem = caption_problem(sentence)
    if problem:
        problems.append(problem)
    if not _ENDS.search(sentence):
        problems.append("does not end with '.', '?' or '!'")
    if _INNER_END.search(sentence):
        problems.append("holds more than one sentence: give each its own line")
    word = NUMBER_WORD.search(_words_unclaimed(sentence, claimed))
    if word:
        problems.append(
            f"says {word.group(0)!r} as a word: write the number as the page prints"
            " it, or list the phrase under claims with its page of the docs"
        )
    lowered = sentence.lower()
    if (
        _LOCALHOST.search(sentence)
        or "127.0.0.1" in sentence
        or HOST_PORT.search(sentence)
        or _PORT_WORD.search(sentence)
        or guard.reserved_in(sentence, NAMED_PORTS)
    ):
        problems.append("names a host or a port")
    if _URL.search(sentence) or any(
        m.group(0).lower() != SITE for m in _DOMAIN.finditer(sentence)
    ):
        problems.append(f"names an address other than {SITE}")
    if "@" in sentence:
        problems.append("names an email address")
    for text in forbidden:
        if text and text.lower() in lowered:
            problems.append(f"says {text!r}, which the video must never show")
    squeezed = _fixed_value_form(sentence)
    for number, needle in enumerate(config.STATIC_NEEDLES, start=1):
        if _fixed_value_form(needle) in squeezed:
            problems.append(
                f"holds a secret value (static needle {number} of {len(config.STATIC_NEEDLES)})"
            )
    if KEY_PREFIX in lowered:
        problems.append("holds an API key's prefix")
    if LIVE_TAB in sentence and not refresh:
        problems.append(
            f"names {LIVE_TAB}, but neither this beat nor the next clicks Refresh"
        )
    if _LIVE.search(sentence.replace(LIVE_TAB, "")):
        problems.append(f"says 'live': only {LIVE_TAB!r}, with a Refresh on camera")
    found = _NEVER_SAY.search(sentence)
    if found:
        problems.append(f"says {found.group(0)!r}, which the narration never says")
    return problems


def headline_problems(cue: str, say: Iterable[str]) -> List[str]:
    """G3: every number in the headline is spoken in the beat's narration."""
    said = {number_key(n) for sentence in say for n in numbers_in(sentence)}
    missing = [n for n in numbers_in(cue) if number_key(n) not in said]
    if missing:
        return [f"the headline says {missing[0]}; the narration does not"]
    return []


def _clicks_refresh(beat: Any) -> bool:
    for step in [*beat.before, *beat.steps, *beat.exit]:
        if isinstance(step, Click):
            named = " ".join(
                str(v)
                for v in (
                    step.click.name,
                    step.click.text,
                    step.click.testid,
                    step.click.label,
                    step.click.css,
                )
                if v
            )
            if "refresh" in named.lower():
                return True
    return False


def board_problems(board: Storyboard) -> List[str]:
    """G1 and G3 over a whole storyboard, every problem, each with its beat."""
    beats = [
        (scene.id, number, beat)
        for scene in board.scenes
        for number, beat in enumerate(scene.beats, start=1)
    ]
    voiced = any(beat.say for _scene, _number, beat in beats)
    forbidden = tuple(config.FORBIDDEN_TEXT) + tuple(board.never_show)
    problems: List[str] = []
    for position, (scene, number, beat) in enumerate(beats):
        where = f"scene {scene} beat {number}"
        if beat.claims and not beat.say:
            problems.append(f"{where}: claims with nothing said")
        if not voiced:
            continue
        if not beat.say:
            problems.append(
                f"{where}: says nothing; a narrated storyboard narrates every beat"
            )
            continue
        refresh = _clicks_refresh(beat) or (
            position + 1 < len(beats) and _clicks_refresh(beats[position + 1][2])
        )
        claimed = [claim.says for claim in beat.claims]
        for line, sentence in enumerate(beat.say, start=1):
            for problem in sentence_problems(
                sentence, forbidden=forbidden, claimed=claimed, refresh=refresh
            ):
                problems.append(f"{where} say {line}: {problem}")
        for claim in beat.claims:
            if not any(claim.says in sentence for sentence in beat.say):
                problems.append(f"{where}: the claim {claim.says!r} is not said")
            elif not NUMBER_WORD.search(claim.says):
                problems.append(
                    f"{where}: the claim {claim.says!r} holds no number word"
                )
        problems.extend(f"{where}: {p}" for p in headline_problems(beat.cue, beat.say))
    return problems
