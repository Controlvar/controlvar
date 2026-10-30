import React, { useEffect, useRef, useState } from 'react';
import { isApiError } from '@/services/api';
import { ExperimentScheduleUpdate, ExperimentsService } from '@/services/experiments';
import { Experiment } from '@/types/experiments';
import { parseApiTimestamp } from '@/utils/apiTimestamp';

/**
 * Schedule a DRAFT experiment's start and end (#1454): two `datetime-local`
 * pickers in the browser's time zone, sent to `PUT /experiments/{id}/schedule`.
 *
 * - Prefilled from the stored values, which the API sends as UTC with no zone,
 *   so they are read with `parseApiTimestamp`, never `new Date(stored)`.
 * - Only the dates that changed are sent, each as an instant with a `Z`
 *   (`toISOString()`), or null when removed. A draft that is due but not yet
 *   started still stores a start in the past, and the API refuses a past
 *   start that is sent, so resending an unchanged start would fail.
 * - Refused before sending, against what the draft would end up with (what is
 *   sent, and what is stored for the rest): a start or an end in the past, and
 *   an end less than an hour after the start.
 * - Every failure is fixed copy chosen by the answer's status, never the
 *   server's `detail`.
 *
 * A PAUSED experiment's scheduled resume stays in the API: the same route
 * reads `start_date` as the resume time there, and this form is offered on a
 * draft only.
 */

export const SCHEDULE_HEADING = 'Schedule';
export const START_LABEL = 'Start';
export const END_LABEL = 'End (optional)';
export const REMOVE = 'Remove';
export const SAVE_DATES = 'Save dates';
export const SAVING = 'Saving…';
export const SCHEDULE_SAVED = 'Dates saved.';

/** The help under each picker: what the scheduler does with the date. */
export const START_HELP =
  'It starts by itself at this time. A background check runs every 15 minutes by default, so it ' +
  'can start up to that much later. Until then it stays a draft, and Start still starts it at ' +
  'once. Start before the scheduled start moves the start to now. If it is not ready to start ' +
  '(two variants, a control and a metric), it stays a draft until it is.';
export const END_HELP =
  'A running experiment completes by itself at this time. A paused one is not completed by its ' +
  'end date. A draft with only an end never starts on its own.';

/** What the form refuses before sending anything. */
export const START_IN_PAST = 'The start cannot be in the past. To start now, use Start.';
export const END_IN_PAST = 'The end cannot be in the past.';
export const END_TOO_SOON = 'The end must be at least an hour after the start.';

/** The same sentence as ExperimentManageSection's UNREACHABLE (pinned equal in Jest). */
const UNREACHABLE = 'Could not reach the server. Check your connection and try again.';

/**
 * Fixed copy for a refused save, by status. The 400 names both of its causes:
 * the experiment is no longer a draft, or the end is less than an hour after
 * a stored start that changed since the page loaded.
 */
export const SCHEDULE_ERROR_COPY: Record<number, string> & { fallback: string } = {
  0: UNREACHABLE,
  400: 'The dates were not accepted, or this is no longer a draft. Reload the page.',
  403: 'Changing an experiment requires the ADMIN or DEVELOPER role.',
  404: 'This experiment no longer exists.',
  422:
    'The dates were not accepted. The start cannot be in the past, and the end must be at least ' +
    'an hour after the start.',
  fallback: 'Could not save the dates. Try again.',
};

export function scheduleErrorCopy(err: unknown): string {
  if (!isApiError(err)) return SCHEDULE_ERROR_COPY.fallback;
  return SCHEDULE_ERROR_COPY[err.status] ?? SCHEDULE_ERROR_COPY.fallback;
}

const HOUR_MS = 60 * 60 * 1000;

/** A `datetime-local` value: a date and a time, no zone. */
const LOCAL_VALUE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,3})?)?$/;

const pad = (n: number) => String(n).padStart(2, '0');

/**
 * A stored API time as a `datetime-local` value in the browser's zone, to the
 * minute, or '' when there is none. The API sends it as UTC with no zone, so
 * it is read with `parseApiTimestamp` (#1134).
 */
export function toLocalInput(stored: string | null | undefined): string {
  if (!stored) return '';
  const instant = parseApiTimestamp(stored);
  if (Number.isNaN(instant.getTime())) return '';
  return (
    `${instant.getFullYear()}-${pad(instant.getMonth() + 1)}-${pad(instant.getDate())}` +
    `T${pad(instant.getHours())}:${pad(instant.getMinutes())}`
  );
}

/**
 * The instant a `datetime-local` value names in the browser's zone, or null
 * for an empty value. A value with no zone is local time to `Date`, read with
 * that date's own offset, so a date across a daylight-saving change gets the
 * offset it has, not today's.
 */
