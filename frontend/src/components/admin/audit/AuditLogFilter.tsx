import React, { useEffect, useState } from 'react';
import { isDayRangeReversed } from '@/utils/auditDates';
import { ENTITY_LABELS, actionFilterGroups } from './actionLabels';

/**
 * What the filter bar holds. `start_date`/`end_date` are calendar days
 * (`YYYY-MM-DD`); `AuditLogTable` turns them into the API's `from_date` and
 * `to_date`. The API also filters by `user_id`; this bar has no control for it.
 */
export interface AuditLogFilters {
  action_type?: string;
  entity_type?: string;
  start_date?: string;
  end_date?: string;
}

export const REVERSED_RANGE_MESSAGE = 'The To date must be on or after the From date.';

interface AuditLogFilterProps {
  filters: AuditLogFilters;
  onFilterChange: (filters: AuditLogFilters) => void;
}

/** The entity types something writes an entry for. */
const ENTITY_TYPES = Object.entries(ENTITY_LABELS).map(([value, label]) => ({ value, label }));

export function AuditLogFilter({ filters, onFilterChange }: AuditLogFilterProps) {
  // The date inputs show what was typed even while the range is refused, so
  // they keep their own copy; a valid range is passed up, a reversed one is not.
  const [startDraft, setStartDraft] = useState(filters.start_date ?? '');
  const [endDraft, setEndDraft] = useState(filters.end_date ?? '');

  useEffect(() => {
    setStartDraft(filters.start_date ?? '');
    setEndDraft(filters.end_date ?? '');
  }, [filters.start_date, filters.end_date]);

  const reversed = isDayRangeReversed(startDraft, endDraft);

  const handleChange = (key: keyof AuditLogFilters, value: string) => {
    onFilterChange({ ...filters, [key]: value || undefined });
  };

  const handleDateChange = (key: 'start_date' | 'end_date', value: string) => {
    const start = key === 'start_date' ? value : startDraft;
    const end = key === 'end_date' ? value : endDraft;
    setStartDraft(start);
    setEndDraft(end);
    if (isDayRangeReversed(start, end)) return;
    onFilterChange({ ...filters, start_date: start || undefined, end_date: end || undefined });
  };

  const handleClear = () => {
    setStartDraft('');
    setEndDraft('');
    onFilterChange({});
  };

  return (
    <div
      data-testid="audit-log-filter"
      className="bg-white border border-slate-200 rounded-lg p-4 flex flex-wrap gap-3 items-end"
    >
      {/* Action Type */}
      <div className="flex flex-col gap-1 min-w-[160px]">
        <label className="text-xs font-medium text-slate-600" htmlFor="filter-action-type">
          Action type
        </label>
        <select
          id="filter-action-type"
          data-testid="filter-action-type"
          value={filters.action_type ?? ''}
          onChange={(e) => handleChange('action_type', e.target.value)}
          className="border border-slate-300 rounded px-2 py-1.5 text-sm text-slate-800 focus:outline-none focus:ring-2 focus:ring-blue-500"
        >
          <option value="">All actions</option>
          {actionFilterGroups().map(({ group, options }) => (
            <optgroup key={group} label={group}>
              {options.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
      </div>

      {/* Entity Type */}
      <div className="flex flex-col gap-1 min-w-[160px]">
        <label className="text-xs font-medium text-slate-600" htmlFor="filter-entity-type">
          Entity type
        </label>
        <select
          id="filter-entity-type"
          data-testid="filter-entity-type"
          value={filters.entity_type ?? ''}
          onChange={(e) => handleChange('entity_type', e.target.value)}
          className="border border-slate-300 rounded px-2 py-1.5 text-sm text-slate-800 focus:outline-none focus:ring-2 focus:ring-blue-500"
        >
          <option value="">All entities</option>
          {ENTITY_TYPES.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
      </div>

      {/* Start Date */}
      <div className="flex flex-col gap-1">
        <label className="text-xs font-medium text-slate-600" htmlFor="filter-start-date">
          From date
        </label>
        <input
          id="filter-start-date"
          data-testid="filter-start-date"
          type="date"
          value={startDraft}
          onChange={(e) => handleDateChange('start_date', e.target.value)}
          aria-invalid={reversed || undefined}
          aria-describedby={reversed ? 'filter-date-error' : undefined}
          className="border border-slate-300 rounded px-2 py-1.5 text-sm text-slate-800 focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
      </div>

      {/* End Date */}
      <div className="flex flex-col gap-1">
        <label className="text-xs font-medium text-slate-600" htmlFor="filter-end-date">
          To date
        </label>
        <input
          id="filter-end-date"
          data-testid="filter-end-date"
          type="date"
          value={endDraft}
          onChange={(e) => handleDateChange('end_date', e.target.value)}
          aria-invalid={reversed || undefined}
          aria-describedby={reversed ? 'filter-date-error' : undefined}
          className="border border-slate-300 rounded px-2 py-1.5 text-sm text-slate-800 focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
      </div>

      {/* Clear Filters */}
      <button
        type="button"
        data-testid="clear-filters-button"
        onClick={handleClear}
        className="px-3 py-1.5 text-sm font-medium text-slate-600 border border-slate-300 rounded hover:bg-slate-50 transition-colors"
      >
        Clear filters
      </button>

      {reversed && (
        <p
          id="filter-date-error"
          data-testid="filter-date-error"
          role="alert"
          className="basis-full text-sm text-red-600"
        >
          {REVERSED_RANGE_MESSAGE}
        </p>
      )}
    </div>
  );
}
