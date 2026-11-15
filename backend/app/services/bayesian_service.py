"""
Bayesian inference service for A/B experimentation (EP-035 Batch 1).

Provides pure-function implementations of:
- Beta-Binomial conjugate posterior updates
- Equal-tailed credible intervals via scipy
- Probability to Be Best (PtBB) via Monte Carlo sampling
- Expected Loss (Regret) via Monte Carlo sampling
- Bayes Factor via Savage-Dickey density ratio
- Bayesian stopping decision rules

All methods are stateless (no DB access) and thread-safe.

Randomness
----------
Every Monte Carlo draw comes from ``numpy.random.default_rng(seed)``.  Callers
that know the experiment pass a seed derived with
:func:`backend.app.core.stats_engine.derive_seed`; when no seed is given the
fallback is derived from the posteriors themselves, so different data draws a
different stream (Monte Carlo error averages out across experiments instead of
becoming a fixed bias) while a repeated call on the same data stays
reproducible.  Two calls with identical inputs therefore return byte-identical
sample arrays.

Sampling cost
-------------
One analysis needs exactly **one** posterior sample matrix.  The private
``_*_from_samples`` helpers derive every statistic (PtBB, expected loss, ROPE)
from an already-drawn matrix; the public functions draw one when the caller has
none, so they remain usable standalone, and :meth:`BayesianService.analyze`
draws once and threads the matrix through.
"""

import logging
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from scipy import stats as scipy_stats

from backend.app.core.stats_engine import ENGINE_VERSION, derive_seed, make_rng
from backend.app.schemas.bayesian import BayesianConfig, BayesianDecision

logger = logging.getLogger(__name__)

#: Monte Carlo samples per variant used by the results API.
DEFAULT_N_SAMPLES: int = 100_000

#: The best variant's probability to be best must reach this before
#: ``STOP_WINNER`` is reported (#242).  An expected-loss threshold alone is
#: crossed by two identical arms once there is modest traffic at a low base
#: rate.  Under an A/A test the best arm's probability to be best is roughly
#: uniform on [0.5, 1], so this threshold puts a single-look false winner near
#: 2 x (1 - 0.975) = 5%.  The value was chosen by a 2,000-simulation A/A run
#: per cell (base rate 1%, 10%, 50% x 5,000 and 20,000 users per arm), and is
#: held there by ``backend/tests/unit/stats_validation/test_bayesian_stop_winner.py``.
PROB_BEST_THRESHOLD: float = 0.975

#: Namespace for the fallback seed used when a caller passes ``seed=None``.
_FALLBACK_SEED_NAMESPACE = "bayesian_service"


def posterior_fingerprint(
    posteriors: Optional[Sequence[Dict[str, float]]],
) -> str:
    """Return a stable string identifying a set of Beta posteriors.

    Used as the reproducibility bucket of the seedless fallback so that the
    stream depends on the data being analysed.  ``%.17g`` round-trips a
    float exactly, so two runs on the same posteriors produce the same
    fingerprint and two different posterior sets (almost surely) do not.
    """
    if not posteriors:
        return ""
    return ";".join(
        f"{float(p['alpha']):.17g}:{float(p['beta']):.17g}" for p in posteriors
    )


def resolve_seed(
    seed: Optional[int],
    n_samples: int,
    posteriors: Optional[Sequence[Dict[str, float]]] = None,
) -> int:
    """Return ``seed``, or a fallback derived from the data being analysed.

    A caller that knows the experiment passes a seed derived from
    ``(experiment_id, UTC day, n_samples)``.  Without one the seed is derived
    from ``(namespace, posterior fingerprint, n_samples)``: every distinct set
    of posteriors gets its own stream — so Monte Carlo error stays random
    noise across experiments rather than one fixed bias shared by the whole
    platform — while repeating a call on the same posteriors reproduces it
    exactly.  ``posteriors=None`` (no data to key on) keeps the old
    single-stream behaviour.
    """
    if seed is not None:
        return int(seed)
    return derive_seed(
        _FALLBACK_SEED_NAMESPACE, posterior_fingerprint(posteriors), n_samples
    )


def _draw_posterior_samples(
    posteriors: List[Dict[str, float]],
    n_samples: int,
    seed: Optional[int],
) -> np.ndarray:
    """Draw ``n_samples`` from each Beta posterior; shape ``[n_variants, n_samples]``."""
    rng = make_rng(resolve_seed(seed, n_samples, posteriors))
    return np.array(
        [rng.beta(float(p["alpha"]), float(p["beta"]), n_samples) for p in posteriors]
    )


