"""The claims registry holds every unit of a registered document (#1142).

``scripts/doc_claims.py`` splits a document into units (headings, sentences,
list items, table rows, admonition titles) and checks each against
``docs/_claims/<document>.toml``: every unit on the page has an entry naming
what verifies it, every entry is on the page as many times as it says, and
every target resolves. Its docstring is the contract; this file holds it:

* the real tree: the registries, ``CLAIMS_PENDING``, ``CLAIMS_DEFERRED``, the
  ledger and ``MECHANISMS`` have no problem, and the counts are printed;
* ``CLAIMS_DEFERRED`` is exactly the 24 documents of #1107, a list kept here
  too, so moving a document there is a change to two files that review sees;
* the splitter, construct by construct, on fixtures, and byte for byte the
  same under two ``PYTHONHASHSEED`` values;
* one planted defect per refusal, on a small tree built in ``tmp_path``, with
  the unplanted tree as the positive control, and the edits that must NOT
  refuse (markup only, rewrapping, a comment);
* ``mkdocs.yml`` keeps the registries off the site.

The script is loaded by path: it imports nothing from ``backend`` or
``modules`` and reads files only, so this runs the same in
``scripts/core_build.sh``'s copy, where ``modules/`` targets are checked for
their form only. It is in the docs-only gate's "Docs content tests" through its
directory, ``backend/tests/unit/docs/``.
"""

from __future__ import annotations

import ast
import collections
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest
import yaml

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts" / "doc_claims.py"

_spec = importlib.util.spec_from_file_location("doc_claims", SCRIPT)
dc = importlib.util.module_from_spec(_spec)
sys.modules["doc_claims"] = dc  # dataclasses looks its module up
_spec.loader.exec_module(dc)

#: The 24 documents #1107 holds back: the mobile SDKs and the SDKs not yet
#: published. Kept here as well as in CLAIMS_DEFERRED on purpose.
DEFERRED_1107 = tuple(
    sorted(
        [f"docs/sdk/{s}.md" for s in ("java", "react", "openfeature", "edge")]
        + [f"docs/sdk/{s}.md" for s in ("ruby", "php", "dotnet", "elixir")]
        + [f"docs/sdk/{s}.md" for s in ("ios", "android", "flutter", "react-native")]
        + [f"sdk/{s}/README.md" for s in ("java", "react", "openfeature", "edge")]
        + [f"sdk/{s}/README.md" for s in ("ruby", "php", "dotnet", "elixir")]
        + [f"sdk/{s}/README.md" for s in ("ios", "android", "flutter")]
        + ["sdk/android/examples/ComposeExample/README.md"]
    )
)


# ---------------------------------------------------------------------------
# The real tree
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def real():
    return dc.check()


def test_the_registries_hold(real):
    print("\n".join(dc.report_lines(real.counts)))
    assert not real.problems, "\n".join(real.problems)


def test_something_is_registered(real):
    """A checker that found no registry would pass every rule above."""
    assert real.counts["registered"] >= 1
    assert real.counts["units"] >= 20
    assert (REPO_ROOT / dc.registry_path("docs/workspaces/quickstart.md")).is_file()


def test_deferred_is_exactly_the_documents_of_1107():
    assert len(DEFERRED_1107) == 24
    assert dc.CLAIMS_DEFERRED == DEFERRED_1107, (
        "CLAIMS_DEFERRED moves only with a founder decision; change this list with it"
    )


def test_the_policy_documents_are_in_scope():
    documents, _ = dc.inventory_documents(REPO_ROOT)
    assert set(dc.POLICY_DOCUMENTS) <= set(documents)


def test_the_report_names_what_is_out_of_scope_and_counts_the_exemptions(real):
    """The set of out-of-scope documents and the exemptions by category are
    printed, so a document moved out of scope or a unit exempted shows."""
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "report"],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO_ROOT,
    ).stdout
    documents, outside = dc.inventory_documents(REPO_ROOT)
    assert outside, "the inventory has no out-of-scope document to list"
    assert f"out of scope: {len(outside)}" in out
    for path, category in outside.items():
        assert f"  {path} ({category})" in out
    exempt = [k for k in real.counts["dispositions"] if k.startswith("exempt:")]
    assert exempt, "the registered units carry no exemption to count"
    for kind in exempt:
        assert f"  {kind}: {real.counts['dispositions'][kind]}" in out
    assert "ledger lines:" in out and "verifying targets:" in out


