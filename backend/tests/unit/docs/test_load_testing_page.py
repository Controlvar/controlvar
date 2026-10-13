"""The load-testing page publishes no response time and no throughput.

Its own sentence says so: the figures come from a run of the Performance Tests
workflow, never from the page. Any number given in milliseconds or seconds of
response, or in requests or users a second, on the page or in its blocks, is
one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
PAGE = REPO_ROOT / "docs" / "performance" / "load-testing.md"

#: A figure: a number, then a unit of time per request or of throughput.
FIGURE = re.compile(
    r"\b[0-9][0-9,.]{0,12}\s{0,2}"
    r"(?:ms|milliseconds?|µs|rps|req/s|requests? (?:per|a|each) second)\b",
    re.IGNORECASE,
)


def figures(text: str) -> list[str]:
    return [m.group(0) for m in FIGURE.finditer(text)]


def test_the_page_publishes_no_response_time_or_throughput():
    text = PAGE.read_text(encoding="utf-8")
    assert "This page publishes no response time or throughput." in " ".join(
        text.split()
    )
    assert figures(text) == []


@pytest.mark.parametrize(
    "line",
    [
        "The baseline answers in under 50 ms.",
        "p95 is 120 milliseconds.",
        "It sustains 500 requests per second.",
        "It sustains 1,200 rps.",
        "Expect 80 requests a second.",
    ],
)
def test_a_planted_figure_is_found(line):
    assert figures(line), line
