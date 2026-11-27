"""Segment preview over the wire: the share of the users seen most recently
that a segment's rules match, read from real exposures, against a running
Controlvar (``_platform.yml``'s ``usecases-flags`` suite, nightly).

An experiment is made and started, then 43 users are exposed to it through
``POST /tracking/assign``, each with attributes as ``context``: 12 premium
users in the US, 14 free users in the US, 14 premium users in Germany and,
last, 3 with no attributes. Then a segment is made and previewed with
``sample_size`` 40 (``POST /segments/{id}/preview``).

* **P1, the share.** Rules "country equals US and plan equals premium" match
  12 of the 40 users with attributes: ``estimated_percentage`` 30.0, ``matched``
  12, ``sample_size`` 40, ``source`` ``exposure_metadata``, ``reason`` null.
* **P2, the rules decide.** Other rules ("country equals DE and plan equals
  premium") over the same users answer 14 of 40, 35.0: the answer follows the
  rules, not a constant.
* **P3, users without attributes** are not in the sample: the 3 last exposed
  are counted in ``users_without_context`` and not in ``sample_size``.

The preview reads the most recent exposures of the whole database, so it
expects nothing else to expose users while it runs, as nothing else does in
this root. Wall clock is never gated. The signed-in run is ``conftest.py``'s.
"""

from __future__ import annotations

from typing import Any

from usecases import admin

from conftest import Live, _fail_on

US_PREMIUM = 12
US_FREE = 14
DE_PREMIUM = 14
NO_CONTEXT = 3
SAMPLE = US_PREMIUM + US_FREE + DE_PREMIUM


def _rules(country: str, plan: str) -> dict[str, Any]:
    return {
        "logical_operator": "AND",
        "groups": [
            {
                "logical_operator": "AND",
                "conditions": [
                    {"attribute": "country", "operator": "equals", "value": country},
                    {"attribute": "plan", "operator": "equals", "value": plan},
                ],
            }
        ],
    }


def preview_findings(
    label: str, answer: dict[str, Any], matched: int, percentage: float
) -> list[str]:
    """P1/P2 for one preview, and P3's counts."""
    expected = {
        "matched": matched,
        "sample_size": SAMPLE,
        "estimated_percentage": percentage,
        "source": "exposure_metadata",
        "reason": None,
        "ended_by": "sample_size",
    }
    found = [
        f"{label}: {name} is {answer.get(name)!r}, expected {want!r}"
        for name, want in expected.items()
        if answer.get(name) != want
    ]
    without = answer.get("users_without_context")
    if not isinstance(without, int) or without < NO_CONTEXT:
        found.append(
            f"{label}: users_without_context is {without!r}, expected at least"
            f" {NO_CONTEXT} (the users exposed with no attributes)"
        )
    return found


def test_a_preview_counts_the_users_the_rules_match_from_real_exposures(
    live: Live,
) -> None:
    api = live.admin
    key = f"preview-{live.run}"
    experiment = api.create_experiment(key)
    api.start_experiment(str(experiment["id"]))

    users = (
        [
            (f"{key}-usp{i}", {"country": "US", "plan": "premium"})
            for i in range(US_PREMIUM)
        ]
        + [(f"{key}-usf{i}", {"country": "US", "plan": "free"}) for i in range(US_FREE)]
        + [
            (f"{key}-dep{i}", {"country": "DE", "plan": "premium"})
            for i in range(DE_PREMIUM)
        ]
        + [(f"{key}-bare{i}", {}) for i in range(NO_CONTEXT)]
    )
    for user_id, context in users:
        api.assign(key, user_id, context)

    us_premium = _rules("US", "premium")
    segment = api.create_segment(f"Preview {live.run}", us_premium)
    segment_id = str(segment["id"])
    findings: list[str] = []
    try:
        first = api.preview_segment(segment_id, us_premium, SAMPLE)
        findings += preview_findings("US premium", first, US_PREMIUM, 30.0)
        second = api.preview_segment(segment_id, _rules("DE", "premium"), SAMPLE)
        findings += preview_findings("DE premium", second, DE_PREMIUM, 35.0)
    finally:
        try:
            api.delete_segment(segment_id)
        except admin.AdminError as exc:
            findings.append(f"cleanup: {exc}")
    live.summary.append(
        f"Segment preview: {len(users)} exposed users, {len(findings)} findings"
    )
    _fail_on(findings, "the segment preview use case")
