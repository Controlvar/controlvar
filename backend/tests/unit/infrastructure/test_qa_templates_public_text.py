"""The QA alerts and summaries say what to do, and stay safe to publish.

``.github/qa-templates/`` holds the fixed wording of every issue, comment and
step summary the automated QA workflows post, one ``.tmpl`` file each: the
synthetic staging check, API fuzzing and the docs journeys (QA plan, UX D1-D3,
D9, D11.2, D11.5). This
repository is public, so each of those is public the moment it is posted, and
an edit afterwards does not recall the notification e-mail. The rules are
therefore checked on the template, before anything is posted:

* placeholders are only the names in ``PLACEHOLDERS``, whose kinds are the ones
  the plan allows in public text (UX check V1); no other brace, no dollar sign
  (a shell, ``envsubst`` or a workflow expression would expand it), no angle
  brackets, no printf-style placeholder;
* ASCII only; no e-mail address outside ``@example.com``, no ``@`` mention, no
  UUID, no long number, nothing shaped like a key, token or credential header,
  no fenced block, no URL or hostname other than this repository's and its docs
  site's, no name of a private repository or its directories, and none of the
  words the project keeps out of automated public text (UX D9, check V2);
* each template's verdict word is pinned and must be on its first line (the
  title, for an issue); the first two lines carry "What to do" or a count, and
  an output whose verdict is not green says "What to do" there (UX check V7).
  For an issue the body's first two lines carry the rest;
* each template's placeholders are pinned, title and body separately, so a
  docs-defect issue cannot lose its step number (UX check V11), and the
  synthetic issue names all eight steps;
* every workflow that posts to an issue or pull request, or writes its step
  summary, is classified in ``WORKFLOW_POSTERS``: ``template`` (posts with
  ``--body-file`` from a rendered template, never ``--body``) or
  ``legacy-inline`` (a named list of the workflows that built their text inline
  before these templates existed). A new poster fails until it is classified.

Every refusal is also planted against a real template in ``PLANTS`` below, so
a rule that stops firing fails here, not only when a template breaks it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, NamedTuple, Optional, Tuple

import pytest

from backend.tests.unit.infrastructure.test_docs_only_gate import _load

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
GITHUB_DIR = REPO_ROOT / ".github"
TEMPLATES_DIR = GITHUB_DIR / "qa-templates"
README = "README.md"

#: Every placeholder name, by the kind of value it holds. The kinds are exactly
#: the ones the QA plan allows in public text (UX check V1): date, commit, run
#: link, check name, step, HTTP status, count, seed, duration, and for the docs
#: runs the guide path, step number and heading. A name starts with its kind;
#: the suffix only tells two values of one kind apart in one template.
PLACEHOLDERS: Dict[str, Tuple[str, ...]] = {
    "date": ("date", "date_time"),
    "sha": ("sha",),
    "run_link": ("run_link",),
    "check": ("check",),
    "step": ("step",),
    "status": ("status",),
    "count": (
        "count_runs",
        "count_fuzzed",
        "count_operations",
        "count_5xx",
        "count_5xx_unlisted",
        "count_known_quiet",
        "count_reached",
        "count_unreached",
        "count_unreached_unlisted",
        "count_unreached_stale",
        "count_pass",
        "count_fail",
        "count_partial",
        "count_nav",
        "count_not_covered",
        "count_steps",
        "count_steps_pass",
        "count_not_run",
    ),
    "seed": ("seed",),
    "duration": ("duration",),
    "guide_path": ("guide_path",),
    "step_number": ("step_number",),
    "heading": ("heading_guide", "heading_step"),
}
ALLOWED = frozenset(name for names in PLACEHOLDERS.values() for name in names)

_FUZZ_RED = frozenset(
    {
        "check",
        "date",
        "sha",
        "seed",
        "count_fuzzed",
        "count_operations",
        "count_5xx",
        "count_5xx_unlisted",
        "count_known_quiet",
        "count_reached",
        "count_unreached_unlisted",
        "count_unreached_stale",
        "run_link",
    }
)
_DOCS_RUN = frozenset(
    {
        "date",
        "sha",
        "count_pass",
        "count_fail",
        "count_partial",
        "count_nav",
        "count_not_covered",
    }
)

#: The words a template may give as its verdict. Case-sensitive: "pass" in
#: "superuser pass" is not one, and neither is "PASS" in ``PASS={check}``,
#: because only the word pinned for the template, on its first line, counts.
VERDICT_WORDS = (
    "GREEN",
    "RED",
    "PASS",
    "FAIL",
    "PARTIAL",
    "Green",
    "red",
    "failed",
    "fails",
)
GREEN_VERDICTS = {"GREEN", "PASS", "Green"}


class Template(NamedTuple):
    """A template's verdict word and its placeholders, title and body apart.

    Only an ``-issue.tmpl`` file has a title (its first line); the others have
    None. A renderer can rely on the two sets: they are exactly what it must
    supply."""

    verdict: str
    title: Optional[FrozenSet[str]]
    body: FrozenSet[str]


_DOCS_GUIDE_FAIL = frozenset(
    {
        "heading_guide",
        "guide_path",
        "date",
        "sha",
        "step_number",
        "heading_step",
        "count_steps_pass",
        "count_steps",
        "run_link",
    }
)

TEMPLATES: Dict[str, Template] = {
    "synthetic-issue.tmpl": Template(
        "red",
        frozenset({"check"}),
        frozenset({"check", "count_runs", "date_time", "run_link", "step", "status"}),
    ),
    "synthetic-step-changed.tmpl": Template(
        "red",
        None,
        frozenset({"check", "step", "status", "run_link", "count_runs"}),
    ),
    "synthetic-recovered.tmpl": Template(
        "Green",
        None,
        frozenset({"date_time", "count_runs", "duration", "run_link"}),
    ),
    "fuzz-green.tmpl": Template(
        "GREEN",
        None,
        frozenset(
            {
                "check",
                "date",
                "sha",
                "seed",
                "count_fuzzed",
                "count_operations",
                "count_5xx",
                "count_reached",
                "count_unreached",
            }
        ),
    ),
    "fuzz-red.tmpl": Template("RED", None, _FUZZ_RED),
    "fuzz-issue.tmpl": Template("red", frozenset(), _FUZZ_RED),
    "docs-run-green.tmpl": Template("GREEN", None, _DOCS_RUN),
    "docs-run-red.tmpl": Template("RED", None, _DOCS_RUN | {"run_link"}),
    "docs-guide-pass.tmpl": Template(
        "PASS",
        None,
        frozenset({"heading_guide", "guide_path", "date", "sha", "count_steps"}),
    ),
    "docs-guide-fail.tmpl": Template("FAIL", None, _DOCS_GUIDE_FAIL),
    "docs-guide-partial.tmpl": Template(
        "PARTIAL",
        None,
        frozenset(
            {
                "heading_guide",
                "guide_path",
                "date",
                "sha",
                "count_not_run",
                "count_steps_pass",
                "count_steps",
            }
        ),
    ),
    "docs-journey-issue.tmpl": Template(
        "red", frozenset({"guide_path"}), _DOCS_GUIDE_FAIL
    ),
    "docs-defect-issue.tmpl": Template(
        "fails",
        frozenset({"heading_guide", "step_number", "heading_step"}),
        frozenset(
            {
                "step_number",
                "guide_path",
                "count_runs",
                "sha",
                "run_link",
                "heading_step",
            }
        ),
    ),
}

#: An issue whose body is another template, verbatim: (issue, title, body).
ISSUE_BODIES = [
    ("fuzz-issue.tmpl", "API fuzzing is red", "fuzz-red.tmpl"),
    (
        "docs-journey-issue.tmpl",
        "Docs journey ({guide_path}) is red",
        "docs-guide-fail.tmpl",
    ),
]

#: The labels the workflows create on first use; the README documents each.
LABELS = ("synthetic-failure", "fuzz-failure", "docs-journey-failure", "qa-agent")

#: The synthetic check's steps, in order (QA plan v1.1, section S).
SYNTHETIC_STEPS = [
    "Ready",
    "Sign in",
    "Key expiry",
    "Results before",
    "Assign",
    "Track",
    "Evaluate",
    "Results after",
]

# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------

PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")

#: Word stems the project keeps out of automated public text, matched
#: case-insensitively anywhere in a word.
BANNED_STEMS = (
    "security",
    "vulnerab",
    "attack",
    "exploit",
    "inject",
    "enumerat",
    "leak",
    "disclos",
    "exposure",
    "privilege",
    "escalat",
    "internal",
    "echo",
    "bypass",
)

#: The private repository and its directories, by name; and any repository of
#: the organisation other than this one. Both organisations' names are held:
#: the former one, which keeps the frozen packages, and the current one, whose
#: owner GitHub compares without regard to case.
PRIVATE_NAMES = ("launch-readiness", "experimently-internal", "controlvar-internal")
OTHER_REPOSITORY = re.compile(
    r"(?:getexperimently/(?!experimently(?![A-Za-z0-9_.-]))"
    r"|(?<![A-Za-z0-9_.-])(?<![Cc][Oo][Nn][Tt][Rr][Oo][Ll][Vv][Aa][Rr]/)"
    r"(?i:controlvar)/(?!controlvar(?![A-Za-z0-9_.-])))[A-Za-z0-9_.-]+"
)

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
MENTION = re.compile(r"(?<![A-Za-z0-9._%+-])@[A-Za-z0-9]")
UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
LONG_NUMBER = re.compile(r"[0-9]{7,}")
CREDENTIAL_SHAPES = (
    ("an API key prefix", re.compile(r"eptk", re.I)),
    ("a live or test key prefix", re.compile(r"\b[a-z]{2,4}_(?:live|test)_", re.I)),
    ("an AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{12,}")),
    ("a GitHub token", re.compile(r"\b(?:gh[pousr]_|github_pat_)")),
    ("a JSON web token", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}")),
    ("a bearer credential", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")),
    (
        "a credential header",
        re.compile(r"\b(?:authorization|cookie|set-cookie|x-api-key)\s*:", re.I),
    ),
)
FENCE = re.compile(r"^\s*(?:```|~~~)", re.M)
URL = re.compile(r"https?://[^\s)\]]+")
ALLOWED_URL_PREFIXES = (
    "https://github.com/Controlvar/controlvar/",
    "https://docs.controlvar.com/",
)
HOST = re.compile(
    r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|io|net|org|dev|app|cloud|ai|co)\b", re.I
)
ALLOWED_HOSTS = {"github.com", "docs.controlvar.com", "example.com"}

WHAT_TO_DO = "What to do"
COUNT = re.compile(r"\{count(?:_[a-z0-9_]+)?\}")


def syntax_problems(text: str) -> List[str]:
    """Placeholders outside the allowed names, and every other way to template."""
    found = [
        f"placeholder {{{name}}} is not one the QA plan allows"
        for name in sorted(set(PLACEHOLDER.findall(text)) - ALLOWED)
    ]
    rest = PLACEHOLDER.sub("", text)
    if "{" in rest or "}" in rest:
        found.append("a brace that is not a placeholder")
    if "$" in text:
        found.append(
            "a dollar sign: a shell, envsubst or a workflow expression would expand it"
        )
    if "<" in text or ">" in text:
        found.append(
            "angle brackets: an HTML tag hides text, and <name> is no placeholder"
        )
    if re.search(r"%\(|%[sdr]\b", text):
        found.append("a printf-style placeholder")
    return found


def shape_problems(text: str) -> List[str]:
    """What never goes in public text (UX D9), and the words kept out of it."""
    found = []
    lowered = text.lower()
    found += [f"the word stem {stem!r}" for stem in BANNED_STEMS if stem in lowered]
    found += [f"a private name: {name}" for name in PRIVATE_NAMES if name in lowered]
    found += [
        f"another repository of the organisation: {match.group(0)}"
        for match in OTHER_REPOSITORY.finditer(text)
    ]
    for match in EMAIL.finditer(text):
        if match.group(1).lower() != "example.com":
            found.append(f"an e-mail address outside @example.com: {match.group(0)}")
    if MENTION.search(text):
        found.append("an @ mention")
    if UUID.search(text):
        found.append("a UUID: ids never go in public text")
    if LONG_NUMBER.search(text):
        found.append("a run of seven or more digits: an id belongs in a placeholder")
    found += [label for label, shape in CREDENTIAL_SHAPES if shape.search(text)]
    if FENCE.search(text):
        found.append("a fenced block: that is where a pasted body goes")
    for url in URL.findall(text):
        url = url.rstrip(".,;:")
        if not url.startswith(ALLOWED_URL_PREFIXES):
            found.append(f"a URL outside this repository and its docs site: {url}")
    for match in HOST.finditer(URL.sub("", text)):
        if match.group(0).lower() not in ALLOWED_HOSTS:
            found.append(f"a hostname: {match.group(0)}")
    return found


def split_issue(name: str, text: str) -> Tuple[Optional[str], str, List[str]]:
    """(title, body, layout problems); a non-issue template has no title."""
    if not name.endswith("-issue.tmpl"):
        return None, text, []
    lines = text.split("\n")
    if len(lines) < 3 or not lines[0].strip() or lines[1] != "":
        return (
            None,
            text,
            ["an issue template is its title, a blank line, then the body"],
        )
    return lines[0], "\n".join(lines[2:]), []


def first_line(text: str) -> str:
    return next((line for line in text.splitlines() if line.strip()), "")


def opening_problems(text: str, green: bool) -> List[str]:
    """The rest of the two-line rule (UX check V7), on the first two non-blank
    lines: "What to do" or a count, and "What to do" when not green."""
    head = "\n".join([line for line in text.splitlines() if line.strip()][:2])
    found = []
    if WHAT_TO_DO not in head and not COUNT.search(head):
        found.append('neither "What to do" nor a count in the first two lines')
    if not green and WHAT_TO_DO not in head:
        found.append('not green, and "What to do" is not in the first two lines')
    return found


def problems(name: str, text: str) -> List[str]:
    """Everything wrong with one template, judged against its pinned verdict."""
    verdict = TEMPLATES[name].verdict
    found = syntax_problems(text) + shape_problems(text)
    if not text.isascii():
        found.append("a character outside ASCII: it can hide or impersonate text")
    title, body, layout = split_issue(name, text)
    found += layout
    first = first_line(body) if title is None else title
    if not re.search(rf"\b{re.escape(verdict)}\b", first):
        found.append(
            f"the first line (the title, for an issue) does not carry its verdict {verdict!r}"
        )
    found += opening_problems(body, green=verdict in GREEN_VERDICTS)
    return found


def read(name: str) -> str:
    return (TEMPLATES_DIR / name).read_text(encoding="utf-8")


def placeholders(text: Optional[str]) -> Optional[FrozenSet[str]]:
    return None if text is None else frozenset(PLACEHOLDER.findall(text))


# ---------------------------------------------------------------------------
# The templates in this tree
# ---------------------------------------------------------------------------


def test_the_directory_holds_exactly_the_pinned_templates():
    """A template the test does not list would be posted unchecked; a missing
    one would make the parametrised checks below pass over nothing."""
    assert TEMPLATES_DIR.is_dir(), f"{TEMPLATES_DIR} is missing"
    present = sorted(path.name for path in TEMPLATES_DIR.iterdir())
    assert present == sorted([*TEMPLATES, README])


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_the_template_is_safe_to_publish(name):
    found = problems(name, read(name))
    assert not found, f"{name}:\n  " + "\n  ".join(found)


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_the_template_uses_exactly_its_placeholders(name):
    title, body, _ = split_issue(name, read(name))
    spec = TEMPLATES[name]
    assert (placeholders(title), placeholders(body)) == (spec.title, spec.body)


def test_every_pinned_verdict_is_a_verdict_word():
    assert {spec.verdict for spec in TEMPLATES.values()} <= set(VERDICT_WORDS)


def test_every_placeholder_kind_is_one_the_plan_allows():
    assert set(PLACEHOLDERS) == {
        "date",
        "sha",
        "run_link",
        "check",
        "step",
        "status",
        "count",
        "seed",
        "duration",
        "guide_path",
        "step_number",
        "heading",
    }
    for kind, names in PLACEHOLDERS.items():
        for name in names:
            assert name == kind or name.startswith(kind + "_"), (kind, name)
            assert PLACEHOLDER.fullmatch("{" + name + "}"), name


def test_the_readme_is_safe_to_publish_and_documents_every_name_and_label():
    text = read(README)
    assert shape_problems(text) == []
    missing = [name for name in sorted(ALLOWED) if f"`{{{name}}}`" not in text]
    missing += [label for label in LABELS if f"`{label}`" not in text]
    assert not missing, f"not in {README}: {missing}"


def test_the_synthetic_issue_lists_the_eight_steps_in_order():
    _, body, _ = split_issue("synthetic-issue.tmpl", read("synthetic-issue.tmpl"))
    listed = re.findall(r"^([0-9]+)\. ([A-Z][a-z]+(?: [a-z]+)*):", body, re.M)
    numbers = range(1, len(SYNTHETIC_STEPS) + 1)
    assert listed == [(str(n), step) for n, step in zip(numbers, SYNTHETIC_STEPS)]


@pytest.mark.parametrize("issue, title, source", ISSUE_BODIES)
def test_the_issue_body_is_its_source_template(issue, title, source):
    """The fuzzing issue is the red summary, and the issue for a failing guide
    is that guide's report header, word for word."""
    found_title, body, _ = split_issue(issue, read(issue))
    assert found_title == title
    assert body == read(source)


