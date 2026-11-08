"""
AI-Powered Experiment Design Service using Claude API.

Provides:
- Experiment design suggestions (hypothesis, metrics, sample size)
- Results interpretation in plain English
- Sample size calculations using statistical formulas
- Graceful degradation to template-based responses when AI is unavailable
"""

import logging
import math
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from backend.app.core.anthropic_compat import first_text
from backend.app.core.config import settings
from backend.app.services.power_calculator_service import (
    SAMPLE_SIZE_NOT_FINITE_MESSAGE,
    TREATMENT_RATE_CEILING_MESSAGE,
    PowerCalculatorService,
)

logger = logging.getLogger(__name__)

#: Each call to Claude gives up when Claude has not answered within
#: ``CLAUDE_TIMEOUT_SECONDS`` and is tried at most ``CLAUDE_MAX_RETRIES`` more
#: times; when every attempt fails, the caller answers with its template. The
#: SDK's own defaults are a 600-second read timeout and 2 retries.
CLAUDE_TIMEOUT_SECONDS = 30.0
CLAUDE_MAX_RETRIES = 1

#: The sample-size estimate plans a control and one treatment: ``total_required``
#: is twice ``required_per_variant``, and the daily traffic is split between them.
SAMPLE_SIZE_VARIANTS = 2


class SampleSizeInputError(ValueError):
    """``estimate_sample_size`` cannot calculate with these inputs (#958).

    ``str()`` is ``sample_size_input_problem``'s fixed text.
    """


class SampleSizeOutOfRangeError(ValueError):
    """The treatment rate is below 0, or the size or the days are not a finite
    number above 0 (#958); the route answers 422 with its out-of-range text."""


def sample_size_input_problem(
    baseline_rate: float, mde: float, confidence: float, power: float
) -> Optional[str]:
    """The fixed text ``GET /api/v1/ai/sample-size`` answers with a 422 for
    inputs it cannot calculate with, or None (#958). It never repeats a value
    sent. Checked in this order; NaN fails every comparison, so it is refused
    too.
    """
    for name, value in (
        ("baseline_rate", baseline_rate),
        ("confidence", confidence),
        ("power", power),
    ):
        if not 0 <= value <= 1:
            return f"{name} must be a number from 0 to 1"
    if mde == 0:
        return SAMPLE_SIZE_NOT_FINITE_MESSAGE
    if baseline_rate + mde > 1:
        return TREATMENT_RATE_CEILING_MESSAGE
    return None


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ExperimentDesignSuggestion:
    """Structured experiment design suggestion."""

    hypothesis: str
    primary_metric: str
    guardrail_metrics: List[str]
    recommended_sample_size: int
    recommended_duration_days: int
    variant_descriptions: List[str]
    confidence: str  # "ai_generated" | "template_based"
    reasoning: Optional[str] = None


@dataclass
class ResultsInterpretation:
    """Plain-English interpretation of experiment results."""

    summary: str
    recommendation: str  # "ship" | "continue_testing" | "stop_futility"
    confidence_statement: str
    key_findings: List[str]
    generated_by: str  # "ai" | "template"


@dataclass
class SampleSizeEstimate:
    """Statistical sample size estimate."""

    required_per_variant: int
    total_required: int
    days_to_significance: Optional[int]
    assumptions: Dict[str, Any]


# ---------------------------------------------------------------------------
# AIDesignService
# ---------------------------------------------------------------------------


