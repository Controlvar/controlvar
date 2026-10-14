"""The registry generator, ``sync`` and the baseline check (#1142).

``scripts/doc_claims.py generate`` writes a page group's registries from the
doc-claims audit, whose rows are private: the script reads them from a path its
caller gives. Nothing here is copied from them. Every page, row and test below
is made up, one fixture per shape of the audit's ``by`` column and one planted
defect per rule:

* each ``by`` shape maps to its target, or is printed (by hand, or refused);
* rows map to units by line, the excerpt deciding among a line's units;
* uncovered rows give ledgered debt, units with no row ``exempt:baseline``, and
  a unit new since the audit no entry;
* an R or X row still on the page stops that page being written;
* what ``generate`` writes passes ``check``, end to end through ``git show``;
* ``sync`` prints near matches and writes nothing;
* ``baseline`` finds an audit claim row left at ``exempt:baseline``;
* the OpenAPI snapshot test is a ``test:`` target, and only while it reads no
  page.

The script is loaded by path, as ``test_doc_claims.py`` loads it.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts" / "doc_claims.py"

_spec = importlib.util.spec_from_file_location("doc_claims", SCRIPT)
dc = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("doc_claims", dc)
_spec.loader.exec_module(dc)

SNAPSHOT_FULL = dc.SNAPSHOT_TARGETS["full"]
#: A made-up commit: with ``old_text`` given, the generator reads no git.
FAKE_SHA = "abc1234"
SNAPSHOT_STABLE = dc.SNAPSHOT_TARGETS["stable"]


# ---------------------------------------------------------------------------
# A planted tree: the page at the audit commit, the page now, and its tests
# ---------------------------------------------------------------------------
OLD_GUIDE = """# Guide

The service answers 200 for a healthy check. It never caches a flag.

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

```{.bash exec}
true
```

| Setting | Default |
|---|---|
| `TIMEOUT` | 30 s |

- Retries stop after three attempts.
- Webhooks are signed.

Old sentence that is reworded later.

Exports are kept for a week.

The cache is warmed hourly.

```text
answers in 5 ms
```

Two more:

```{.bash exec}
true
```
<!-- expect: "shared" -->
<!-- expect: "fourth" -->

```{.bash exec}
true
```
<!-- expect: "shared" -->
"""

NOW_GUIDE = (
    OLD_GUIDE.replace(
        "Old sentence that is reworded later.", "A sentence written since then."
    )
    .replace("The cache is warmed hourly.", "The cache is warmed when it starts.")
    .replace("Exports are kept for a week.", "Exports are kept for a week.\n\nGuide")
)

INVENTORY = """
[pages."guide.md"]
class = "reference"
reason = "r"

[pages."run.md"]
class = "workflow"
workflow = "run-it"
reason = "r"

[pages."sdk/ios.md"]
class = "reference"
reason = "r"

[documents."CONTRIBUTING.md"]
scope = "out"
out = "contributor"
reason = "r"
"""

ENROLMENT = """[meta]
documents = 1
exec = 5
universe = []

[[document]]
path = "docs/guide.md"
environment = "bare"
exec = 5
skip = 0
expects = 5
"""

TESTS_PY = """import pytest


def test_alpha():
    pass


class TestBeta:
    def test_gamma(self):
        pass

    def test_twice(self):
        pass


class TestDelta:
    def test_twice(self):
        pass


@pytest.mark.benchmark
def test_slow():
    pass
"""

SNAPSHOT_PY = """REPO_ROOT = None
DOCS_API = REPO_ROOT / "docs" / "api"
FULL = DOCS_API / "openapi-v1.full.json"


def test_core_api_matches_stable_snapshot():
    pass


def test_full_api_matches_full_snapshot():
    pass
"""

JOURNEY = """guide: guide.md
steps:
  - id: archive
    do: {}
  - id: restore
    do: {}
