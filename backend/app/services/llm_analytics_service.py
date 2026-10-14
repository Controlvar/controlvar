"""
LLM Evaluation Analytics Service (EP-046).

Computes per-variant statistics for LLM experiments: mean latency, cost,
auto-eval scores, business metrics, 95 % CIs, p-values (Welch's t-test), and
declares a winner.

Also provides:
- submit_human_rating  — store a user-provided 1–5 rating
- run_llm_as_judge     — use Claude as a judge to score responses
"""

import logging
import math
import statistics
from typing import Dict, List, Optional
from uuid import UUID

from scipy import stats
from sqlalchemy.orm import Session

from backend.app.core.anthropic_compat import first_text
from backend.app.core.config import settings
from backend.app.models.llm_experiment import (
    LLMEvaluation,
    LLMExperiment,
)
from backend.app.schemas.llm_experiments import (
    LLMExperimentResults,
    LLMJudgeResult,
    VariantStats,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Statistical helpers
# ---------------------------------------------------------------------------


def _mean(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return statistics.mean(values)


def _std(values: List[float]) -> Optional[float]:
    if len(values) < 2:
        return None
    return statistics.stdev(values)


def _confidence_interval_95(
    values: List[float],
) -> tuple[Optional[float], Optional[float]]:
    """Return (lower, upper) 95 % CI using t-distribution (df=n-1)."""
    n = len(values)
    if n < 2:
        m = _mean(values)
        return (m, m)
    m = statistics.mean(values)
    s = statistics.stdev(values)
    se = s / math.sqrt(n)
    # Exact t critical value for a 95 % CI at df = n - 1
    t_crit = _t_critical(n - 1)
    margin = t_crit * se
    return (m - margin, m + margin)


def _t_critical(df: int) -> float:
    """
    Exact t critical value for a 95 % CI (two-tailed, alpha = 0.05).

    Student's t quantile at 0.975 for ``df`` degrees of freedom (#1192).
    ``df <= 0`` has no t distribution and returns 1.96, as before.
    """
    if df <= 0:
        return 1.96
    return float(stats.t.ppf(0.975, df))


def _welch_t_test(a: List[float], b: List[float]) -> Optional[float]:
    """
    Welch's t-test p-value (two-tailed) for two independent samples.

    The p-value is exact: the two-tailed tail area of Student's t distribution
    at the Welch-Satterthwaite degrees of freedom, at every sample size (#1192).

    Returns None if either sample has fewer than 2 observations, and 1.0 when
    both samples have zero variance (the standard error is 0).
    """
    if len(a) < 2 or len(b) < 2:
        return None
    mean_a = statistics.mean(a)
    mean_b = statistics.mean(b)
    var_a = statistics.variance(a)
    var_b = statistics.variance(b)
    n_a = len(a)
    n_b = len(b)
    se = math.sqrt(var_a / n_a + var_b / n_b)
    if se == 0:
        return 1.0
    t_stat = abs(mean_a - mean_b) / se
    # Welch-Satterthwaite degrees of freedom
    num = (var_a / n_a + var_b / n_b) ** 2
    denom = ((var_a / n_a) ** 2 / (n_a - 1)) + ((var_b / n_b) ** 2 / (n_b - 1))
    df = num / denom if denom > 0 else 1.0
    return min(float(2.0 * stats.t.sf(t_stat, df)), 1.0)


def _cohens_d(a: List[float], b: List[float]) -> Optional[float]:
    """Cohen's d effect size (pooled std)."""
    if len(a) < 2 or len(b) < 2:
        return None
    mean_diff = statistics.mean(b) - statistics.mean(a)
    pooled_var = (
        (len(a) - 1) * statistics.variance(a) + (len(b) - 1) * statistics.variance(b)
    ) / (len(a) + len(b) - 2)
    if pooled_var <= 0:
        return 0.0
    return mean_diff / math.sqrt(pooled_var)


# ---------------------------------------------------------------------------
# Analytics Service
# ---------------------------------------------------------------------------


class LLMEvaluationAnalyticsService:
    """
    Computes statistics and analytics for LLM experiment results.
    """

    def get_experiment_results(
        self, db: Session, experiment_id: UUID
    ) -> LLMExperimentResults:
        """
        Return per-variant statistics for an LLM experiment.

        Includes:
        - n_evaluations per variant
        - mean/CI for latency, cost, auto_eval_score, business_metric
        - p-value and Cohen's d vs control
        - winner determination
        """
        experiment = (
            db.query(LLMExperiment).filter(LLMExperiment.id == experiment_id).first()
        )
        if experiment is None:
            raise ValueError(f"Experiment {experiment_id} not found")

        all_evals = (
            db.query(LLMEvaluation)
            .filter(LLMEvaluation.llm_experiment_id == experiment_id)
            .all()
        )

        # Group evaluations by variant
        evals_by_variant: Dict[str, List[LLMEvaluation]] = {}
        for variant in experiment.variants:
            evals_by_variant[str(variant.id)] = []
        for ev in all_evals:
            vid = str(ev.variant_id)
            if vid not in evals_by_variant:
                evals_by_variant[vid] = []
            evals_by_variant[vid].append(ev)

        # Find control variant evaluations
        control_variant = next((v for v in experiment.variants if v.is_control), None)
        control_bm: List[float] = []
        control_ae: List[float] = []
        if control_variant:
            control_evals = evals_by_variant.get(str(control_variant.id), [])
            control_bm = [
                e.business_metric_value
                for e in control_evals
                if e.business_metric_value is not None
            ]
            control_ae = [
                e.auto_eval_score
                for e in control_evals
                if e.auto_eval_score is not None
            ]

        variant_stats_list: List[VariantStats] = []
        winner_score: Optional[float] = None
        winner_id: Optional[str] = None
        winner_name: Optional[str] = None

        for variant in experiment.variants:
            vid = str(variant.id)
            evals = evals_by_variant.get(vid, [])

            latencies = [e.latency_ms for e in evals if e.latency_ms is not None]
            costs = [
                e.estimated_cost_usd for e in evals if e.estimated_cost_usd is not None
            ]
            ae_scores = [
                e.auto_eval_score for e in evals if e.auto_eval_score is not None
            ]
            hr_scores = [e.human_rating for e in evals if e.human_rating is not None]
            bm_values = [
                e.business_metric_value
                for e in evals
                if e.business_metric_value is not None
            ]

            lat_ci = _confidence_interval_95(latencies)
            cost_ci = _confidence_interval_95(costs)
            ae_ci = _confidence_interval_95(ae_scores)
            bm_ci = _confidence_interval_95(bm_values)

            # p-value and effect size vs control
            p_val: Optional[float] = None
            eff: Optional[float] = None
            if not variant.is_control:
                if bm_values and control_bm:
                    p_val = _welch_t_test(control_bm, bm_values)
                    eff = _cohens_d(control_bm, bm_values)
                elif ae_scores and control_ae:
                    p_val = _welch_t_test(control_ae, ae_scores)
                    eff = _cohens_d(control_ae, ae_scores)

            # Determine winner by primary metric (business_metric > auto_eval)
            primary_score = _mean(bm_values) if bm_values else _mean(ae_scores)
            if primary_score is not None and (
                winner_score is None or primary_score > winner_score
            ):
                winner_score = primary_score
                winner_id = vid
                winner_name = variant.name

            variant_stats_list.append(
                VariantStats(
                    variant_id=vid,
                    variant_name=variant.name,
                    is_control=variant.is_control,
                    n_evaluations=len(evals),
                    mean_latency_ms=_mean([float(l) for l in latencies]),
                    latency_ci_lower=lat_ci[0],
                    latency_ci_upper=lat_ci[1],
                    mean_cost_usd=_mean(costs),
                    cost_ci_lower=cost_ci[0],
                    cost_ci_upper=cost_ci[1],
                    mean_auto_eval_score=_mean(ae_scores),
                    auto_eval_ci_lower=ae_ci[0],
                    auto_eval_ci_upper=ae_ci[1],
                    mean_human_rating=_mean(hr_scores),
                    mean_business_metric=_mean(bm_values),
                    business_metric_ci_lower=bm_ci[0],
                    business_metric_ci_upper=bm_ci[1],
                    p_value=p_val,
                    effect_size=eff,
                )
            )

        return LLMExperimentResults(
            experiment_id=str(experiment_id),
            experiment_name=experiment.name,
            status=experiment.status.value,
            evaluation_metric=experiment.evaluation_metric.value,
            variant_stats=variant_stats_list,
            winner_variant_id=winner_id,
            winner_variant_name=winner_name,
            total_evaluations=len(all_evals),
        )

    def submit_human_rating(
        self,
        db: Session,
        evaluation_id: UUID,
        rating: float,
    ) -> LLMEvaluation:
        """
        Persist a human rating (1–5) on an existing evaluation.

        Raises:
            ValueError: if evaluation not found or rating out of range.
        """
        if not 1.0 <= rating <= 5.0:
            raise ValueError("human_rating must be between 1.0 and 5.0")
        evaluation = (
            db.query(LLMEvaluation).filter(LLMEvaluation.id == evaluation_id).first()
        )
        if evaluation is None:
            raise ValueError(f"Evaluation {evaluation_id} not found")
        evaluation.human_rating = rating
        db.commit()
        db.refresh(evaluation)
        return evaluation

    def submit_business_metric(
        self,
        db: Session,
        evaluation_id: UUID,
        value: float,
    ) -> LLMEvaluation:
        """Persist a business metric value on an existing evaluation."""
        evaluation = (
            db.query(LLMEvaluation).filter(LLMEvaluation.id == evaluation_id).first()
        )
        if evaluation is None:
            raise ValueError(f"Evaluation {evaluation_id} not found")
        evaluation.business_metric_value = value
        db.commit()
        db.refresh(evaluation)
        return evaluation

    async def run_llm_as_judge(
        self,
        db: Session,
        experiment_id: UUID,
        judge_criteria: str = "helpfulness",
        judge_model: Optional[str] = None,
        evaluation_ids: Optional[List[UUID]] = None,
    ) -> List[LLMJudgeResult]:
        """
        Use an LLM as a judge to score model responses.

        ``judge_model`` defaults to ``settings.LLM_DEFAULT_JUDGE_MODEL`` rather
        than a literal: this signature carried its own copy of a 2024 model id,
        which meant the configured default was ignored by anyone calling it
        positionally.

        Calls the judge model with a structured prompt asking it to rate each
        response on the given criteria (0–1).  Stores the auto_eval_score on
        each LLMEvaluation record.

        If evaluation_ids is None, scores all evaluations for the experiment.
        """
        query = db.query(LLMEvaluation).filter(
            LLMEvaluation.llm_experiment_id == experiment_id
        )
        if evaluation_ids:
            query = query.filter(LLMEvaluation.id.in_(evaluation_ids))
        evaluations = query.all()

        judge_model = judge_model or settings.LLM_DEFAULT_JUDGE_MODEL

        results: List[LLMJudgeResult] = []
        for ev in evaluations:
            score, reasoning = await self._judge_response(
                response=ev.model_response,
                prompt=ev.rendered_prompt,
                criteria=judge_criteria,
                judge_model=judge_model,
            )
            ev.auto_eval_score = score
            db.add(ev)
            results.append(
                LLMJudgeResult(
                    evaluation_id=str(ev.id),
                    score=score,
                    reasoning=reasoning,
                    judge_model=judge_model,
                )
            )
        db.commit()
        return results

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _judge_response(
        self,
        response: str,
        prompt: str,
        criteria: str,
        judge_model: str,
    ) -> tuple[float, str]:
        """
        Ask the judge model to score a response on a criteria.

        Returns (score_0_to_1, reasoning_text).
        """
        judge_prompt = (
            f"You are an impartial evaluator.  Rate the following AI response on "
            f"'{criteria}' using a score from 0 (terrible) to 1 (excellent).\n\n"
            f"Original prompt:\n{prompt}\n\n"
            f"Response:\n{response}\n\n"
            f"Reply with ONLY a JSON object: "
            f'{{"score": <float 0-1>, "reasoning": "<one sentence>"}}'
        )
        try:
            import anthropic  # type: ignore

            client = anthropic.AsyncAnthropic()
            msg = await client.messages.create(
                model=judge_model,
                # 200 was sized for the answer alone. With adaptive thinking
                # on -- which `judge_model` may well select, since it is
                # whatever the user configured -- the budget can be spent
                # before the verdict starts, and every evaluation then scored
                # exactly 0.5 and fed the t-tests as if a judge had produced it.
                max_tokens=4096,
                messages=[{"role": "user", "content": judge_prompt}],
            )
            raw = first_text(msg)
            return self._parse_judge_response(raw)
        except Exception as exc:
            logger.warning(f"LLM-as-judge call failed: {exc}, using fallback score")
            return 0.5, "Judge unavailable — fallback score assigned"

    @staticmethod
    def _parse_judge_response(raw: str) -> tuple[float, str]:
        """Parse JSON judge response, with graceful fallback."""
        import json
        import re

        try:
            data = json.loads(raw)
            score = float(data.get("score", 0.5))
            score = max(0.0, min(1.0, score))
            reasoning = str(data.get("reasoning", ""))
            return score, reasoning
        except Exception:
            pass
        # Try to extract a float from the raw text
        nums = re.findall(r"\b0?\.\d+\b|\b[01]\b", raw)
        if nums:
            score = max(0.0, min(1.0, float(nums[0])))
            return score, raw[:200]
        return 0.5, raw[:200] if raw else "Parse error"
