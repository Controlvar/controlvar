"""What the documents say about a metric's conversion window (#1934).

* Three pages (Limitations, the warehouse analysis, How events count) say what
  a window does not give, in the same two sentences (``NOT_A_FULL_WINDOW``).
* No page calls a number an N-day conversion rate: with a window, users who
  join near the end have less than N days, so the number is not one. The one
  use allowed is the sentence that says so (``ALLOWED``).
* The warehouse page's comparison no longer says ``/results`` has no
  conversion window, and How events count's lines that a window makes false
  name the window.
* The upgrade notes and the rollback runbook say that an older dashboard or
  API replica clears a window.
* Each of those sentences is held in its registry by a test of the behaviour,
  never by a test that reads the page (``doc_claims`` refuses that anyway).

``doc_claims`` sees only that a sentence changed, not that one should have:
the old warehouse sentence would have stayed registered and green. These
tests read the pages for what must have changed.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
DOCS = REPO_ROOT / "docs"
CLAIMS = DOCS / "_claims" / "docs"

NOT_A_FULL_WINDOW = (
    "With a window, users assigned within the window before the experiment "
    "stops never get a full window, and this cannot change after it ends. The "
    "number is not the N-day conversion rate: a change that makes people "
    "convert later can show the opposite sign, even when no window does not."
)
PAGES = (
    "getting-started/limitations.md",
    "api/warehouse-analytics.md",
    "guides/how-events-count.md",
)

#: "7-day conversion rate", "N day conversion rates", "30-day-conversion-rate".
RATE = re.compile(
    r"\b(?:\d{1,4}|n|one|two|three|seven|fourteen|thirty|ninety)[- ]"
    r"(?:hour|day|week|month)s?[- ]conversion[- ]rates?\b",
    re.IGNORECASE,
)
#: The one sentence that may name it: the one saying the number is not one.
ALLOWED = ("The number is not the N-day conversion rate",)

#: What an older dashboard or API replica does to a window: nothing guards
#: against it, so the upgrade notes and the rollback runbook say it.
CLEARS = (
    "a draft saved in an older dashboard, or a PUT through an older API "
    "replica, clears the window"
)


def _flat(text: str) -> str:
    """The text as one line, without backticks or emphasis."""
    return re.sub(r"\s+", " ", text.replace("`", "").replace("*", ""))


def _page(relative: str) -> str:
    return _flat((DOCS / relative).read_text(encoding="utf-8"))


def _section(relative: str, heading: str) -> str:
    """The text under *heading* up to the next heading of any level."""
    lines = (DOCS / relative).read_text(encoding="utf-8").splitlines()
    (start,) = [i for i, line in enumerate(lines) if line.strip() == heading]
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("#")),
        len(lines),
    )
    return _flat("\n".join(lines[start + 1 : end]))


def rate_uses(root: Path, allowed: Tuple[str, ...] = ALLOWED) -> List[str]:
    """``page: words`` for every N-day conversion rate under *root* that is
    not inside an allowed sentence."""
    found = []
    for path in sorted(root.rglob("*.md")):
        text = _flat(path.read_text(encoding="utf-8"))
        spans = [
            (m.start(), m.end())
            for sentence in allowed
            for m in re.finditer(re.escape(sentence), text)
        ]
        for match in RATE.finditer(text):
            if not any(a <= match.start() and match.end() <= b for a, b in spans):
                found.append(f"{path.relative_to(root)}: {match.group(0)}")
    return found


# --- the two sentences -------------------------------------------------------


@pytest.mark.parametrize("page", PAGES)
def test_the_pages_say_a_window_is_not_a_full_window_or_an_n_day_rate(page):
    assert NOT_A_FULL_WINDOW in _page(page)


def test_no_page_calls_a_number_an_n_day_conversion_rate():
    assert rate_uses(DOCS) == []


def test_the_gate_finds_a_rate_and_allows_only_the_negation(tmp_path):
    (tmp_path / "a.md").write_text(
        "# A\n\nThe 7-day conversion rate is\n12%.\n", encoding="utf-8"
    )
    (tmp_path / "b.md").write_text(NOT_A_FULL_WINDOW + "\n", encoding="utf-8")
    (tmp_path / "c.md").write_text("A 30 day\nconversion rate.\n", encoding="utf-8")
    assert rate_uses(tmp_path) == [
        "a.md: 7-day conversion rate",
        "c.md: 30 day conversion rate",
    ]
    assert rate_uses(tmp_path, allowed=()) == [
        "a.md: 7-day conversion rate",
        "b.md: N-day conversion rate",
        "c.md: 30 day conversion rate",
    ]


# --- the lines a window made false ---------------------------------------------


def test_the_warehouse_comparison_names_the_window():
    text = _page("api/warehouse-analytics.md")
    # The sentence that was false once a metric could have a window, and the
    # conclusion drawn from it.
    assert not re.search(r"(?:^|\. )/results counts converting users", text)
    assert "the warehouse numbers are the stricter ones" not in text
    for words in (
        "Without a conversion window of its own, a metric in /results counts "
        "converting users with no bound on the exposure time and no conversion "
        "window.",
        "With one (conversion_window_hours, beta, the same name, unit and range)",
        "The two then agree when the hours are equal",
        "the analysis window covers the whole run",
        "/results starts a window at the start of the second the user was assigned in",
    ):
        assert words in text, words


def test_how_events_count_names_the_window_where_the_timestamp_matters():
    raw = (DOCS / "guides/how-events-count.md").read_text(encoding="utf-8")
    text = _flat(raw)
    assert "with an earlier `timestamp` | Yes | Yes |" not in raw
    assert "Its timestamp does not change that." not in text
    # The sentences the event contract quotes (tests/ground_truth) stay word
    # for word; what a window changes is said beside them.
    assert (
        re.search(
            r"(?:^|\. )GET /api/v1/results/\{experiment_id\} does not read it", text
        )
        is None
    )
    for words in (
        "Yes, unless the metric has a conversion window",
        "for a metric with a conversion window, it decides whether the event "
        "counts at all",
        "and in no variant while the user is assigned to another or to none. "
        "For a metric with a conversion window, it counts only when its "
        "timestamp is inside the window.",
        "For a metric without a conversion window, GET "
        "/api/v1/results/{experiment_id} does not read it. For a metric with "
        "one, it decides whether the event counts",
    ):
        assert words in text, words
    section = _section("guides/how-events-count.md", "## Conversion window")
    assert '"conversion_window_hours": 1' in raw
    assert "two hours after the assignment counts 0 converting users" in section
    assert "inside the hour, and counts 1" in section


@pytest.mark.parametrize(
    "page, heading",
    [
        (
            "self-hosting/migrations.md",
            "### `t1934_metric_window` adds a conversion window to each metric",
        ),
        ("deployment/rollback-runbook.md", "### Rolling back past metric windows"),
    ],
)
def test_the_upgrade_and_rollback_notes_say_an_older_release_clears_it(page, heading):
    assert CLEARS in _section(page, heading)


# --- each sentence held by a test of the behaviour ----------------------------

#: (page, the start of a unit) -> a test file one of its targets must name.
HELD_BY: Dict[Tuple[str, str], str] = {
    ("api/warehouse-analytics.md", "Without a conversion window of its own"): (
        "test_limits_roles_and_counting.py"
    ),
    ("api/warehouse-analytics.md", "With one (conversion_window_hours"): (
        "test_metric_window_results.py"
    ),
    ("api/warehouse-analytics.md", "The two then agree"): "test_window_parity.py",
    ("api/warehouse-analytics.md", "They still differ"): "test_window_parity.py",
    ("self-hosting/migrations.md", "During the rolling upgrade, and after"): (
        "test_metric_window_writes.py"
    ),
    ("deployment/rollback-runbook.md", "Rolling the API back without"): (
        "test_metric_window_writes.py"
    ),
    ("guides/how-events-count.md", "A purchase they send with a timestamp"): (
        "test_metric_window_writes.py"
    ),
    ("getting-started/limitations.md", "A conversion window (beta) that gives"): (
        "test_metric_window_writes.py"
    ),
}
for _page_name in PAGES[1:]:
    HELD_BY[(_page_name, "With a window, users assigned within the window")] = (
        "test_metric_window_open_users.py"
    )
    HELD_BY[(_page_name, "The number is not the N-day conversion rate")] = (
        "test_metric_window_writes.py"
    )


def _entries(page: str) -> List[dict]:
    registry = CLAIMS / f"{page}.toml"
    return tomllib.loads(registry.read_text(encoding="utf-8"))["unit"]


@pytest.mark.parametrize("page, start", sorted(HELD_BY))
def test_each_sentence_is_held_by_a_test_of_the_behaviour(page, start):
    entries = [e for e in _entries(page) if e["text"].startswith(start)]
    assert entries, f"no unit of {page} starts {start!r}"
    for entry in entries:
        assert all(t.startswith("test:") for t in entry["by"]), entry
        assert not any("/tests/unit/docs/" in t for t in entry["by"]), entry
        assert any(HELD_BY[(page, start)] in t for t in entry["by"]), entry
