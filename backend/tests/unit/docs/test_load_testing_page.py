"""The load-testing page publishes no response time and no throughput.

Its own sentence says so: the figures come from a run of the Performance Tests
workflow, never from the page. So no number on the page, in its prose or its
blocks, is given in a unit of time (milliseconds, seconds) or of throughput
(requests, queries, users or calls a second, ``rps`` and the like), with or
without a ``k`` or ``M`` suffix.

The only time figures the page may give are the lengths of the runs it shows:
the value of ``--run-time`` or ``--duration``, and "N users for N seconds".
Those are taken out before the page is read; nothing else is.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
PAGE = REPO_ROOT / "docs" / "performance" / "load-testing.md"

_NUMBER = r"(?<![\w.])[0-9][0-9,.]{0,12}(?:\s{0,2}[kKM])?"
_PER_SECOND = r"\s{0,2}(?:/|per|a|each)\s{0,2}(?:s|sec|secs|second)\b"
_UNIT = (
    r"(?:ms|msecs?|milliseconds?|µs|us|microseconds?|s|secs?|seconds?"
    r"|rps|qps|tps|rpm"
    r"|(?:requests?|reqs?|queries|query|users?|calls?|hits?|transactions?)"
    + _PER_SECOND
    + r")"
)
#: A figure: a number, then a unit of time or of throughput.
FIGURE = re.compile(_NUMBER + r"\s{0,2}" + _UNIT + r"(?![\w])", re.IGNORECASE)

#: The lengths of the runs the page shows, which are not results.
RUN_LENGTHS = (
    re.compile(r"--(?:run-time|duration)[ =][0-9]{1,6}[smh]\b"),
    re.compile(r"\b[0-9]{1,6} users for [0-9]{1,6} seconds\b"),
)


def figures(text: str) -> list[str]:
    for run_length in RUN_LENGTHS:
        text = run_length.sub("", text)
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
        "It sustains 500 requests/second.",
        "It sustains 1,200 rps.",
        "It sustains 10k rps.",
        "It sustains 10 k RPS.",
        "Expect 80 requests a second.",
        "Expect 50 req/sec.",
        "Expect 50 req/s.",
        "It answers in 2 seconds.",
        "It answers in 0.2s.",
        "It answers within 1.5 s at p99.",
        "It holds 300 users a second.",
        "It holds 300 users/s.",
        "It serves 40 queries per second.",
        "Under 900 µs.",
    ],
)
def test_a_planted_figure_is_found(line):
    assert figures(line), line


@pytest.mark.parametrize(
    "line",
    [
        "Baseline, 10 users for 20 seconds, which stays under the limits:",
        "--run-time 20s \\",
        "--duration 60s \\",
        "6000 requests a minute for each SDK route",
        "p50, p95, p99 and requests per second beside its target",
        "Locust exits 1 on any failed request.",
        "an API on port 8000 that uses them",
        "answers 429 to an address that passes a route's rate limit",
    ],
)
def test_what_the_page_may_say_is_not_a_figure(line):
    assert figures(line) == [], line