def _ptbb_from_samples(samples: np.ndarray) -> List[float]:
    """Probability to be best per variant from an already-drawn sample matrix."""
    winner = np.argmax(samples, axis=0)  # shape: [n_samples]
    return [float(np.mean(winner == i)) for i in range(samples.shape[0])]


def _expected_loss_from_samples(samples: np.ndarray) -> List[float]:
    """Expected loss per variant from an already-drawn sample matrix."""
    max_samples = np.max(samples, axis=0)  # shape: [n_samples]
    return [float(np.mean(max_samples - samples[i])) for i in range(samples.shape[0])]


# ---------------------------------------------------------------------------
# Public pure-function API
# ---------------------------------------------------------------------------


def compute_posterior(
    prior: Dict[str, float],
    observations: Dict[str, int],
) -> Dict[str, float]:
    """Compute Beta-Binomial conjugate posterior.

    Args:
        prior: Dict with keys 'alpha' and 'beta' (prior hyperparameters).
        observations: Dict with keys 'conversions' and 'total'.
            - conversions: Number of successes (>= 0).
            - total: Total number of trials (>= 0).

    Returns:
        Dict with keys 'alpha' and 'beta' representing posterior parameters.

    Raises:
        ValueError: If observations are invalid (negative conversions,
                    conversions > total, or negative total).
    """
    alpha_prior = float(prior["alpha"])
    beta_prior = float(prior["beta"])

    conversions = int(observations["conversions"])
    total = int(observations["total"])

    # Validate inputs
    if conversions < 0:
        raise ValueError(f"conversions must be >= 0, got {conversions}")
    if total < 0:
        raise ValueError(f"total must be >= 0, got {total}")
    if conversions > total:
        raise ValueError(f"conversions ({conversions}) must be <= total ({total})")

    # Handle zero observations: return prior unchanged
    if total == 0:
        return {"alpha": alpha_prior, "beta": beta_prior}

    non_conversions = total - conversions

    # Conjugate Beta-Binomial update:
    #   alpha_posterior = alpha_prior + conversions
    #   beta_posterior  = beta_prior  + non_conversions
    alpha_posterior = alpha_prior + conversions
    beta_posterior = beta_prior + non_conversions

    return {"alpha": alpha_posterior, "beta": beta_posterior}


def compute_credible_interval(
    posterior: Dict[str, float],
    level: float = 0.95,
) -> Tuple[float, float]:
    """Compute an equal-tailed credible interval for a posterior.

    The bounds are the ``(1 - level) / 2`` and ``1 - (1 - level) / 2``
    quantiles of the posterior (at 95%, the 2.5th and 97.5th percentiles),
    not the highest-density interval: the two differ on a skewed posterior.

    Supports Beta (keys: alpha, beta), Normal (keys: mu, sigma), and
    Gamma (keys: alpha, beta, family='gamma') posteriors.

    Args:
        posterior: Dict describing the posterior distribution.
            Must contain 'family' key ('beta', 'normal', or 'gamma')
            OR alpha/beta keys (Beta is assumed by default).
        level: Credible level in (0, 1), e.g. 0.95 for 95% CI.

    Returns:
        (lower, upper) credible interval bounds.

    Raises:
        ValueError: If level is not in (0, 1).
    """
    if not (0.0 < level < 1.0):
        raise ValueError(f"level must be in (0, 1), got {level}")

    family = posterior.get("family", "beta")

    if family == "normal":
        mu = float(posterior["mu"])
        sigma = float(posterior["sigma"])
        dist = scipy_stats.norm(loc=mu, scale=sigma)
        lower, upper = dist.interval(level)
        return float(lower), float(upper)

    elif family == "gamma":
        # scipy's Gamma uses shape (a) and scale (1/rate)
        # Our 'beta' parameter is the rate, so scale = 1/beta
        alpha = float(posterior["alpha"])
        beta_rate = float(posterior["beta"])
        scale = 1.0 / beta_rate
        dist = scipy_stats.gamma(a=alpha, scale=scale)
        lower, upper = dist.interval(level)
        return float(lower), float(upper)

    else:
        # Default: Beta distribution
        alpha = float(posterior["alpha"])
        beta = float(posterior["beta"])
        dist = scipy_stats.beta(a=alpha, b=beta)
        lower, upper = dist.interval(level)
        return float(lower), float(upper)