# ---------------------------------------------------------------------------
# Each rule fires: a defect planted against a real template
# ---------------------------------------------------------------------------


def _add(line: str) -> Callable[[str], str]:
    return lambda text: text + "\n" + line + "\n"


def _move_what_to_do_down(text: str) -> str:
    """The "What to do" paragraph swapped with the one after it."""
    paragraphs = text.split("\n\n")
    index = next(
        i for i in range(len(paragraphs)) if paragraphs[i].startswith(WHAT_TO_DO)
    )
    paragraphs[index], paragraphs[index + 1] = paragraphs[index + 1], paragraphs[index]
    return "\n\n".join(paragraphs)


def _replace(old: str, new: str) -> Callable[[str], str]:
    def plant(text: str) -> str:
        assert old in text, f"the plant's anchor {old!r} is gone"
        return text.replace(old, new)

    return plant


#: (id, template, plant, a fragment the problems must contain)
PLANTS = [
    (
        "workflow-expression",
        "synthetic-issue.tmpl",
        _add("Base URL: ${{ vars.PUBLIC_BASE_URL }}"),
        "dollar sign",
    ),
    ("real-e-mail", "synthetic-issue.tmpl", _add("Ask user@realdomain.io."), "e-mail"),
    ("what-to-do-moved", "synthetic-issue.tmpl", _move_what_to_do_down, WHAT_TO_DO),
    ("unknown-placeholder", "fuzz-red.tmpl", _add("Body: {response_body}"), "not one"),
    ("stray-brace", "fuzz-red.tmpl", _add('Body: {"assigned": true}'), "brace"),
    ("angle-placeholder", "docs-run-red.tmpl", _add("Experiment: <id>"), "angle"),
    ("printf", "docs-run-red.tmpl", _add("User: %(user)s"), "printf"),
    ("mention", "synthetic-recovered.tmpl", _add("cc @someone"), "mention"),
    (
        "uuid",
        "synthetic-recovered.tmpl",
        _add("Experiment 123e4567-e89b-12d3-a456-426614174000"),
        "UUID",
    ),
    ("long-number", "synthetic-recovered.tmpl", _add("User 12345678"), "digits"),
    (
        "api-key",
        "fuzz-green.tmpl",
        _add("Key: " + "ep" + "tk_" + "0" * 32),
        "API key prefix",
    ),
    ("key-prefix", "fuzz-green.tmpl", _add("sk" + "_live_" + "x"), "key prefix"),
    ("aws-key", "fuzz-green.tmpl", _add("AK" + "IA" + "X" * 16), "AWS"),
    ("github-token", "fuzz-green.tmpl", _add("gh" + "p_" + "x" * 8), "GitHub"),
    ("jwt", "fuzz-green.tmpl", _add("ey" + "J" + "x" * 20), "JSON web token"),
    ("bearer", "docs-guide-fail.tmpl", _add("Bearer " + "x" * 20), "bearer"),
    ("header", "docs-guide-fail.tmpl", _add("X-API-Key: x"), "header"),
    ("fence", "docs-guide-pass.tmpl", _add("```\nok\n```"), "fenced"),
    ("other-url", "docs-defect-issue.tmpl", _add("See https://example.net/x"), "URL"),
    ("hostname", "docs-defect-issue.tmpl", _add("On staging.example.net"), "hostname"),
    (
        "title-without-verdict",
        "synthetic-issue.tmpl",
        _replace("Synthetic check ({check}) is red\n", "Synthetic check ({check})\n"),
        "title",
    ),
    (
        "issue-without-blank-line",
        "docs-defect-issue.tmpl",
        _replace(" fails as written\n\n", " fails as written\n"),
        "blank line",
    ),
    (
        "no-verdict",
        "fuzz-green.tmpl",
        _replace("commit {sha}: GREEN", "commit {sha}"),
        "verdict",
    ),
    (
        "no-count-and-no-what-to-do",
        "docs-guide-pass.tmpl",
        _replace("all {count_steps} steps pass", "every step passes"),
        "neither",
    ),
    (
        "red-without-what-to-do",
        "docs-guide-fail.tmpl",
        _move_what_to_do_down,
        WHAT_TO_DO,
    ),
    # The verdict deleted from the first line, where an incidental word in the
    # first two lines ("PASS=" in the reproduce command, "failed") once
    # satisfied the rule.
    (
        "verdict-removed-fuzz-red",
        "fuzz-red.tmpl",
        _replace("commit {sha}: RED", "commit {sha}"),
        "verdict 'RED'",
    ),
    (
        "verdict-removed-docs-run-red",
        "docs-run-red.tmpl",
        _replace("commit {sha}: RED", "commit {sha}"),
        "verdict 'RED'",
    ),
    (
        "verdict-removed-docs-guide-partial",
        "docs-guide-partial.tmpl",
        _replace(": PARTIAL, {count_not_run}", ": {count_not_run}"),
        "verdict 'PARTIAL'",
    ),
    (
        "verdict-removed-synthetic-step-changed",
        "synthetic-step-changed.tmpl",
        _replace("is still red, and", "is still down, and"),
        "verdict 'red'",
    ),
    (
        "verdict-removed-docs-journey-issue",
        "docs-journey-issue.tmpl",
        _replace(") is red\n", ")\n"),
        "verdict 'red'",
    ),
    ("smart-quotes", "fuzz-green.tmpl", _add("\u201cquoted\u201d"), "ASCII"),
    ("zero-width-space", "fuzz-green.tmpl", _add("a\u200bb"), "ASCII"),
    (
        "private-directory",
        "docs-run-red.tmpl",
        _add("Notes: launch-readiness/qa"),
        "private name",
    ),
    (
        "private-repository",
        "docs-run-red.tmpl",
        _add("See getexperimently/qa-notes"),
        "another repository",
    ),
    (
        "private-repository-renamed-org",
        "docs-run-red.tmpl",
        _add("See Controlvar/other"),
        "another repository",
    ),
    (
        "private-repository-lowercase-owner",
        "docs-run-red.tmpl",
        _add("See controlvar/qa-notes"),
        "another repository",
    ),
    (
        "private-repository-in-a-path",
        "docs-run-red.tmpl",
        _add("See ghcr.io/Controlvar/other"),
        "another repository",
    ),
    (
        "private-repository-renamed",
        "docs-run-red.tmpl",
        _add("Notes: controlvar-internal"),
        "private name",
    ),
] + [
    (f"stem-{n}", "docs-guide-partial.tmpl", _add(f"x{stem}x"), "word stem")
    for n, stem in zip(range(len(BANNED_STEMS)), BANNED_STEMS)
]


