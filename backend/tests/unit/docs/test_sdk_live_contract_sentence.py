"""Each SDK page says whether CI runs its contract smoke against a real API (#1075).

The SDK pages carried hand-dated lines such as "Verified against a live
backend: yes (2026-09-11)", held to nothing, and five of them disagreed with
what CI runs. Each page now carries one undated sentence in one of two forms:

    CI runs this SDK's contract smoke against a real API: `<sdk>` is in the
    `sdks:` list of the SDK Live Contract job in pr-qa-gate.yml.

    CI does not run this SDK's contract smoke against a real API: `<sdk>` is
    not in the `sdks:` list of the SDK Live Contract job in pr-qa-gate.yml.

This test holds those sentences to that list, read by parsing
``.github/workflows/pr-qa-gate.yml`` (the list is a folded ``>-`` string, one
YAML value, not lines a search could rely on):

* every page under ``docs/sdk/`` except ``NOT_AN_SDK_PAGE`` carries at least
  one sentence, and none carries the old "Verified against a live backend";
* every SDK a sentence names is a ``--sdk`` name of the live runner,
  ``tests/sdk-contract/live/run_live_contract.py`` (its ``MANIFEST``, read
  with ``ast``), and no SDK is named twice;
* a "runs" sentence names an SDK in the job's list, a "does not run" sentence
  one that is not, and every SDK in the list has a "runs" sentence.

The SDK READMEs under ``sdk/`` carried the same dated line: none may carry it
now, and a README's sentence is held to the same list.

Line breaks inside a sentence do not matter: each page's whitespace is folded
before matching. Reads only files; no git, so it runs the same in
``scripts/core_build.sh``'s copy. It is in the docs-only gate's "Docs content
tests" through its directory, ``backend/tests/unit/docs/``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Set

import pytest
import yaml

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
SDK_DOCS = REPO_ROOT / "docs" / "sdk"
GATE = REPO_ROOT / ".github" / "workflows" / "pr-qa-gate.yml"
RUNNER = REPO_ROOT / "tests" / "sdk-contract" / "live" / "run_live_contract.py"

#: The job of pr-qa-gate.yml whose `sdks:` list the sentences are held to.
JOB = "sdk-live-contract"

#: Pages under docs/sdk/ that are not one SDK's page, with the reason.
NOT_AN_SDK_PAGE: Dict[str, str] = {
    "local-evaluation.md": "server-side local evaluation, shared by the Python and JS SDKs",
}

LINK = (
    r"\[pr-qa-gate\.yml\]\(https://github\.com/Controlvar/controlvar/blob/main/"
    r"\.github/workflows/pr-qa-gate\.yml\)"
)
SENTENCE = re.compile(
    r"CI (?P<verb>runs|does not run) this SDK's contract smoke against a real API:"
    r" `(?P<sdk>[^`\s]+)` (?P<listed>is|is not) in the `sdks:` list of the"
    r" SDK Live Contract job in " + LINK + r"\."
)
OLD_FORM = "Verified against a live backend"


def gate_sdks(text: str) -> List[str]:
    """The `sdks:` list of the SDK Live Contract job, from the parsed workflow."""
    job = yaml.safe_load(text)["jobs"][JOB]
    assert job.get("uses") == "./.github/workflows/_platform.yml", job
    return job["with"]["sdks"].split()


def runner_sdks(text: str) -> Set[str]:
    """The keys of the live runner's ``MANIFEST`` dict literal."""
    for node in ast.parse(text).body:
        target = node.target if isinstance(node, ast.AnnAssign) else None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == "MANIFEST":
            assert isinstance(node.value, ast.Dict)
            return {ast.literal_eval(key) for key in node.value.keys}
    raise AssertionError("run_live_contract.py has no MANIFEST")