"""

FILES: Dict[str, str] = {
    dc.INVENTORY: INVENTORY,
    dc.ENROLMENT: ENROLMENT,
    "docs/guide.md": NOW_GUIDE,
    "docs/run.md": "The chart installs.\n",
    "docs/sdk/ios.md": "Deferred.\n",
    "CONTRIBUTING.md": "How to help.\n",
    "tests/test_things.py": TESTS_PY,
    "tests/unit/test_unique.py": "def test_u():\n    pass\n",
    "a/test_same_name.py": "def test_s():\n    pass\n",
    "b/test_same_name.py": "def test_s():\n    pass\n",
    "sdk/js/tests/client.test.ts": "it('getAllFlags', () => {});\n",
    "scripts/sign.py": "print(1)\n",
    "backend/app/core/hashing.py": "X = 1\n",
    "backend/tests/unit/docs/test_wording.py": "def test_reads():\n    pass\n",
    dc.SNAPSHOT_TEST: SNAPSHOT_PY,
    f"{dc.JOURNEYS}/demo.yaml": JOURNEY,
    dc.DOCS_GATE: f'DOCS_TESTS = ("backend/tests/unit/docs/", "{dc.SNAPSHOT_TEST}")\n'
    "NOT_DOCS_TESTS = {}\n",
    ".github/workflows/run-it.yml": "on: push\n",
    ".github/workflows/nightly.yml": "on: push\n",
}


def build(root: Path, files: Optional[Dict[str, str]] = None) -> Path:
    for rel, text in (files or FILES).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def line_of(text: str, needle: str) -> int:
    """The 1-based line of the first line of *text* that contains *needle*."""
    lines = text.split("\n")
    found = [n for n, line in zip(range(1, len(lines) + 1), lines) if needle in line]
    assert found, f"the fixture no longer has {needle!r}"
    return found[0]


def row(
    needle: str,
    *,
    by: Optional[str] = None,
    tag: Optional[str] = None,
    kind: str = "claim",
    doc: str = "docs/guide.md",
    text: Optional[str] = None,
    line: Optional[int] = None,
    old: str = OLD_GUIDE,
) -> Dict[str, Any]:
    """A made-up audit row on the line of *old* that holds *needle*."""
    return {
        "batch": "B00",
        "doc": doc,
        "kind": kind,
        "line": line or line_of(old, needle),
        "text": text if text is not None else needle,
        "covered": by is not None,
        "link": "behaviour" if by else None,
        "by": by,
        "tag": tag,
    }


def old_texts(**extra: str):
    texts = {"docs/guide.md": OLD_GUIDE, "docs/run.md": "The chart installs.\n"}
    texts.update(extra)
    return texts.get


def resolver(root: Path, document: str = "docs/guide.md", **old: str):
    documents, _ = dc.inventory_documents(root)
    found = dc.Resolver(dc.Context(root, documents, dc.MECHANISMS), old_texts(**old))
    found.document = documents[document]
    return found


def plan(root: Path, rows: List[Dict[str, Any]], document: str = "docs/guide.md"):
    documents, _ = dc.inventory_documents(root)
    loaded = [dc._row_of(r) for r in rows]
    assert all(isinstance(r, dc.Row) for r in loaded), loaded
    return dc.plan_document(resolver(root, document), documents[document], loaded)


def entry(result, text: str) -> Optional[Dict[str, Any]]:
    return next((e for e in result.entries if e["text"] == text), None)


# ---------------------------------------------------------------------------
# One fixture per shape of ``by``
# ---------------------------------------------------------------------------
FIRST_BLOCK = line_of(OLD_GUIDE, "```{.bash exec}")
THIRD_BLOCK = FIRST_BLOCK + 10
FOURTH_BLOCK = line_of(OLD_GUIDE, "Two more:") + 2
FIFTH_BLOCK = FOURTH_BLOCK + 6

#: (id, by, the targets it maps to, a substring of what is printed by hand or
#: refused, or None when nothing is).
BY_SHAPES = [
    (
        "path::name",
        "tests/test_things.py::test_alpha",
        ["test:tests/test_things.py::test_alpha"],
        None,
    ),
    (
        "path::Class::method",
        "tests/test_things.py::TestBeta::test_gamma",
        ["test:tests/test_things.py::TestBeta::test_gamma"],
        None,
    ),
    (
        "path::method, its class found",
        "tests/test_things.py::test_gamma",
        ["test:tests/test_things.py::TestBeta::test_gamma"],
        None,
    ),
    (
        "path::method in two classes",
        "tests/test_things.py::test_twice",
        [],
        "has no test_twice",
    ),
    (
        "path::a, b and ::c lists",
        "tests/test_things.py::test_alpha, test_gamma (a note), ::TestDelta::test_twice",
        [
            "test:tests/test_things.py::test_alpha",
            "test:tests/test_things.py::TestBeta::test_gamma",
            "test:tests/test_things.py::TestDelta::test_twice",
        ],
        None,
    ),
    (
        "path (name, name)",
        "tests/test_things.py (test_alpha, TestBeta::test_gamma)",
        [
            "test:tests/test_things.py::test_alpha",
            "test:tests/test_things.py::TestBeta::test_gamma",
        ],
        None,
    ),
    (
        "a parametrised id",
        "tests/test_things.py::test_alpha[case-1]",
        ["test:tests/test_things.py::test_alpha"],
        None,
    ),
    (
        "a benchmark",
        "tests/test_things.py::test_slow",
        [],
        "benchmark",
    ),
    (
        "a file path",
        "tests/test_things.py (asserts the 200)",
        ["test:tests/test_things.py"],
        None,
    ),
    (
        "a bare file name, found",
        "test_unique.py",
        ["test:tests/unit/test_unique.py"],
        None,
    ),
    (
        "a bare file name, twice in the tree",
        "test_same_name.py",
        [],
        "no such test file here, or several",
    ),
    (
        "a test file with a line",
        "sdk/js/tests/client.test.ts:12",
        ["test:sdk/js/tests/client.test.ts"],
        None,
    ),
    (
        "a JS test by name",
        "sdk/js/tests/client.test.ts::getAllFlags",
        ["test:sdk/js/tests/client.test.ts::getAllFlags"],
        None,
    ),
    (
        "two items",
        "tests/test_things.py::test_alpha; journey demo#archive",
        ["test:tests/test_things.py::test_alpha", "journey:demo#archive"],
        None,
    ),
    (
        "journey s#a,b",
        "journey demo#archive,restore (both checked)",
        ["journey:demo#archive", "journey:demo#restore"],
        None,
    ),
    (
        "journey s#a (note), #b",
        "journey demo#archive (says so), #restore",
        ["journey:demo#archive", "journey:demo#restore"],
        None,
    ),
    (
        "journey, no step",
        "journey demo (the guide)",
        [],
        "names no step",
    ),
    (
        "journey, no such step",
        "journey demo#rename",
        [],
        "has no step 'rename'",
    ),
    (
        "doc-examples page:line",
        f"doc-examples docs/guide.md:{FIRST_BLOCK} (bare), expect ok",
        ['docex:docs/guide.md#"ok"'],
        None,
    ),
    (
        "doc-examples bare page name, :N and :M",
        f"doc-examples guide.md:{FIRST_BLOCK} (one) and :{FIRST_BLOCK + 5} (two)",
        ['docex:docs/guide.md#"ok"', 'docex:docs/guide.md#"second"'],
        None,
    ),
    (
        "doc-examples page:a-b",
        f"doc-examples docs/guide.md:{FIRST_BLOCK}-{FIRST_BLOCK + 6}",
        ['docex:docs/guide.md#"ok"', 'docex:docs/guide.md#"second"'],
        None,
    ),
    (
        "doc-examples, a block with no expect line",
        f"doc-examples docs/guide.md:{THIRD_BLOCK}",
        [],
        "no expect line",
    ),
    (
        "doc-examples, the expectation only that block has today",
        f"doc-examples docs/guide.md:{FOURTH_BLOCK}",
        ['docex:docs/guide.md#"fourth"'],
        None,
    ),
    (
        "doc-examples, an expectation another block has too",
        f"doc-examples docs/guide.md:{FIFTH_BLOCK}",
        [],
        "no expectation of that block names exactly one block today",
    ),
    (
        "doc-examples, no line",
        "doc-examples docs/guide.md (bare): the whole page",
        [],
        "names no block",
    ),
    (
        "doc-examples, a page not enrolled",
        "doc-examples docs/run.md:1",
        [],
        "not one document Doc Examples enrols",
    ),
    (
        "the OpenAPI snapshot test, full",
        f"{dc.SNAPSHOT_TEST} (route in docs/api/openapi-v1.full.json)",
        [SNAPSHOT_FULL],
        None,
    ),
    (
        "the stable snapshot named",
        "docs/api/openapi-v1.stable.json has the route",
        [SNAPSHOT_STABLE],
        None,
    ),
    (
        "openapi-v1.*.json",
        "the route is in openapi-v1.*.json",
        [SNAPSHOT_FULL],
        None,
    ),
    (
        "the snapshot test by function",
        f"{dc.SNAPSHOT_TEST}::test_core_api_matches_stable_snapshot",
        [SNAPSHOT_STABLE],
        None,
    ),
    (
        "a workflow, page not of that class",
        ".github/workflows/nightly.yml (the nightly job)",
        [],
        "is not class workflow",
    ),
    (
        "a bare workflow name, page not of that class",
        "nightly.yml Benchmarks job",
        [],
        "is not class workflow",
    ),
    (
        "a script",
        "scripts/sign.py signs them",
        [],
        "a script or application source",
    ),
    (
        "application source",
        "backend/app/core/hashing.py MD5",
        [],
        "a script or application source",
    ),
    (
        "app source by a short path",
        "core/hashing.py",
        [],
        "a script or application source",
    ),
    (
        "a test that reads a page",
        "backend/tests/unit/docs/test_wording.py::test_reads",
        [],
        "reads documentation",
    ),
    (
        "a config file",
        "pyproject.toml requires-python",
        [],
        "not a test, a journey step",
    ),
    (
        "prose only",
        "`reuse lint` in the required lint job",
        [],
        "names no test, journey step",
    ),
    (
        "a directory",
        "modules/backend/tests (workspace routes)",
        [],
        "names no test, journey step",
    ),
]


@pytest.mark.parametrize(
    "by, targets, printed",
    [case[1:] for case in BY_SHAPES],
    ids=[case[0] for case in BY_SHAPES],
)
def test_each_shape_of_by_maps_or_is_printed(tmp_path, by, targets, printed):
    mapped = resolver(build(tmp_path)).targets(by)
    assert mapped.targets == targets
    shown = mapped.by_hand + mapped.refused
    if printed is None:
        assert not shown, shown
    else:
        assert any(printed in line for line in shown), shown
    if "a script or application source" in (printed or ""):
        assert mapped.refused and not mapped.targets


def test_a_workflow_maps_on_a_page_of_that_class(tmp_path):
    root = build(tmp_path)
    found = resolver(root, "docs/run.md").targets(
        ".github/workflows/run-it.yml runs it"
    )
    assert found.targets == ["workflow:run-it.yml"], found


# ---------------------------------------------------------------------------
# Rows to units
# ---------------------------------------------------------------------------
HEALTH = "The service answers 200 for a healthy check."
CACHES = "It never caches a flag."
RETRY = "Retries stop after three attempts."


def base_rows() -> List[Dict[str, Any]]:
    return [
        row(HEALTH, by="tests/test_things.py::test_alpha"),
        row(CACHES, by="tests/test_things.py::test_gamma"),
        row("A flag can be archived.", by="journey demo#archive,restore"),
        row(
            "Run the check:",
            by=f"doc-examples docs/guide.md:{FIRST_BLOCK} (bare)",
        ),
        row("`TIMEOUT` | 30 s", tag="T"),
        row(RETRY, tag="S"),
        row(RETRY, tag="A", kind="flow"),
        row("Webhooks are signed.", by="scripts/sign.py"),
        row("Old sentence that is reworded later.", by="tests/test_things.py"),
        row("The cache is warmed hourly.", tag="R"),
        row("answers in 5 ms", tag="T"),
        row(
            "true",
            by=f"doc-examples docs/guide.md:{FIRST_BLOCK}",
            kind="flow",
            line=FIRST_BLOCK + 1,
        ),
    ]


def test_the_planted_rows_map(tmp_path):
    """The positive control: every rule below differs from this by one row."""
    result = plan(build(tmp_path), base_rows())
    assert result.error is None and not result.present
    assert entry(result, HEALTH)["by"] == ["test:tests/test_things.py::test_alpha"]
    assert entry(result, CACHES)["by"] == [
        "test:tests/test_things.py::TestBeta::test_gamma"
    ]
    assert entry(result, "A flag can be archived.")["by"] == [
        "journey:demo#archive",
        "journey:demo#restore",
    ]
    assert entry(result, "Run the check:")["by"] == ['docex:docs/guide.md#"ok"']
    assert entry(result, "TIMEOUT | 30 s")["by"] == ["untested:T"]
    assert entry(result, "Guide") == {
        "text": "Guide",
        "by": ["exempt:baseline"],
        "count": 2,
    }
    counts = result.counts
    assert counts["R/X gone"] == 1 and counts["R/X on the page"] == 0
    assert counts["outside prose, uncovered"] == 1
    assert counts["outside prose, covered"] == 1
    assert counts["unit changed since the audit"] == 1
    assert counts["units new since the audit"] == 2


def test_uncovered_rows_are_ledgered_debt_the_heaviest_kind_first(tmp_path):
    result = plan(build(tmp_path), base_rows())
    assert entry(result, RETRY)["by"] == ["untested:A"]
    assert ("untested:A", "docs/guide.md", dc.unit_hash(RETRY)) in result.ledger
    assert ("untested:T", "docs/guide.md", dc.unit_hash("TIMEOUT | 30 s")) in (
        result.ledger
    )


def test_a_unit_with_no_row_is_baseline_and_ledgered(tmp_path):
    result = plan(build(tmp_path), base_rows())
    text = "Setting | Default"
    assert entry(result, text)["by"] == ["exempt:baseline"]
    assert ("exempt:baseline", "docs/guide.md", dc.unit_hash(text)) in result.ledger
    assert all(line[0] in dc.LEDGER_KINDS for line in result.ledger), (
        "only debt and baseline are ledgered"
    )
    assert len(result.ledger) == sum(
        e["by"][0] in dc.LEDGER_KINDS for e in result.entries
    )


def test_a_unit_new_since_the_audit_gets_no_entry(tmp_path):
    result = plan(build(tmp_path), base_rows())
    for text in (
        "A sentence written since then.",
        "The cache is warmed when it starts.",
    ):
        assert entry(result, text) is None
        assert any(text in line for line in result.new), result.new


def test_a_row_on_a_changed_unit_is_counted_not_carried(tmp_path):
    result = plan(build(tmp_path), base_rows())
    assert entry(result, "Old sentence that is reworded later.") is None
    assert entry(result, "A sentence written since then.") is None
    assert result.counts["unit changed since the audit"] == 1


def test_a_script_named_as_a_test_is_refused_never_written(tmp_path):
    result = plan(build(tmp_path), base_rows())
    assert entry(result, "Webhooks are signed.") is None
    assert any("scripts/sign.py" in line for line in result.refused), result.refused
    assert result.counts["refused"] == 1


def test_uncovered_text_in_a_fence_is_printed(tmp_path):
    result = plan(build(tmp_path), base_rows())
    assert any("answers in 5 ms" in line for line in result.outside), result.outside


def test_a_row_one_line_off_maps_by_its_excerpt(tmp_path):
    rows = base_rows()
    rows[0]["line"] -= 1  # the blank line above the sentence
    result = plan(build(tmp_path), rows)
    assert entry(result, HEALTH)["by"] == ["test:tests/test_things.py::test_alpha"]


def test_a_line_of_several_units_the_excerpt_does_not_pick_is_printed(tmp_path):
    """Neither sentence on the line gets an entry, and neither becomes
    exempt:baseline: the row is printed for the author."""
    rows = base_rows()[2:]
    rows.insert(
        0, row(HEALTH, by="tests/test_things.py::test_alpha", text="healthy...")
    )
    rows[0]["text"] = "the check, paraphrased"
    result = plan(build(tmp_path), rows)
    assert entry(result, HEALTH) is None and entry(result, CACHES) is None
    assert any("does not pick one" in line for line in result.by_hand), result.by_hand


def test_a_target_that_does_not_resolve_is_printed(tmp_path):
    rows = base_rows()
    rows[0]["by"] = "tests/test_things.py::test_renamed"
    result = plan(build(tmp_path), rows)
    assert entry(result, HEALTH) is None
    assert any("has no test_renamed" in line for line in result.by_hand)
    assert result.counts["by hand"] >= 1


def test_an_unmapped_row_takes_its_unit_out_of_the_baseline(tmp_path):
    rows = base_rows()
    rows[0]["covered"], rows[0]["by"] = True, None  # covered, by names nothing
    result = plan(build(tmp_path), rows)
    assert entry(result, HEALTH) is None
    assert any("by names nothing" in line for line in result.by_hand)


@pytest.mark.parametrize("tag", ["R", "X"])
def test_an_r_or_x_row_still_on_the_page_stops_the_page(tmp_path, tag):
    rows = base_rows()
    rows.append(row("Exports are kept for a week.", tag=tag))
    result = plan(build(tmp_path), rows)
    assert any("Exports are kept for a week." in line for line in result.present)
    assert entry(result, "Exports are kept for a week.") is None


def test_an_r_row_in_a_fence_still_on_the_page_counts(tmp_path):
    rows = base_rows()
    rows[-2]["tag"] = "R"  # "answers in 5 ms", inside a text fence
    result = plan(build(tmp_path), rows)
    assert result.counts["R/X on the page"] == 1


# ---------------------------------------------------------------------------
# The command: writing, refusals, and the round trip through check and git
# ---------------------------------------------------------------------------
def write_audit(root: Path, rows: List[Dict[str, Any]]) -> Path:
    path = root / "audit.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def check(root: Path):
    """``check`` on the planted tree: its other pages pending or deferred."""
    return dc.check(
        root, pending=["docs/run.md"], deferred=["docs/sdk/ios.md"], mechanisms={}
    )


def generate(root: Path, rows, documents=("docs/guide.md",), **kw) -> int:
    kw.setdefault("old_text", old_texts())
    return dc.run_generate(
        root, [write_audit(root, rows)], FAKE_SHA, list(documents), **kw
    )


def test_generate_writes_a_registry_check_accepts_but_for_new_units(tmp_path, capsys):
    root = build(tmp_path)
    assert generate(root, base_rows()) == 0
    out = capsys.readouterr().out
    assert "rows 12: mapped" in out
    assert (root / dc.registry_path("docs/guide.md")).is_file()
    problems = check(root).problems
    unregistered = sorted(p for p in problems if "unregistered unit" in p)
    assert len(unregistered) == len(problems), problems
    assert sorted(p.split("'")[1] for p in unregistered) == sorted(
        [
            "A sentence written since then.",
            "The cache is warmed when it starts.",
            "Webhooks are signed.",
        ]
    )


@pytest.mark.parametrize("tag", ["R", "X"])
def test_generate_exits_1_and_writes_nothing_while_r_or_x_is_on_the_page(
    tmp_path, capsys, tag
):
    root = build(tmp_path)
    rows = base_rows() + [row("Exports are kept for a week.", tag=tag)]
    assert generate(root, rows) == 1
    assert "R OR X STILL ON THE PAGE" in capsys.readouterr().out
    assert not (root / dc.registry_path("docs/guide.md")).exists()
    assert not (root / dc.LEDGER).exists()


# ---------------------------------------------------------------------------
# An R or X row is judged by its own excerpt (#1189)
# ---------------------------------------------------------------------------
OLD_ENDPOINTS = """# Endpoints