def compute_probability_to_be_best(
    posteriors: List[Dict[str, float]],
    n_samples: int = DEFAULT_N_SAMPLES,
    seed: Optional[int] = None,
    *,
    samples: Optional[np.ndarray] = None,
) -> List[float]:
    """Compute Probability to Be Best (PtBB) via Monte Carlo sampling.

    For each variant, draws n_samples from its Beta posterior and computes
    the fraction of samples in which that variant has the highest value.

    Args:
        posteriors: List of dicts, each with 'alpha' and 'beta' keys.
            Must have at least 2 elements.
        n_samples: Number of Monte Carlo samples (higher → more precision).
        seed: RNG seed (see :func:`backend.app.core.stats_engine.derive_seed`);
            ``None`` derives one from the posteriors (see :func:`resolve_seed`).
        samples: Posterior draws already made for these posteriors, shape
            ``[n_variants, n_samples]``.  Supplied by callers that need more
            than one statistic from the same matrix; omit it and the function
            draws its own.

    Returns:
        List of probabilities (floats in [0, 1]) summing to 1.0.

    Raises:
        ValueError: If fewer than 2 posteriors are provided.
    """
    if len(posteriors) < 2:
        raise ValueError(
            f"At least 2 posteriors are required for PtBB, got {len(posteriors)}"
        )

    # Draw samples from each posterior (shape: [n_variants, n_samples])
    if samples is None:
        samples = _draw_posterior_samples(posteriors, n_samples, seed)

    return _ptbb_from_samples(samples)


def compute_expected_loss(
    posteriors: List[Dict[str, float]],
    n_samples: int = DEFAULT_N_SAMPLES,
    seed: Optional[int] = None,
    *,
    samples: Optional[np.ndarray] = None,
) -> List[float]:
    """Compute Expected Loss (regret) for each variant via Monte Carlo.

    For variant i, the expected loss is E[max_j(theta_j) - theta_i],
    i.e., how much we expect to "lose" by choosing variant i instead of
    the best variant.

    Args:
        posteriors: List of dicts, each with 'alpha' and 'beta' keys.
            Must have at least 2 elements.
        n_samples: Number of Monte Carlo samples.
        seed: RNG seed; ``None`` derives one from the posteriors
            (see :func:`resolve_seed`).
        samples: Posterior draws already made for these posteriors, shape
            ``[n_variants, n_samples]``; omit it and the function draws its own.

    Returns:
        List of expected loss values (floats >= 0), one per variant.

    Raises:
        ValueError: If fewer than 2 posteriors are provided.
    """
    if len(posteriors) < 2:
        raise ValueError(
            f"At least 2 posteriors are required for expected loss, got {len(posteriors)}"
        )

    # Draw samples from each posterior (shape: [n_variants, n_samples])
    if samples is None:
        samples = _draw_posterior_samples(posteriors, n_samples, seed)

    return _expected_loss_from_samples(samples)


def compute_bayes_factor(
    prior: Dict[str, float],
    posterior: Dict[str, float],
    null_value: Optional[float] = None,
) -> float:
    """Compute the Bayes Factor using the Savage-Dickey density ratio.

    The Savage-Dickey density ratio for a point null H0: theta = theta_0 is:

        BF01 = p(theta=theta_0 | data) / p(theta=theta_0 | prior)

    BF01 > 1 means the data favor H0 (null hypothesis, no effect).

    We return BF10 = 1 / BF01, so:
        BF10 > 1 means the data favor H1 (alternative, there IS an effect).
        BF10 > 10 indicates strong evidence for a real difference.

    By convention, null_value defaults to the prior mean when not specified.
    This represents a hypothesis that the parameter equals its prior central value.

    Args:
        prior: Dict with 'alpha' and 'beta' (prior Beta distribution).
        posterior: Dict with 'alpha' and 'beta' (posterior Beta distribution).
        null_value: The null hypothesis theta value (default: prior mean).

    Returns:
        Bayes Factor BF10 (float > 0). BF10 > 10 = strong evidence for effect.
    """
    alpha_prior = float(prior["alpha"])
    beta_prior = float(prior["beta"])
    alpha_post = float(posterior["alpha"])
    beta_post = float(posterior["beta"])

    # Default null value is the prior mean
    if null_value is None:
        null_value = alpha_prior / (alpha_prior + beta_prior)

    # Clamp null_value to avoid numerical issues at the boundaries
    null_value = max(1e-10, min(1.0 - 1e-10, null_value))

    # Savage-Dickey density ratio:
    # BF01 = posterior_density / prior_density at null_value
    # BF10 = prior_density / posterior_density at null_value
    prior_density = scipy_stats.beta.pdf(null_value, alpha_prior, beta_prior)
    posterior_density = scipy_stats.beta.pdf(null_value, alpha_post, beta_post)

    # BF10: evidence for alternative hypothesis (effect exists)
    # When the data pull the posterior away from null, posterior_density at null
    # becomes low → prior_density / posterior_density becomes large → BF10 >> 1
    if posterior_density == 0.0:
        return float("inf")

    bf10 = prior_density / posterior_density
    return float(bf10)