def problems(
    pages: Mapping[str, str], gate: Sequence[str], runner: Set[str]
) -> List[str]:
    """Every way the pages disagree with the gate's list; empty when they agree.

    ``pages`` maps a page's name under docs/sdk/ to its text.
    """
    found: List[str] = []
    named: Dict[str, str] = {}
    runs: Set[str] = set()
    for name in sorted(pages):
        page = f"docs/sdk/{name}"
        text = " ".join(pages[name].split())
        if OLD_FORM in text:
            found.append(f"{page}: still says {OLD_FORM!r}; use the sentence instead")
        sentences = list(SENTENCE.finditer(text))
        if not sentences and name not in NOT_AN_SDK_PAGE:
            found.append(
                f"{page}: no sentence saying whether CI runs this SDK's contract smoke"
            )
        for match in sentences:
            sdk = match["sdk"]
            if (match["verb"] == "runs") != (match["listed"] == "is"):
                found.append(f"{page}: the sentence for {sdk!r} contradicts itself")
                continue
            if sdk not in runner:
                found.append(
                    f"{page}: {sdk!r} is not a --sdk name of"
                    " tests/sdk-contract/live/run_live_contract.py"
                )
            if sdk in named:
                found.append(f"{page}: {sdk!r} is also named by {named[sdk]}")
            named[sdk] = page
            listed = sdk in gate
            if match["verb"] == "runs":
                runs.add(sdk)
                if not listed:
                    found.append(
                        f"{page}: says CI runs {sdk!r}, but the {JOB} job's"
                        " sdks: list in pr-qa-gate.yml does not have it"
                    )
            elif listed:
                found.append(
                    f"{page}: says CI does not run {sdk!r}, but the {JOB} job's"
                    " sdks: list in pr-qa-gate.yml has it"
                )
    for sdk in gate:
        if sdk not in runs:
            found.append(
                f"{sdk!r} is in the {JOB} job's sdks: list, and no page under"
                " docs/sdk/ says CI runs it"
            )
    return found


def _real_pages() -> Dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in SDK_DOCS.glob("*.md")}


def _real_gate() -> List[str]:
    return gate_sdks(GATE.read_text(encoding="utf-8"))


def _real_runner() -> Set[str]:
    return runner_sdks(RUNNER.read_text(encoding="utf-8"))


def test_every_sdk_page_says_what_ci_runs_and_agrees_with_the_gate():
    found = problems(_real_pages(), _real_gate(), _real_runner())
    assert not found, "\n".join(found)


def test_the_inputs_parsed_to_real_lists():
    """A list that came back empty would leave nothing to hold the pages to."""
    gate = _real_gate()
    assert len(gate) >= 10 and len(set(gate)) == len(gate), gate
    assert set(gate) <= _real_runner()
    assert set(NOT_AN_SDK_PAGE) <= set(_real_pages())


# ---------------------------------------------------------------------------
# Planted defects, each against the real pages and lists: each refusal fires
# ---------------------------------------------------------------------------
def _plant(
    pages: Optional[Mapping[str, str]] = None, gate: Optional[Sequence[str]] = None
) -> List[str]:
    return problems(
        _real_pages() if pages is None else pages,
        _real_gate() if gate is None else gate,
        _real_runner(),
    )


def _edit(name: str, old: str, new: str) -> Dict[str, str]:
    pages = _real_pages()
    text = " ".join(pages[name].split())
    assert old in text, (name, old)
    pages[name] = text.replace(old, new)
    return pages


@pytest.mark.regression
def test_adding_ios_to_the_gate_contradicts_ios_md():
    assert "ios" not in _real_gate()
    assert _plant(gate=_real_gate() + ["ios"]) == [
        "docs/sdk/ios.md: says CI does not run 'ios', but the sdk-live-contract"
        " job's sdks: list in pr-qa-gate.yml has it",
        "'ios' is in the sdk-live-contract job's sdks: list, and no page under"
        " docs/sdk/ says CI runs it",
    ]


@pytest.mark.regression
def test_dropping_an_sdk_from_the_gate_contradicts_its_page():
    gate = [sdk for sdk in _real_gate() if sdk != "go"]
    assert _plant(gate=gate) == [
        "docs/sdk/go.md: says CI runs 'go', but the sdk-live-contract job's"
        " sdks: list in pr-qa-gate.yml does not have it"
    ]


