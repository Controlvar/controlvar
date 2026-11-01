/**
 * The results of one succeeded warehouse analysis: where they came from, the
 * sample-ratio check, and one table per metric: conversion for a proportion
 * metric, the mean and its interval for a mean metric. A metric the run could
 * not compute says "Not computed" and why; it never shows 0. A treatment whose
 * comparison was not computed (no variation in either variant) says so in its
 * Result cell.
 *
 * Each computed metric's heading says the direction the run analysed it in,
 * "lower is better" or "higher is better", from the run's own result: its
 * source's setting when the run started, not the source's setting now. A
 * significant change of a treatment is called better or worse in that
 * direction, by the core Results table's rule (`changeVerdict`). A result
 * stored before the direction existed shows neither.
 *
 * Nothing here is signalled by colour alone: significance, the direction, the
 * verdict, the SRM warning and "Not computed" are all words.
 */
import React from 'react';
import { changeVerdict } from '@/components/results/shared/resultFormat';
import type {
  MetricResult as CoreMetricResult,
  VariantResult as CoreVariantResult,
} from '@/types/results';
import {
  MetricResult,
  RESULTS_DIFFERENCE,
  RunMetric,
  RunResults as Results,
  VariantResult,
  WarehouseRun,
  costText,
  durationText,
  failureCopy,
  isKnownFailure,
  meanText,
  percent,
  pValueText,
  utcText,
  warehouseName,
} from '@modules/services/warehouseRuns';

export interface RunResultsProps {
  run: WarehouseRun;
  results: Results;
  experimentKey: string;
  /**
   * Opens View SQL; given the button so focus can return to it. Left out for a
   * role that may not see the SQL, which hides the button.
   */
  onViewSql?: (trigger: HTMLElement) => void;
}

function Time({ iso }: { iso: string | null }) {
  if (!iso) return <>—</>;
  return <time dateTime={iso}>{utcText(iso)}</time>;
}

function SourceStrip({ run, onViewSql }: Pick<RunResultsProps, 'run' | 'onViewSql'>) {
  const cost = costText(run);
  const took = durationText(run);
  return (
    <dl
      className="flex flex-wrap gap-x-6 gap-y-1 text-sm text-slate-700"
      data-testid="warehouse-source-strip"
    >
      <div className="flex gap-1.5">
        <dt className="text-slate-600">Source</dt>
        <dd>
          {warehouseName(run.warehouse_type)} · {run.connection_name}
        </dd>
      </div>
      <div className="flex gap-1.5">
        <dt className="text-slate-600">Window</dt>
        <dd>
          <Time iso={run.window_start} /> to <Time iso={run.window_end} />
        </dd>
      </div>
      <div className="flex gap-1.5">
        <dt className="text-slate-600">Analysed</dt>
        <dd>
          <Time iso={run.finished_at} />
          {took ? ` · took ${took}` : ''}
        </dd>
      </div>
      {cost && (
        <div className="flex gap-1.5">
          <dt className="text-slate-600">Cost reported by the warehouse</dt>
          <dd data-testid="warehouse-cost">{cost}</dd>
        </div>
      )}
      {run.statements && run.statements.length > 0 && onViewSql && (
        <div>
          <dt className="sr-only">SQL</dt>
          <dd>
            <button
              type="button"
              onClick={(e) => onViewSql(e.currentTarget)}
              className="font-medium text-blue-700 underline"
              data-testid="warehouse-view-sql"
            >
              View SQL
            </button>
          </dd>
        </div>
      )}
    </dl>
  );
}

