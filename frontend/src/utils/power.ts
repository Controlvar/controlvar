/**
 * Power and sample-size calculations, in the browser.
 *
 * WHY THIS EXISTS. The power calculator used to POST to
 * `/api/v1/power/sample-size` and `/power/curve`. On the published site there
 * is no API behind those paths, so every field rendered `--` -- the page
 * looked like it was waiting for input and was in fact receiving 404s.
 *
 * None of this needs a server. `power_calculator_service.py` is closed-form
 * z-test arithmetic whose only scipy use is `norm.ppf`, which is ~30 lines
 * here. A calculator that works with no backend also works forever, offline,
 * and for a reader evaluating the project before they clone anything.
 *
 * THE SERVER IS THE SPECIFICATION. Every formula below is transcribed from
 * `backend/app/services/power_calculator_service.py`. `src/tests/utils/power.test.ts`
 * checks this file against vectors that service produced once and that are
 * committed in the test. The test never runs the server, so it catches a server
 * change only when someone regenerates the vectors. No generator script is
 * committed: to regenerate a row, call the service in the backend's Python
 * environment from the repository root, as the test file's header shows. For
 * the test's second row:
 *
 *   from backend.app.services.power_calculator_service import PowerCalculatorService
 *   r = PowerCalculatorService().compute_sample_size(
 *       baseline_rate=0.05, minimum_detectable_effect=0.10, n_variants=4)
 *   r.per_variant, r.total  # (41661, 166644)
 */

/** Inverse standard normal CDF (`scipy.stats.norm.ppf`). */
export function normPpf(p: number): number {
  if (p <= 0 || p >= 1) throw new RangeError(`normPpf expects 0 < p < 1, got ${p}`);

  // Acklam's rational approximation. Measured against scipy.stats.norm.ppf
  // the absolute error is 4e-10 to 2.4e-9 across the range this page uses,
  // which is far below anything that survives the ceiling downstream.
  //
  // There is NO Halley refinement here, though the obvious version of this
  // function has one. Refining against `normCdf` below made the answer ~500x
  // WORSE -- 1.2e-6 instead of 1.1e-9 -- because that CDF is an Abramowitz &
  // Stegun approximation good to only ~7.5e-8, so the correction step injects
  // its own error. A refinement is only as good as the function it refines
  // against.
  const a = [-3.969683028665376e1, 2.209460984245205e2, -2.759285104469687e2,
             1.383577518672690e2, -3.066479806614716e1, 2.506628277459239];
  const b = [-5.447609879822406e1, 1.615858368580409e2, -1.556989798598866e2,
             6.680131188771972e1, -1.328068155288572e1];
  const c = [-7.784894002430293e-3, -3.223964580411365e-1, -2.400758277161838,
             -2.549732539343734, 4.374664141464968, 2.938163982698783];
  const d = [7.784695709041462e-3, 3.224671290700398e-1, 2.445134137142996,
             3.754408661907416];
  const pLow = 0.02425, pHigh = 1 - pLow;
  let x: number;

  if (p < pLow) {
    const q = Math.sqrt(-2 * Math.log(p));
    x = (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) /
        ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1);
  } else if (p <= pHigh) {
    const q = p - 0.5, r = q * q;
    x = (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q /
        (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1);
  } else {
    const q = Math.sqrt(-2 * Math.log(1 - p));
    x = -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) /
         ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1);
  }

  return x;
}

/** Standard normal CDF, via an Abramowitz & Stegun erf approximation. */
export function normCdf(x: number): number {
  const t = 1 / (1 + 0.2316419 * Math.abs(x));
  const d = 0.3989422804014327 * Math.exp((-x * x) / 2);
  const prob = d * t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 +
               t * (-1.821255978 + t * 1.330274429))));
  return x > 0 ? 1 - prob : prob;
}

export type MetricType = 'proportion' | 'mean' | 'ratio';

/**
 * Sample size per group for a two-proportion z-test, unpooled (Fleiss, 2003):
 *
 *   n = [z_a * sqrt(2 * p_bar * (1 - p_bar))
 *        + z_b * sqrt(p1(1-p1) + p2(1-p2))]^2 / (p2 - p1)^2
 */
export function sampleSizeProportions(
  p1: number, p2: number, alpha: number, power: number, twoTailed = true,
): number {
  const delta = Math.abs(p2 - p1);
  if (delta === 0) throw new RangeError('p1 and p2 must differ');
  const zAlpha = normPpf(1 - alpha / (twoTailed ? 2 : 1));
  const zPower = normPpf(power);
  const pooled = (p1 + p2) / 2;
  const n = Math.pow(
    zAlpha * Math.sqrt(2 * pooled * (1 - pooled)) +
    zPower * Math.sqrt(p1 * (1 - p1) + p2 * (1 - p2)), 2) / (delta * delta);
  return Math.ceil(n);
}

/** Sample size per group for a two-sample z-test on means. */
export function sampleSizeMeans(
  mean1: number, mean2: number, std: number,
  alpha: number, power: number, twoTailed = true,
): number {
  const delta = Math.abs(mean2 - mean1);
  if (delta === 0) throw new RangeError('means must differ');
  if (std <= 0) throw new RangeError('std must be positive');
  const zAlpha = normPpf(1 - alpha / (twoTailed ? 2 : 1));
  const zPower = normPpf(power);
  return Math.ceil((2 * Math.pow(zAlpha + zPower, 2) * std * std) / (delta * delta));
}