@pytest.mark.parametrize(
    "template, plant, fragment",
    [plant[1:] for plant in PLANTS],
    ids=[plant[0] for plant in PLANTS],
)
def test_the_rule_fires_on_a_planted_defect(template, plant, fragment):
    clean = read(template)
    assert problems(template, clean) == []
    found = problems(template, plant(clean))
    assert any(fragment in problem for problem in found), found


@pytest.mark.parametrize(
    "line",
    [
        "Sign in as admin@example.com.",
        "See https://github.com/Controlvar/controlvar/blob/main/README.md",
        "See https://docs.controlvar.com/",
        "The pass named {check}, at 50% of the 8 steps.",
    ],
)
def test_what_public_text_may_say_passes(line):
    """The rules do not refuse the things a template legitimately says."""
    text = read("synthetic-recovered.tmpl") + "\n" + line + "\n"
    assert problems("synthetic-recovered.tmpl", text) == []


# ---------------------------------------------------------------------------
# Which workflows may post, and how
# ---------------------------------------------------------------------------

#: Every workflow and composite action under .github/ that posts to an issue or
#: a pull request, or writes its step summary, by path under .github/.
#: ``template``: posts only with ``--body-file``, from a template rendered out
#: of .github/qa-templates/. ``legacy-inline``: built its text inline before the
#: templates existed; this list may shrink, and a new poster is a template one.
WORKFLOW_POSTERS: Dict[str, Tuple[str, str]] = {
    "workflows/chart-kind.yml": (
        "legacy-inline",
        "the chart-kind-failure issue: date, commit and run link",
    ),
    "workflows/db-migrate.yml": (
        "legacy-inline",
        "the migration result table in the step summary",
    ),
    "workflows/dependabot-lock.yml": (
        "legacy-inline",
        "the lock-refresh comment on a Dependabot pull request, and its outcome table",
    ),
    "workflows/deploy.yml": ("legacy-inline", "the deploy result in the step summary"),
    "workflows/doc-examples.yml": (
        "legacy-inline",
        "the doc-examples-failure issue: date, commit and run link",
    ),
    "workflows/docs-journeys.yml": (
        "template",
        "the docs-journey-failure issue of each failing guide and its comments,"
        " and the run's summary, rendered by scripts/qa_render.py",
    ),
    "workflows/nightly-qa.yml": (
        "legacy-inline",
        "the nightly-failure issue (date, commit, run link) and one summary line",
    ),
    "workflows/rollback.yml": (
        "legacy-inline",
        "the rollback result in the step summary",
    ),
    "workflows/sdk-unit-tests.yml": (
        "legacy-inline",
        "which SDK tiers ran, in the step summary",
    ),
    "workflows/synthetic.yml": (
        "template",
        "the synthetic-failure issue and its comments, and the run's step "
        "summary, rendered by scripts/qa_render.py",
    ),
    # The fuzz arm writes each fuzzing pass's step summary; fuzz.yml keeps the
    # rolling fuzz-failure issue.
    "workflows/_platform.yml": (
        "template",
        "each API fuzzing pass's step summary, rendered from fuzz-green.tmpl "
        "or fuzz-red.tmpl by scripts/fuzz_check.py through scripts/qa_render.py",
    ),
    "workflows/fuzz.yml": (
        "template",
        "the fuzz-failure issue and its comments, rendered from fuzz-issue.tmpl "
        "and fuzz-red.tmpl by scripts/fuzz_report.py through scripts/qa_render.py",
    ),
}
POSTER_KINDS = ("template", "legacy-inline")