@pytest.mark.parametrize(
    "name, old, new, expected",
    [
        pytest.param(
            "ios.md",
            "CI does not run this SDK's contract smoke against a real API: `ios` is not",
            "CI runs this SDK's contract smoke against a real API: `ios` is",
            "docs/sdk/ios.md: says CI runs 'ios', but",
            id="page-claims-a-run-the-gate-lacks",
        ),
        pytest.param(
            "ruby.md",
            "CI runs this SDK's contract smoke",
            "Verified against a live backend: yes (2026-09-11). CI runs this SDK's"
            " contract smoke",
            "docs/sdk/ruby.md: still says 'Verified against a live backend'",
            id="old-form-left-beside-the-sentence",
        ),
        pytest.param(
            "java.md",
            "CI runs this SDK's contract smoke against a real API",
            "CI ran this SDK's contract smoke against a real API",
            "docs/sdk/java.md: no sentence",
            id="page-without-a-sentence",
        ),
        pytest.param(
            "java.md",
            "`java` is in",
            "`ruby` is in",
            "docs/sdk/ruby.md: 'ruby' is also named by docs/sdk/java.md",
            id="sdk-named-twice",
        ),
        pytest.param(
            "ios.md",
            "`ios` is not",
            "`swift` is not",
            "docs/sdk/ios.md: 'swift' is not a --sdk name",
            id="name-the-runner-does-not-know",
        ),
        pytest.param(
            "elixir.md",
            "CI does not run this SDK's contract smoke against a real API: `elixir` is not",
            "CI does not run this SDK's contract smoke against a real API: `elixir` is",
            "docs/sdk/elixir.md: the sentence for 'elixir' contradicts itself",
            id="sentence-contradicts-itself",
        ),
        pytest.param(
            "react.md",
            "CI runs this SDK's contract smoke against a real API: `react` is in",
            "CI runs the contract smoke: `react` is in",
            "'react' is in the sdk-live-contract job's sdks: list, and no page",
            id="gate-sdk-no-page-claims",
        ),
    ],
)
def test_planted_defect_is_refused(name, old, new, expected):
    found = _plant(pages=_edit(name, old, new))
    assert any(expected in line for line in found), found


def test_gate_sdks_reads_the_folded_list_of_the_named_job():
    text = (
        "jobs:\n"
        "  other:\n"
        "    uses: ./.github/workflows/_platform.yml\n"
        "    with:\n"
        "      sdks: ios\n"
        "  sdk-live-contract:\n"
        "    uses: ./.github/workflows/_platform.yml\n"
        "    with:\n"
        "      # ios is not in this list\n"
        "      sdks: >-\n"
        "        python js\n"
        "        go\n"
    )
    assert gate_sdks(text) == ["python", "js", "go"]


def test_runner_sdks_reads_the_manifest_keys():
    text = (
        "OTHER = {'ios': 1}\n"
        "MANIFEST: dict[str, tuple] = {\n"
        "    'python': ('python x.py', ('python',)),\n"
        "    'go': ('go run .', ('go',)),\n"
        "}\n"
    )
    assert runner_sdks(text) == {"python", "go"}


# ---------------------------------------------------------------------------
# The SDK READMEs under sdk/ carried the same hand-dated line. A README that
# carries the sentence is held to the gate's list the same way; none may carry
# the old form.
# ---------------------------------------------------------------------------
SDK_SOURCES = REPO_ROOT / "sdk"


def readme_problems(readmes: Mapping[str, str], gate: Sequence[str]) -> List[str]:
    """Every way the READMEs disagree with the gate's list; empty when they agree.

    ``readmes`` maps an SDK directory under sdk/ to its README.md text.
    """
    found: List[str] = []
    for name in sorted(readmes):
        readme = f"sdk/{name}/README.md"
        text = " ".join(readmes[name].split())
        if OLD_FORM in text:
            found.append(f"{readme}: still says {OLD_FORM!r}; use the sentence instead")
        for match in SENTENCE.finditer(text):
            says_runs = match["verb"] == "runs"
            if says_runs != (match["listed"] == "is") or says_runs != (
                match["sdk"] in gate
            ):
                found.append(
                    f"{readme}: the sentence for {match['sdk']!r} disagrees with the"
                    f" {JOB} job's sdks: list in pr-qa-gate.yml"
                )
    return found


def _real_readmes() -> Dict[str, str]:
    return {
        p.parent.name: p.read_text(encoding="utf-8")
        for p in SDK_SOURCES.glob("*/README.md")
    }


def test_every_sdk_readme_agrees_with_the_gate():
    readmes = _real_readmes()
    assert len(readmes) >= 10, sorted(readmes)
    found = readme_problems(readmes, _real_gate())
    assert not found, "\n".join(found)


@pytest.mark.regression
def test_a_readme_with_the_old_form_or_a_wrong_claim_is_refused():
    readmes = _real_readmes()
    readmes["ruby"] = "Verified against a live backend: yes (2026-09-11)"
    assert readme_problems(readmes, _real_gate()) == [
        "sdk/ruby/README.md: still says 'Verified against a live backend';"
        " use the sentence instead"
    ]
    assert readme_problems(_real_readmes(), _real_gate() + ["ios"]) == [
        "sdk/ios/README.md: the sentence for 'ios' disagrees with the"
        " sdk-live-contract job's sdks: list in pr-qa-gate.yml"
    ]