/**
 * Deliberately the SAME shape the API returned, field for field, so the page
 * consuming it did not have to change and a future server-backed build can
 * swap back without touching the component.
 */
export interface SampleSizeResult {
  per_variant: number;
  total: number;
  alpha: number;
  power: number;
  baseline_rate: number;
  mde_absolute: number;
  mde_relative: number;
  confidence_level: number;
  runtime_days: number | null;
  n_variants: number;
  two_tailed: boolean;
  metric_type: string;
  corrected_alpha: number;
  treatment_rate: number;
}

/**
 * The whole calculation, matching `PowerCalculatorService.compute_sample_size`.
 *
 * `n_variants > 2` applies a Bonferroni correction over `n_variants - 1`
 * comparisons, exactly as the server does.
 */
export function computeSampleSize(params: {
  baselineRate: number; mde: number; alpha?: number; power?: number;
  nVariants?: number; twoTailed?: boolean; metricType?: MetricType;
  baselineStd?: number; dailyTraffic?: number | null; trafficAllocation?: number;
}): SampleSizeResult {
  const { baselineRate, mde, alpha = 0.05, power = 0.8, nVariants = 2,
          twoTailed = true, metricType = 'proportion', baselineStd,
          dailyTraffic = null, trafficAllocation = 1.0 } = params;

  // A proportion is a rate in (0, 1); a mean or ratio is in the metric's own
  // units, so any finite value above 0 (#1034).
  if (metricType === 'proportion') {
    if (!(baselineRate > 0 && baselineRate < 1)) throw new RangeError('baseline rate must be between 0 and 1');
  } else if (!(baselineRate > 0 && Number.isFinite(baselineRate))) {
    throw new RangeError('baseline mean must be a finite number above 0');
  }
  if (!(mde > 0 && mde < 1)) throw new RangeError('minimum detectable effect must be between 0 and 1');
  if (!(alpha > 0 && alpha < 0.5)) throw new RangeError('alpha must be between 0 and 0.5');
  if (!(power > 0 && power < 1)) throw new RangeError('power must be between 0 and 1');
  if (nVariants < 2) throw new RangeError('there must be at least 2 variants');
  if ((metricType === 'mean' || metricType === 'ratio') && !baselineStd) {
    throw new RangeError('baseline standard deviation is required for a mean metric');
  }

  const nComparisons = Math.max(1, nVariants - 1);
  const correctedAlpha = alpha / nComparisons;
  const mdeAbsolute = baselineRate * mde;
  const p1 = baselineRate;
  const p2 = baselineRate + mdeAbsolute;

  // The server raises here, and so must this: `sqrt(p2 * (1 - p2))` with
  // p2 > 1 is NaN rather than an error, so without this the page would render
  // "NaN" instead of saying what is wrong. Message matched to the server's.
  if (metricType === 'proportion' && p2 >= 1) {
    throw new RangeError(
      `baseline rate + absolute effect = ${p2.toFixed(4)} >= 1.0. ` +
      'Reduce the baseline rate or the minimum detectable effect.',
    );
  }

  const perVariant = metricType === 'proportion'
    ? sampleSizeProportions(p1, p2, correctedAlpha, power, twoTailed)
    : sampleSizeMeans(p1, p2, baselineStd as number, correctedAlpha, power, twoTailed);

  let runtimeDays: number | null = null;
  if (dailyTraffic && dailyTraffic > 0) {
    const perVariantPerDay = (dailyTraffic * trafficAllocation) / nVariants;
    runtimeDays = perVariantPerDay > 0 ? perVariant / perVariantPerDay : null;
  }

  return {
    per_variant: perVariant,
    total: perVariant * nVariants,
    alpha,
    power,
    baseline_rate: p1,
    mde_absolute: mdeAbsolute,
    mde_relative: mde,
    confidence_level: 1 - alpha,
    runtime_days: runtimeDays,
    n_variants: nVariants,
    two_tailed: twoTailed,
    metric_type: metricType,
    corrected_alpha: correctedAlpha,
    treatment_rate: p2,
  };
}

/** Same shape the API returned, so the chart consumes it unchanged. */
export interface PowerCurvePoint {
  effect_size_relative: number;
  sample_size_per_variant: number;
  is_current_target: boolean;
}

/** Sample size across a range of relative effects — the curve the page plots. */
export function computePowerCurve(params: {
  baselineRate: number; alpha?: number; power?: number; nVariants?: number;
  minEffect?: number; maxEffect?: number; points?: number;
  metricType?: MetricType; baselineStd?: number; currentMde?: number;
}): PowerCurvePoint[] {
  const { baselineRate, alpha = 0.05, power = 0.8, nVariants = 2,
          minEffect = 0.01, maxEffect = 0.5, points = 25,
          metricType = 'proportion', baselineStd, currentMde } = params;
  const out: PowerCurvePoint[] = [];
  const step = (maxEffect - minEffect) / Math.max(1, points - 1);
  for (let i = 0; i < points; i += 1) {
    const effect = minEffect + step * i;
    try {
      const r = computeSampleSize({ baselineRate, mde: effect, alpha, power,
                                    nVariants, metricType, baselineStd });
      out.push({
        effect_size_relative: effect,
        sample_size_per_variant: r.per_variant,
        // The point nearest the user's own MDE, so the chart can mark it.
        is_current_target: currentMde !== undefined
          && Math.abs(effect - currentMde) < step / 2,
      });
    } catch {
      // An effect that pushes the treatment rate past 1 is not plottable;
      // the curve simply stops rather than the page failing.
    }
  }
  return out;
}