class AIDesignService:
    """
    AI-powered experiment design assistant.

    All public methods gracefully degrade to template-based responses
    when the Anthropic API is unavailable or returns an error.
    """

    # Suggested metrics indexed by experiment type
    EXPERIMENT_METRICS: Dict[str, List[str]] = {
        "checkout": ["conversion_rate", "revenue_per_user", "cart_abandonment_rate"],
        "onboarding": ["activation_rate", "day7_retention", "time_to_first_action"],
        "pricing": ["conversion_rate", "revenue_per_user", "churn_rate"],
        "email": ["open_rate", "click_through_rate", "unsubscribe_rate"],
        "landing_page": ["conversion_rate", "bounce_rate", "time_on_page"],
        "default": ["conversion_rate", "engagement_rate", "session_duration"],
    }

    # ---------------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------------

    @staticmethod
    def is_ai_available() -> bool:
        """Return True when ANTHROPIC_API_KEY is set in the environment."""
        return bool(os.environ.get("ANTHROPIC_API_KEY"))

    @classmethod
    def suggest_experiment_design(
        cls,
        description: str,
        experiment_type: str = "default",
    ) -> ExperimentDesignSuggestion:
        """
        Generate an experiment design suggestion.

        Attempts to use the Claude API when available; falls back to a
        template-based suggestion on any error.

        Args:
            description: Natural language description of the experiment goal.
            experiment_type: One of checkout, onboarding, pricing, email,
                             landing_page, or default.

        Returns:
            ExperimentDesignSuggestion with hypothesis, metrics, and guidance.
        """
        if cls.is_ai_available():
            try:
                return cls._ai_suggest(description, experiment_type)
            except Exception as exc:
                logger.warning(
                    "AI suggestion failed, falling back to template: %s", exc
                )
        return cls._template_suggest(description, experiment_type)

    @classmethod
    def interpret_results(cls, results: Dict[str, Any]) -> ResultsInterpretation:
        """
        Generate a plain-English interpretation of experiment results.

        Args:
            results: Dict containing p_value, relative_improvement_pct,
                     and variant_name.

        Returns:
            ResultsInterpretation with recommendation and key findings.
        """
        if cls.is_ai_available():
            try:
                return cls._ai_interpret(results)
            except Exception as exc:
                logger.warning("AI interpretation failed, using template: %s", exc)
        return cls._template_interpret(results)

    @staticmethod
    def suggest_metrics_for_experiment(experiment_type: str) -> List[str]:
        """
        Return a list of suggested metrics for a given experiment type.

        Args:
            experiment_type: Type identifier (e.g. 'checkout', 'onboarding').

        Returns:
            List of metric name strings.
        """
        return AIDesignService.EXPERIMENT_METRICS.get(
            experiment_type.lower(),
            AIDesignService.EXPERIMENT_METRICS["default"],
        )

    @staticmethod
    def estimate_sample_size(
        baseline_rate: float,
        mde: float,
        confidence: float = 0.95,
        power: float = 0.80,
        daily_traffic: Optional[int] = None,
    ) -> SampleSizeEstimate:
        """
        Calculate required sample size using the normal approximation.

        Args:
            baseline_rate: Current conversion/success rate (0–1).
            mde: Minimum detectable effect — absolute change (0–1).
            confidence: Desired statistical confidence level (default 0.95).
            power: Desired statistical power (default 0.80).
            daily_traffic: Optional users a day who enter the experiment, split
                evenly between the two variants; used to compute
                days_to_significance, the whole days until each variant has
                ``required_per_variant`` users.

        Returns:
            SampleSizeEstimate with per-variant and total counts.

        Raises:
            SampleSizeInputError: ``sample_size_input_problem`` names a
                problem: ``baseline_rate``, ``confidence`` or ``power`` is NaN
                or outside 0 to 1, ``mde`` is 0, or the treatment rate
                ``baseline_rate + mde`` is above 1 (#958).
            SampleSizeOutOfRangeError: the treatment rate is below 0, or the
                size or the days are not a finite number above 0 (#958).
        """
        from scipy import stats

        problem = sample_size_input_problem(baseline_rate, mde, confidence, power)
        if problem is not None:
            raise SampleSizeInputError(problem)
        if baseline_rate + mde < 0:
            raise SampleSizeOutOfRangeError()

        alpha = 1 - confidence
        z_alpha = stats.norm.ppf(1 - alpha / 2)
        z_beta = stats.norm.ppf(power)

        p1 = baseline_rate
        p2 = baseline_rate + mde
        p_bar = (p1 + p2) / 2

        n = (z_alpha + z_beta) ** 2 * 2 * p_bar * (1 - p_bar) / (mde**2)
        # Not a size: NaN (an mde of NaN), infinity (a confidence or power of
        # 1, or an effect whose square underflows) and 0 (a confidence of 0
        # with a power of 0.5).
        if not (math.isfinite(n) and n > 0):
            raise SampleSizeOutOfRangeError()
        n = math.ceil(n)

        days: Optional[int] = None
        if daily_traffic is not None:
            # Each variant receives only its share of the day's users, so the
            # days are the power calculator's runtime estimate, not
            # required_per_variant / daily_traffic (which counts one variant).
            try:
                runtime = PowerCalculatorService().compute_runtime_estimate(
                    required_sample_size=n,
                    daily_traffic=daily_traffic,
                    traffic_allocation=1.0,
                    n_variants=SAMPLE_SIZE_VARIANTS,
                )
            except OverflowError:
                # The days leave floating-point range (a size near its top).
                raise SampleSizeOutOfRangeError()
            days = math.ceil(runtime.days_to_significance)

        return SampleSizeEstimate(
            required_per_variant=n,
            total_required=n * SAMPLE_SIZE_VARIANTS,
            days_to_significance=days,
            assumptions={
                "baseline_rate": p1,
                "mde": mde,
                "confidence": confidence,
                "power": power,
            },
        )

    @staticmethod
    def generate_plain_english_summary(results: Dict[str, Any]) -> str:
        """
        Generate a concise plain-English summary string from a results dict.

        Args:
            results: Dict containing p_value, relative_improvement_pct, variant_name.

        Returns:
            Non-empty summary string.
        """
        interp = AIDesignService._template_interpret(results)
        return interp.summary

    # ---------------------------------------------------------------------------
    # Private helpers — AI path
    # ---------------------------------------------------------------------------

    @classmethod
    def _ai_suggest(
        cls, description: str, experiment_type: str
    ) -> ExperimentDesignSuggestion:
        """Call Claude API to generate an experiment design suggestion."""
        try:
            import anthropic
        except ImportError:
            raise RuntimeError("anthropic package not installed")

        client = anthropic.Anthropic(
            timeout=CLAUDE_TIMEOUT_SECONDS, max_retries=CLAUDE_MAX_RETRIES
        )
        prompt = (
            f"You are an expert in A/B testing and experimentation.\n"
            f'Given this experiment description: "{description}"\n'
            f"Experiment type: {experiment_type}\n\n"
            f"Provide a structured experiment design with:\n"
            f"1. A clear hypothesis (one sentence)\n"
            f"2. Primary metric to measure success\n"
            f"3. 2-3 guardrail metrics to monitor\n"
            f"4. Brief description of control and treatment variants\n"
            f"5. Recommended minimum runtime in days\n\n"
            f"Be concise and specific."
        )
        message = client.messages.create(
            model=settings.ANTHROPIC_MODEL,
            # Raised from 1024: adaptive thinking draws on the same budget.
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        response_text = first_text(message)
        return cls._parse_ai_response(response_text, experiment_type)

    @classmethod
    def _ai_interpret(cls, results: Dict[str, Any]) -> ResultsInterpretation:
        """Call Claude API for results interpretation."""
        try:
            import anthropic
        except ImportError:
            raise RuntimeError("anthropic package not installed")

        client = anthropic.Anthropic(
            timeout=CLAUDE_TIMEOUT_SECONDS, max_retries=CLAUDE_MAX_RETRIES
        )
        prompt = (
            f"Interpret these A/B test results in plain English:\n{results}\n\n"
            f"Provide:\n"
            f"1) One-sentence summary\n"
            f"2) Recommendation (ship / continue_testing / stop_futility)\n"
            f"3) Key findings"
        )
        message = client.messages.create(
            model=settings.ANTHROPIC_MODEL,
            # Raised from 512: adaptive thinking draws on the same budget.
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        text = first_text(message)

        # Determine recommendation from response text
        rec = "continue_testing"
        text_lower = text.lower()
        if "stop_futility" in text_lower or "futility" in text_lower:
            rec = "stop_futility"
        elif "ship" in text_lower:
            rec = "ship"

        return ResultsInterpretation(
            summary=text[:300],
            recommendation=rec,
            confidence_statement="AI-generated interpretation",
            key_findings=[text[:200]],
            generated_by="ai",
        )

    # ---------------------------------------------------------------------------
    # Private helpers — template path
    # ---------------------------------------------------------------------------

    @classmethod
    def _template_suggest(
        cls, description: str, experiment_type: str
    ) -> ExperimentDesignSuggestion:
        """Return a template-based experiment design suggestion."""
        metrics = cls.EXPERIMENT_METRICS.get(
            experiment_type.lower(),
            cls.EXPERIMENT_METRICS["default"],
        )
        return ExperimentDesignSuggestion(
            hypothesis=f"Changing the {experiment_type} flow will improve user outcomes",
            primary_metric=metrics[0],
            guardrail_metrics=metrics[1:3],
            recommended_sample_size=1000,
            recommended_duration_days=14,
            variant_descriptions=[
                "Control: current experience",
                "Variant A: proposed change",
            ],
            confidence="template_based",
            reasoning="Template-based suggestion (AI not available)",
        )

    @staticmethod
    def _template_interpret(results: Dict[str, Any]) -> ResultsInterpretation:
        """Return a template-based results interpretation."""
        p_value = results.get("p_value", 1.0)
        effect = results.get("relative_improvement_pct", 0.0)
        variant_name = results.get("variant_name", "the variant")

        if p_value < 0.05 and effect > 0:
            rec = "ship"
            summary = (
                f"{variant_name} showed a {effect:.1f}% improvement "
                f"(p={p_value:.3f}). Recommend shipping."
            )
        elif p_value < 0.05 and effect < 0:
            rec = "stop_futility"
            summary = (
                f"{variant_name} showed a {abs(effect):.1f}% decline "
                f"(p={p_value:.3f}). Do not ship."
            )
        else:
            rec = "continue_testing"
            summary = (
                f"Results are not yet statistically significant "
                f"(p={p_value:.3f}). Continue testing."
            )

        return ResultsInterpretation(
            summary=summary,
            recommendation=rec,
            confidence_statement=f"Statistical confidence: {(1 - p_value) * 100:.1f}%",
            key_findings=[summary],
            generated_by="template",
        )

    @classmethod
    def _parse_ai_response(
        cls, response_text: str, experiment_type: str
    ) -> ExperimentDesignSuggestion:
        """Parse a Claude response string into an ExperimentDesignSuggestion."""
        metrics = cls.EXPERIMENT_METRICS.get(
            experiment_type.lower(),
            cls.EXPERIMENT_METRICS["default"],
        )
        return ExperimentDesignSuggestion(
            hypothesis=response_text[:200],
            primary_metric=metrics[0],
            guardrail_metrics=metrics[1:3],
            recommended_sample_size=1000,
            recommended_duration_days=14,
            variant_descriptions=[
                "Control: current experience",
                "Variant A: AI-suggested change",
            ],
            confidence="ai_generated",
            reasoning=response_text,
        )
