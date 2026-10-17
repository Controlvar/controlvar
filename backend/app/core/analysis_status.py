"""
Whether each analysis's numbers can be relied on: ``ga`` or ``beta``.

This is the one table behind the ``analysis_status`` and ``analysis_notice``
fields of the analysis responses.  It is about the *numbers*, and is separate
from a route's ``x-stability`` (``docs/api/stability.md``), which is about the
*shape* of the request and response.

* ``ga``: the analysis computes what its fields say; ``analysis_notice`` is null.
* ``beta``: part of the analysis is not computed yet, or not computed from what
  its name says; ``analysis_notice`` says which part, and links the issue.

A notice is present exactly when the status is ``beta``: the table refuses any
other combination when this module is imported.  Responses read the table when
they are built, never a copy of it, so changing an entry here changes every
response that carries it.
"""

from dataclasses import dataclass
from typing import Dict, Literal, Optional

AnalysisStatusValue = Literal["ga", "beta"]

GA: AnalysisStatusValue = "ga"
BETA: AnalysisStatusValue = "beta"

_ISSUES = "https://github.com/Controlvar/controlvar/issues"


@dataclass(frozen=True)
class AnalysisLabel:
    """The status of one analysis and, when it is beta, why."""

    status: AnalysisStatusValue
    notice: Optional[str] = None

    def __post_init__(self) -> None:
        if self.status not in (GA, BETA):
            raise ValueError(
                f"analysis status must be 'ga' or 'beta', not {self.status!r}"
            )
        if (self.status == BETA) != bool(self.notice):
            raise ValueError(
                "an analysis notice is required when the status is 'beta' "
                "and must be absent when it is 'ga'"
            )


#: Keyed by the analysis, not the route: ``interactions`` labels
#: ``GET /interactions/{a}/{b}``, ``cuped`` labels ``GET /results/{id}/cuped``,
#: ``sequential`` labels ``GET /results/{id}/sequential`` and ``holdout``
#: labels ``GET /holdout/{id}/results``.
ANALYSIS_STATUS: Dict[str, AnalysisLabel] = {
    # #217: the covariate is each user's own events before assignment.  The
    # route itself stays x-stability: beta (its response shape may change).
    "cuped": AnalysisLabel(GA),
    "interactions": AnalysisLabel(
        BETA,
        "Beta: tests whether each treatment's lift, in percentage points, on its "
        "experiment's primary conversion metric differs across the other "
        "experiment's variants, among the users in both. p_value is uncorrected; "
        "corrected_p_value applies the experiment's stored correction across its "
        "own treatments, and is_significant uses it at the experiment's "
        "confidence level. Pairs are tested one at a time, with no correction "
        "across pairs. Secondary and non-conversion metrics are not tested. "
        f"{_ISSUES}/219",
    ),
    # The confidence sequence is the inverted mSPRT (#231), so it agrees with
    # the stop decision; no planned-looks table is computed (#232).  The
    # /sequential route may append sentences (the always_valid alias, an
    # unusable stored alpha) to this notice.
    "sequential": AnalysisLabel(
        BETA,
        "Beta: the stop/continue decision is mSPRT alone, at the significance "
        "level shown by the boundary (1/alpha). alpha_spending is always empty: "
        "the planned-looks (alpha-spending) table is not computed yet. "
        f"{_ISSUES}/232",
    ),
    # GET /holdout/{id}/results (#445).  The route appends one sentence when
    # held-out users were assigned to an experiment while it was active.
    "holdout": AnalysisLabel(
        BETA,
        "Beta: this measures the combined effect of running experiments' "
        "treatment arms on users first seen while the holdout was active. "
        "Feature flags and split-URL experiments ignore the holdout: once a "
        "winner ships behind a flag, users in the holdout get it too, so "
        "shipped features are not part of this comparison. "
        "always_valid_ci_lower and always_valid_ci_upper stay valid however "
        "often you check. "
        f"{_ISSUES}/445",
    ),
    # Warehouse analysis (#312): proportion and mean results computed from
    # the per-variant counts and sums a warehouse returns, by
    # ``services/sufficient_stats_analysis.py``.
    "warehouse_proportion": AnalysisLabel(GA),
    "warehouse_mean": AnalysisLabel(GA),
}


def analysis_label(analysis: str) -> AnalysisLabel:
    """The current label of ``analysis``; a ``KeyError`` for an unknown one."""
    return ANALYSIS_STATUS[analysis]


def analysis_status(analysis: str) -> AnalysisStatusValue:
    """``"ga"`` or ``"beta"`` for ``analysis``, read from the table now."""
    return analysis_label(analysis).status


def analysis_notice(analysis: str) -> Optional[str]:
    """The notice for ``analysis``: text when it is beta, ``None`` when ga."""
    return analysis_label(analysis).notice
