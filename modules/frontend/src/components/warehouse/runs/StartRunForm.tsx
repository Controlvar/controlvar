/**
 * "Analyse now": pick a connection, its assignment source and one to ten of
 * its metric sources, and start a warehouse analysis. ADMIN and DEVELOPER
 * only — the caller renders this only for them.
 *
 * Proportion and mean metric sources can both be ticked; each says which it
 * is. A refusal from the API (the daily limit, another analysis in progress,
 * a source that is not validated…) is shown as an alert naming what to do.
 */
import React, { useEffect, useMemo, useState } from 'react';
import {
  StartRefusal,
  StartRunRequest,
  WarehouseConnectionSummary,
  WarehouseSourceSummary,
  startRefusal,
  warehouseName,
  warehouseRunsService,
} from '@modules/services/warehouseRuns';

export const MAX_METRICS = 10;

export interface StartRunFormProps {
  experimentId: string;
  /** Whether the experiment has a start date (the default window's start). */
  hasStartDate: boolean;
  onStarted: (runId: string) => void;
}

type Load =
  | { state: 'loading' }
  | { state: 'error'; message: string }
  | { state: 'ready'; connections: WarehouseConnectionSummary[]; sources: WarehouseSourceSummary[] };

/** `2026-09-01T00:00` (a datetime-local value, read as UTC) → ISO with Z. */
export function utcFromLocalInput(value: string): string | undefined {
  if (!value) return undefined;
  const iso = `${value.length === 16 ? `${value}:00` : value}Z`;
  return Number.isNaN(Date.parse(iso)) ? undefined : iso;
}

/** The first validated assignment source on the connection, else the first. */
export function defaultAssignment(connectionId: string, sources: WarehouseSourceSummary[]): string {
  const on = sources.filter((x) => x.connection_id === connectionId && x.kind === 'assignment');
  const first = on.find((x) => x.validated_at) ?? on[0];
  return first ? first.id : '';
}

