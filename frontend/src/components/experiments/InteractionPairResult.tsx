import React from 'react';
import { AnalysisNotice } from '@/components/results/shared/AnalysisNotice';
import { StatisticalBadge } from '@/components/results/shared/StatisticalBadge';
import { docsUrl } from '@/services/docs';
import { InteractionPairResponse, InteractionRow } from '@/types/interactions';
import { INTERACTIONS_COPY as COPY, ROW_COLUMNS, liftCell, overlapLine, reasonText, verdictText } from '@/utils/interactions';

interface Props {
  pair: InteractionPairResponse;
  /** The first and second experiment as the pair test was asked, by id. */
  experimentAId: string;
  experimentBId: string;
  /** An experiment's name, or null when the list does not have it. */
  nameOf: (id: string) => string | null;
  /** The heading, focused after each run. */
  headingRef: React.RefObject<HTMLHeadingElement | null>;
}

/** A name, or the id in monospace when the list does not have it: never blank. */
export function ExperimentName({ id, nameOf }: { id: string; nameOf: (id: string) => string | null }) {
  const name = nameOf(id);
  return name !== null ? <>{name}</> : <code className="font-mono text-slate-800">{id}</code>;
}

function Result({ row, pair }: { row: InteractionRow; pair: InteractionPairResponse }) {
  if (row.unavailable_reason) {
    return <span className="text-slate-800">{reasonText(row.unavailable_reason, pair)}</span>;
  }
  const corrected = typeof row.corrected_p_value === 'number';
  return (
    <StatisticalBadge
      pValue={corrected ? row.corrected_p_value : row.p_value}
      confidenceLevel={row.confidence_level}
      isSignificant={row.is_significant ?? undefined}
      pLabel={corrected ? 'corrected p' : 'p'}
    />
  );
}

/**
 * The pair test's answer (beta, #219): its own beta notice, the overlap, the
 * verdict, and one table row per tested or untested treatment, each with its
 * reason. The server's `recommendations` are not shown.
 */
export function InteractionPairResult({ pair, experimentAId, experimentBId, nameOf, headingRef }: Props) {
  const label = (id: string) => nameOf(id) ?? id;
  const rows = Array.isArray(pair.interaction_results) ? pair.interaction_results : [];
  const anySignificant = rows.some((row) => row.is_significant === true);

  return (
    <section aria-labelledby="interaction-pair-result-heading" className="space-y-3" data-testid="interaction-pair-result">
      <h3
        ref={headingRef}
        id="interaction-pair-result-heading"
        tabIndex={-1}
        className="text-base font-semibold text-slate-900 focus:outline-none"
        data-testid="interaction-pair-result-heading"
      >
        <ExperimentName id={experimentAId} nameOf={nameOf} /> and <ExperimentName id={experimentBId} nameOf={nameOf} />
      </h3>
      <AnalysisNotice status={pair.analysis_status} notice={pair.analysis_notice} />
      <p className="text-sm text-slate-800" data-testid="interaction-pair-overlap">
        {overlapLine(pair, label(experimentAId), label(experimentBId))}
      </p>
      <p className="text-sm font-medium text-slate-900" data-testid="interaction-pair-verdict">
        {verdictText(pair.has_interaction)}
      </p>
      {rows.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm" data-testid="interaction-pair-table">
            <caption className="text-left text-sm font-medium text-slate-700 pb-2">{COPY.rowsCaption}</caption>
            <thead className="border-b border-slate-200">
              <tr>
                {ROW_COLUMNS.map((column) => (
                  <th key={column} scope="col" className="text-left px-3 py-2 text-slate-700 font-medium">
                    {column}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {rows.map((row, index) => (
                <tr key={`${row.experiment_id}-${row.variant_id ?? 'all'}-${index}`} data-testid="interaction-pair-row">
                  <td className="px-3 py-2 text-slate-900">
                    <ExperimentName id={row.experiment_id} nameOf={nameOf} />
                  </td>
                  <td className="px-3 py-2 text-slate-800" data-testid="interaction-pair-treatment">
                    {row.variant_id ? (
                      (row.variant_name ?? <code className="font-mono">{row.variant_id}</code>)
                    ) : (
                      COPY.allTreatments
                    )}
                  </td>
                  <td className="px-3 py-2 text-slate-800">{row.metric_name ?? '—'}</td>
                  <td className="px-3 py-2 text-slate-800" data-testid="interaction-pair-lift">
                    {liftCell(row.arms)}
                  </td>
                  <td className="px-3 py-2" data-testid="interaction-pair-outcome">
                    <Result row={row} pair={pair} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {anySignificant && (
        <p className="text-sm text-slate-800" data-testid="interaction-pair-significant-note">
          A significant row means that experiment&apos;s result is still a valid average over the other&apos;s current
          split, but it may change once the other ships one variant: consider deciding the other first. The same
          pattern can also appear when one experiment changes who enters the other;{' '}
          <a
            href={docsUrl('api/interaction-detection')}
            className="text-blue-700 underline"
            target="_blank"
            rel="noreferrer"
          >
            the guide
          </a>{' '}
          shows how to check.
        </p>
      )}
      {rows.length > 0 && (
        <p className="text-sm text-slate-700" data-testid="interaction-pair-footnote">
          {COPY.footnote}
        </p>
      )}
    </section>
  );
}