export function fromLocalInput(value: string): Date | null {
  if (!LOCAL_VALUE.test(value)) return null;
  const instant = new Date(value);
  return Number.isNaN(instant.getTime()) ? null : instant;
}

/** The browser's IANA zone, or null when it does not say. */
export function browserTimeZone(): string | null {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || null;
  } catch {
    return null;
  }
}

/**
 * Some ICU versions put a narrow no-break space before AM/PM; the copy uses a
 * plain one. `\s` matches both kinds of no-break space.
 */
const plainSpaces = (text: string) => text.replace(/\s/g, ' ');

/**
 * How a picked time will be stored: "Saved as Mon, Nov 2, 2026, 9:00 AM PST
 * (17:00 UTC)." The UTC date is named too when it is not the local one. A
 * time a daylight-saving change skips shows here as the time it becomes.
 */
export function readBack(instant: Date): string {
  const local = new Intl.DateTimeFormat('en-US', {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    timeZoneName: 'short',
  }).format(instant);
  const sameDay =
    instant.getUTCFullYear() === instant.getFullYear() &&
    instant.getUTCMonth() === instant.getMonth() &&
    instant.getUTCDate() === instant.getDate();
  const utcDay = sameDay
    ? ''
    : `${new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' }).format(instant)}, `;
  const utcTime = `${pad(instant.getUTCHours())}:${pad(instant.getUTCMinutes())}`;
  return plainSpaces(`Saved as ${local} (${utcDay}${utcTime} UTC).`);
}

/** The two pickers' values, as `datetime-local` strings ('' for none). */
export interface ScheduleDraft {
  start: string;
  end: string;
}

export function scheduleDraftFrom(experiment: Experiment): ScheduleDraft {
  return { start: toLocalInput(experiment.start_date), end: toLocalInput(experiment.end_date) };
}

/**
 * The body of a save: only the dates whose picker changed since the form
 * opened, each as an instant with a `Z`, or null when it was removed. Never
 * `time_zone`.
 */
export function scheduleChanges(initial: ScheduleDraft, draft: ScheduleDraft): ExperimentScheduleUpdate {
  const body: ExperimentScheduleUpdate = {};
  if (draft.start !== initial.start) body.start_date = fromLocalInput(draft.start)?.toISOString() ?? null;
  if (draft.end !== initial.end) body.end_date = fromLocalInput(draft.end)?.toISOString() ?? null;
  return body;
}

export type ScheduleField = 'start' | 'end';

export interface ScheduleProblem {
  field: ScheduleField;
  message: string;
}

function storedMs(value: string | null | undefined): number | null {
  if (!value) return null;
  const ms = parseApiTimestamp(value).getTime();
  return Number.isNaN(ms) ? null : ms;
}

/**
 * The first reason the form refuses a save, or null. A date that is sent is
 * checked against now; the hour between start and end is checked against
 * what the draft would end up with, a stored date standing in for one that is
 * not sent (as the API does). A stored start already past is not refused:
 * it is not sent, so the end can still be changed.
 */
export function scheduleProblem(
  experiment: Experiment,
  body: ExperimentScheduleUpdate,
  nowMs: number,
): ScheduleProblem | null {
  const sentStart = body.start_date ? Date.parse(body.start_date) : null;
  const sentEnd = body.end_date ? Date.parse(body.end_date) : null;
  if (sentStart !== null && sentStart < nowMs) return { field: 'start', message: START_IN_PAST };
  if (sentEnd !== null && sentEnd < nowMs) return { field: 'end', message: END_IN_PAST };
  const start = 'start_date' in body ? sentStart : storedMs(experiment.start_date);
  const end = 'end_date' in body ? sentEnd : storedMs(experiment.end_date);
  if (start !== null && end !== null && end - start < HOUR_MS) {
    return { field: 'end_date' in body ? 'end' : 'start', message: END_TOO_SOON };
  }
  return null;
}

interface ExperimentScheduleSectionProps {
  experiment: Experiment;
  /** Called with the API's answer after a save. */
  onSaved: (updated: Experiment) => void;
  /** Cancel, or a save with nothing changed. */
  onClose: () => void;
}

const ERROR_ID = 'schedule-error';
const FIELD_HINT = 'mt-1 text-xs text-slate-600';