## Read a flag

- **Response**: 200 OK

```json
{"cached": true, "ttl": 300}
```

## List flags

- **Response**: 200 OK

```json
{"items": []}
```

## Delete a flag

```text
### 404 Not Found
The flag comes back after a day.
```

## Errors

```text
### 404 Not Found
No flag has that key.
```

Flags are read from the database. Reads take 5 ms.
"""

#: The same page with each false line rewritten. What the rows below also
#: stood on is still there: "- **Response**: 200 OK" twice, "### 404 Not
#: Found" in two fences, and the first sentence of the last line.
NOW_ENDPOINTS = (
    OLD_ENDPOINTS.replace('{"cached": true, "ttl": 300}', '{"key": "beta"}')
    .replace("The flag comes back after a day.", "The flag is gone for good.")
    .replace("Reads take 5 ms.", "Reads wait for the database.")
)

#: (id, the line's needle, the excerpt): each excerpt is gone from the page
#: now, but something else the row stood on is not.
REWRITTEN_ROWS = [
    (
        "runs into a fence after a unit that is unchanged",
        "- **Response**: 200 OK",
        '- **Response**: 200 OK\n```json\n{"cached": true, "ttl": 300}',
    ),
    (
        "on a fence whose first line repeats",
        "### 404 Not Found",
        "### 404 Not Found\nThe flag comes back after a day.",
    ),
    (
        "on a line it shares with an unchanged sentence",
        "Reads take 5 ms.",
        "Flags are read from the database. Reads take 5 ms.",
    ),
    (
        "not verbatim, on a line it shares with an unchanged sentence",
        "Reads take 5 ms.",
        "reads are answered in 5ms",
    ),
]


def endpoints_tree(root: Path, now: str) -> Path:
    return build(root, {**FILES, "docs/guide.md": now})


def endpoints_old():
    return old_texts(**{"docs/guide.md": OLD_ENDPOINTS})


@pytest.mark.regression
@pytest.mark.parametrize("tag", ["R", "X"])
@pytest.mark.parametrize(
    "needle, excerpt",
    [r[1:] for r in REWRITTEN_ROWS],
    ids=[r[0] for r in REWRITTEN_ROWS],
)
def test_a_row_whose_excerpt_is_gone_lets_the_page_be_written(
    tmp_path, capsys, tag, needle, excerpt
):
    """The page is written once the row's own excerpt has left it.

    It used to be refused while a unit the row mapped to, another unit on its
    line, or its line at the audit commit was still somewhere on the page,
    however the false text itself had been rewritten.
    """
    root = endpoints_tree(tmp_path, NOW_ENDPOINTS)
    audit = [row(needle, tag=tag, text=excerpt, old=OLD_ENDPOINTS)]
    assert generate(root, audit, old_text=endpoints_old()) == 0
    out = capsys.readouterr().out
    assert "STILL ON THE PAGE" not in out, out
    assert "R/X on the page 0 (gone 1)" in out, out
    assert (root / dc.registry_path("docs/guide.md")).is_file()


@pytest.mark.parametrize(
    "needle, excerpt",
    [r[1:] for r in REWRITTEN_ROWS[:3]]
    + [("- **Response**: 200 OK", '**Response**: 200 OK ... "ttl": 300')],
    ids=[r[0] for r in REWRITTEN_ROWS[:3]] + ["split by an ellipsis"],
)
def test_a_row_whose_excerpt_is_still_there_stops_the_page(
    tmp_path, capsys, needle, excerpt
):
    """The same rows on the page before the rewrite: each excerpt is on it
    (normalised as a unit is, fences included), so nothing is written."""
    root = endpoints_tree(tmp_path, OLD_ENDPOINTS)
    audit = [row(needle, tag="R", text=excerpt, old=OLD_ENDPOINTS)]
    assert generate(root, audit, old_text=endpoints_old()) == 1
    out = capsys.readouterr().out
    assert "R OR X STILL ON THE PAGE" in out, out
    assert "R/X on the page 1 (gone 0)" in out, out
    assert not (root / dc.registry_path("docs/guide.md")).exists()


#: A page whose false lines are outside prose: a front-matter key and an HTML
#: comment. Neither holds a unit, so ``check`` has nothing to report there.
OLD_HIDDEN = """---
title: Endpoints
cache: flags are cached for an hour
---

