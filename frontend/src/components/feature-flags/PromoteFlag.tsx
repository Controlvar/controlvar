import React, { useEffect, useId, useRef, useState } from 'react';
import { InlineConfirm } from '@/components/ui/InlineConfirm';
import { useFlagEnvironment } from '@/contexts/FlagEnvironmentContext';
import { FeatureFlagsService, FlagEnvironmentSettings, RolloutSchedule } from '@/services/featureFlags';
import { FLAG_ENVIRONMENT_LABELS, FlagEnvironment } from '@/utils/flagEnvironments';
import {
  NOT_COPIED_NOTE,
  PRODUCTION_CONFIRM_TEXT,
  PromotionRow,
  SCHEDULE_UNKNOWN_NOTE,
  nothingConfiguredText,
  promoteLabel,
  promoteSummary,
  promotedText,
  promotionRows,
  scheduleNote,
  statusWord,
} from '@/utils/flagPromotion';

/** What the dialog read when it opened: the flag in both environments, and Production's schedule. */
interface Loaded {
  source: FlagEnvironmentSettings;
  target: FlagEnvironmentSettings;
  /** Production as the target only: its schedule's status, `null` for none, `undefined` if unreadable. */
  schedule: string | null | undefined;
}

type Phase = { kind: 'loading' } | { kind: 'error'; message: string } | { kind: 'ready'; loaded: Loaded };

/** The schedule worth naming: active, then paused, then draft. */
function scheduleStatus(items: RolloutSchedule[]): string | null {
  const rank = ['active', 'paused', 'draft'];
  const open = items.filter((item) => rank.includes(item.status));
  if (open.length === 0) return null;
  open.sort((a, b) => rank.indexOf(a.status) - rank.indexOf(b.status));
  const status = open[0].status;
  return status.charAt(0).toUpperCase() + status.slice(1);
}

const LOAD_ERROR = 'Could not read the flag in both environments. Close this and try again.';
const PROMOTE_ERROR = 'Could not promote the flag.';

function Cell({ lines }: { lines: string[] }) {
  return (
    <>
      {lines.map((line, index) => (
        <div key={`${index}-${line}`}>{line}</div>
      ))}
    </>
  );
}