POSTS = re.compile(
    r"\bgh\s+(?:issue|pr)\s+(?:create|comment|edit|close|reopen|review)\b"
)
SUMMARY = "GITHUB_STEP_SUMMARY"
INLINE_BODY = re.compile(r"(?<!\S)(?:--body|-b)(?=[\s=])")
BODY_FILE = re.compile(r"(?<!\S)--body-file(?=[\s=])")
TEMPLATES_REFERENCE = ".github/qa-templates/"


def scripts(document: Any) -> List[str]:
    """Every ``run:`` script of a workflow's jobs or a composite action."""
    steps: List[Any] = []
    if isinstance(document, dict):
        for job in (document.get("jobs") or {}).values():
            if isinstance(job, dict):
                steps += job.get("steps") or []
        runs = document.get("runs")
        if isinstance(runs, dict):
            steps += runs.get("steps") or []
    return [
        step["run"]
        for step in steps
        if isinstance(step, dict) and isinstance(step.get("run"), str)
    ]


def posting_commands(document: Any) -> List[str]:
    """Each command that posts (continuation lines joined), and SUMMARY once
    for every script that writes the step summary."""
    found = []
    for script in scripts(document):
        for line in script.replace("\\\n", " ").splitlines():
            if POSTS.search(line):
                found.append(line.strip())
        if SUMMARY in script:
            found.append(SUMMARY)
    return found