function Srm({ results }: { results: Results }) {
  if (results.srm_skipped === 'adaptive_allocation') {
    return (
      <div
        className="rounded-md border border-slate-200 bg-slate-50 p-3 text-sm text-slate-700"
        data-testid="warehouse-srm-skipped"
      >
        <p className="font-medium text-slate-800">Sample ratio check: skipped</p>
        <p className="mt-1">
          This experiment adapts its traffic split as it learns, so there is no fixed split to check
          the exposures against.
        </p>
      </div>
    );
  }
  const srm = results.srm;
  if (!srm) {
    return (
      <p className="text-sm text-slate-600" data-testid="warehouse-srm-none">
        Sample ratio check: not computed (fewer than two variants with traffic, or no exposures).
      </p>
    );
  }
  const names = new Map(results.variants.map((v) => [v.variant_id, v.variant_name]));
  const expectedTotal = Object.values(srm.expected).reduce((a, b) => a + b, 0);
  const observedTotal = Object.values(srm.observed).reduce((a, b) => a + b, 0);
  const rows = Object.keys(srm.expected).map((id) => ({
    id,
    name: names.get(id) ?? id,
    expected: expectedTotal ? srm.expected[id] / expectedTotal : null,
    observed: observedTotal ? (srm.observed[id] ?? 0) / observedTotal : null,
    units: srm.observed[id] ?? 0,
  }));
  return (
    <div
      className={
        srm.warning
          ? 'rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900'
          : 'rounded-md border border-slate-200 bg-white p-3 text-sm text-slate-700'
      }
      data-testid={srm.warning ? 'warehouse-srm-warning' : 'warehouse-srm-ok'}
    >
      <p className="font-medium">
        {srm.warning
          ? `Warning: sample ratio mismatch (p = ${pValueText(srm.p_value)}).`
          : `Sample ratio check: passed (p = ${pValueText(srm.p_value)}).`}
      </p>
      {srm.warning && (
        <p className="mt-1">
          The exposures are not split the way the experiment allocates traffic, so the results
          below may be biased. Check how exposures are logged before acting on them.
        </p>
      )}
      <table className="mt-2 text-sm">
        <caption className="sr-only">Expected and observed split of exposed units</caption>
        <thead>
          <tr>
            <th scope="col" className="pr-6 text-left font-medium">Variant</th>
            <th scope="col" className="pr-6 text-right font-medium">Expected</th>
            <th scope="col" className="pr-6 text-right font-medium">Observed</th>
            <th scope="col" className="text-right font-medium">Units</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id}>
              <th scope="row" className="pr-6 text-left font-normal">{row.name}</th>
              <td className="pr-6 text-right tabular-nums">{percent(row.expected, 1)}</td>
              <td className="pr-6 text-right tabular-nums">{percent(row.observed, 1)}</td>
              <td className="text-right tabular-nums">{row.units.toLocaleString()}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Diagnostics({ results }: { results: Results }) {
  const d = results.diagnostics;
  const notes: string[] = [];
  if (d && d.multi_variant_units > 0) {
    notes.push(
      `${d.multi_variant_units.toLocaleString()} units were exposed to more than one variant and are left out.`,
    );
  }
  if (d && d.null_key_rows > 0) {
    notes.push(`${d.null_key_rows.toLocaleString()} exposure rows had no unit ID and are left out.`);
  }
  for (const u of results.unmapped_labels ?? []) {
    notes.push(
      `The variant value “${u.label}” (${u.units.toLocaleString()} units) matches no variant of this experiment and is left out.`,
    );
  }
  if (!notes.length) return null;
  return (
    <ul className="list-disc space-y-1 pl-5 text-sm text-slate-700" data-testid="warehouse-diagnostics">
      {notes.map((n) => (
        <li key={n}>{n}</li>
      ))}
    </ul>
  );
}

function intervalText(v: VariantResult): string {
  const ci = v.confidence_interval;
  if (!ci || ci.length !== 2) return '—';
  return `${meanText(ci[0])} to ${meanText(ci[1])}`;
}

/** The direction a result was analysed in, as its heading says it; null when it carries none. */
export function directionText(result: MetricResult | null): string | null {
  if (typeof result?.lower_is_better !== 'boolean') return null;
  return result.lower_is_better ? 'lower is better' : 'higher is better';
}

/**
 * Better or worse, by the core Results table's rule: only a treatment's
 * significant, finite, non-zero change, and nothing for a result without a
 * direction. The rule reads the metric's `lower_is_better` and the variant's
 * `is_control`, `is_significant` and `relative_improvement_pct`, which a
 * warehouse result carries with the same meaning; the cast is because the
 * rest of core's types do not fit one (its `metric_type` is `conversion` or
 * `mean`).
 */
export function warehouseVerdict(result: MetricResult, variant: VariantResult): 'better' | 'worse' | null {
  return changeVerdict(result as unknown as CoreMetricResult, variant as unknown as CoreVariantResult);
}

function changeClass(verdict: 'better' | 'worse' | null): string {
  // As core: the colour repeats the word, never replaces it.
  if (verdict === 'better') return ' text-green-700';
  if (verdict === 'worse') return ' text-red-700';
  return '';
}

function changeText(result: MetricResult, v: VariantResult): string {
  if (v.is_control || v.relative_improvement_pct === null) return '—';
  const pct = `${v.relative_improvement_pct > 0 ? '+' : ''}${v.relative_improvement_pct.toFixed(2)}%`;
  const verdict = warehouseVerdict(result, v);
  return verdict ? `${pct} · ${verdict}` : pct;
}

function MetricTable({ metric, experimentKey, run }: { metric: RunMetric; experimentKey: string; run: WarehouseRun }) {
  const direction = metric.computed ? directionText(metric.result) : null;
  const heading = (
    <h4 className="text-sm font-semibold text-slate-800">
      {metric.name}
      {metric.is_primary && <span className="text-xs font-medium text-slate-600"> (primary)</span>}
      {direction && <span className="text-xs font-medium text-slate-600"> · {direction}</span>}
    </h4>
  );
  if (!metric.computed || !metric.result) {
    const copy = failureCopy(metric.not_computed_reason, null, {
      warehouse: warehouseName(run.warehouse_type),
      connection: run.connection_name,
      experimentKey,
    });
    // The API's own "Not computed: …" line is the fallback for a reason
    // the dashboard has no words for.
    const reason = isKnownFailure(metric.not_computed_reason)
      ? copy.title
      : (metric.message ?? '').replace(/^Not computed:\s*/, '') ||
        (metric.not_computed_reason ? `code ${metric.not_computed_reason}.` : 'no reason was given.');
    return (
      <div className="rounded-md border border-slate-200 p-3" data-testid="warehouse-metric-not-computed">
        {heading}
        <p className="mt-1 text-sm text-slate-800">
          <strong>Not computed:</strong> {reason}
        </p>
        {copy.fix && <p className="mt-1 text-sm text-slate-600">{copy.fix}</p>}
      </div>
    );
  }
  const result = metric.result;
  const isMean = metric.metric_type === 'mean';
  return (
    <div className="overflow-x-auto" data-testid="warehouse-metric">
      {heading}
      <table className="mt-2 w-full text-sm">
        <caption className="sr-only">
          {isMean ? `${metric.name}: mean by variant` : `${metric.name}: conversion by variant`}
        </caption>
        <thead className="border-b border-slate-200 bg-slate-50">
          <tr>
            <th scope="col" className="px-3 py-2 text-left font-medium text-slate-700">Variant</th>
            <th scope="col" className="px-3 py-2 text-right font-medium text-slate-700">Units</th>
            {isMean ? (
              <>
                <th scope="col" className="px-3 py-2 text-right font-medium text-slate-700">Mean</th>
                <th scope="col" className="px-3 py-2 text-right font-medium text-slate-700">95% interval</th>
              </>
            ) : (
              <>
                <th scope="col" className="px-3 py-2 text-right font-medium text-slate-700">Converted</th>
                <th scope="col" className="px-3 py-2 text-right font-medium text-slate-700">Rate</th>
              </>
            )}
            <th scope="col" className="px-3 py-2 text-right font-medium text-slate-700">Change</th>
            <th scope="col" className="px-3 py-2 text-right font-medium text-slate-700">p-value</th>
            <th scope="col" className="px-3 py-2 text-left font-medium text-slate-700">Result</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {result.variants.map((v) => (
            <tr key={v.variant_id} data-testid="warehouse-variant-row">
              <th scope="row" className="px-3 py-2 text-left font-normal text-slate-800">
                {v.variant_name}
                {v.is_control && <span className="text-xs text-slate-600"> (control)</span>}
              </th>
              <td className="px-3 py-2 text-right tabular-nums">{v.sample_size.toLocaleString()}</td>
              {isMean ? (
                <>
                  <td className="px-3 py-2 text-right tabular-nums">{meanText(v.mean)}</td>
                  <td className="px-3 py-2 text-right tabular-nums">{intervalText(v)}</td>
                </>
              ) : (
                <>
                  <td className="px-3 py-2 text-right tabular-nums">{(v.conversions ?? 0).toLocaleString()}</td>
                  <td className="px-3 py-2 text-right tabular-nums">{percent(v.mean)}</td>
                </>
              )}
              <td className={`px-3 py-2 text-right tabular-nums${changeClass(warehouseVerdict(result, v))}`}>
                {changeText(result, v)}
              </td>
              <td className="px-3 py-2 text-right tabular-nums">
                {v.is_control ? '—' : pValueText(v.adjusted_p_value ?? v.p_value)}
              </td>
              <td className="px-3 py-2 text-slate-800">
                {v.is_control
                  ? 'Baseline'
                  : v.note
                    ? v.note
                    : v.is_significant
                      ? 'Significant'
                      : 'Not significant'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function RunResults({ run, results, experimentKey, onViewSql }: RunResultsProps) {
  return (
    <div className="space-y-4" data-testid="warehouse-results">
      <SourceStrip run={run} onViewSql={onViewSql} />
      <Srm results={results} />
      <Diagnostics results={results} />
      <div className="space-y-4">
        {results.metrics.map((metric) => (
          <MetricTable key={metric.metric_source_id} metric={metric} experimentKey={experimentKey} run={run} />
        ))}
      </div>
      <p className="text-xs text-slate-600" data-testid="warehouse-results-difference">
        {RESULTS_DIFFERENCE}
      </p>
    </div>
  );
}