function DiffTable({
  rows,
  source,
  target,
}: {
  rows: PromotionRow[];
  source: FlagEnvironment;
  target: FlagEnvironment;
}) {
  const into = FLAG_ENVIRONMENT_LABELS[target];
  return (
    <div
      className="overflow-x-auto rounded-md focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-600"
      role="region"
      aria-label={`${FLAG_ENVIRONMENT_LABELS[source]} copied into ${into}`}
      tabIndex={0}
    >
      <table className="w-full text-sm" data-testid="promote-diff">
        <caption className="sr-only">{`What changes in ${into}`}</caption>
        <thead className="border-b border-slate-200">
          <tr>
            <th scope="col" className="px-3 py-2 text-left font-medium text-slate-700">
              Setting
            </th>
            <th scope="col" className="px-3 py-2 text-left font-medium text-slate-700">
              Change
            </th>
            <th scope="col" className="px-3 py-2 text-left font-medium text-slate-700">{`${into} now`}</th>
            <th scope="col" className="px-3 py-2 text-left font-medium text-slate-700">{`${into} after`}</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {rows.map((row) => (
            <tr key={row.field} data-testid={`promote-row-${row.field}`} data-changes={row.changes ? 'yes' : 'no'}>
              <th scope="row" className="px-3 py-2 text-left font-medium text-slate-800">
                {row.label}
              </th>
              <td className="px-3 py-2 font-medium text-slate-800" data-testid={`promote-change-${row.field}`}>
                {row.changes ? 'Changes' : 'No change'}
              </td>
              <td className="px-3 py-2 text-slate-800" data-testid={`promote-now-${row.field}`}>
                <Cell lines={row.now} />
              </td>
              <td className="px-3 py-2 text-slate-800" data-testid={`promote-after-${row.field}`}>
                <Cell lines={row.after} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * "Promote to Staging" (from Development) or "Promote to Production" (from
 * Staging), with the dialog that shows what it would change.
 *
 * The dialog reads the flag in both environments when it opens, lists the
 * three fields the API copies (status, rollout percentage, targeting rules)
 * as the target has them now and as they will be, names every field that
 * changes, and says so when the source has nothing configured and the target
 * will be turned off. Nothing is sent until the confirming button. Into
 * Production the confirmation is a second step in the same dialog.
 *
 * It belongs to the environment on screen: the flag page starts again when
 * another environment is chosen, so an answer that arrives later has nothing
 * to land in.
 */
export function PromoteFlag({
  flagId,
  flagKey,
  source,
  target,
  onPromoted,
}: {
  flagId: string;
  flagKey: string;
  source: FlagEnvironment;
  target: FlagEnvironment;
  /** The target's settings as the promotion left them. */
  onPromoted?: (settings: FlagEnvironmentSettings) => void;
}) {
  const { choose } = useFlagEnvironment();
  const [open, setOpen] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [phase, setPhase] = useState<Phase>({ kind: 'loading' });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);
  const button = useRef<HTMLButtonElement>(null);
  const alive = useRef(true);
  // Which opening of the dialog an answer belongs to.
  const opening = useRef(0);
  const sending = useRef(false);
  const titleId = useId();
  const summaryId = useId();

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  // Moving between the two steps unmounts the button that was pressed: focus
  // goes to the other step's safe answer (Back, or Continue again).
  const continueButton = useRef<HTMLButtonElement>(null);
  const backButton = useRef<HTMLButtonElement>(null);
  const stepMoved = useRef(false);
  useEffect(() => {
    if (!stepMoved.current) return;
    (confirming ? backButton : continueButton).current?.focus();
  }, [confirming]);

  // The diff pushes Cancel, which has focus while the dialog reads, out of
  // view on a small or zoomed screen: bring it back when the diff arrives.
  const cancelButton = useRef<HTMLButtonElement>(null);
  const confirmTextId = useId();
  useEffect(() => {
    const cancel = cancelButton.current;
    if (phase.kind === 'ready' && cancel && document.activeElement === cancel) {
      // Absent in jsdom.
      if (typeof cancel.scrollIntoView === 'function') cancel.scrollIntoView({ block: 'nearest' });
    }
  }, [phase.kind]);

  const label = promoteLabel(target);
  const from = FLAG_ENVIRONMENT_LABELS[source];
  const into = FLAG_ENVIRONMENT_LABELS[target];

  /** "Show Staging": the page starts again in the target, and focus stays on the Environment select. */
  const showTarget = () => {
    choose(target);
    document.querySelector<HTMLElement>('[data-testid="environment-select"]')?.focus();
  };

  const close = () => {
    opening.current += 1;
    setOpen(false);
    setConfirming(false);
    setError(null);
  };

  const openDialog = () => {
    if (open) return;
    const mine = ++opening.current;
    setOpen(true);
    setConfirming(false);
    setError(null);
    setResult(null);
    setPhase({ kind: 'loading' });
    void (async () => {
      try {
        const [all, schedules] = await Promise.all([
          FeatureFlagsService.getEnvironments(flagId),
          target === 'production'
            ? FeatureFlagsService.listRolloutSchedules(flagId).then(
                (res) => scheduleStatus(res.items ?? []),
                () => undefined,
              )
            : Promise.resolve<string | null | undefined>(null),
        ]);
        if (!alive.current || opening.current !== mine) return;
        const find = (name: FlagEnvironment) => all.environments?.find((s) => s.environment === name);
        const sourceSettings = find(source);
        const targetSettings = find(target);
        if (!sourceSettings || !targetSettings) {
          setPhase({ kind: 'error', message: LOAD_ERROR });
          return;
        }
        setPhase({ kind: 'ready', loaded: { source: sourceSettings, target: targetSettings, schedule: schedules } });
      } catch (err) {
        if (!alive.current || opening.current !== mine) return;
        setPhase({ kind: 'error', message: err instanceof Error ? err.message : LOAD_ERROR });
      }
    })();
  };

  const promote = async () => {
    // `aria-disabled` does not stop a second click.
    if (sending.current) return;
    sending.current = true;
    const mine = opening.current;
    setBusy(true);
    setError(null);
    try {
      const done = await FeatureFlagsService.promote(flagId, target, source);
      if (!alive.current || opening.current !== mine) return;
      setResult(promotedText(source, target, done));
      setOpen(false);
      setConfirming(false);
      onPromoted?.(done);
    } catch (err) {
      if (!alive.current || opening.current !== mine) return;
      setError(err instanceof Error && err.message ? err.message : PROMOTE_ERROR);
    } finally {
      sending.current = false;
      if (alive.current) setBusy(false);
    }
  };

  const loaded = phase.kind === 'ready' ? phase.loaded : null;
  const rows = loaded ? promotionRows(loaded.source, loaded.target) : [];
  const emptySource = loaded ? !loaded.source.configured : false;
  const nothingChanges = loaded ? rows.every((row) => !row.changes) : false;
  const production = target === 'production';
  const stepOne = !(production && confirming);

  // What a screen reader hears while the dialog works, in one polite region
  // that is in the dialog from its first render: the reading, one sentence
  // when the diff arrives (it is the only place that sentence is said), and
  // "Promoting…". It is empty after an error, which is an alert of its own.
  let status = '';
  if (busy) status = 'Promoting…';
  else if (phase.kind === 'loading') status = `Reading ${flagKey} in ${from} and ${into}…`;
  else if (loaded && !error) {
    const changing = rows.filter((row) => row.changes).length;
    status = changing === 0 ? `No setting changes in ${into}.` : `${changing} of ${rows.length} settings change in ${into}.`;
    if (emptySource) status += ` ${nothingConfiguredText(source, target, statusWord(loaded.target) === 'On')}`;
  }

  return (
    <div className="mt-4" data-testid="promote-flag">
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          ref={button}
          onClick={openDialog}
          className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-800 hover:bg-slate-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-600"
          data-testid="promote-button"
        >
          {label}
        </button>
        {/* Always in the page, so a screen reader hears the text added to it. */}
        <div role="status" className="text-sm text-slate-700" data-testid="promote-result">
          {result}
        </div>
        {result && (
          <button
            type="button"
            onClick={showTarget}
            className="text-sm text-blue-700 underline focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-600"
            data-testid="promote-show"
          >
            {`Show ${into}`}
          </button>
        )}
      </div>

      {open && (
        <InlineConfirm
          labelledBy={titleId}
          describedBy={summaryId}
          onCancel={close}
          busy={busy}
          returnFocus={button}
          tone="warning"
          className="mt-3 rounded-lg border p-4 text-sm text-slate-800"
          testId="promote-dialog"
        >
          <h2 id={titleId} className="text-base font-semibold text-slate-900">
            {label}
          </h2>
          <p id={summaryId} className="mt-1" data-testid="promote-summary">
            {promoteSummary(source, target)}
          </p>
          <div role="status" className="sr-only" data-testid="promote-status">
            {status}
          </div>
          {phase.kind === 'loading' && (
            <p className="mt-3 text-slate-700" data-testid="promote-loading">
              {`Reading ${flagKey} in ${from} and ${into}…`}
            </p>
          )}
          {phase.kind === 'error' && (
            <p role="alert" className="mt-3 text-red-700" data-testid="promote-load-error">
              {phase.message}
            </p>
          )}

          {loaded && stepOne && (
            <div className="mt-3 space-y-3">
              {emptySource && (
                <p className="font-medium text-slate-900" data-testid="promote-source-empty">
                  {nothingConfiguredText(source, target, statusWord(loaded.target) === 'On')}
                </p>
              )}
              <DiffTable rows={rows} source={source} target={target} />
              {nothingChanges && (
                <p data-testid="promote-no-change">{`${into} already has these settings, so no field changes.`}</p>
              )}
              <p data-testid="promote-not-copied">{NOT_COPIED_NOTE}</p>
              {production && loaded.schedule !== null && (
                <p data-testid="promote-schedule-note">
                  {loaded.schedule === undefined ? SCHEDULE_UNKNOWN_NOTE : scheduleNote(loaded.schedule)}
                </p>
              )}
            </div>
          )}

          {loaded && !stepOne && (
            <div className="mt-3 space-y-2" data-testid="promote-confirm-step">
              {/* Only this part describes Back and the confirming button: the warning, the
                  changes and a target turned off. The notes below stay readable but are
                  not read again at each button. */}
              <div className="space-y-2" id={confirmTextId} data-testid="promote-confirm-text">
                <p className="font-medium text-slate-900">{PRODUCTION_CONFIRM_TEXT}</p>
                <p data-testid="promote-confirm-fields">
                  {nothingChanges
                    ? `No field changes in ${into}.`
                    : `Changes in ${into}: ${rows
                        .filter((row) => row.changes)
                        .map((row) => row.label.toLowerCase())
                        .join(', ')}.`}
                </p>
                {emptySource && (
                  <p data-testid="promote-source-empty">
                    {nothingConfiguredText(source, target, statusWord(loaded.target) === 'On')}
                  </p>
                )}
              </div>
              <p data-testid="promote-not-copied">{NOT_COPIED_NOTE}</p>
              {loaded.schedule !== null && (
                <p data-testid="promote-schedule-note">
                  {loaded.schedule === undefined ? SCHEDULE_UNKNOWN_NOTE : scheduleNote(loaded.schedule)}
                </p>
              )}
            </div>
          )}

          {error && (
            <p role="alert" className="mt-3 text-red-700" data-testid="promote-error">
              {error}
            </p>
          )}

          <div className="mt-4 flex flex-wrap gap-3">
            {loaded && stepOne && production && (
              <button
                type="button"
                ref={continueButton}
                onClick={() => {
                  stepMoved.current = true;
                  setConfirming(true);
                }}
                className="rounded-lg bg-blue-700 px-3 py-1.5 font-medium text-white hover:bg-blue-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-600"
                data-testid="promote-continue"
              >
                Continue
              </button>
            )}
            {loaded && (!production || confirming) && (
              <button
                type="button"
                onClick={() => void promote()}
                aria-disabled={busy}
                aria-describedby={confirming ? confirmTextId : undefined}
                className="rounded-lg bg-blue-700 px-3 py-1.5 font-medium text-white hover:bg-blue-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-600 aria-disabled:opacity-60"
                data-testid="promote-confirm"
              >
                {busy ? 'Promoting…' : `Promote ${from} to ${into}`}
              </button>
            )}
            {production && confirming && (
              <button
                type="button"
                ref={backButton}
                onClick={() => {
                  if (!busy) setConfirming(false);
                }}
                aria-disabled={busy}
                aria-describedby={confirmTextId}
                className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 font-medium text-slate-800 hover:bg-slate-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-600"
                data-testid="promote-back"
              >
                Back
              </button>
            )}
            <button
              type="button"
              onClick={() => {
                if (!busy) close();
              }}
              aria-disabled={busy}
              data-autofocus
              ref={cancelButton}
              className="scroll-mb-2 rounded-lg border border-slate-300 bg-white px-3 py-1.5 font-medium text-slate-800 hover:bg-slate-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-600"
              data-testid="promote-cancel"
            >
              Cancel
            </button>
          </div>
        </InlineConfirm>
      )}
    </div>
  );
}
