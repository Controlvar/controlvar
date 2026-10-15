/**
 * LiveResultsPanel — EP-058: Real-time WebSocket Streaming Results
 *
 * Displays live experiment results streamed over WebSocket.
 * Shows connection status, a live pulsing indicator, a variant results table,
 * and a Refresh button.
 */

import React, { useEffect, useState } from 'react';
import {
  useExperimentStream,
  ConnectionStatus,
  VariantResult,
} from '@/hooks/useExperimentStream';
import {
  NOT_ENOUGH_DATA,
  formatSignedPct,
  isFiniteNumber,
} from '@/components/results/shared/resultFormat';
import { parseApiTimestamp } from '@/utils/apiTimestamp';

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface LiveResultsPanelProps {
  experimentId: string;
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function StatusBadge({ status }: { status: ConnectionStatus }) {
  const configs: Record<ConnectionStatus, { dotClass: string; label: string }> = {
    connected: { dotClass: 'bg-green-500', label: 'Live' },
    connecting: { dotClass: 'bg-yellow-400 animate-pulse', label: 'Connecting…' },
    disconnected: { dotClass: 'bg-slate-400', label: 'Disconnected' },
    error: { dotClass: 'bg-red-500', label: 'Error' },
    unauthorized: { dotClass: 'bg-red-500', label: 'Not authorized' },
  };

  const { dotClass, label } = configs[status];

  return (
    <span className="inline-flex items-center gap-1.5 text-sm font-medium" data-testid="status-badge">
      <span className={`inline-block h-2.5 w-2.5 rounded-full ${dotClass}`} data-testid="status-dot" />
      {label}
    </span>
  );
}

function LiveIndicator() {
  return (
    <span
      className="inline-flex items-center gap-1 rounded-full bg-green-100 px-2 py-0.5 text-xs font-semibold text-green-700"
      data-testid="live-indicator"
    >
      <span className="relative flex h-2 w-2">
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-green-400 opacity-75" />
        <span className="relative inline-flex h-2 w-2 rounded-full bg-green-500" />
      </span>
      LIVE
    </span>
  );
}

function VariantsTable({ variants }: { variants: VariantResult[] }) {
  if (variants.length === 0) {
    return (
      <p className="text-sm text-slate-500 italic" data-testid="no-variants-message">
        No variant data available yet.
      </p>
    );
  }

  return (
    <div className="overflow-x-auto" data-testid="variants-table">
      <table className="min-w-full divide-y divide-slate-200 text-sm">
        <thead>
          <tr className="text-left text-xs font-semibold uppercase tracking-wide text-slate-500">
            <th className="pb-2 pr-4">Variant</th>
            <th className="pb-2 pr-4 text-right">Participants</th>
            <th className="pb-2 pr-4 text-right">Conversions</th>
            <th className="pb-2 pr-4 text-right">Rate</th>
            <th className="pb-2 pr-4 text-right">Lift vs Control</th>
            <th className="pb-2 pr-4 text-right">p-value (unadjusted)</th>
            <th className="pb-2 text-center">Live z-test</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {variants.map((variant) => {
            // The stream's own z-test at a fixed 0.05, unadjusted and on
            // conversions only. It is not the experiment's significance
            // decision (that is on the Overview tab), so it is never called
            // "Significant" here.
            const belowLiveThreshold =
              !variant.isControl &&
              isFiniteNumber(variant.pValue) &&
              variant.pValue < 0.05;
            const hasRate = isFiniteNumber(variant.conversionRate) && variant.participantCount > 0;
            const hasLift = isFiniteNumber(variant.relativeLift);

            return (
              <tr
                key={variant.key}
                className="hover:bg-slate-50 transition-colors"
                data-testid={`variant-row-${variant.key}`}
              >
                <td className="py-2 pr-4 font-medium text-slate-800">
                  {variant.name}
                  {variant.isControl && (
                    <span className="ml-2 rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-500">
                      Control
                    </span>
                  )}
                </td>
                <td className="py-2 pr-4 text-right tabular-nums text-slate-700">
                  {variant.participantCount.toLocaleString()}
                </td>
                <td className="py-2 pr-4 text-right tabular-nums text-slate-700">
                  {variant.conversionCount.toLocaleString()}
                </td>
                <td className="py-2 pr-4 text-right tabular-nums text-slate-700">
                  {hasRate ? `${(variant.conversionRate * 100).toFixed(2)}%` : NOT_ENOUGH_DATA}
                </td>
                <td className="py-2 pr-4 text-right tabular-nums">
                  {variant.isControl ? (
                    <span className="text-slate-600">—</span>
                  ) : hasLift ? (
                    <span
                      // Coloured only below the live threshold, as the
                      // Overview table colours only a significant result:
                      // otherwise a lift is not a win (#1244).
                      className={
                        !belowLiveThreshold
                          ? 'text-slate-600'
                          : variant.relativeLift > 0
                          ? 'text-green-700'
                          : variant.relativeLift < 0
                          ? 'text-red-700'
                          : 'text-slate-600'
                      }
                      data-testid={`lift-${variant.key}`}
                    >
                      {formatSignedPct(variant.relativeLift * 100)}
                    </span>
                  ) : (
                    NOT_ENOUGH_DATA
                  )}
                </td>
                <td className="py-2 pr-4 text-right tabular-nums text-slate-700">
                  {variant.isControl ? (
                    <span className="text-slate-600">—</span>
                  ) : isFiniteNumber(variant.pValue) ? (
                    variant.pValue.toFixed(3)
                  ) : (
                    NOT_ENOUGH_DATA
                  )}
                </td>
                <td className="py-2 text-center">
                  {variant.isControl ? null : belowLiveThreshold ? (
                    <span className="inline-block rounded-full bg-green-100 px-2 py-0.5 text-xs font-semibold text-green-800">
                      p &lt; 0.05 (unadjusted)
                    </span>
                  ) : (
                    <span className="inline-block rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-700">
                      Not yet
                    </span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function LiveResultsPanel({ experimentId }: LiveResultsPanelProps) {
  const { snapshot, status, error, refresh, connect } =
    useExperimentStream(experimentId);

  const [reconnectCountdown, setReconnectCountdown] = useState<number | null>(null);

  // Show a countdown timer when disconnected (informational only)
  useEffect(() => {
    if (status === 'disconnected' || status === 'error') {
      setReconnectCountdown(3);
      const interval = setInterval(() => {
        setReconnectCountdown((prev) => {
          if (prev === null || prev <= 1) {
            clearInterval(interval);
            return null;
          }
          return prev - 1;
        });
      }, 1000);
      return () => clearInterval(interval);
    } else {
      setReconnectCountdown(null);
    }
  }, [status]);

  const lastUpdated = snapshot?.timestamp
    ? parseApiTimestamp(snapshot.timestamp).toLocaleTimeString()
    : null;

  return (
    <div
      className="rounded-xl border border-slate-200 bg-white shadow-sm"
      data-testid="live-results-panel"
    >
      {/* Header */}
      <div className="flex items-center justify-between border-b border-slate-100 px-5 py-4">
        <div className="flex items-center gap-3">
          <h2 className="text-base font-semibold text-slate-800">Live Results</h2>
          {status === 'connected' && <LiveIndicator />}
        </div>

        <div className="flex items-center gap-4">
          <StatusBadge status={status} />

          <button
            onClick={refresh}
            disabled={status !== 'connected'}
            className="inline-flex items-center gap-1.5 rounded-md bg-indigo-50 px-3 py-1.5 text-sm font-medium text-indigo-700 hover:bg-indigo-100 disabled:cursor-not-allowed disabled:opacity-40 transition-colors"
            data-testid="refresh-button"
          >
            <svg
              className="h-4 w-4"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"
              />
            </svg>
            Refresh
          </button>
        </div>
      </div>

      {/* Content */}
      <div className="px-5 py-4 space-y-4">
        {/* The stream runs its own test; say which, so it is not read as the
            experiment's result at its confidence level. */}
        <p className="text-sm text-slate-700" data-testid="live-method-label">
          Live estimate: z-test, unadjusted, conversion only
        </p>

        {/* Error banner */}
        {error && (
          <div
            className="rounded-md bg-red-50 border border-red-200 px-4 py-3 text-sm text-red-700"
            data-testid="error-banner"
          >
            {error}
            {(status === 'disconnected' || status === 'error') && (
              <button
                onClick={connect}
                className="ml-3 underline hover:no-underline font-medium"
                data-testid="reconnect-button"
              >
                Reconnect
              </button>
            )}
          </div>
        )}

        {/* Auto-reconnect countdown */}
        {(status === 'disconnected' || status === 'error') &&
          reconnectCountdown !== null && (
            <p className="text-xs text-slate-400" data-testid="reconnect-countdown">
              Auto-reconnecting in {reconnectCountdown}s…
            </p>
          )}

        {/* Experiment-level metadata */}
        {snapshot && (
          <div className="flex flex-wrap gap-6 text-sm text-slate-600">
            <div>
              <span className="font-medium text-slate-500 uppercase text-xs tracking-wide">
                Status
              </span>
              <p className="mt-0.5 font-medium capitalize text-slate-800">
                {snapshot.status}
              </p>
            </div>
            <div>
              <span className="font-medium text-slate-500 uppercase text-xs tracking-wide">
                Total Participants
              </span>
              <p className="mt-0.5 font-medium text-slate-800">
                {snapshot.totalParticipants.toLocaleString()}
              </p>
            </div>
            {snapshot.daysRunning !== null && (
              <div>
                <span className="font-medium text-slate-500 uppercase text-xs tracking-wide">
                  Days Running
                </span>
                <p className="mt-0.5 font-medium text-slate-800">
                  {snapshot.daysRunning}
                </p>
              </div>
            )}
            <div>
              <span className="font-medium text-slate-500 uppercase text-xs tracking-wide">
                Live z-test p &lt; 0.05 (unadjusted)
              </span>
              <p
                className={`mt-0.5 font-semibold ${
                  snapshot.isSignificant ? 'text-green-600' : 'text-slate-500'
                }`}
                data-testid="significance-indicator"
              >
                {snapshot.isSignificant ? 'Yes' : 'Not yet'}
              </p>
            </div>
          </div>
        )}

        {/* Variants table */}
        {snapshot ? (
          <VariantsTable variants={snapshot.variants} />
        ) : status === 'connecting' ? (
          <div className="animate-pulse space-y-2" data-testid="loading-skeleton">
            <div className="h-4 rounded bg-slate-200 w-3/4" />
            <div className="h-4 rounded bg-slate-200 w-full" />
            <div className="h-4 rounded bg-slate-200 w-5/6" />
          </div>
        ) : (
          <p className="text-sm text-slate-400 italic" data-testid="no-data-message">
            No data available. Connect to start streaming.
          </p>
        )}

        {/* Footer */}
        {lastUpdated && (
          <p className="text-xs text-slate-400" data-testid="last-updated">
            Last updated: {lastUpdated}
          </p>
        )}
      </div>
    </div>
  );
}

export default LiveResultsPanel;