def get_bayes_factor_label(bf: float) -> str:
    """Return a human-readable interpretation label for a Bayes Factor.

    Uses the Jeffreys / Kass-Raftery scale:
        1  –  3  : Anecdotal
        3  – 10  : Moderate
        10 – 30  : Strong
        >  30    : Very Strong

    Args:
        bf: Bayes Factor value (BF10, should be >= 0).

    Returns:
        String label from the scale above.
    """
    if bf < 3.0:
        return "Anecdotal"
    elif bf < 10.0:
        return "Moderate"
    elif bf < 30.0:
        return "Strong"
    else:
        return "Very Strong"


def should_stop(
    posteriors: List[Dict[str, float]],
    config: BayesianConfig,
    n_samples: int = DEFAULT_N_SAMPLES,
    seed: Optional[int] = None,
    *,
    samples: Optional[np.ndarray] = None,
    losses: Optional[List[float]] = None,
    ptbb: Optional[List[float]] = None,
) -> BayesianDecision:
    """Apply Bayesian stopping rules to determine whether to stop the experiment.

    Stopping rules (evaluated in order):
    1. STOP_WINNER: the variant most likely to be best has a probability to
       be best of at least :data:`PROB_BEST_THRESHOLD` **and** an expected
       loss below ``config.loss_threshold``.  The probability condition is
       what keeps two identical arms from being called a winner (#242); the
       loss condition lets a caller ask for more than the default.
    2. STOP_EQUIVALENT (ROPE): If config.rope is set and the posterior
       probability that the difference between the best and worst variant
       falls within ROPE is > 0.95, stop as equivalent.
    3. CONTINUE: Otherwise, keep running.

    One sample matrix is enough for every rule: the probability to be best,
    the expected loss and the ROPE comparison all come from the same draws.

    Args:
        posteriors: List of posterior dicts (alpha, beta per variant).
        config: BayesianConfig with thresholds.
        n_samples: Monte Carlo samples for loss calculation.
        seed: RNG seed; ``None`` derives one from the posteriors
            (see :func:`resolve_seed`).
        samples: Posterior draws already made for these posteriors, shape
            ``[n_variants, n_samples]``.
        losses: Expected loss per variant already computed from ``samples``.
        ptbb: Probability to be best per variant already computed from
            ``samples``.  Supply all three from :meth:`BayesianService.analyze`,
            which has them; supply none and the function draws once itself.

    Returns:
        BayesianDecision enum value.

    Raises:
        ValueError: If fewer than 2 posteriors are provided.
    """
    if len(posteriors) < 2:
        raise ValueError(
            f"At least 2 posteriors are required for a stopping decision, "
            f"got {len(posteriors)}"
        )

    # Draw only when something below still needs the samples.
    if samples is None and (losses is None or ptbb is None or config.rope is not None):
        samples = _draw_posterior_samples(posteriors, n_samples, seed)
    if losses is None:
        losses = _expected_loss_from_samples(samples)
    if ptbb is None:
        ptbb = _ptbb_from_samples(samples)
    min_loss = min(losses)

    # Rule 1: STOP_WINNER when the variant most likely to be best is likely
    # enough (#242) and choosing it is cheap enough.  With the threshold above
    # 0.5 at most one variant can qualify.  The leader here is argmax P(best);
    # the ROPE rule below compares against argmin expected loss, as before.
    leader = max(range(len(ptbb)), key=ptbb.__getitem__)
    if ptbb[leader] >= PROB_BEST_THRESHOLD and losses[leader] < config.loss_threshold:
        return BayesianDecision.STOP_WINNER

    # Rule 2: STOP_EQUIVALENT via ROPE (same draws as the loss above)
    if config.rope is not None:
        rope_lower, rope_upper = config.rope
        # Compare each non-best variant to the best
        best_idx = losses.index(min_loss)
        best_samples = samples[best_idx]
        all_in_rope = True
        for i, _ in enumerate(posteriors):
            if i == best_idx:
                continue
            diff = best_samples - samples[i]
            prob_in_rope = float(np.mean((diff >= rope_lower) & (diff <= rope_upper)))
            if prob_in_rope <= 0.95:
                all_in_rope = False
                break
        if all_in_rope:
            return BayesianDecision.STOP_EQUIVALENT

    return BayesianDecision.CONTINUE