def test_mkdocs_keeps_the_registries_off_the_site():
    config = yaml.safe_load((REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8"))
    patterns = str(config.get("exclude_docs", "")).split()
    assert "_claims/" in patterns, (
        "mkdocs.yml exclude_docs must list _claims/, or the site publishes"
        " docs/_claims/"
    )


def test_the_script_uses_the_standard_library_only():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
    assert names, "the scan found no import: it is broken, not clean"
    outside = sorted(
        n for n in names if n not in sys.stdlib_module_names | {"__future__"}
    )
    assert not outside, f"scripts/doc_claims.py imports {outside}"


def test_pending_and_deferred_are_sorted_lists_of_documents():
    for listed in (dc.CLAIMS_PENDING, dc.CLAIMS_DEFERRED):
        assert list(listed) == sorted(set(listed))


# ---------------------------------------------------------------------------
# The splitter
# ---------------------------------------------------------------------------
def texts(markdown: str) -> List[str]:
    return [u.text for u in dc.units(markdown)]


SPLITS = [
    (
        "sentences of a hard-wrapped paragraph",
        "The API answers 200.\nIt never\ncaches a flag. Done!\n",
        ["The API answers 200.", "It never caches a flag.", "Done!"],
    ),
    (
        "a heading is a unit, its marks and attribute list removed",
        "# Title\n\n## Sub-millisecond `evaluation` ##\n\n### Next {#next}\n",
        ["Title", "Sub-millisecond evaluation", "Next"],
    ),
    (
        "a first-column # with no space is a heading, as the site renders it",
        "Text before.\n#217 and more\n",
        ["Text before.", "217 and more"],
    ),
    (
        "a setext heading: the line above the underline",
        "Intro line\nTitle line\n---\n\nOther\n===\n",
        ["Intro line", "Title line", "Other"],
    ),
    (
        "a --- after a blank line is a break, not a heading",
        "Above.\n\n---\n\nBelow.\n",
        ["Above.", "Below."],
    ),
    (
        "a fence at column 0 is dropped",
        "Run:\n\n```bash\ndocker compose up\n```\n\nThen look.\n",
        ["Run:", "Then look."],
    ),
    (
        "an indented fence under a list item is dropped",
        "1. Start it:\n\n    ```bash\n    docker compose up\n    ```\n\n2. Open it.\n",
        ["Start it:", "Open it."],
    ),
    (
        "a four-backtick fence holding a three-backtick line is one fence",
        "Before.\n\n````markdown\n```bash\nnot prose. At all.\n```\n````\n\nAfter.\n",
        ["Before.", "After."],
    ),
    (
        "a tilde fence",
        "~~~\nhidden text.\n~~~\nShown.\n",
        ["Shown."],
    ),
    (
        "an inline backtick run is not a fence",
        "Use ```x``` inline.\n",
        ["Use x inline."],
    ),
    (
        "HTML comments, inline and over several lines",
        "Kept <!-- dropped --> text.\n\n<!--\nA hidden claim.\n-->\nAfter.\n",
        ["Kept text.", "After."],
    ),
    (
        "a raw HTML block: tags removed, text kept",
        '<p align="center">\n  <b>Fast</b> and small.\n</p>\n',
        ["Fast and small."],
    ),
    (
        "an admonition: its title, then its body",
        '!!! note "Part of the `x` module"\n    Present in the full profile.\n'
        "    Absent from the core one.\n\n??? tip\n    Collapsed body.\n",
        [
            "Part of the x module",
            "Present in the full profile.",
            "Absent from the core one.",
            "Collapsed body.",
        ],
    ),
    (
        "a table: the header row and each body row, the separator dropped",
        "| Setting | Default |\n|---|---:|\n| `TIMEOUT` | 30 s |\n| **RETRIES** | 3 |\n\nAfter.\n",
        ["Setting | Default", "TIMEOUT | 30 s", "RETRIES | 3", "After."],
    ),
    (
        "an escaped pipe is part of its cell (javascript.md's onError row)",
        "| Option | Type |\n|---|---|\n| `onError` | `'track' \\| 'refresh'` |\n",
        ["Option | Type", "onError | 'track' | 'refresh'"],
    ),
    (
        "a table inside a list item",
        "- Item:\n\n    | A | B |\n    |---|---|\n    | 1 | 2 |\n",
        ["Item:", "A | B", "1 | 2"],
    ),
    (
        "list items and a nested list, each a block",
        "- One. Two\n  continued.\n- Three\n    - Four\n1. Five\n2) Six\n",
        ["One.", "Two continued.", "Three", "Four", "Five", "Six"],
    ),
    (
        "a blockquote is joined within its depth",
        "> Quoted\n> line.\n>\n> Second.\nPlain.\n",
        ["Quoted line.", "Second.", "Plain."],
    ),
    (
        "front matter is dropped",
        "---\ntitle: x\n---\nBody.\n",
        ["Body."],
    ),
    (
        "links, images, reference links and autolinks keep their text",
        "See [the guide](a.md#x), ![a chart](c.png), [docs][ref] and"
        " <https://example.com/x>.\n\n[ref]: https://example.com\n",
        ["See the guide, a chart, docs and https://example.com/x."],
    ),
    (
        "escapes resolved, backticks and stars removed",
        "Set \\*not bold\\* and `CODE` with **bold** text.\n",
        ["Set not bold and CODE with bold text."],
    ),
    (
        "a unit with no letter or digit is dropped",
        "| A |\n|---|\n| — |\n\n***\n\n...\n",
        ["A"],
    ),
]


@pytest.mark.parametrize(
    "markdown, expected", [c[1:] for c in SPLITS], ids=[c[0] for c in SPLITS]
)
def test_the_splitter(markdown, expected):
    assert texts(markdown) == expected


@pytest.mark.parametrize(
    "markdown",
    ["```bash\nnever closed\n", "Text\n<!-- never closed\nmore\n"],
    ids=["fence", "comment"],
)
def test_an_unclosed_block_is_an_error(markdown):
    with pytest.raises(dc.SplitError, match="never closes"):
        dc.units(markdown)


def test_a_unit_keeps_the_line_its_block_starts_on():
    units = dc.units("# T\n\nOne.\nTwo.\n\n- Three.\n")
    assert [(u.text, u.line) for u in units] == [
        ("T", 1),
        ("One.", 3),
        ("Two.", 3),
        ("Three.", 6),
    ]


def test_the_split_is_byte_identical_across_runs_and_hash_seeds():
    outputs = []
    for seed in ("0", "4242", "4242"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        outputs.append(
            subprocess.run(
                [sys.executable, str(SCRIPT), "units", "--all"],
                capture_output=True,
                check=True,
                cwd=REPO_ROOT,
                env=env,
            ).stdout
        )
    assert outputs[0].count(b"[[unit]]") > 10000, "the split found almost nothing"
    # Compared by digest: a diff of two megabytes would take minutes to print.
    digests = [hashlib.sha256(output).hexdigest() for output in outputs]
    assert len(set(digests)) == 1, f"the split differs between runs: {digests}"


# ---------------------------------------------------------------------------
# Planted trees
# ---------------------------------------------------------------------------
GUIDE = """# Guide

The service answers 200 for a healthy check.

A flag can be archived.
A flag can be archived.

Run the check:

```{.bash exec}
true
```
<!-- expect: "ok" -->

```{.bash exec}
true
```
<!-- expect: "second" -->

| Setting | Default |
|---|---|
| `TIMEOUT` | 30 s |

- Retries stop after three attempts.
"""

INVENTORY = """
[pages."guide.md"]
class = "reference"
reason = "r"

[pages."ops.md"]
class = "aws"
exercised_by = ["none"]
reason = "r"

[pages."run.md"]
class = "workflow"
workflow = "run-it"
reason = "r"

[pages."later.md"]
class = "reference"
reason = "r"

[documents."SECURITY.md"]
scope = "in"
class = "reference"
reason = "r"

[documents."CONTRIBUTING.md"]
scope = "out"
out = "contributor"
reason = "r"

[documents."app/Card.tsx"]
scope = "in"
class = "journey"
journey = "card"
pending = true
planned = "D73"
reason = "r"

[documents."hub.tsx#desc"]
scope = "in"
class = "reference"
extractor = "docs-hub-desc"
reason = "r"
"""

ENROLMENT = """[meta]
documents = 1
exec = 2
universe = []

[[document]]
path = "docs/guide.md"
environment = "bare"
exec = 2
skip = 0
expects = 2
"""

TESTS_PY = """import pytest


def test_alpha():
    pass


def helper():
    pass


@pytest.mark.benchmark
def test_slow():
    pass


class TestBeta:
    def test_gamma(self):
        pass


@pytest.mark.benchmark
class TestTimed:
    def test_fast(self):
        pass
"""

WORDING_PY = """def test_reads():
    pass


def test_quotes():
    assert "Retries stop after `three` attempts."
"""

JOURNEY = """guide: guide.md
steps:
  - id: archive
    do: {}
  - id: restore
    do: {}
"""


def unit(text: str, *by: str, **extra: Any) -> Dict[str, Any]:
    return {"text": text, "by": list(by), **extra}


def base() -> Dict[str, Any]:
    """The planted tree, as data: every file, the registries, and the lists."""
    return {
        "files": {
            dc.INVENTORY: INVENTORY,
            dc.ENROLMENT: ENROLMENT,
            "docs/guide.md": GUIDE,
            "docs/ops.md": "Restore was rehearsed on 2026-10-01.\n\n"
            "Failover is not rehearsed as of v0.28.2.\n",
            "docs/run.md": "The chart installs.\n",
            "docs/later.md": "Not registered yet.\n",
            "SECURITY.md": "We answer within two days.\n",
            "CONTRIBUTING.md": "How to help.\n",
            "app/Card.tsx": "export {}\n",
            "hub.tsx": "desc: 'x'\n",
            "tests/test_things.py": TESTS_PY,
            "tests/test_bench.py": "import pytest\n\npytestmark = pytest.mark.benchmark\n\n\ndef test_x():\n    pass\n",
            "tests/helpers.py": "def test_x():\n    pass\n",
            "backend/app/test_core.py": "def test_x():\n    pass\n",
            "backend/tests/unit/docs/test_wording.py": WORDING_PY,
            "sdk/js/tests/client.test.ts": "it('caches a flag', () => {});\n",
            "modules/tests/test_mod.py": "def test_m():\n    pass\n",
            f"{dc.JOURNEYS}/demo.yaml": JOURNEY,
        },
        "registries": {
            "docs/guide.md": [
                unit("Guide", "exempt:label"),
                unit(
                    "The service answers 200 for a healthy check.",
                    "test:tests/test_things.py::test_alpha",
                ),
                unit("A flag can be archived.", "journey:demo#archive", count=2),
                unit("Run the check:", 'docex:docs/guide.md#"ok"'),
                unit("Setting | Default", "mechanism:reads-guide"),
                unit(
                    "TIMEOUT | 30 s", "test:tests/test_things.py::TestBeta::test_gamma"
                ),
                unit("Retries stop after three attempts.", "untested:T"),
            ],
            "docs/ops.md": [
                unit(
                    "Restore was rehearsed on 2026-10-01.",
                    "evidence:https://github.com/getexperimently/experimently/actions/runs/123",
                ),
                unit("Failover is not rehearsed as of v0.28.2.", "exempt:intent"),
            ],
            "docs/run.md": [unit("The chart installs.", "workflow:run-it.yml")],
            "SECURITY.md": [unit("We answer within two days.", "exempt:policy")],
        },
        "ledger": [
            ("untested:T", "docs/guide.md", "Retries stop after three attempts."),
        ],
        "pending": ["docs/later.md", "hub.tsx#desc"],
        "deferred": [],
        "mechanisms": {
            "reads-guide": dc.Mechanism(
                "backend/tests/unit/docs/test_wording.py::test_reads",
                reads=("docs/guide.md",),
            ),
            "quotes": dc.Mechanism(
                "backend/tests/unit/docs/test_wording.py::test_quotes", quotes=True
            ),
        },
    }


def toml_registry(document: str, entries: List[Dict[str, Any]]) -> str:
    out = [f"document = {json.dumps(document)}"]
    for entry in entries:
        out.append("\n[[unit]]")
        out += [f"{k} = {json.dumps(v, ensure_ascii=False)}" for k, v in entry.items()]
    return "\n".join(out) + "\n"


def ledger_line(kind: str, document: str, text: str) -> str:
    return f"{kind}\t{document}\t{dc.unit_hash(text)}"


def build(tmp_path: Path, tree: Dict[str, Any]) -> dc.Result:
    for rel, text in tree["files"].items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    for document, entries in tree["registries"].items():
        path = tmp_path / dc.registry_path(document)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(toml_registry(document, entries), encoding="utf-8")
    lines = tree.get("ledger_raw") or sorted(
        ledger_line(*line) for line in tree["ledger"]
    )
    ledger = tmp_path / dc.LEDGER
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("# kind\tdocument\thash\n" + "".join(f"{l}\n" for l in lines))
    return dc.check(
        tmp_path,
        pending=tree["pending"],
        deferred=tree["deferred"],
        mechanisms=tree["mechanisms"],
    )


def test_the_planted_tree_is_clean(tmp_path):
    """The positive control: every case below differs from this by one defect."""
    result = build(tmp_path, base())
    assert not result.problems, "\n".join(result.problems)
    assert result.counts["registered"] == 4
    assert result.counts["targets"]["mechanism"] == 1
    assert result.counts["form only"] == 0


def edit(tree, path: str, old: str, new: str) -> None:
    text = tree["files"][path]
    assert old in text, f"the fixture no longer has {old!r}"
    tree["files"][path] = text.replace(old, new, 1)


def entry_of(tree, document: str, text: str) -> Dict[str, Any]:
    return next(e for e in tree["registries"][document] if e["text"] == text)


def retarget(document: str, text: str, *by: str, **extra: Any) -> Callable:
    def plant(tree):
        entry = entry_of(tree, document, text)
        entry["by"] = list(by)
        entry.update(extra)
        if (
            by[:1] != ("untested:T",)
            and ("untested:T", document, text) in tree["ledger"]
        ):
            tree["ledger"].remove(("untested:T", document, text))

    return plant


def add_unit(page_text: str) -> Callable:
    return lambda tree: edit(
        tree, "docs/guide.md", "Run the check:", page_text + "\n\nRun the check:"
    )


GUIDE_TEXT = "The service answers 200 for a healthy check."
RETRY = "Retries stop after three attempts."

#: (id, the one defect, a substring of the refusal).
PLANTED = [
    # The page and the registry, both ways.
    (
        "unregistered sentence",
        add_unit("The dashboard works offline."),
        "unregistered unit 'The dashboard works offline.'",
    ),
    (
        "listed, not on the page",
        lambda t: edit(t, "docs/guide.md", GUIDE_TEXT + "\n", ""),
        "is listed but no longer on docs/guide.md",
    ),
    (
        "one of two copies edited",
        lambda t: edit(
            t, "docs/guide.md", "A flag can be archived.\n", "A flag can be deleted.\n"
        ),
        "registered x2, found x1",
    ),
    (
        "listed twice",
        lambda t: t["registries"]["docs/guide.md"].append(
            unit("Guide", "exempt:label")
        ),
        "listed twice",
    ),
    (
        "count 1",
        lambda t: entry_of(t, "docs/guide.md", "Guide").update(count=1),
        "count is 2-999",
    ),
    (
        "count not a number",
        lambda t: entry_of(t, "docs/guide.md", "Guide").update(count="2"),
        "count is 2-999",
    ),
    ("no target", retarget("docs/guide.md", "Guide"), "by must be a non-empty list"),
    (
        "a target twice",
        retarget("docs/guide.md", "Guide", "exempt:label", "exempt:label"),
        "names a target twice",
    ),
    (
        "unknown kind",
        retarget("docs/guide.md", "Guide", "issue:12"),
        "unknown target kind 'issue'",
    ),
    (
        "unknown entry key",
        lambda t: entry_of(t, "docs/guide.md", "Guide").update(note="x"),
        "unknown keys ['note']",
    ),
    # test:
    (
        "test file gone",
        retarget("docs/guide.md", GUIDE_TEXT, "test:tests/test_gone.py::test_alpha"),
        "tests/test_gone.py does not exist",
    ),
    (
        "test function gone",
        retarget(
            "docs/guide.md", GUIDE_TEXT, "test:tests/test_things.py::test_missing"
        ),
        "has no test_missing",
    ),
    (
        "test method gone",
        retarget(
            "docs/guide.md",
            GUIDE_TEXT,
            "test:tests/test_things.py::TestBeta::test_missing",
        ),
        "has no TestBeta::test_missing",
    ),
    (
        "not a test function",
        retarget("docs/guide.md", GUIDE_TEXT, "test:tests/test_things.py::helper"),
        "is not a test",
    ),
    (
        "application code",
        retarget("docs/guide.md", GUIDE_TEXT, "test:backend/app/test_core.py::test_x"),
        "is not a test (application code",
    ),
    (
        "a wording test",
        retarget(
            "docs/guide.md",
            GUIDE_TEXT,
            "test:backend/tests/unit/docs/test_wording.py::test_reads",
        ),
        "reads the page's wording",
    ),
    (
        "not a test file",
        retarget("docs/guide.md", GUIDE_TEXT, "test:tests/helpers.py::test_x"),
        "is not a test file",
    ),
    (
        "benchmark function",
        retarget("docs/guide.md", GUIDE_TEXT, "test:tests/test_things.py::test_slow"),
        "is marked benchmark",
    ),
    (
        "benchmark class",
        retarget(
            "docs/guide.md",
            GUIDE_TEXT,
            "test:tests/test_things.py::TestTimed::test_fast",
        ),
        "is marked benchmark",
    ),
    (
        "benchmark module",
        retarget("docs/guide.md", GUIDE_TEXT, "test:tests/test_bench.py"),
        "is marked benchmark",
    ),
    (
        "a parameter id",
        retarget(
            "docs/guide.md", GUIDE_TEXT, "test:tests/test_things.py::test_alpha[x]"
        ),
        "not a function or Class::method",
    ),
    (
        "another language, name gone",
        retarget(
            "docs/guide.md",
            GUIDE_TEXT,
            "test:sdk/js/tests/client.test.ts::caches nothing",
        ),
        "does not contain 'caches nothing'",
    ),
    (
        "a modules/ target that is gone",
        retarget(
            "docs/guide.md", GUIDE_TEXT, "test:modules/tests/test_gone.py::test_m"
        ),
        "modules/tests/test_gone.py does not exist",
    ),
    # journey:
    (
        "journey not written",
        retarget(
            "docs/guide.md", "A flag can be archived.", "journey:card#archive", count=2
        ),
        "a pending journey holds nothing",
    ),
    (
        "journey step gone",
        retarget(
            "docs/guide.md", "A flag can be archived.", "journey:demo#delete", count=2
        ),
        "has no step 'delete'",
    ),
    # docex:
    (
        "docex, page not enrolled",
        retarget("docs/guide.md", "Run the check:", 'docex:docs/ops.md#"ok"'),
        "does not enrol docs/ops.md",
    ),
    (
        "docex, no such expectation",
        retarget("docs/guide.md", "Run the check:", 'docex:docs/guide.md#"gone"'),
        "0 exec blocks",
    ),
    (
        "docex, its block switched to skip",
        lambda t: edit(
            t,
            "docs/guide.md",
            '```{.bash exec}\ntrue\n```\n<!-- expect: "ok" -->',
            '```{.bash skip reason="aws: x"}\ntrue\n```',
        ),
        "0 exec blocks of docs/guide.md expect",
    ),
    (
        "docex, two blocks",
        lambda t: edit(t, "docs/guide.md", '"second"', '"ok"'),
        "2 exec blocks",
    ),
    # workflow:
    (
        "workflow on a reference page",
        retarget("docs/guide.md", GUIDE_TEXT, "workflow:run-it.yml"),
        "is not class workflow",
    ),
    (
        "workflow, another one",
        retarget("docs/run.md", "The chart installs.", "workflow:other.yml"),
        "is not class workflow",
    ),
    # mechanism:
    (
        "unknown mechanism",
        retarget("docs/guide.md", GUIDE_TEXT, "mechanism:nope"),
        "not one of MECHANISMS",
    ),
    (
        "mechanism reads another page",
        retarget("docs/run.md", "The chart installs.", "mechanism:reads-guide"),
        "reads docs/guide.md, not docs/run.md",
    ),
    (
        "mechanism does not quote it",
        retarget("docs/guide.md", GUIDE_TEXT, "mechanism:quotes"),
        "does not quote this unit word for word",
    ),
    (
        "mechanism's test gone",
        lambda t: t["mechanisms"].update(
            gone=dc.Mechanism(
                "backend/tests/unit/docs/test_wording.py::test_gone", reads=("x",)
            )
        ),
        "MECHANISMS['gone']: ",
    ),
    (
        "mechanism both ways",
        lambda t: t["mechanisms"].update(
            both=dc.Mechanism(
                "backend/tests/unit/docs/test_wording.py::test_reads",
                reads=("x",),
                quotes=True,
            )
        ),
        "either quotes or reads",
    ),
    # evidence:
    (
        "evidence on a page not aws",
        retarget(
            "docs/run.md",
            "The chart installs.",
            "evidence:https://github.com/getexperimently/experimently/actions/runs/1",
        ),
        "only for a document of class aws",
    ),
    (
        "evidence from elsewhere",
        retarget(
            "docs/ops.md",
            "Restore was rehearsed on 2026-10-01.",
            "evidence:https://example.com/runs/1",
        ),
        "not evidence:",
    ),
    (
        "evidence, undated",
        lambda t: (
            edit(t, "docs/ops.md", "on 2026-10-01", "last week"),
            entry_of(t, "docs/ops.md", "Restore was rehearsed on 2026-10-01.").update(
                text="Restore was rehearsed last week."
            ),
        ),
        "needs a dated unit",
    ),
    # exempt:
    (
        "unknown category",
        retarget("docs/guide.md", "Guide", "exempt:obvious"),
        "exempt:obvious is not one of",
    ),
    (
        "intent on a T unit of a page not aws",
        lambda t: (
            edit(
                t,
                "docs/run.md",
                "The chart installs.",
                "As of v0.29.0, the chart installs.",
            ),
            t["registries"].update(
                {
                    "docs/run.md": [
                        unit("As of v0.29.0, the chart installs.", "exempt:intent")
                    ]
                }
            ),
        ),
        "only for a document of class aws",
    ),
    (
        "intent, undated",
        lambda t: (
            edit(t, "docs/ops.md", "as of v0.28.2", "yet"),
            entry_of(
                t, "docs/ops.md", "Failover is not rehearsed as of v0.28.2."
            ).update(text="Failover is not rehearsed yet."),
        ),
        "needs a dated unit",
    ),
    (
        "intent, a version but not the phrase",
        lambda t: (
            edit(t, "docs/ops.md", "as of v0.28.2", "in v0.28.2"),
            entry_of(
                t, "docs/ops.md", "Failover is not rehearsed as of v0.28.2."
            ).update(text="Failover is not rehearsed in v0.28.2."),
        ),
        "needs a dated unit",
    ),
    (
        "intent, no such date",
        lambda t: (
            edit(t, "docs/ops.md", "as of v0.28.2", "on 2026-13-40"),
            entry_of(
                t, "docs/ops.md", "Failover is not rehearsed as of v0.28.2."
            ).update(text="Failover is not rehearsed on 2026-13-40."),
        ),
        "needs a dated unit",
    ),
    (
        "policy outside the three",
        retarget("docs/guide.md", GUIDE_TEXT, "exempt:policy"),
        "exempt:policy is only for",
    ),
    (
        "a tag on a claim",
        lambda t: entry_of(t, "docs/guide.md", "Guide").update(tag="X"),
        "tag is only for an exempt:intent unit",
    ),
    (
        "a tag other than X",
        lambda t: entry_of(
            t, "docs/ops.md", "Failover is not rehearsed as of v0.28.2."
        ).update(tag="T"),
        'tag takes "X" only',
    ),
    (
        "exempt with a test",
        retarget(
            "docs/guide.md",
            "Guide",
            "exempt:label",
            "test:tests/test_things.py::test_alpha",
        ),
        "stands alone",
    ),
    (
        "untested with a test",
        lambda t: entry_of(t, "docs/guide.md", RETRY).update(
            by=["untested:T", "test:tests/test_things.py::test_alpha"]
        ),
        "stands alone",
    ),
    # The ledger.
    (
        "debt with no ledger line",
        lambda t: t["ledger"].clear(),
        "has no line in docs/_claims/_frozen.tsv",
    ),
    (
        "a ledger line with no debt",
        lambda t: t["ledger"].append(("untested:S", "docs/guide.md", "Guide")),
        "matches no untested:S unit",
    ),
    (
        "the ledger names another kind",
        lambda t: t["ledger"].__setitem__(0, ("untested:S", "docs/guide.md", RETRY)),
        "matches no untested:S unit",
    ),
    (
        "debt swapped for other debt",
        lambda t: t["ledger"].__setitem__(0, ("untested:T", "docs/guide.md", "Guide")),
        "has no line in",
    ),
    (
        "a malformed ledger line",
        lambda t: t.update(ledger_raw=["untested:T docs/guide.md abc"]),
        "is not kind<TAB>document<TAB>hash",
    ),
    (
        "an unsorted ledger",
        lambda t: t.update(
            ledger_raw=[
                ledger_line("untested:T", "docs/guide.md", RETRY),
                ledger_line("exempt:baseline", "docs/guide.md", "Guide"),
            ]
        ),
        "sorted and appear once",
    ),
    # Which documents have registries.
    (
        "a registry for an out-of-scope file",
        lambda t: t["registries"].update(
            {"CONTRIBUTING.md": [unit("How to help.", "exempt:advice")]}
        ),
        "is out of scope, or held by its journey",
    ),
    (
        "a registry for a journey's surface",
        lambda t: t["registries"].update({"app/Card.tsx": [unit("x", "exempt:label")]}),
        "is out of scope, or held by its journey",
    ),
    (
        "a registry for no document",
        lambda t: t["registries"].update({"docs/nope.md": [unit("x", "exempt:label")]}),
        "not the registry of an in-scope document",
    ),
    (
        "a stray file",
        lambda t: t["files"].update({f"{dc.CLAIMS_DIR}/notes.txt": "x\n"}),
        "not the registry of an in-scope document",
    ),
    (
        "a registry for a surface with no extractor yet",
        lambda t: (
            t["pending"].remove("hub.tsx#desc"),
            t["registries"].update({"hub.tsx#desc": [unit("x", "exempt:label")]}),
        ),
        "has no 'docs-hub-desc' extractor yet",
    ),
    (
        "a registry for a pending page",
        lambda t: t["registries"].update(
            {"docs/later.md": [unit("Not registered yet.", "exempt:label")]}
        ),
        "docs/later.md has a registry; take it off the list",
    ),
    (
        "a registry naming another document",
        lambda t: (
            t["registries"].pop("docs/run.md"),
            t["files"].update(
                {dc.registry_path("docs/run.md"): 'document = "docs/guide.md"\n'}
            ),
        ),
        "document = 'docs/run.md'",
    ),
    (
        "a registry that does not parse",
        lambda t: (
            t["registries"].pop("docs/run.md"),
            t["files"].update({dc.registry_path("docs/run.md"): "document = \n"}),
        ),
        "does not parse",
    ),
    (
        "a page that does not split",
        lambda t: edit(
            t,
            "docs/run.md",
            "The chart installs.\n",
            "The chart installs.\n\n```bash\nnever closed\n",
        ),
        "never closes",
    ),
    (
        "a document with nothing",
        lambda t: t["pending"].remove("docs/later.md"),
        "docs/later.md: in scope, with no registry",
    ),
    (
        "pending, not in scope",
        lambda t: t["pending"].append("docs/zz-deleted.md"),
        "docs/zz-deleted.md is not an in-scope document",
    ),
    (
        "deferred, not in scope",
        lambda t: t["deferred"].append("docs/zz-deleted.md"),
        "CLAIMS_DEFERRED: docs/zz-deleted.md is not an in-scope document",
    ),
    (
        "pending and deferred",
        lambda t: t["deferred"].append("docs/later.md"),
        "in CLAIMS_PENDING and CLAIMS_DEFERRED",
    ),
    ("pending unsorted", lambda t: t["pending"].reverse(), "CLAIMS_PENDING is sorted"),
]


@pytest.mark.parametrize(
    "plant, refusal", [c[1:] for c in PLANTED], ids=[c[0] for c in PLANTED]
)
def test_each_refusal_fires(tmp_path, plant, refusal):
    tree = base()
    plant(tree)
    problems = build(tmp_path, tree).problems
    assert any(refusal in p for p in problems), "\n".join(problems) or "no problem"


#: QA's planted claims (#1142): each is refused as an unregistered unit.
NEW_CLAIMS = [
    ("prose", "The dashboard works offline."),
    ("table row", "| Offline | The dashboard works offline. |\n|---|---|\n| a | b |"),
    ("list item", "- The dashboard works offline."),
    ("hard-wrapped", "The dashboard works\noffline."),
    ("admonition body", '!!! note "Note"\n    The dashboard works offline.'),
    ("heading", "## Sub-millisecond evaluation"),
    (
        "the paraphrase",
        "Each experiment gets its own Jira ticket, and its results appear on the ticket.",
    ),
]


@pytest.mark.parametrize(
    "claim", [c[1] for c in NEW_CLAIMS], ids=[c[0] for c in NEW_CLAIMS]
)
def test_a_new_claim_in_any_shape_is_refused(tmp_path, claim):
    tree = base()
    add_unit(claim)(tree)
    problems = build(tmp_path, tree).problems
    assert any("unregistered unit" in p for p in problems), "\n".join(problems)


def test_a_reflow_that_makes_a_list_item_is_refused(tmp_path):
    """A wrapped line that now starts "+ " renders as a list item: new units."""
    tree = base()
    edit(tree, "docs/guide.md", "for a healthy check.", "for a healthy\n+ check.")
    problems = build(tmp_path, tree).problems
    assert any("unregistered unit 'check.'" in p for p in problems), "\n".join(problems)


#: Edits that change no unit: none may refuse (precision).
NOT_EDITS = [
    (
        "inside a comment",
        lambda t: add_unit("<!-- The dashboard works offline. -->")(t),
    ),
    (
        "rewrapped",
        lambda t: edit(t, "docs/guide.md", "answers 200 for a", "answers 200\nfor a"),
    ),
    (
        "table re-padded",
        lambda t: edit(
            t, "docs/guide.md", "| `TIMEOUT` | 30 s |", "|   `TIMEOUT`   |   30 s   |"
        ),
    ),
    ("backticks dropped", lambda t: edit(t, "docs/guide.md", "`TIMEOUT`", "TIMEOUT")),
    (
        "bold added",
        lambda t: edit(t, "docs/guide.md", "answers 200", "answers **200**"),
    ),
    (
        "link added",
        lambda t: edit(t, "docs/guide.md", "healthy check", "[healthy check](x.md)"),
    ),
    (
        "a unit the mechanism quotes",
        lambda t: (
            retarget("docs/guide.md", RETRY, "mechanism:quotes")(t),
            t["ledger"].clear(),
        ),
    ),
    (
        "a modules/ target where modules/ is absent",
        lambda t: (
            retarget(
                "docs/guide.md", GUIDE_TEXT, "test:modules/tests/test_gone.py::test_m"
            )(t),
            t["files"].pop("modules/tests/test_mod.py"),
        ),
    ),
]


@pytest.mark.parametrize(
    "plant", [c[1] for c in NOT_EDITS], ids=[c[0] for c in NOT_EDITS]
)
def test_what_is_not_an_edit_is_not_refused(tmp_path, plant):
    tree = base()
    plant(tree)
    result = build(tmp_path, tree)
    assert not result.problems, "\n".join(result.problems)


def test_a_modules_target_without_modules_is_counted_not_checked(tmp_path):
    tree = base()
    retarget("docs/guide.md", GUIDE_TEXT, "test:modules/tests/test_gone.py::test_m")(
        tree
    )
    tree["files"].pop("modules/tests/test_mod.py")
    result = build(tmp_path, tree)
    assert not (tmp_path / "modules").exists()
    assert result.counts["form only"] == 1
    assert "for form only (no modules/ here): 1" in "\n".join(
        dc.report_lines(result.counts)
    )


def test_a_malformed_modules_target_is_refused_without_modules(tmp_path):
    tree = base()
    retarget("docs/guide.md", GUIDE_TEXT, "test:modules/tests/helpers.py::test_m")(tree)
    tree["files"].pop("modules/tests/test_mod.py")
    problems = build(tmp_path, tree).problems
    assert any("is not a test file" in p for p in problems), "\n".join(problems)


def test_an_intent_tagged_x_may_stand_on_any_page(tmp_path):
    """The other half of the intent rule: an X unit, dated, tagged."""
    tree = base()
    edit(tree, "docs/run.md", "The chart installs.", "Measured on 2026-10-01: 40 ms.")
    tree["registries"]["docs/run.md"] = [
        unit("Measured on 2026-10-01: 40 ms.", "exempt:intent", tag="X")
    ]
    result = build(tmp_path, tree)
    assert not result.problems, "\n".join(result.problems)


def test_the_counts_add_up(tmp_path):
    result = build(tmp_path, base())
    counts = result.counts
    assert counts["units"] == 12  # 8 on the guide (one twice), 2, 1, 1
    assert counts["dispositions"] == {
        "exempt:intent": 1,
        "exempt:label": 1,
        "exempt:policy": 1,
        "untested:T": 1,
        "verified": 8,
    }
    assert counts["ledger"] == {"untested:T": 1}
    assert counts["out of scope"] == {"CONTRIBUTING.md": "contributor"}
    assert collections.Counter(counts["targets"]) == collections.Counter(
        {
            "test": 2,
            "journey": 1,
            "docex": 1,
            "mechanism": 1,
            "workflow": 1,
            "evidence": 1,
        }
    )


def test_a_whole_file_target_is_counted_apart(tmp_path):
    """A test: target with no function holds a claim with any test of the file:
    allowed, and counted as test-file, so the report shows how many there are."""
    tree = base()
    retarget("docs/guide.md", GUIDE_TEXT, "test:tests/test_things.py")(tree)
    result = build(tmp_path, tree)
    assert not result.problems, "\n".join(result.problems)
    assert result.counts["targets"]["test-file"] == 1
    assert result.counts["targets"]["test"] == 1
    assert "  test-file: 1" in dc.report_lines(result.counts)


def test_a_skeleton_lists_every_unit_once_with_its_count():
    text = dc.skeleton("docs/x.md", "# T\n\nSame. Same.\n")
    assert text.count("[[unit]]") == 2
    assert 'text = "Same."\nby = []\ncount = 2\n' in text