# Endpoints

<!-- Reads are answered from the cache in 5 ms. -->

Flags are read from the database.
"""

#: (id, the page, the line's needle, a paraphrase of it): each false line is
#: still on the page, and none of the excerpts is on it, then or now.
UNPLACED_ROWS = [
    ("in a fence", OLD_GUIDE, "answers in 5 ms", "responds within five milliseconds"),
    (
        "in an HTML comment",
        OLD_HIDDEN,
        "Reads are answered from the cache",
        "reads come from the cache in five milliseconds",
    ),
    (
        "in front matter",
        OLD_HIDDEN,
        "cache: flags are cached",
        "the cache keeps a flag for sixty minutes",
    ),
]


@pytest.mark.regression
@pytest.mark.parametrize("tag", ["R", "X"])
@pytest.mark.parametrize(
    "page, needle, excerpt",
    [r[1:] for r in UNPLACED_ROWS],
    ids=[r[0] for r in UNPLACED_ROWS],
)
def test_a_row_neither_its_excerpt_nor_a_unit_places_stops_the_page(
    tmp_path, capsys, tag, page, needle, excerpt
):
    """Fail closed: a paraphrased excerpt on a line that holds no unit.

    The excerpt cannot say whether the false text is gone (it was never on
    the page), and no unit is held back for ``check`` to report, so counting
    the row as gone would write a registry that ``check`` passes while the
    false line stays. The row is printed for hand mapping instead.
    """
    root = build(tmp_path, {**FILES, "docs/guide.md": page})
    audit = [row(needle, tag=tag, text=excerpt, old=page)]
    assert generate(root, audit, old_text=old_texts(**{"docs/guide.md": page})) == 1
    out = capsys.readouterr().out
    assert "R OR X NOT PLACED" in out, out
    assert f"docs/guide.md:{line_of(page, needle)} " in out, out
    assert excerpt in out, out
    assert "R/X on the page 0 (gone 0), R/X not placed 1" in out, out
    assert not (root / dc.registry_path("docs/guide.md")).exists()


@pytest.mark.parametrize(
    "document, refusal",
    [
        ("docs/missing.md", "not an in-scope document"),
        ("CONTRIBUTING.md", "not an in-scope document"),
        ("docs/sdk/ios.md", "CLAIMS_DEFERRED"),
    ],
)
def test_generate_refuses_a_document_it_must_not_register(
    tmp_path, capsys, document, refusal
):
    assert generate(build(tmp_path), base_rows(), [document]) == 2
    assert refusal in capsys.readouterr().out


def test_generate_keeps_an_existing_registry_unless_told(tmp_path, capsys):
    root = build(tmp_path)
    assert generate(root, base_rows()) == 0
    registry = root / dc.registry_path("docs/guide.md")
    registry.write_text("# edited by hand\n", encoding="utf-8")
    assert generate(root, base_rows()) == 2
    assert "pass --replace" in capsys.readouterr().out
    assert registry.read_text(encoding="utf-8") == "# edited by hand\n"
    assert generate(root, base_rows(), replace=True) == 0
    ledger = (root / dc.LEDGER).read_text(encoding="utf-8").splitlines()
    assert ledger == sorted(set(ledger)), "replacing does not duplicate the ledger"


def test_generate_writes_under_out_and_leaves_the_tree(tmp_path):
    root = build(tmp_path / "tree")
    out = tmp_path / "scratch"
    assert generate(root, base_rows(), out=out) == 0
    assert (out / dc.registry_path("docs/guide.md")).is_file()
    assert (out / dc.LEDGER).is_file()
    assert not (root / "docs/_claims").exists()


@pytest.mark.parametrize("command", ["generate", "baseline"])
def test_an_audit_with_no_row_is_refused(tmp_path, capsys, command):
    """An empty or cut-off audit file must not read as nothing found."""
    root = build(tmp_path)
    registered(root)
    capsys.readouterr()
    empty = root / "empty.jsonl"
    empty.write_text("\n", encoding="utf-8")
    run = dc.run_generate if command == "generate" else dc.run_baseline
    kw = {"replace": True} if command == "generate" else {}
    status = run(root, [empty], FAKE_SHA, ["docs/guide.md"], old_text=old_texts(), **kw)
    assert status == 2
    assert "the audit has no rows" in capsys.readouterr().out


def test_baseline_says_how_many_rows_it_read_and_mapped(tmp_path, capsys):
    root = build(tmp_path)
    registered(root)
    capsys.readouterr()
    audit = write_audit(root, base_rows())
    status = dc.run_baseline(
        root, [audit], FAKE_SHA, ["docs/guide.md"], old_text=old_texts()
    )
    assert status == 0
    claims = sum(r["kind"] == "claim" for r in base_rows())
    out = capsys.readouterr().out
    assert f"rows read {len(base_rows())};" in out
    assert f"claim rows on them {claims}, outside prose 1, mapped to a unit" in out


@pytest.mark.parametrize(
    "line, problem",
    [
        ("{not json", "not JSON"),
        (
            '{"doc": "docs/guide.md", "kind": "flow", "line": 0, "text": "", '
            '"covered": false, "by": null, "tag": "T"}',
            "line is a number from 1",
        ),
        (
            '{"doc": "docs/guide.md", "kind": "flow", "line": 3, "text": "", '
            '"covered": false, "by": null, "tag": "Q"}',
            "tag is null or one of",
        ),
    ],
)
def test_an_audit_line_that_is_not_a_row_is_refused(tmp_path, capsys, line, problem):
    root = build(tmp_path)
    audit = root / "audit.jsonl"
    audit.write_text(line + "\n", encoding="utf-8")
    status = dc.run_generate(
        root, [audit], FAKE_SHA, ["docs/guide.md"], old_text=old_texts()
    )
    assert status == 2
    assert problem in capsys.readouterr().out
    assert not (root / "docs/_claims").exists()


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_generate_reads_the_audited_commit_with_git(tmp_path, capsys):
    """End to end: the old page is committed, the page edited after it; the
    rows' lines are the committed page's, and the result passes check."""
    root = build(tmp_path, {**FILES, "docs/guide.md": OLD_GUIDE})
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "audited")
    sha = git(root, "rev-parse", "HEAD")
    (root / "docs/guide.md").write_text(NOW_GUIDE, encoding="utf-8")
    audit = write_audit(root, base_rows())
    assert dc.run_generate(root, [audit], sha, ["docs/guide.md"]) == 0
    assert "rows 12: mapped" in capsys.readouterr().out
    problems = check(root).problems
    assert all("unregistered unit" in p for p in problems), problems
    assert dc.run_generate(root, [audit], "notasha", ["docs/guide.md"]) == 2
    assert "is not a commit id" in capsys.readouterr().out
    assert dc.run_generate(root, [audit], "0" * 40, ["docs/guide.md"]) == 2
    out = capsys.readouterr().out
    assert "is not a commit of" in out, "a missing commit is refused before any page"
    assert "rows" not in out


# ---------------------------------------------------------------------------
# sync and the baseline check
# ---------------------------------------------------------------------------
def registered(root: Path) -> Path:
    assert generate(root, base_rows()) == 0
    return root / dc.registry_path("docs/guide.md")


def test_sync_prints_a_near_match_and_writes_nothing(tmp_path):
    root = build(tmp_path)
    registry = registered(root)
    before = registry.read_text(encoding="utf-8")
    page = root / "docs/guide.md"
    page.write_text(
        page.read_text().replace(
            HEALTH, "The service answers 200 for a healthy checks."
        ),
        encoding="utf-8",
    )
    lines, status = dc.sync_lines(root, "docs/guide.md")
    text = "\n".join(lines)
    assert status == 1
    assert (
        "not in the registry: 'The service answers 200 for a healthy checks.'" in text
    )
    assert f"near (0.99): {HEALTH!r}" in text
    assert "test:tests/test_things.py::test_alpha" in text
    assert registry.read_text(encoding="utf-8") == before


def test_sync_does_not_offer_a_distant_sentence(tmp_path):
    root = build(tmp_path)
    registered(root)
    page = root / "docs/guide.md"
    page.write_text(page.read_text().replace(HEALTH, "Something else entirely."))
    lines, _ = dc.sync_lines(root, "docs/guide.md")
    text = "\n".join(lines)
    assert "nothing registered within 0.9" in text
    assert f"listed, no longer on the page: {HEALTH!r}" in text


def test_sync_compares_at_most_2000_characters(tmp_path):
    """Two units equal in their first 2,000 characters are a full match."""
    same = "a" * dc.NEAR_INPUT
    assert dc.near_matches(same + "b" * 5000, [same + "c" * 5000]) == [
        (1.0, same + "c" * 5000)
    ]


def test_sync_is_quiet_when_page_and_registry_agree(tmp_path):
    root = build(tmp_path)
    registered(root)
    root.joinpath("docs/guide.md").write_text(
        NOW_GUIDE.replace("A sentence written since then.\n\n", "")
        .replace("The cache is warmed when it starts.\n\n", "")
        .replace("- Webhooks are signed.\n", "")
    )
    assert dc.sync_lines(root, "docs/guide.md") == ([], 0)
    assert dc.sync_lines(root, "docs/run.md")[1] == 2


def test_baseline_finds_a_claim_row_left_at_baseline(tmp_path, capsys):
    root = build(tmp_path)
    registry = registered(root)
    rows = base_rows()
    audit = write_audit(root, rows)
    old = {"old_text": old_texts()}
    assert dc.run_baseline(root, [audit], FAKE_SHA, ["docs/guide.md"], **old) == 0
    text = registry.read_text(encoding="utf-8")
    edited = text.replace(
        'by = ["test:tests/test_things.py::test_alpha"]', 'by = ["exempt:baseline"]', 1
    )
    assert edited != text
    registry.write_text(edited, encoding="utf-8")
    assert dc.run_baseline(root, [audit], FAKE_SHA, ["docs/guide.md"], **old) == 1
    assert f"maps to {HEALTH!r}, registered exempt:baseline" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# The OpenAPI snapshot test as a target, and the splitter's lines
# ---------------------------------------------------------------------------
def test_the_real_snapshot_test_is_a_target():
    documents, _ = dc.inventory_documents(REPO_ROOT)
    ctx = dc.Context(REPO_ROOT, documents, dc.MECHANISMS)
    for target in dc.SNAPSHOT_TARGETS.values():
        assert dc.test_problem(ctx, target) is None, target
    assert dc.API_SNAPSHOT_TESTS == (dc.SNAPSHOT_TEST,)


#: (id, what the planted snapshot test adds). The rule is a drift guard: these
#: are the shapes it refuses, not all the ways round it.
SNAPSHOT_READS = [
    ("an f-string", 'PAGE = f"docs/api/{1}.json"\n'),
    ("a split literal", 'PAGE = "docs/api/" + "endpoints.m" + "d"\n'),
    ("a second docs literal", 'import os\nPAGE = os.path.join("docs", "x.json")\n'),
]


@pytest.mark.parametrize(
    "added",
    [case[1] for case in SNAPSHOT_READS],
    ids=[case[0] for case in SNAPSHOT_READS],
)
def test_a_snapshot_test_names_docs_once(tmp_path, added):
    root = build(tmp_path)
    documents, _ = dc.inventory_documents(root)
    ctx = dc.Context(root, documents, dc.MECHANISMS)
    assert dc.test_problem(ctx, SNAPSHOT_FULL) is None, "the positive control"
    (root / dc.SNAPSHOT_TEST).write_text(SNAPSHOT_PY + added, encoding="utf-8")
    ctx = dc.Context(root, documents, dc.MECHANISMS)
    problem = dc.test_problem(ctx, SNAPSHOT_FULL) or ""
    assert "string literals naming docs" in problem, problem
    assert "reads documentation" in problem, problem


def test_a_snapshot_test_that_names_a_page_reads_wording(tmp_path):
    root = build(tmp_path)
    path = root / dc.SNAPSHOT_TEST
    path.write_text(SNAPSHOT_PY + 'PAGE = "docs/api/endpoints.md"\n', encoding="utf-8")
    documents, _ = dc.inventory_documents(root)
    ctx = dc.Context(root, documents, dc.MECHANISMS)
    assert "reads documentation" in (dc.test_problem(ctx, SNAPSHOT_FULL) or "")


def test_each_unit_spans_its_own_lines():
    page = "First one. Second\nwraps here. Third\nends.\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
    assert [(u.text, a, b) for u, a, b in dc.unit_lines(page)] == [
        ("First one.", 1, 1),
        ("Second wraps here.", 1, 2),
        ("Third ends.", 2, 3),
        ("a | b", 5, 5),
        ("1 | 2", 7, 7),
    ]
    assert dc.units(page) == [u for u, _, _ in dc.unit_lines(page)]


def test_markup_across_a_line_break_spans_the_block():
    """An empty link split over two lines leaves its bracket as a word of the
    first line, so the counts differ; each sentence then spans its whole
    block rather than a guessed part of it."""
    page = "Alpha beta [\n](x) gamma. Delta.\n"
    assert [u.text for u in dc.units(page)] == ["Alpha beta gamma.", "Delta."]
    assert [(a, b) for _, a, b in dc.unit_lines(page)] == [(1, 2), (1, 2)]