export function ExperimentScheduleSection({ experiment, onSaved, onClose }: ExperimentScheduleSectionProps) {
  // What the pickers held when the form opened: a picker left as it was is
  // not sent.
  const [initial] = useState<ScheduleDraft>(() => scheduleDraftFrom(experiment));
  const [draft, setDraft] = useState<ScheduleDraft>(initial);
  const [problem, setProblem] = useState<ScheduleProblem | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  // Where focus goes after the next render: the start on opening, a refused
  // field, or Save after a refused request.
  const [focusOn, setFocusOn] = useState<ScheduleField | 'save' | null>('start');

  const startInput = useRef<HTMLInputElement>(null);
  const endInput = useRef<HTMLInputElement>(null);
  const saveButton = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (focusOn === null || saving) return;
    ({ start: startInput, end: endInput, save: saveButton })[focusOn].current?.focus();
    setFocusOn(null);
  }, [focusOn, saving]);

  const zone = browserTimeZone();

  const change = (field: ScheduleField, value: string) => {
    setDraft((previous) => ({ ...previous, [field]: value }));
    setProblem(null);
    setError(null);
  };

  const remove = (field: ScheduleField) => {
    change(field, '');
    setFocusOn(field);
  };

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    const body = scheduleChanges(initial, draft);
    if (Object.keys(body).length === 0) {
      onClose();
      return;
    }
    const refused = scheduleProblem(experiment, body, Date.now());
    if (refused) {
      setProblem(refused);
      setFocusOn(refused.field);
      return;
    }
    setProblem(null);
    setSaving(true);
    try {
      const updated = await ExperimentsService.schedule(experiment.id, body);
      onSaved(updated);
    } catch (err) {
      setError(scheduleErrorCopy(err));
      setSaving(false);
      setFocusOn('save');
    }
  };

  const field = (which: ScheduleField) => {
    const id = `schedule-${which}`;
    const value = draft[which];
    const instant = fromLocalInput(value);
    const invalid = problem?.field === which;
    const describedBy = [
      invalid ? ERROR_ID : null,
      zone ? `${id}-zone` : null,
      instant ? `${id}-readback` : null,
      `${id}-help`,
    ]
      .filter(Boolean)
      .join(' ');
    return (
      <div>
        <label htmlFor={id} className="block text-sm font-medium text-slate-700">
          {which === 'start' ? START_LABEL : END_LABEL}
        </label>
        <div className="mt-1 flex flex-wrap items-center gap-2">
          <input
            ref={which === 'start' ? startInput : endInput}
            id={id}
            type="datetime-local"
            value={value}
            aria-invalid={invalid || undefined}
            aria-describedby={describedBy}
            onChange={(event) => change(which, event.target.value)}
            disabled={saving}
            className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-800"
            data-testid={id}
          />
          {value !== '' && (
            <button
              type="button"
              onClick={() => remove(which)}
              disabled={saving}
              className="px-2 py-1 text-sm text-blue-700 underline disabled:opacity-50"
              data-testid={`${id}-remove`}
            >
              {REMOVE}
              <span className="sr-only">{which === 'start' ? ' the start' : ' the end'}</span>
            </button>
          )}
        </div>
        {zone && (
          <p id={`${id}-zone`} className={FIELD_HINT} data-testid={`${id}-zone`}>
            In your time zone, {zone}.
          </p>
        )}
        {instant && (
          <p id={`${id}-readback`} className={FIELD_HINT} data-testid={`${id}-readback`}>
            {readBack(instant)}
          </p>
        )}
        <p id={`${id}-help`} className={FIELD_HINT} data-testid={`${id}-help`}>
          {which === 'start' ? START_HELP : END_HELP}
        </p>
      </div>
    );
  };

  const alert = problem?.message ?? error;

  return (
    <form
      onSubmit={(event) => void save(event)}
      aria-labelledby="schedule-heading"
      noValidate
      className="mt-3 rounded-lg border border-slate-200 bg-slate-50 p-4 space-y-4"
      data-testid="experiment-schedule-form"
    >
      <h2 id="schedule-heading" className="text-sm font-semibold text-slate-800">
        {SCHEDULE_HEADING}
      </h2>
      {field('start')}
      {field('end')}
      {alert && (
        <p
          id={ERROR_ID}
          role="alert"
          className="rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-700"
          data-testid={ERROR_ID}
        >
          {alert}
        </p>
      )}
      <div className="flex gap-2">
        <button
          ref={saveButton}
          type="submit"
          disabled={saving}
          className="px-3 py-1.5 rounded-md text-sm font-medium bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
          data-testid="schedule-save"
        >
          {saving ? SAVING : SAVE_DATES}
        </button>
        <button
          type="button"
          onClick={onClose}
          disabled={saving}
          className="px-3 py-1.5 rounded-md text-sm font-medium text-slate-700 border border-slate-300 bg-white hover:bg-slate-50"
          data-testid="schedule-cancel"
        >
          Cancel
        </button>
      </div>
    </form>
  );
}
