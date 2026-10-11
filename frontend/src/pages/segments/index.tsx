import React, { useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { PageTitle } from '@/components/PageTitle';
import { useOptionalAuth } from '@/contexts/AuthContext';
import { SEGMENT_PAGE_SIZE, Segment, SegmentStatus, SegmentsService } from '@/services/segments';
import { ROLE_NOTE, segmentErrorCopy } from '@/utils/segmentErrors';
import { canChangeSegments } from '@/utils/segmentPermissions';
import { segmentRulesAreValid } from '@/utils/segmentRules';
import { KIND_LABELS, STATUS_COLORS, STATUS_LABELS } from '@/components/segments/segmentLabels';
import { parseApiTimestamp } from '@/utils/apiTimestamp';

type Filter = SegmentStatus | 'all';

const FILTERS: Array<{ label: string; value: Filter }> = [
  { label: 'Active', value: 'active' },
  { label: 'Inactive', value: 'inactive' },
  { label: 'Archived', value: 'archived' },
  { label: 'All', value: 'all' },
];

function formatDate(value: string): string {
  const d = parseApiTimestamp(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleDateString();
}

export default function SegmentsPage() {
  const auth = useOptionalAuth();
  const canChange = canChangeSegments(auth?.user);
  const [filter, setFilter] = useState<Filter>('active');
  const [page, setPage] = useState(0);
  const [segments, setSegments] = useState<Segment[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const requestSeq = useRef(0);

  const load = useCallback(async () => {
    requestSeq.current += 1;
    const seq = requestSeq.current;
    setLoading(true);
    setError(null);
    try {
      const items = await SegmentsService.list({
        status: filter === 'all' ? undefined : filter,
        limit: SEGMENT_PAGE_SIZE,
        offset: page * SEGMENT_PAGE_SIZE,
      });
      if (seq !== requestSeq.current) return;
      setSegments(items);
    } catch (err) {
      if (seq !== requestSeq.current) return;
      setError(segmentErrorCopy(err, 'load'));
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [filter, page]);

  useEffect(() => {
    void load();
  }, [load]);

  const chooseFilter = (value: Filter) => {
    setFilter(value);
    setPage(0);
  };

  const isEmpty = !loading && !error && segments.length === 0;
  const hasNext = segments.length === SEGMENT_PAGE_SIZE;

  return (
    <div className="flex-1 bg-slate-50">
      <PageTitle title="Segments" />
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <div className="flex items-center justify-between mb-6 gap-4 flex-wrap">
          <div>
            <h1 className="text-2xl font-bold text-slate-900">Segments</h1>
            <p className="text-sm text-slate-600 mt-1">
              Name an audience once and target flags and experiments at it.
            </p>
          </div>
          {canChange ? (
            <Link
              href="/segments/new"
              className="inline-flex items-center gap-2 px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 transition-colors"
              data-testid="new-segment-btn"
            >
              + New segment
            </Link>
          ) : (
            <p className="text-sm text-slate-600" data-testid="segments-role-note">
              {ROLE_NOTE}
            </p>
          )}
        </div>

        <div className="flex gap-2 mb-6 flex-wrap" role="group" aria-label="Filter by status" data-testid="segment-status-filter">
          {FILTERS.map((f) => (
            <button
              key={f.value}
              type="button"
              data-testid={`segment-filter-${f.value}`}
              aria-pressed={filter === f.value}
              onClick={() => chooseFilter(f.value)}
              className={`px-3 py-1.5 rounded-full text-sm font-medium transition-colors ${
                filter === f.value ? 'bg-blue-600 text-white' : 'bg-white text-slate-700 border border-slate-300 hover:bg-slate-50'
              }`}
            >
              {f.label}
            </button>
          ))}
        </div>

        {loading && (
          <div className="text-center py-12" data-testid="segments-loading">
            <p className="text-slate-600 text-sm">Loading segments...</p>
          </div>
        )}

        {!loading && error && (
          <div
            role="alert"
            className="rounded-lg bg-red-50 border border-red-200 p-4 text-sm text-red-800 flex items-center justify-between gap-4"
            data-testid="segments-error"
          >
            <span>{error}</span>
            <button
              type="button"
              onClick={() => void load()}
              className="px-3 py-1 rounded-md text-xs font-medium bg-white border border-red-200 hover:bg-red-100"
              data-testid="segments-retry"
            >
              Retry
            </button>
          </div>
        )}

        {isEmpty && (
          <div className="text-center py-16 bg-white rounded-lg border border-slate-200" data-testid="segments-empty">
            {filter === 'all' && page === 0 ? (
              <>
                <p className="text-slate-600 mb-4">No segments yet.</p>
                {canChange ? (
                  <Link
                    href="/segments/new"
                    className="inline-flex items-center gap-2 px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700"
                    data-testid="segments-empty-create"
                  >
                    Create segment
                  </Link>
                ) : (
                  <p className="text-sm text-slate-600">{ROLE_NOTE}</p>
                )}
              </>
            ) : (
              <>
                <p className="text-slate-600 mb-4">No segments match this filter.</p>
                <button
                  type="button"
                  onClick={() => chooseFilter('all')}
                  className="text-sm text-blue-700 hover:underline"
                  data-testid="segments-show-all"
                >
                  Show all segments
                </button>
              </>
            )}
          </div>
        )}

        {!loading && !error && segments.length > 0 && (
          <div className="bg-white rounded-lg border border-slate-200 overflow-x-auto" data-testid="segments-table">
            <table className="w-full text-sm">
              <thead className="bg-slate-50 border-b border-slate-200">
                <tr>
                  <th scope="col" className="text-left px-4 py-3 text-slate-700 font-medium">Name</th>
                  <th scope="col" className="text-left px-4 py-3 text-slate-700 font-medium">Type</th>
                  <th scope="col" className="text-left px-4 py-3 text-slate-700 font-medium">Status</th>
                  <th scope="col" className="text-left px-4 py-3 text-slate-700 font-medium">Updated</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {segments.map((segment) => {
                  const invalid = segment.kind === 'rules' && !segmentRulesAreValid(segment.rules);
                  return (
                    <tr key={segment.id} data-testid="segment-row" data-segment-id={segment.id}>
                      <td className="px-4 py-3">
                        <Link
                          href={`/segments/${segment.id}`}
                          className="font-medium text-blue-700 hover:text-blue-900 hover:underline"
                          data-testid="segment-link"
                        >
                          {segment.name}
                        </Link>
                        {segment.description && (
                          <p className="text-xs text-slate-600 mt-0.5 truncate max-w-xs">{segment.description}</p>
                        )}
                      </td>
                      <td className="px-4 py-3 text-slate-700" data-testid="segment-kind">
                        {KIND_LABELS[segment.kind] ?? segment.kind}
                        {invalid && (
                          <span
                            className="ml-2 inline-flex px-2 py-0.5 rounded-full text-xs font-medium bg-red-100 text-red-800"
                            data-testid="segment-rules-not-valid"
                          >
                            Rules not valid
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-3">
                        <span
                          className={`inline-flex px-2 py-0.5 rounded-full text-xs font-medium ${STATUS_COLORS[segment.status] ?? ''}`}
                          data-testid="segment-status"
                        >
                          {STATUS_LABELS[segment.status] ?? segment.status}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-slate-600">{formatDate(segment.updated_at)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        {!loading && !error && (page > 0 || hasNext) && (
          <nav className="mt-4 flex gap-2" aria-label="Pages">
            <button
              type="button"
              onClick={() => setPage((p) => Math.max(p - 1, 0))}
              disabled={page === 0}
              className="px-3 py-1.5 rounded-md text-sm border border-slate-300 bg-white disabled:opacity-50"
              data-testid="segments-previous"
            >
              Previous
            </button>
            <button
              type="button"
              onClick={() => setPage((p) => p + 1)}
              disabled={!hasNext}
              className="px-3 py-1.5 rounded-md text-sm border border-slate-300 bg-white disabled:opacity-50"
              data-testid="segments-next"
            >
              Next
            </button>
          </nav>
        )}
      </div>
    </div>
  );
}
