import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import Link from 'next/link';
import { PageTitle } from '@/components/PageTitle';
import { ExperimentName, InteractionPairResult } from '@/components/experiments/InteractionPairResult';
import { useOptionalAuth } from '@/contexts/AuthContext';
import { isApiError } from '@/services/api';
import { docsUrl } from '@/services/docs';
import { ExperimentsService } from '@/services/experiments';
import { InteractionsService } from '@/services/interactions';
import { Experiment, experimentStatusLabel } from '@/types/experiments';
import { InteractionPairResponse, InteractionScanResponse } from '@/types/interactions';
import { canAnalyseInteractions } from '@/utils/experimentPermissions';
import {
  INTERACTIONS_COPY as COPY,
  OVERLAP_LEVELS,
  SCAN_COLUMNS,
  pairErrorCopy,
  partialListLine,
  roleNote,
  scanErrorCopy,
  scanSummary,
} from '@/utils/interactions';

/** The list route's largest page (`GET /api/v1/experiments/`, limit <= 500): one request, no paging. */
const LIST_LIMIT = 500;

type ListState =
  | { kind: 'loading' }
  | { kind: 'error' }
  | { kind: 'loaded'; items: Experiment[]; total: number };

type ScanState =
  | { kind: 'idle' }
  | { kind: 'running' }
  | { kind: 'done'; scan: InteractionScanResponse }
  | { kind: 'refused' }
  | { kind: 'error'; message: string };

type PairState =
  | { kind: 'idle' }
  | { kind: 'running' }
  | { kind: 'done'; a: string; b: string; pair: InteractionPairResponse }
  | { kind: 'refused' }
  | { kind: 'error'; message: string };

const BUTTON =
  'px-3 py-1.5 rounded-md text-sm font-medium border transition-colors disabled:opacity-50 disabled:cursor-not-allowed';
const SECONDARY = `${BUTTON} bg-white text-slate-700 border-slate-300 hover:bg-slate-50`;
const PRIMARY = `${BUTTON} bg-blue-600 text-white border-blue-600 hover:bg-blue-700`;
const SELECT =
  'mt-1 block w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus:border-blue-600 focus:outline-none focus:ring-1 focus:ring-blue-600';
const CARD = 'rounded-lg border border-slate-200 bg-white p-6 space-y-4';
const NOTE = 'rounded-lg border border-slate-200 bg-white p-4 text-sm text-slate-700';
const ALERT = 'rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-800';

/**
 * A 403 shows the role note (both routes are GETs, which demo mode never
 * refuses, so it is never the demo's refusal); anything else the fixed copy.
 */
function scanFailure(err: unknown): ScanState {
  if (isApiError(err) && err.status === 403) return { kind: 'refused' };
  return { kind: 'error', message: scanErrorCopy(err) };
}

function pairFailure(err: unknown): PairState {
  if (isApiError(err) && err.status === 403) return { kind: 'refused' };
  return { kind: 'error', message: pairErrorCopy(err) };
}

/**
 * Experiment interactions (#1960): the scan (stable) and the pair test (beta).
 *
 * The API admits a superuser or a role in `INTERACTION_ROLES`
 * (`canAnalyseInteractions`). Anyone else is told which roles can, and the
 * page sends no request at all, not even the list; nor does it before the
 * user is known.
 *
 * For a permitted user it reads the experiments once
 * (`GET /api/v1/experiments/?limit=500`) for their names and the pair test's
 * choices; `interactions-page`, the capability table's witness, marks only
 * that loaded state. The scan runs on request (it compares every pair of
 * active experiments). The pair test runs on any two different experiments;
 * changing either choice clears its answer, and only the latest request may
 * write one.
 */