def poster_files() -> List[Path]:
    paths = [
        *GITHUB_DIR.glob("workflows/*.yml"),
        *GITHUB_DIR.glob("workflows/*.yaml"),
        *GITHUB_DIR.glob("actions/*/action.yml"),
        *GITHUB_DIR.glob("actions/*/action.yaml"),
    ]
    return sorted(paths)


def scan() -> Tuple[Dict[str, List[str]], Dict[str, str]]:
    """(the posting commands of each file that posts, every file's text)."""
    posters, texts = {}, {}
    for path in poster_files():
        rel = path.relative_to(GITHUB_DIR).as_posix()
        texts[rel] = path.read_text(encoding="utf-8")
        commands = posting_commands(_load(path))
        if commands:
            posters[rel] = commands
    return posters, texts


def poster_problems(
    posters: Dict[str, List[str]],
    texts: Dict[str, str],
    classified: Dict[str, Tuple[str, str]] = WORKFLOW_POSTERS,
) -> List[str]:
    found = [
        f"{path} posts or writes a step summary and is not in WORKFLOW_POSTERS"
        for path in sorted(set(posters) - set(classified))
    ]
    found += [
        f"{path} is in WORKFLOW_POSTERS but no longer posts: remove it"
        for path in sorted(set(classified) - set(posters))
    ]
    for path, (kind, reason) in sorted(classified.items()):
        if kind not in POSTER_KINDS or not reason.strip():
            found.append(
                f"{path}: the kind must be one of {POSTER_KINDS}, with a reason"
            )
        if kind != "template" or path not in posters:
            continue
        if TEMPLATES_REFERENCE not in texts.get(path, ""):
            found.append(
                f"{path}: a template poster that renders nothing from {TEMPLATES_REFERENCE}"
            )
        for command in posters[path]:
            if command != SUMMARY and (
                INLINE_BODY.search(command) or not BODY_FILE.search(command)
            ):
                found.append(f"{path}: posts without --body-file: {command}")
    return found


