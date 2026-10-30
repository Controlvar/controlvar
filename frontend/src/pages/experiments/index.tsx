import React, { useState } from 'react';
import Link from 'next/link';
import {
  ExperimentStatus,
  ExperimentListResponse,
  experimentStatusColor,
  experimentStatusLabel,
  experimentTypeLabel,
} from '@/types/experiments';
import { ExperimentsService } from '@/services/experiments';
import { useApi } from '@/hooks/useApi';
import { PageTitle } from '@/components/PageTitle';
import { FirstRunChecklist } from '@/components/experiments/FirstRunChecklist';
import { useOptionalAuth } from '@/contexts/AuthContext';
import { canCreateExperiment, canReadEligibility } from '@/utils/experimentPermissions';
import { parseApiTimestamp } from '@/utils/apiTimestamp';

const STATUS_FILTERS: Array<{ label: string; value: ExperimentStatus | 'all' }> = [
  { label: 'All', value: 'all' },
  { label: 'Draft', value: 'draft' },
  { label: 'Active', value: 'active' },
  { label: 'Paused', value: 'paused' },
  { label: 'Completed', value: 'completed' },
];

export default function ExperimentsPage() {
  const [statusFilter, setStatusFilter] = useState<ExperimentStatus | 'all'>('all');
  // Analysts and viewers are refused by the API, so they are not offered the
  // button; the page is otherwise identical for every role.
  const auth = useOptionalAuth();
  const canCreate = canCreateExperiment(auth?.user);
  // Intent, not a second check: today canReadEligibility answers exactly as
  // canCreateExperiment does, so inside `canCreate &&` this is always true. It
  // names the group routes' own question, so the link follows them if the two
  // ever differ.
  const canManageEligibility = canReadEligibility(auth?.user);

  const { data, loading: isLoading, error } = useApi<ExperimentListResponse>(
    () => ExperimentsService.list(statusFilter !== 'all' ? { status: statusFilter } : undefined),
    [statusFilter],
  );

  const experiments = data?.items ?? [];
  const isEmpty = !isLoading && !error && experiments.length === 0;
  // The onboarding card only makes sense when the workspace has no experiments
  // at all — a filtered-empty view keeps the plain empty state.
  const showChecklist = isEmpty && statusFilter === 'all';

  return (
    <div className="flex-1 bg-slate-50">
      <PageTitle title="Experiments" />
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {/* Header */}
        <div className="flex flex-wrap items-center justify-between gap-3 mb-6">
          <div>
            <h1 className="text-2xl font-bold text-slate-900">Experiments</h1>
            <p className="text-sm text-slate-500 mt-1">
              Manage and monitor your A/B tests and feature experiments
            </p>
          </div>
          {canCreate && (
            <div className="flex flex-wrap items-center justify-end gap-x-4 gap-y-2">
              {/* Secondary links (#1455): the same roles as the API's group
                  routes (canReadEligibility, EXPERIMENT CREATE). */}
              {canManageEligibility && (
                <Link
                  href="/experiments/exclusion-groups"
                  className="text-sm font-medium text-blue-700 underline"
                  data-testid="exclusion-groups-link"
                >
                  Exclusion groups
                </Link>
              )}
              {canManageEligibility && (
                <Link
                  href="/experiments/holdouts"
                  className="text-sm font-medium text-blue-700 underline"
                  data-testid="holdouts-link"
                >
                  Global holdouts
                </Link>
              )}
              <Link
                href="/experiments/new"
                className="inline-flex items-center gap-2 px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 transition-colors"
                data-testid="new-experiment-btn"
              >
                + New Experiment
              </Link>
            </div>
          )}
        </div>

        {/* Status filter buttons */}
        <div className="flex gap-2 mb-6 flex-wrap" data-testid="status-filter">
          {STATUS_FILTERS.map((f) => (
            <button
              key={f.value}
              type="button"
              data-testid={`filter-${f.value}`}
              aria-pressed={statusFilter === f.value}
              onClick={() => setStatusFilter(f.value)}
              className={`px-3 py-1.5 rounded-full text-sm font-medium transition-colors ${
                statusFilter === f.value
                  ? 'bg-blue-600 text-white'
                  : 'bg-white text-slate-600 border border-slate-300 hover:bg-slate-50'
              }`}
            >
              {f.label}
            </button>
          ))}
        </div>

        {/* Loading state */}
        {isLoading && (
          <div className="text-center py-12" data-testid="experiments-loading">
            <div className="inline-block w-6 h-6 border-2 border-blue-600 border-t-transparent rounded-full animate-spin" />
            <p className="text-slate-500 mt-2 text-sm">Loading experiments...</p>
          </div>
        )}

        {/* Error state */}
        {!isLoading && error && (
          <div
            role="alert"
            className="rounded-lg bg-red-50 border border-red-200 p-4 text-sm text-red-700"
            data-testid="experiments-error"
          >
            {error}
          </div>
        )}

        {/* Empty states */}
        {showChecklist && <FirstRunChecklist />}
        {isEmpty && !showChecklist && (
          <div
            className="text-center py-16 bg-white rounded-lg border border-slate-200"
            data-testid="experiments-empty"
          >
            <p className="text-slate-500 mb-4">
              No {experimentStatusLabel(statusFilter).toLowerCase()} experiments
            </p>
            <button
              type="button"
              onClick={() => setStatusFilter('all')}
              className="text-sm text-blue-600 hover:underline"
            >
              Show all experiments
            </button>
          </div>
        )}

        {/* Experiments table */}
        {!isLoading && !error && experiments.length > 0 && (
          <div
            className="bg-white rounded-lg border border-slate-200 overflow-hidden"
            data-testid="experiments-table"
          >
            <table className="w-full text-sm">
              <thead className="bg-slate-50 border-b border-slate-200">
                <tr>
                  <th className="text-left px-4 py-3 text-slate-600 font-medium">Name</th>
                  <th className="text-left px-4 py-3 text-slate-600 font-medium">Status</th>
                  <th className="text-left px-4 py-3 text-slate-600 font-medium">Type</th>
                  <th className="text-left px-4 py-3 text-slate-600 font-medium">Variants</th>
                  <th className="text-left px-4 py-3 text-slate-600 font-medium">Created</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {experiments.map((exp) => (
                  <tr
                    key={exp.id}
                    className="hover:bg-slate-50 transition-colors"
                    data-testid="experiment-row"
                    data-experiment-id={exp.id}
                  >
                    <td className="px-4 py-3">
                      <Link
                        href={`/experiments/${exp.id}`}
                        className="font-medium text-blue-600 hover:text-blue-800 hover:underline"
                        data-testid="experiment-link"
                      >
                        {exp.name}
                      </Link>
                      <p className="text-xs text-slate-500 mt-0.5 font-mono">{exp.key ?? ''}</p>
                    </td>
                    <td className="px-4 py-3">
                      <span
                        className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${experimentStatusColor(exp.status)}`}
                        data-testid="experiment-status-pill"
                      >
                        {experimentStatusLabel(exp.status)}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-slate-600" data-testid="experiment-type-cell">
                      {experimentTypeLabel(exp.experiment_type)}
                    </td>
                    <td className="px-4 py-3 text-slate-600 tabular-nums">
                      {exp.variants?.length ?? 0}
                    </td>
                    <td className="px-4 py-3 text-slate-500">
                      {parseApiTimestamp(exp.created_at).toLocaleDateString()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