export default function InteractionsPage() {
  const auth = useOptionalAuth();
  const user = auth?.user ?? null;
  const permitted = canAnalyseInteractions(user);

  const [list, setList] = useState<ListState>({ kind: 'loading' });
  const [scan, setScan] = useState<ScanState>({ kind: 'idle' });
  const [scanned, setScanned] = useState(false);
  const [first, setFirst] = useState('');
  const [second, setSecond] = useState('');
  const [pair, setPair] = useState<PairState>({ kind: 'idle' });
  const pairSeq = useRef(0);
  const resultHeading = useRef<HTMLHeadingElement>(null);

  const loadList = useCallback(async (showLoading: boolean) => {
    if (showLoading) setList({ kind: 'loading' });
    try {
      const answer = await ExperimentsService.list({ limit: LIST_LIMIT });
      const items = Array.isArray(answer?.items) ? answer.items : [];
      const total = typeof answer?.total === 'number' ? answer.total : items.length;
      setList({ kind: 'loaded', items, total });
    } catch {
      if (showLoading) setList({ kind: 'error' });
    }
  }, []);

  useEffect(() => {
    if (!permitted) return;
    void loadList(true);
  }, [permitted, loadList]);

  const names = useMemo(() => {
    const byId = new Map<string, Experiment>();
    if (list.kind === 'loaded') for (const item of list.items) byId.set(item.id, item);
    return byId;
  }, [list]);
  const nameOf = useCallback((id: string) => names.get(id)?.name ?? null, [names]);
  const label = (id: string) => nameOf(id) ?? id;

  useEffect(() => {
    if (pair.kind === 'done') resultHeading.current?.focus();
  }, [pair]);

  const runScan = async () => {
    setScan({ kind: 'running' });
    try {
      setScan({ kind: 'done', scan: await InteractionsService.scan() });
    } catch (err) {
      setScan(scanFailure(err));
    }
    setScanned(true);
  };

  const runPair = async (a: string, b: string) => {
    const seq = ++pairSeq.current;
    setPair({ kind: 'running' });
    try {
      const answer = await InteractionsService.pair(a, b);
      if (seq === pairSeq.current) setPair({ kind: 'done', a, b, pair: answer });
    } catch (err) {
      if (seq !== pairSeq.current) return;
      setPair(pairFailure(err));
      // One of the two has gone: read the list again so the choices are what the server holds.
      if (isApiError(err) && err.status === 404) void loadList(false);
    }
  };

  /** A changed choice clears the answer, and an answer still on its way is dropped. */
  const choose = (which: 'first' | 'second', id: string) => {
    pairSeq.current += 1;
    setPair({ kind: 'idle' });
    if (which === 'first') setFirst(id);
    else setSecond(id);
  };

  const testThisPair = (a: string, b: string) => {
    setFirst(a);
    setSecond(b);
    void runPair(a, b);
  };

  const sameTwice = first !== '' && first === second;
  const canTest = first !== '' && second !== '' && !sameTwice && pair.kind !== 'running';

  // A choice the list does not hold (a scan pair past its 500) is still shown, by its id.
  const extraChoices = Array.from(new Set([first, second].filter((id) => id !== '' && !names.has(id))));
  const choices =
    list.kind === 'loaded'
      ? [
          ...list.items.map((item) => ({ id: item.id, text: `${item.name} (${experimentStatusLabel(item.status)})` })),
          ...extraChoices.map((id) => ({ id, text: id })),
        ]
      : [];

  const role = user?.role ?? null;

  return (
    <div className="flex-1 bg-slate-50">
      <PageTitle title="Interactions" />
      <div className="max-w-5xl mx-auto px-4 sm:px-6 lg:px-8 py-8 space-y-6">
        <Link href="/experiments" className="text-sm text-blue-700 underline">
          ← Experiments
        </Link>
        <h1 className="text-2xl font-bold text-slate-900">Experiment interactions</h1>

        <p className="text-sm text-slate-700" data-testid="interactions-intro">
          {COPY.intro}{' '}
          <a
            href={docsUrl('api/interaction-detection')}
            className="text-blue-700 underline"
            target="_blank"
            rel="noreferrer"
          >
            {COPY.guideLink}
          </a>
        </p>

        {user !== null && !permitted && (
          <p className={NOTE} data-testid="interactions-refused">
            {roleNote(role)}
          </p>
        )}

        {(user === null || (permitted && list.kind === 'loading')) && (
          <p className="text-sm text-slate-600" role="status" data-testid="interactions-loading">
            Loading…
          </p>
        )}

        {permitted && list.kind === 'error' && (
          <div role="alert" className={`${ALERT} flex flex-wrap items-center gap-3`} data-testid="interactions-list-error">
            <span>{COPY.listFailed}</span>
            <button type="button" className={SECONDARY} onClick={() => void loadList(true)}>
              Try again
            </button>
          </div>
        )}

        {permitted && list.kind === 'loaded' && (
          <div className="space-y-6" data-testid="interactions-page">
            {list.total > list.items.length && (
              <p className={NOTE} data-testid="interactions-partial-list">
                {partialListLine(list.items.length, list.total)}
              </p>
            )}

            <section aria-labelledby="interactions-scan-heading" className={CARD}>
              <h2 id="interactions-scan-heading" className="text-lg font-semibold text-slate-900">
                {COPY.scanHeading}
              </h2>
              <p className="text-sm text-slate-700">{COPY.scanExplanation}</p>
              <button
                type="button"
                className={PRIMARY}
                disabled={scan.kind === 'running'}
                onClick={() => void runScan()}
                data-testid="interactions-scan-button"
              >
                {scanned ? COPY.scanAgain : COPY.scanButton}
              </button>

              {scan.kind === 'running' && (
                <p className="text-sm text-slate-700" role="status" data-testid="interactions-scan-running">
                  {COPY.scanning}
                </p>
              )}
              {scan.kind === 'refused' && (
                <p className={NOTE} data-testid="interactions-scan-refused">
                  {roleNote(role)}
                </p>
              )}
              {scan.kind === 'error' && (
                <div role="alert" className={ALERT} data-testid="interactions-scan-error">
                  {scan.message}
                </div>
              )}
              {scan.kind === 'done' && (
                <ScanAnswer scan={scan.scan} nameOf={nameOf} label={label} onTest={testThisPair} />
              )}
            </section>

            <section aria-labelledby="interactions-pair-heading" className={CARD}>
              <h2
                id="interactions-pair-heading"
                className="flex items-center gap-2 text-lg font-semibold text-slate-900"
                data-testid="interactions-pair-heading"
              >
                {COPY.pairHeading}
                <span className="inline-flex items-center rounded-full bg-amber-100 px-2 py-0.5 text-xs font-semibold text-amber-900 ring-1 ring-inset ring-amber-300">
                  Beta
                </span>
              </h2>
              <p className="text-sm text-slate-700">{COPY.pairExplanation}</p>

              <div className="grid gap-4 sm:grid-cols-2">
                <div>
                  <label htmlFor="interaction-first" className="block text-sm font-medium text-slate-800">
                    {COPY.firstLabel}
                  </label>
                  <select
                    id="interaction-first"
                    className={SELECT}
                    value={first}
                    onChange={(event) => choose('first', event.target.value)}
                    data-testid="interaction-first"
                  >
                    <option value="">{COPY.placeholder}</option>
                    {choices.map((choice) => (
                      <option key={choice.id} value={choice.id}>
                        {choice.text}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label htmlFor="interaction-second" className="block text-sm font-medium text-slate-800">
                    {COPY.secondLabel}
                  </label>
                  <select
                    id="interaction-second"
                    className={SELECT}
                    value={second}
                    onChange={(event) => choose('second', event.target.value)}
                    aria-invalid={sameTwice || undefined}
                    aria-describedby={sameTwice ? 'interaction-same-twice' : undefined}
                    data-testid="interaction-second"
                  >
                    <option value="">{COPY.placeholder}</option>
                    {choices.map((choice) => (
                      <option key={choice.id} value={choice.id}>
                        {choice.text}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
              {sameTwice && (
                <p id="interaction-same-twice" className="text-sm text-red-800" data-testid="interaction-same-twice">
                  {COPY.sameTwice}
                </p>
              )}
              <button
                type="button"
                className={PRIMARY}
                disabled={!canTest}
                onClick={() => void runPair(first, second)}
                data-testid="interaction-pair-button"
              >
                {COPY.pairButton}
              </button>

              {pair.kind === 'running' && (
                <p className="text-sm text-slate-700" role="status" data-testid="interaction-pair-running">
                  {COPY.testing}
                </p>
              )}
              {pair.kind === 'refused' && (
                <p className={NOTE} data-testid="interaction-pair-refused">
                  {roleNote(role)}
                </p>
              )}
              {pair.kind === 'error' && (
                <div role="alert" className={ALERT} data-testid="interaction-pair-error">
                  {pair.message}
                </div>
              )}
              {pair.kind === 'done' && (
                <InteractionPairResult
                  pair={pair.pair}
                  experimentAId={pair.a}
                  experimentBId={pair.b}
                  nameOf={nameOf}
                  headingRef={resultHeading}
                />
              )}
            </section>
          </div>
        )}
      </div>
    </div>
  );
}

interface ScanAnswerProps {
  scan: InteractionScanResponse;
  nameOf: (id: string) => string | null;
  label: (id: string) => string;
  onTest: (a: string, b: string) => void;
}

/** The scan's answer: overlap only, never an interaction verdict. Totals are the server's, not counted here. */
function ScanAnswer({ scan, nameOf, label, onTest }: ScanAnswerProps) {
  const pairs = Array.isArray(scan.analyses) ? scan.analyses : [];
  if (scan.total_active_experiments < 2) {
    return (
      <p className="text-sm text-slate-800" data-testid="interactions-scan-too-few">
        {COPY.scanTooFew}
      </p>
    );
  }
  if (pairs.length === 0) {
    return (
      <p className="text-sm text-slate-800" data-testid="interactions-scan-empty">
        {COPY.scanEmpty}
      </p>
    );
  }
  return (
    <div className="space-y-3">
      <p className="text-sm text-slate-800" data-testid="interactions-scan-summary">
        {scanSummary(scan)}
      </p>
      <div className="overflow-x-auto">
        <table className="w-full text-sm" data-testid="interactions-scan-table">
          <caption className="text-left text-sm font-medium text-slate-700 pb-2">{COPY.scanCaption}</caption>
          <thead className="border-b border-slate-200">
            <tr>
              {SCAN_COLUMNS.map((column) => (
                <th key={column} scope="col" className="text-left px-3 py-2 text-slate-700 font-medium">
                  {column}
                </th>
              ))}
              <th scope="col" className="px-3 py-2">
                <span className="sr-only">{COPY.pairHeading}</span>
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {pairs.map((item) => (
              <tr key={`${item.experiment_a_id}-${item.experiment_b_id}`} data-testid="interactions-scan-row">
                <td className="px-3 py-2 text-slate-900">
                  <ExperimentName id={item.experiment_a_id} nameOf={nameOf} />
                </td>
                <td className="px-3 py-2 text-slate-900">
                  <ExperimentName id={item.experiment_b_id} nameOf={nameOf} />
                </td>
                <td className="px-3 py-2 tabular-nums text-slate-800" data-testid="interactions-scan-overlap">
                  {item.overlap_coefficient.toFixed(2)}
                </td>
                <td className="px-3 py-2 text-slate-800" data-testid="interactions-scan-level">
                  {OVERLAP_LEVELS[item.overall_risk] ?? item.overall_risk}
                </td>
                <td className="px-3 py-2">
                  <button
                    type="button"
                    className={SECONDARY}
                    aria-label={`Test ${label(item.experiment_a_id)} and ${label(item.experiment_b_id)}`}
                    onClick={() => onTest(item.experiment_a_id, item.experiment_b_id)}
                    data-testid="interactions-scan-test-pair"
                  >
                    Test this pair
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="text-sm text-slate-700" data-testid="interactions-scan-under">
        {COPY.scanUnder}
      </p>
    </div>
  );
}