def test_every_workflow_that_posts_is_classified():
    paths = poster_files()
    assert len(paths) > 20, "the scan found almost nothing: it is broken, not clean"
    posters, texts = scan()
    found = poster_problems(posters, texts)
    assert not found, "\n".join(found)


def _workflow(run: str) -> Dict[str, Any]:
    return {"on": {"workflow_dispatch": None}, "jobs": {"j": {"steps": [{"run": run}]}}}


_RENDERED = (
    'body="$RUNNER_TEMP/body.md"  # rendered from .github/qa-templates/fuzz-red.tmpl\n'
)

#: (id, the planted file's script, its classification or None, the file's
#: text, a fragment the problems must contain; None = no problem)
POSTER_PLANTS = [
    (
        "unclassified",
        'gh issue comment 1 --body "$X"',
        None,
        "",
        "not in WORKFLOW_POSTERS",
    ),
    (
        "unclassified-summary",
        'cat out.md >> "$GITHUB_STEP_SUMMARY"',
        None,
        "",
        "not in WORKFLOW_POSTERS",
    ),
    (
        "template-inline-body",
        'gh issue comment 1 --body "$X"',
        "template",
        _RENDERED,
        "without --body-file",
    ),
    (
        "template-short-flag",
        'gh issue comment 1 -b "$X"',
        "template",
        _RENDERED,
        "without --body-file",
    ),
    (
        "template-continued-line",
        'gh issue create --title "$T" \\\n  --body "$X"',
        "template",
        _RENDERED,
        "without --body-file",
    ),
    (
        "template-renders-nothing",
        'gh issue comment 1 --body-file "$RUNNER_TEMP/body.md"',
        "template",
        "",
        "renders nothing",
    ),
    (
        "template-summary-renders-nothing",
        'cat out.md >> "$GITHUB_STEP_SUMMARY"',
        "template",
        "",
        "renders nothing",
    ),
    (
        "template-done-right",
        'gh issue comment 1 --body-file "$RUNNER_TEMP/body.md"',
        "template",
        _RENDERED,
        None,
    ),
]


@pytest.mark.parametrize(
    "run, kind, text, fragment",
    [plant[1:] for plant in POSTER_PLANTS],
    ids=[plant[0] for plant in POSTER_PLANTS],
)
def test_the_poster_rule_fires_on_a_planted_workflow(run, kind, text, fragment):
    posters, texts = scan()
    assert poster_problems(posters, texts) == []
    planted = "workflows/planted.yml"
    posters[planted] = posting_commands(_workflow(run))
    texts[planted] = text
    classified = dict(WORKFLOW_POSTERS)
    if kind is not None:
        classified[planted] = (kind, "planted")
    found = poster_problems(posters, texts, classified)
    if fragment is None:
        assert found == []
    else:
        assert any(fragment in problem for problem in found), found


def test_a_poster_that_stops_posting_must_leave_the_list():
    posters, texts = scan()
    posters.pop("workflows/nightly-qa.yml")
    found = poster_problems(posters, texts)
    assert any("no longer posts" in problem for problem in found), found
