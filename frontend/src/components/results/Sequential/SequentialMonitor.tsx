import React from 'react';
import { SequentialTestingResponse } from '@/types/sequential';
import { EarlyStoppingBanner } from './EarlyStoppingBanner';
import { EvidenceRatioChart } from './EvidenceRatioChart';
import { AnalysisNotice } from '@/components/results/shared/AnalysisNotice';

interface SequentialMonitorProps {
  data: SequentialTestingResponse;
  /**
   * Whether a lower value is the better one for the primary metric, which the
   * analysis ran on. The banner needs it to tell a better variant from a worse
   * one; undefined when the metric could not be read.
   */
  lowerIsBetter?: boolean;
}

export function SequentialMonitor({ data, lowerIsBetter }: SequentialMonitorProps) {
  const boundary = data.msprt_result?.boundary ?? 1 / 0.05; // default 20

  // `at_risk` is the top-level advisory flag; older responses carry only
  // `long_running_risk.is_at_risk`.
  const atRisk = data.at_risk ?? data.long_running_risk?.is_at_risk ?? false;

  return (
    <div data-testid="sequential-monitor" className="space-y-6">
      <AnalysisNotice status={data.analysis_status} notice={data.analysis_notice} />

      {/* Stopping banner */}
      <EarlyStoppingBanner
        msprtResult={data.msprt_result}
        recommendedAction={data.recommended_action}
        confidenceSequence={data.confidence_sequence}
        lowerIsBetter={lowerIsBetter}
      />

      {/* Evidence ratio chart */}
      <section aria-label="Evidence trajectory">
        <h4 className="text-sm font-semibold text-slate-700 mb-3">
          Evidence Ratio Over Time
        </h4>
        <EvidenceRatioChart
          trajectory={data.evidence_trajectory}
          boundary={boundary}
        />
      </section>

      {/* Confidence sequence */}
      {data.confidence_sequence && (
        <section
          aria-label="Confidence sequence"
          data-testid="confidence-sequence"
          className="bg-slate-50 rounded-lg p-4"
        >
          <h4 className="text-sm font-semibold text-slate-700 mb-2">
            Always-Valid Confidence Interval
          </h4>
          <div className="flex gap-8 text-sm">
            <div>
              <span className="text-slate-500">Lower: </span>
              <span className="font-mono font-medium" data-testid="ci-lower">
                {data.confidence_sequence.lower.toFixed(4)}
              </span>
            </div>
            <div>
              <span className="text-slate-500">Upper: </span>
              <span className="font-mono font-medium" data-testid="ci-upper">
                {data.confidence_sequence.upper.toFixed(4)}
              </span>
            </div>
            <div>
              <span className="text-slate-500">Width: </span>
              <span className="font-mono font-medium" data-testid="ci-width">
                {data.confidence_sequence.width.toFixed(4)}
              </span>
            </div>
            <div>
              <span className="text-slate-500">N: </span>
              <span className="font-mono font-medium" data-testid="ci-n">
                {data.confidence_sequence.sample_size.toLocaleString()}
              </span>
            </div>
          </div>
        </section>
      )}

      {/* Long-running risk: an advisory note, not a stop recommendation. The
          stop/continue decision is the banner above (#286). */}
      {atRisk && (
        <div
          data-testid="long-running-risk"
          className="bg-slate-50 border border-slate-300 rounded-lg p-4"
          role="note"
        >
          <p className="text-sm font-semibold text-slate-800">
            Note: running longer than planned
          </p>
          {data.long_running_risk && (
            <p className="text-xs text-slate-700 mt-1">
              Running {data.long_running_risk.actual_duration_days} of{' '}
              {data.long_running_risk.expected_duration_days} expected days (
              {(data.long_running_risk.risk_ratio * 100).toFixed(0)}%).{' '}
              {data.long_running_risk.recommendation}
            </p>
          )}
          <p className="text-xs text-slate-700 mt-1">
            This is advisory: running long is not a reason to stop and not
            evidence of no effect.
          </p>
        </div>
      )}

      {/* Alpha spending boundaries */}
      {data.alpha_spending.length > 0 && (
        <section aria-label="Alpha spending boundaries">
          <h4 className="text-sm font-semibold text-slate-700 mb-2">
            Alpha Spending Schedule
          </h4>
          <div className="overflow-x-auto">
            <table
              className="min-w-full text-sm"
              data-testid="alpha-spending-table"
            >
              <thead>
                <tr className="border-b border-slate-200">
                  <th className="text-left py-2 pr-4 text-slate-600 font-medium">
                    Look
                  </th>
                  <th className="text-right py-2 pr-4 text-slate-600 font-medium">
                    Cumulative Alpha
                  </th>
                  <th className="text-right py-2 pr-4 text-slate-600 font-medium">
                    Boundary Z
                  </th>
                  <th className="text-right py-2 text-slate-600 font-medium">
                    Boundary P
                  </th>
                </tr>
              </thead>
              <tbody>
                {data.alpha_spending.map((row) => (
                  <tr key={row.look_number} className="border-b border-slate-100">
                    <td className="py-1.5 pr-4">{row.look_number}</td>
                    <td className="py-1.5 pr-4 text-right font-mono">
                      {row.cumulative_alpha.toFixed(4)}
                    </td>
                    <td className="py-1.5 pr-4 text-right font-mono">
                      {row.boundary_z.toFixed(3)}
                    </td>
                    <td className="py-1.5 text-right font-mono">
                      {row.boundary_p.toFixed(4)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </div>
  );
}
