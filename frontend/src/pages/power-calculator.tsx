'use client';

import { useState, useEffect, useCallback } from 'react';
import { docsUrl } from '@/services/docs';
import {
  computeSampleSize,
  computePowerCurve,
  type SampleSizeResult,
  type PowerCurvePoint,
} from '@/utils/power';
import { PageTitle } from '@/components/PageTitle';
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  ReferenceDot,
} from 'recharts';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function debounce<T extends (...args: Parameters<T>) => void>(
  fn: T,
  delay: number,
): (...args: Parameters<T>) => void {
  let timer: ReturnType<typeof setTimeout> | null = null;
  return (...args: Parameters<T>) => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => fn(...args), delay);
  };
}

function fmtNumber(n: number): string {
  return n.toLocaleString();
}

function fmtDays(d: number | null): string {
  if (d === null) return '--';
  if (d < 1) return '< 1 day';
  if (d < 7) return `${d.toFixed(1)} days`;
  const weeks = d / 7;
  return `${weeks.toFixed(1)} weeks (${Math.round(d)} days)`;
}

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export default function PowerCalculatorPage() {
  // Form state
  const [baselineRate, setBaselineRate] = useState<number>(0.05);
  const [mde, setMde] = useState<number>(0.10);
  const [alpha, setAlpha] = useState<number>(0.05);
  const [power, setPower] = useState<number>(0.80);
  const [nVariants, setNVariants] = useState<number>(2);
  const [dailyTraffic, setDailyTraffic] = useState<string>('');
  const [trafficAllocation, setTrafficAllocation] = useState<number>(1.0);

  // Results state
  const [result, setResult] = useState<SampleSizeResult | null>(null);
  const [curveData, setCurveData] = useState<PowerCurvePoint[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  // ---------------------------------------------------------------------------
  // Debounced calculation
  // ---------------------------------------------------------------------------

  const calculate = useCallback(async (
    baseline: number,
    mdeVal: number,
    alphaVal: number,
    powerVal: number,
    variants: number,
    traffic: string,
    allocation: number,
  ) => {
    // Validate
    if (baseline <= 0 || baseline >= 1) return;
    if (mdeVal <= 0 || mdeVal >= 1) return;
    if (alphaVal <= 0 || alphaVal >= 0.5) return;
    if (powerVal <= 0 || powerVal >= 1) return;
    if (baseline + baseline * mdeVal >= 1) return;

    setLoading(true);
    setError(null);

    const dailyTrafficNum = traffic.trim() ? parseInt(traffic, 10) : null;

    try {
      // Computed here, not fetched. `utils/power.ts` is a transcription of
      // the Python service, pinned to it by power.test.ts. The API round trip
      // bought nothing -- the arithmetic is closed form -- and cost the page
      // its ability to work at all wherever the API is not deployed, which is
      // the published site.
      const sampleResult = computeSampleSize({
        baselineRate: baseline,
        mde: mdeVal,
        alpha: alphaVal,
        power: powerVal,
        nVariants: variants,
        dailyTraffic: dailyTrafficNum,
        trafficAllocation: allocation,
      });
      const curvePoints = computePowerCurve({
        baselineRate: baseline,
        alpha: alphaVal,
        power: powerVal,
        nVariants: variants,
        currentMde: mdeVal,
      });
      setResult(sampleResult);
      setCurveData(curvePoints);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Calculation failed';
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, []);

  // eslint-disable-next-line react-hooks/exhaustive-deps
  const debouncedCalculate = useCallback(
    debounce(calculate, 400),
    [calculate],
  );

  useEffect(() => {
    debouncedCalculate(
      baselineRate, mde, alpha, power, nVariants, dailyTraffic, trafficAllocation,
    );
  }, [baselineRate, mde, alpha, power, nVariants, dailyTraffic, trafficAllocation, debouncedCalculate]);

  // ---------------------------------------------------------------------------
  // Chart data transformation
  // ---------------------------------------------------------------------------

  const chartData = curveData.map((p) => ({
    effectPct: Math.round(p.effect_size_relative * 100),
    sampleSize: p.sample_size_per_variant,
    isTarget: p.is_current_target,
  }));

  const targetPoint = chartData.find((p) => p.isTarget);

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

  return (
    <>
      <PageTitle
        title="Power Calculator"
        path="/power-calculator"
        description="Pre-experiment statistical power calculator: compute sample size, MDE, and runtime estimates for A/B tests. No login required."
      />

      <div className="flex-1 bg-gray-50">

        <div className="max-w-7xl mx-auto px-6 lg:px-8 py-10">
          {/* Header */}
          <div className="mb-8">
            <h1 className="text-3xl font-bold text-gray-900 mb-2">
              Pre-Experiment Power Calculator
            </h1>
            <p className="text-gray-500 text-base">
              Calculate the required sample size, detect the minimum effect size, and estimate
              how long your experiment needs to run. No account required.{' '}
              <a href={docsUrl('statistics/power-analysis')} className="text-blue-600 hover:text-blue-700">
                Learn more
              </a>
            </p>
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
            {/* ----------------------------------------------------------------
                LEFT COLUMN: Inputs
            ---------------------------------------------------------------- */}
            <div className="lg:col-span-1 space-y-4">

              {/* Baseline Conversion Rate */}
              <div className="bg-white rounded-xl border border-gray-200 p-5">
                <h2 className="text-sm font-semibold text-gray-700 mb-4 uppercase tracking-wide">
                  Experiment Parameters
                </h2>

                <div className="space-y-5">
                  {/* Baseline Rate */}
                  <div>
                    <label htmlFor="power-baseline" className="block text-sm font-medium text-gray-700 mb-1">
                      <span id="power-baseline-name">Baseline Conversion Rate</span>
                      <span className="ml-2 text-blue-600 font-semibold">
                        {(baselineRate * 100).toFixed(1)}%
                      </span>
                    </label>
                    <input
                      id="power-baseline"
                      aria-labelledby="power-baseline-name"
                      type="range"
                      min="0.01"
                      max="0.50"
                      step="0.01"
                      value={baselineRate}
                      onChange={(e) => setBaselineRate(parseFloat(e.target.value))}
                      className="w-full accent-blue-600"
                    />
                    <div className="flex justify-between text-xs text-gray-400 mt-1">
                      <span>1%</span><span>50%</span>
                    </div>
                  </div>

                  {/* MDE */}
                  <div>
                    <label htmlFor="power-mde" className="block text-sm font-medium text-gray-700 mb-1">
                      <span id="power-mde-name">Minimum Detectable Effect (relative)</span>
                      <span className="ml-2 text-blue-600 font-semibold">
                        {(mde * 100).toFixed(0)}%
                      </span>
                    </label>
                    <input
                      id="power-mde"
                      aria-labelledby="power-mde-name"
                      type="range"
                      min="0.01"
                      max="0.50"
                      step="0.01"
                      value={mde}
                      onChange={(e) => setMde(parseFloat(e.target.value))}
                      className="w-full accent-blue-600"
                    />
                    <div className="flex justify-between text-xs text-gray-400 mt-1">
                      <span>1%</span><span>50%</span>
                    </div>
                  </div>

                  {/* Alpha */}
                  <div>
                    <label htmlFor="power-alpha" className="block text-sm font-medium text-gray-700 mb-1">
                      Significance Level (alpha)
                    </label>
                    <select
                      id="power-alpha"
                      value={alpha}
                      onChange={(e) => setAlpha(parseFloat(e.target.value))}
                      className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm text-gray-700 focus:ring-2 focus:ring-blue-500 focus:border-transparent outline-none"
                    >
                      <option value={0.01}>0.01 (99% confidence)</option>
                      <option value={0.05}>0.05 (95% confidence)</option>
                      <option value={0.10}>0.10 (90% confidence)</option>
                    </select>
                  </div>

                  {/* Power */}
                  <div>
                    <label htmlFor="power-power" className="block text-sm font-medium text-gray-700 mb-1">
                      Statistical Power
                    </label>
                    <select
                      id="power-power"
                      value={power}
                      onChange={(e) => setPower(parseFloat(e.target.value))}
                      className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm text-gray-700 focus:ring-2 focus:ring-blue-500 focus:border-transparent outline-none"
                    >
                      <option value={0.70}>0.70 (70%)</option>
                      <option value={0.80}>0.80 (80%) — recommended</option>
                      <option value={0.90}>0.90 (90%)</option>
                      <option value={0.95}>0.95 (95%)</option>
                    </select>
                  </div>

                  {/* Variants */}
                  <div>
                    <label htmlFor="power-variants" className="block text-sm font-medium text-gray-700 mb-1">
                      Number of Variants (including control)
                    </label>
                    <select
                      id="power-variants"
                      value={nVariants}
                      onChange={(e) => setNVariants(parseInt(e.target.value, 10))}
                      className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm text-gray-700 focus:ring-2 focus:ring-blue-500 focus:border-transparent outline-none"
                    >
                      <option value={2}>2 (A/B test)</option>
                      <option value={3}>3 (A/B/C test)</option>
                      <option value={4}>4 (A/B/C/D test)</option>
                      <option value={5}>5 variants</option>
                    </select>
                    {nVariants > 2 && (
                      <p className="text-xs text-amber-600 mt-1">
                        Bonferroni correction applied for {nVariants - 1} comparisons.
                      </p>
                    )}
                  </div>
                </div>
              </div>

              {/* Traffic */}
              <div className="bg-white rounded-xl border border-gray-200 p-5">
                <h2 className="text-sm font-semibold text-gray-700 mb-4 uppercase tracking-wide">
                  Traffic (optional — for runtime estimate)
                </h2>

                <div className="space-y-4">
                  <div>
                    <label htmlFor="power-daily-users" className="block text-sm font-medium text-gray-700 mb-1">
                      Daily Users
                    </label>
                    <input
                      id="power-daily-users"
                      type="number"
                      placeholder="e.g. 10000"
                      value={dailyTraffic}
                      onChange={(e) => setDailyTraffic(e.target.value)}
                      min="1"
                      className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm text-gray-700 focus:ring-2 focus:ring-blue-500 focus:border-transparent outline-none"
                    />
                  </div>

                  <div>
                    <label htmlFor="power-allocation" className="block text-sm font-medium text-gray-700 mb-1">
                      <span id="power-allocation-name">Traffic Allocation</span>
                      <span className="ml-2 text-blue-600 font-semibold">
                        {(trafficAllocation * 100).toFixed(0)}%
                      </span>
                    </label>
                    <input
                      id="power-allocation"
                      aria-labelledby="power-allocation-name"
                      type="range"
                      min="0.05"
                      max="1.00"
                      step="0.05"
                      value={trafficAllocation}
                      onChange={(e) => setTrafficAllocation(parseFloat(e.target.value))}
                      className="w-full accent-blue-600"
                    />
                    <div className="flex justify-between text-xs text-gray-400 mt-1">
                      <span>5%</span><span>100%</span>
                    </div>
                  </div>
                </div>
              </div>

            </div>

            {/* ----------------------------------------------------------------
                MIDDLE + RIGHT: Results
            ---------------------------------------------------------------- */}
            <div className="lg:col-span-2 space-y-4">

              {/* Error Banner */}
              {error && (
                <div className="bg-red-50 border border-red-200 rounded-xl p-4 text-sm text-red-700">
                  {error}
                </div>
              )}

              {/* Key Metrics */}
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
                <div className="bg-blue-600 text-white rounded-xl p-5">
                  <p id="power-per-variant-name" className="text-xs font-medium opacity-80 uppercase tracking-wide mb-1">
                    Sample Size per Variant
                  </p>
                  <output aria-labelledby="power-per-variant-name" aria-live="polite" className="block text-3xl font-bold">
                    {loading ? '...' : result ? fmtNumber(result.per_variant) : '--'}
                  </output>
                  <p className="text-xs opacity-70 mt-1">
                    Total: {loading ? '...' : result ? fmtNumber(result.total) : '--'}
                  </p>
                </div>

                <div className="bg-white border border-gray-200 rounded-xl p-5">
                  <p id="power-mde-absolute-name" className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-1">
                    MDE (absolute)
                  </p>
                  <output aria-labelledby="power-mde-absolute-name" aria-live="polite" className="block text-3xl font-bold text-gray-900">
                    {loading ? '...' : result ? `+${(result.mde_absolute * 100).toFixed(2)}%` : '--'}
                  </output>
                  <p className="text-xs text-gray-400 mt-1">
                    {result
                      ? `${(result.baseline_rate * 100).toFixed(2)}% → ${((result.baseline_rate + result.mde_absolute) * 100).toFixed(2)}%`
                      : 'baseline → treatment'}
                  </p>
                </div>

                <div className="bg-white border border-gray-200 rounded-xl p-5">
                  <p id="power-runtime-name" className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-1">
                    Estimated Runtime
                  </p>
                  <output aria-labelledby="power-runtime-name" aria-live="polite" className="block text-3xl font-bold text-gray-900">
                    {loading ? '...' : fmtDays(result?.runtime_days ?? null)}
                  </output>
                  <p className="text-xs text-gray-400 mt-1">
                    {dailyTraffic ? '' : 'Enter daily traffic above'}
                  </p>
                </div>
              </div>

              {/* Additional info */}
              {result && (
                <div className="bg-white border border-gray-200 rounded-xl p-5">
                  <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 text-sm">
                    <div>
                      <p className="text-gray-500">Confidence level</p>
                      <p className="font-semibold text-gray-900">{(result.confidence_level * 100).toFixed(0)}%</p>
                    </div>
                    <div>
                      <p className="text-gray-500">Statistical power</p>
                      <p className="font-semibold text-gray-900">{(result.power * 100).toFixed(0)}%</p>
                    </div>
                    <div>
                      <p className="text-gray-500">Variants</p>
                      <p className="font-semibold text-gray-900">{result.n_variants}</p>
                    </div>
                    <div>
                      <p className="text-gray-500">Test type</p>
                      <p className="font-semibold text-gray-900">{result.two_tailed ? 'Two-tailed' : 'One-tailed'}</p>
                    </div>
                  </div>
                </div>
              )}

              {/* Power Curve Chart */}
              <div className="bg-white border border-gray-200 rounded-xl p-5">
                <h2 className="text-sm font-semibold text-gray-700 mb-1">
                  Power Curve
                </h2>
                <p className="text-xs text-gray-400 mb-4">
                  Sample size required per variant vs. relative effect size. Larger effects are easier to detect.
                </p>

                {chartData.length > 0 ? (
                  <ResponsiveContainer width="100%" height={220}>
                    <LineChart data={chartData} margin={{ top: 5, right: 20, left: 10, bottom: 5 }}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#f3f4f6" />
                      <XAxis
                        dataKey="effectPct"
                        tickFormatter={(v) => `${v}%`}
                        tick={{ fontSize: 11, fill: '#9ca3af' }}
                        label={{ value: 'Relative Effect Size', position: 'insideBottomRight', offset: -5, fontSize: 11, fill: '#9ca3af' }}
                      />
                      <YAxis
                        tickFormatter={(v) => v >= 1000 ? `${(v / 1000).toFixed(0)}k` : v}
                        tick={{ fontSize: 11, fill: '#9ca3af' }}
                        label={{ value: 'n per variant', angle: -90, position: 'insideLeft', fontSize: 11, fill: '#9ca3af' }}
                      />
                      <Tooltip
                        formatter={(v: unknown) => fmtNumber(v as number)}
                        labelFormatter={(l) => `Effect: ${l}%`}
                      />
                      <Line
                        type="monotone"
                        dataKey="sampleSize"
                        stroke="#2563eb"
                        strokeWidth={2}
                        dot={false}
                      />
                      {targetPoint && (
                        <ReferenceDot
                          x={targetPoint.effectPct}
                          y={targetPoint.sampleSize}
                          r={6}
                          fill="#2563eb"
                          stroke="white"
                          strokeWidth={2}
                          label={{
                            value: `MDE target`,
                            position: 'top',
                            fontSize: 10,
                            fill: '#2563eb',
                          }}
                        />
                      )}
                    </LineChart>
                  </ResponsiveContainer>
                ) : (
                  <div className="h-48 flex items-center justify-center text-gray-400 text-sm">
                    {loading ? 'Computing...' : 'Adjust parameters above to see the power curve.'}
                  </div>
                )}
              </div>

              {/* Learn more */}
              <div className="bg-gray-50 border border-gray-200 rounded-xl p-4 flex items-center justify-between">
                <div>
                  <p className="text-sm font-medium text-gray-700">Want to understand the math?</p>
                  <p className="text-xs text-gray-500">Read our statistics guide on power analysis, MDE, and alpha.</p>
                </div>
                <a
                  href={docsUrl('statistics/power-analysis')}
                  className="text-blue-600 text-sm font-medium hover:text-blue-700 transition whitespace-nowrap ml-4"
                >
                  Read the guide
                </a>
              </div>

            </div>
          </div>
        </div>
      </div>
    </>
  );
}
