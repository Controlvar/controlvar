"""``tests/sdk-contract/visual-visitor-vectors.json`` is the server's answer (#1545).

The browser script's website details are held to this file in the script's
own contract test; this one holds the file to the server, and each corpus
entry to the verdict it was written for. Rows of PLAN-v3.1's VERIFICATION:

* V42 (server vectors): a context as the script sends it, against a rule as
  the builder stores it, gets the server's answer; every website detail,
  absent, against every negative operator, matches nothing (and so R5:
  absent x ``not_equals`` matches nothing). Planted defect: a cleaning that
  passes a ``null`` through, which makes ``not_equals`` match (``aud-14``).
* V89 (server): a campaign value is kept only when it matches
  ``^[A-Za-z0-9._~-]{1,100}$``; 101 characters, ``@``, a space, ``+`` and
  full-width letters are dropped, never cut. One file for the script and the
  server.

Needs no database and no ``.git``, so it runs in ``scripts/core_build.sh``'s
copy too.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from backend.app.schemas import visual_tracking
from backend.scripts import generate_visual_visitor_vectors as vectors

pytestmark = pytest.mark.unit


def _committed():
    return json.loads(vectors.VECTORS_PATH.read_text(encoding="utf-8"))


def test_the_vectors_are_current():
    committed = vectors.VECTORS_PATH.read_text(encoding="utf-8")
    assert committed == vectors.render(vectors.build_vectors()), (
        "vectors stale: run `python -m backend.scripts.generate_visual_visitor_vectors`"
    )


def test_the_check_command_agrees(capsys):
    assert vectors.main(["--check"]) == 0
    assert "is current" in capsys.readouterr().out


def test_the_generator_runs_as_its_own_command():
    """As the docs run it: a fresh interpreter whose first import is the
    generator, so an import cycle the test session hides fails here."""
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "backend.scripts.generate_visual_visitor_vectors",
            "--check",
        ],
        cwd=vectors.REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    assert "is current" in result.stdout


def test_every_detail_verdict_is_the_one_the_corpus_was_written_for():
    data = _committed()
    by_id = {case["id"]: case["kept"] for case in data["details"]}
    expected = {case[0]: case[3] for case in vectors.DETAILS}
    assert by_id == expected
    assert data["counts"]["details"] == len(vectors.DETAILS) == 60
    assert data["counts"]["kept"] == sum(expected.values()) == 22


@pytest.mark.regression
def test_campaign_values_are_kept_only_in_the_unreserved_set():
    """V89: one rule for the script and the server, never a truncation."""
    data = _committed()
    kept = {case["id"]: case["kept"] for case in data["details"]}
    assert kept["utm-plain"] and kept["utm-100"] and kept["utm-unreserved"]
    for dropped in (
        "utm-101",
        "utm-email",
        "utm-encoded-at",
        "utm-space",
        "utm-plus",
        "utm-full-width",
        "utm-empty",
    ):
        assert not kept[dropped], dropped
    queries = {case["id"]: case for case in data["utm_queries"]}
    assert {i: q["kept"] for i, q in queries.items()} == {
        case[0]: case[3] for case in vectors.UTM_QUERIES
    }
    # What URLSearchParams reads: + is a space, %2B is +, %40 is @.
    assert queries["q-plus"]["value"] == "spring sale"
    assert queries["q-encoded-plus"]["value"] == "a+b"
    assert queries["q-encoded-at"]["value"] == "a@b.c"
    # Dropped, never cut: a kept value is the whole value.
    cleaned, dropped = visual_tracking.clean_web_details(
        {"web_utm_campaign": "a" * 101}
    )
    assert cleaned == {} and dropped == ["web_utm_campaign"]


def test_every_audience_verdict_is_the_one_the_corpus_was_written_for():
    data = _committed()
    answers = {case["id"]: case["matches"] for case in data["audiences"]}
    assert answers == {case[0]: case[3] for case in vectors.AUDIENCES}
    assert data["counts"]["audiences"] == len(vectors.AUDIENCES) == 45


@pytest.mark.regression
def test_an_absent_detail_matches_no_negative_rule():
    """V42 / R5: a detail the script did not send never matches "is not",
    "does not contain" or "is not one of"; nor does a null, an empty value or
    a value outside the detail's set, because the server drops each of them
    before targeting sees the context."""
    data = _committed()
    absent = [case for case in data["audiences"] if case["id"].startswith("absent-")]
    assert len(absent) == len(visual_tracking.WEB_DETAIL_RULES) * 3 == 27
    assert [case["id"] for case in absent if case["matches"]] == []
    answers = {case["id"]: case["matches"] for case in data["audiences"]}
    for unknown_device in ("aud-05", "aud-14", "aud-15", "aud-16"):
        assert answers[unknown_device] is False, unknown_device
    assert answers["aud-04"] is True  # a known device that is not a phone


def test_the_file_carries_the_vocabulary_and_limits():
    data = _committed()
    assert data["keys"] == sorted(
        [
            "web_browser",
            "web_device",
            "web_landing_path",
            "web_language",
            "web_referrer",
            "web_utm_campaign",
            "web_utm_medium",
            "web_utm_source",
            "web_visitor",
        ]
    )
    assert data["limits"] == {"referrer": 253, "utm": 100, "landing_path": 255}
    assert data["devices"] == ["phone", "tablet", "desktop"]
    assert data["negative_operators"] == ["not_equals", "not_contains", "not_in"]