# ---------------------------------------------------------------------------
# BayesianService class (wraps pure functions with optional config)
# ---------------------------------------------------------------------------


class BayesianService:
    """High-level service for Bayesian A/B test analysis.

    Wraps the pure-function API with a configurable BayesianConfig and
    provides convenience methods for end-to-end analysis.
    """

    def __init__(self, config: Optional[BayesianConfig] = None) -> None:
        """Initialise with an optional BayesianConfig (defaults used if None)."""
        self.config = config or BayesianConfig()

    def update_posterior(
        self,
        observations: Dict[str, int],
    ) -> Dict[str, float]:
        """Update the prior with new observations and return the posterior."""
        prior = {"alpha": self.config.alpha, "beta": self.config.beta}
        return compute_posterior(prior, observations)

    def analyze(
        self,
        variant_observations: List[Dict[str, int]],
        n_samples: int = DEFAULT_N_SAMPLES,
        seed: Optional[int] = None,
        lower_is_better: bool = False,
    ) -> Dict:
        """Run full Bayesian analysis for all variants.

        Args:
            variant_observations: List of dicts with 'conversions' and 'total'
                per variant.
            n_samples: Monte Carlo samples.
            seed: RNG seed shared by every Monte Carlo step of this analysis
                (see :func:`backend.app.core.stats_engine.derive_seed`);
                ``None`` derives one from the posteriors
                (see :func:`resolve_seed`).
            lower_is_better: the metric's direction.  When true, "best" is the
                LOWEST rate: the draws are negated after they are made, so the
                random stream is the one a higher-is-better analysis draws.
                The probability to be best is then the probability of the
                lowest rate, and the expected loss is the rate a variant is
                above the lowest, still a non-negative figure in the metric's
                units, so ``PROB_BEST_THRESHOLD`` and ``loss_threshold`` keep
                their meaning.  The ROPE is compared with the best variant's
                advantage in the good direction (the other variant's rate
                minus the best one's), not with the raw difference, so an
                asymmetric ROPE keeps its meaning too.  The posteriors and
                credible intervals are the rates, unchanged.

        Returns:
            Dict with posteriors, PtBBs, losses, decision, and the
            ``seed`` / ``n_samples`` / ``engine_version`` that produced them.

        Note:
            The posterior matrix is drawn **once** and reused for PtBB, the
            expected loss and the ROPE check.  Because every step previously
            shared one seed it also shared one matrix, so the numbers are
            unchanged — only the redundant draws are gone.
        """
        prior = {"alpha": self.config.alpha, "beta": self.config.beta}

        # Update posteriors
        posteriors = [compute_posterior(prior, obs) for obs in variant_observations]
        if len(posteriors) < 2:
            raise ValueError(
                f"At least 2 posteriors are required for PtBB, got {len(posteriors)}"
            )

        # Seedless callers key the stream off the posteriors themselves.
        seed = resolve_seed(seed, n_samples, posteriors)

        # Compute credible intervals
        credible_intervals = [
            compute_credible_interval(p, level=self.config.credible_level)
            for p in posteriors
        ]

        # One matrix of draws feeds PtBB, expected loss and the ROPE check.
        samples = _draw_posterior_samples(posteriors, n_samples, seed)
        if lower_is_better:
            # Negated after the seeded draw: the same stream, best = lowest.
            samples = -samples
        ptbb = _ptbb_from_samples(samples)
        losses = _expected_loss_from_samples(samples)

        # Stopping decision
        decision = should_stop(
            posteriors,
            self.config,
            n_samples=n_samples,
            seed=seed,
            samples=samples,
            losses=losses,
            ptbb=ptbb,
        )

        return {
            "posteriors": posteriors,
            "credible_intervals": credible_intervals,
            "probability_to_be_best": ptbb,
            "expected_loss": losses,
            "decision": decision,
            "seed": seed,
            "n_samples": int(n_samples),
            "engine_version": ENGINE_VERSION,
        }