export default function StartRunForm({ experimentId, hasStartDate, onStarted }: StartRunFormProps) {
  const [load, setLoad] = useState<Load>({ state: 'loading' });
  const [connectionId, setConnectionId] = useState('');
  const [assignmentId, setAssignmentId] = useState('');
  const [metricIds, setMetricIds] = useState<string[]>([]);
  const [windowStart, setWindowStart] = useState('');
  const [windowEnd, setWindowEnd] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [refusal, setRefusal] = useState<StartRefusal | null>(null);

  const selectConnection = (id: string, sources: WarehouseSourceSummary[]) => {
    setConnectionId(id);
    setAssignmentId(defaultAssignment(id, sources));
    setMetricIds([]);
    setRefusal(null);
  };

  useEffect(() => {
    let active = true;
    Promise.all([warehouseRunsService.listConnections(), warehouseRunsService.listSources()])
      .then(([c, s]) => {
        if (!active) return;
        const connections = c?.connections ?? [];
        const sources = s?.sources ?? [];
        setLoad({ state: 'ready', connections, sources });
        const usable = connections.find((x) => x.enabled) ?? connections[0];
        if (usable) {
          setConnectionId(usable.id);
          setAssignmentId(defaultAssignment(usable.id, sources));
        }
      })
      .catch((err: unknown) => {
        if (!active) return;
        setLoad({
          state: 'error',
          message: err instanceof Error ? err.message : 'Could not load warehouse connections.',
        });
      });
    return () => {
      active = false;
    };
  }, []);

  const connection =
    load.state === 'ready' ? load.connections.find((c) => c.id === connectionId) : undefined;
  const onConnection = useMemo(
    () => (load.state === 'ready' ? load.sources.filter((s) => s.connection_id === connectionId) : []),
    [load, connectionId],
  );
  const assignments = onConnection.filter((s) => s.kind === 'assignment');
  const metrics = onConnection.filter((s) => s.kind === 'metric');

  if (load.state === 'loading') {
    return (
      <p className="text-sm text-slate-600" data-testid="warehouse-start-loading">
        Loading warehouse connections…
      </p>
    );
  }
  if (load.state === 'error') {
    return (
      <p className="text-sm text-slate-800" data-testid="warehouse-start-load-error">
        Could not load warehouse connections: {load.message}
      </p>
    );
  }
  if (load.connections.length === 0) {
    return (
      <div className="rounded-md border border-dashed border-slate-300 p-4 text-sm text-slate-700" data-testid="warehouse-no-connection">
        <p className="font-medium text-slate-800">No warehouse connection yet.</p>
        <p className="mt-1">
          An admin adds one in Warehouse › Connections. Then define an assignment source and at
          least one metric source on it, and start the analysis here.
        </p>
      </div>
    );
  }

  const toggleMetric = (id: string) => {
    setMetricIds((current) =>
      current.includes(id) ? current.filter((x) => x !== id) : [...current, id],
    );
  };

  const needsWindow = !hasStartDate;
  const windowMissing = needsWindow && (!windowStart || !windowEnd);
  const connectionDisabled = !!connection && !connection.enabled;
  const canSubmit =
    !submitting &&
    !!connection &&
    !connectionDisabled &&
    !!assignmentId &&
    metricIds.length > 0 &&
    metricIds.length <= MAX_METRICS &&
    !windowMissing;

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    setRefusal(null);
    const body: StartRunRequest = {
      connection_id: connectionId,
      assignment_source_id: assignmentId,
      metric_source_ids: metricIds,
    };
    const start = utcFromLocalInput(windowStart);
    const end = utcFromLocalInput(windowEnd);
    if (start) body.window_start = start;
    if (end) body.window_end = end;
    try {
      const accepted = await warehouseRunsService.startRun(experimentId, body);
      onStarted(accepted.run_id);
    } catch (err) {
      setRefusal(startRefusal(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form onSubmit={(e) => void submit(e)} className="space-y-4" data-testid="warehouse-start-form" noValidate>
      <div>
        <label htmlFor="warehouse-connection" className="block text-sm font-medium text-slate-800">
          Warehouse connection
        </label>
        <select
          id="warehouse-connection"
          value={connectionId}
          onChange={(e) => selectConnection(e.target.value, load.sources)}
          aria-describedby={connectionDisabled ? 'warehouse-connection-help' : undefined}
          className="mt-1 block w-full max-w-md rounded-md border border-slate-300 bg-white px-3 py-2 text-sm"
          data-testid="warehouse-connection-select"
        >
          {load.connections.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name} ({warehouseName(c.warehouse_type)}){c.enabled ? '' : ' — not available'}
            </option>
          ))}
        </select>
        {connectionDisabled && (
          <p id="warehouse-connection-help" className="mt-1 text-sm text-slate-700">
            {warehouseName(connection?.warehouse_type)} is not available on this deployment yet, so
            this connection can’t run analyses.
          </p>
        )}
      </div>

      <div>
        <label htmlFor="warehouse-assignment" className="block text-sm font-medium text-slate-800">
          Assignment source
        </label>
        {assignments.length === 0 ? (
          <p className="mt-1 text-sm text-slate-700" data-testid="warehouse-no-assignment-source">
            This connection has no assignment source. A developer or admin defines one in Warehouse ›
            Sources.
          </p>
        ) : (
          <select
            id="warehouse-assignment"
            value={assignmentId}
            onChange={(e) => setAssignmentId(e.target.value)}
            className="mt-1 block w-full max-w-md rounded-md border border-slate-300 bg-white px-3 py-2 text-sm"
          >
            {assignments.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name} ({s.table}){s.validated_at ? '' : ' — not validated'}
              </option>
            ))}
          </select>
        )}
      </div>

      <fieldset>
        <legend className="text-sm font-medium text-slate-800">Metrics</legend>
        <p id="warehouse-metrics-help" className="text-sm text-slate-600">
          Tick 1 to {MAX_METRICS}. The first one you tick is the primary metric.
        </p>
        {metrics.length === 0 ? (
          <p className="mt-1 text-sm text-slate-700" data-testid="warehouse-no-metric-source">
            This connection has no metric source. An analyst, developer or admin defines one in
            Warehouse › Sources.
          </p>
        ) : (
          <ul className="mt-2 space-y-2">
            {metrics.map((m) => {
              const isMean = m.metric_type === 'mean';
              const unvalidated = !m.validated_at;
              const position = metricIds.indexOf(m.id);
              const helpId = `warehouse-metric-${m.id}-help`;
              return (
                <li key={m.id} data-testid="warehouse-metric-option">
                  <label className="flex items-start gap-2 text-sm text-slate-800">
                    <input
                      type="checkbox"
                      checked={position !== -1}
                      onChange={() => toggleMetric(m.id)}
                      aria-describedby={unvalidated ? helpId : 'warehouse-metrics-help'}
                      className="mt-0.5"
                    />
                    <span>
                      {m.name}{' '}
                      <span className="text-slate-600">
                        ({isMean ? 'mean' : 'proportion'}
                        {m.lower_is_better === true ? ', lower is better' : ''}
                        {position === 0 ? ', primary' : ''})
                      </span>
                    </span>
                  </label>
                  {unvalidated && (
                    <p id={helpId} className="ml-6 text-sm text-slate-600">
                      Not validated yet: validate it in Warehouse › Sources before using it.
                    </p>
                  )}
                </li>
              );
            })}
          </ul>
        )}
        {metricIds.length > MAX_METRICS && (
          <p className="mt-1 text-sm text-slate-800">An analysis takes at most {MAX_METRICS} metrics.</p>
        )}
      </fieldset>

      <fieldset>
        <legend className="text-sm font-medium text-slate-800">
          Window (UTC){needsWindow ? '' : ', optional'}
        </legend>
        <p id="warehouse-window-help" className="text-sm text-slate-600">
          {needsWindow
            ? 'This experiment has no start date, so give the window to analyse.'
            : 'Leave empty to analyse from the experiment’s start to its end, or to now.'}
        </p>
        <div className="mt-2 flex flex-wrap gap-4">
          <label className="text-sm text-slate-800">
            From
            <input
              type="datetime-local"
              value={windowStart}
              onChange={(e) => setWindowStart(e.target.value)}
              aria-describedby="warehouse-window-help"
              aria-invalid={needsWindow && !windowStart ? true : undefined}
              className="ml-2 rounded-md border border-slate-300 px-2 py-1"
              data-testid="warehouse-window-start"
            />
          </label>
          <label className="text-sm text-slate-800">
            To
            <input
              type="datetime-local"
              value={windowEnd}
              onChange={(e) => setWindowEnd(e.target.value)}
              aria-describedby="warehouse-window-help"
              aria-invalid={needsWindow && !windowEnd ? true : undefined}
              className="ml-2 rounded-md border border-slate-300 px-2 py-1"
              data-testid="warehouse-window-end"
            />
          </label>
        </div>
      </fieldset>

      {refusal && (
        <div
          role="alert"
          className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800"
          data-testid="warehouse-start-refusal"
          data-code={refusal.code}
        >
          {refusal.message}
        </div>
      )}

      <button
        type="submit"
        disabled={!canSubmit}
        className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50"
        data-testid="warehouse-start"
      >
        {submitting ? 'Starting…' : 'Analyse now'}
      </button>
    </form>
  );
}
