import React, { useEffect, useId, useRef, useState } from 'react';
import { UNREACHABLE } from '@/components/experiments/ExperimentManageSection';
import { isApiError } from '@/services/api';
import type { RolloutSchedule, RolloutStage } from '@/services/featureFlags';
import {
  DashboardTrigger,
  RolloutSchedulesService,
  RolloutStageCreateRequest,
  RolloutStageUpdateRequest,
} from '@/services/rolloutSchedules';
import { parseApiTimestamp } from '@/utils/apiTimestamp';

// ---------------------------------------------------------------------------
// Copy. Fixed sentences only: nothing the server sends in an error is shown.
// ---------------------------------------------------------------------------

export const ROLLOUT_ERROR_COPY: Record<number, string> & { fallback: string } = {
  400: 'The schedule has changed since the page loaded. Reload the page to see it.',
  403: "Changing this flag's rollout requires the ADMIN or DEVELOPER role.",
  404: 'This schedule no longer exists. Reload the page to see it.',
  422: "The schedule was not accepted. Check each stage's name and target, and that targets never go down.",
  fallback: 'Could not change the rollout schedule. Try again.',
};

export const CREATED = 'Schedule created as a draft. Nothing changes for users until you activate it.';

export const FORM_HELP =
  "Each stage sets the flag's rollout percentage when it starts. Targets never go down. " +
  'Time-based stages start by themselves once their time has passed and the stages before them ' +
  'are complete; the scheduler checks every 15 minutes by default, and such a stage lasts at ' +
  'least 24 hours. Manual stages wait for you.';

export const START_HELP = 'Leave blank to start as soon as the stages before it are complete.';
/** The edit form's close button while a stage has unsaved changes ("Done" otherwise). */
export const DISCARD_AND_CLOSE = 'Discard unsaved changes and close';
export const TARGET_RANGE = 'Enter a whole number from 0 to 100.';
export const SCHEDULE_NAME_REQUIRED = 'Give the schedule a name.';

export const stageNameRequired = (n: number) => `Give stage ${n} a name.`;
export const targetBelowPrevious = (n: number, previous: number) =>
  `Stage ${n}'s target must be at least ${previous}%, stage ${n - 1}'s target.`;
export const timeAfterManualNote = (n: number) =>
  `Stage ${n} is time-based but follows a manual stage, so it starts only after you complete stage ${n - 1}.`;

export const TRIGGER_OPTIONS: Array<{ value: DashboardTrigger; label: string }> = [
  { value: 'manual', label: 'When you advance it (manual)' },
  { value: 'time_based', label: 'At a set time (time-based)' },
];

const TRIGGER_LABELS: Record<string, string> = {
  time_based: 'Time-based',
  metric_based: 'Metric-based',
  manual: 'Manual',
};

const STAGE_STATUS_COLORS: Record<string, string> = {
  pending: 'bg-slate-100 text-slate-600',
  in_progress: 'bg-blue-100 text-blue-800',
  completed: 'bg-green-100 text-green-800',
  failed: 'bg-red-100 text-red-800',
};

/** The fixed sentence for a failed request, chosen by the answer's status only. */
export function rolloutErrorCopy(err: unknown): string {
  if (!isApiError(err)) return ROLLOUT_ERROR_COPY.fallback;
  if (err.status === 0) return UNREACHABLE;
  return ROLLOUT_ERROR_COPY[err.status] ?? ROLLOUT_ERROR_COPY.fallback;
}

export function activateConfirm(schedule: Pick<RolloutSchedule, 'name'>): string {
  return `Activate “${schedule.name}”? Its stages then set this flag's rollout percentage.`;
}

export function cancelConfirm(schedule: Pick<RolloutSchedule, 'name'>, flagPercentage: number): string {
  return `Cancel “${schedule.name}”? No more stages start, the flag stays at ${flagPercentage}%, and it cannot be resumed.`;
}

export function pausedNote(flagPercentage: number): string {
  return (
    `Paused. No stage starts or completes until you resume it, and the flag stays at ${flagPercentage}%. ` +
    'A safety rollback pauses a schedule this way too: check Safety check on this page before you resume.'
  );
}

export function startConfirm(stage: Pick<RolloutStage, 'name' | 'target_percentage'>): string {
  return `Start “${stage.name}” now? The flag goes to ${stage.target_percentage}% of users.`;
}

export function completeConfirm(
  stage: Pick<RolloutStage, 'name'>,
  next: Pick<RolloutStage, 'name' | 'target_percentage' | 'trigger_type'> | null,
  flagPercentage: number,
): string {
  if (!next) {
    return `Complete “${stage.name}” now? This completes the schedule; the flag stays at ${flagPercentage}%.`;
  }
  const whatever = next.trigger_type === 'time_based' ? `, whatever ${next.name}'s start time` : '';
  return (
    `Complete “${stage.name}” and start “${next.name}” now? ` +
    `The flag goes from ${flagPercentage}% to ${next.target_percentage}% at once${whatever}.`
  );
}

const RISES_OFF = 'After a rollback to 0% the flag stays off while its percentage rises.';

/**
 * What a resume does, as measured (PE 5): nothing at once; at the scheduler's
 * next check the stage the schedule is at moves on if it can. Time spent
 * paused counts towards a running stage's time, because the stage's clock is
 * not reset. A next stage sets the flag to its own target, which can be above
 * what a safety rollback set, and a rollback to 0% turned the flag off, which
 * no stage turns back on.
 */
export function resumeConfirm(schedule: RolloutSchedule): string {
  const head = `Resume “${schedule.name}”? Nothing changes at once.`;
  const { current, next } = stageSummary(schedule);
  const sets = (stage: RolloutStage) =>
    `sets the flag to ${stage.target_percentage}%, which can be above what a rollback set. ${RISES_OFF}`;
  if (current) {
    if (current.trigger_type !== 'time_based') {
      return `${head} No stage starts by itself: “${current.name}” waits for you to complete it.`;
    }
    const due = `At the next check, if “${current.name}”'s time is up (time paused included)`;
    if (!next) return `${head} ${due}, the schedule completes.`;
    if (next.trigger_type !== 'time_based') {
      return `${head} ${due}, it completes, and “${next.name}” then waits for you to start it.`;
    }
    const startPassed = next.start_date ? ` and “${next.name}”'s start time has passed` : '';
    return `${head} ${due}${startPassed}, “${next.name}” starts and ${sets(next)}`;
  }
  if (!next) return head;
  if (next.trigger_type !== 'time_based') {
    return `${head} No stage starts by itself: “${next.name}” waits for you to start it.`;
  }
  const when = next.start_date ? `, if “${next.name}”'s start time has passed` : '';
  return `${head} At the next check${when}, “${next.name}” starts and ${sets(next)}`;
}

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

/**
 * The schedule's stage in progress (`current`) and the one after it (`next`),
 * by `stage_order`. `next` is the first pending stage after the current one;
 * with no stage in progress it is the first pending stage, the one that starts
 * next. Either is null when there is none: no `next` on the last stage, and
 * neither once every stage is completed.
 */
export function stageSummary(schedule: RolloutSchedule): {
  current: RolloutStage | null;
  next: RolloutStage | null;
} {
  const stages = sortedStages(schedule);
  const currentIndex = stages.findIndex((s) => s.status === 'in_progress');
  const current = currentIndex >= 0 ? stages[currentIndex] : null;
  const next = stages.slice(currentIndex + 1).find((s) => s.status === 'pending') ?? null;
  return { current, next };
}

export function sortedStages(schedule: Pick<RolloutSchedule, 'stages'>): RolloutStage[] {
  return [...schedule.stages].sort((a, b) => a.stage_order - b.stage_order);
}

/**
 * The one stage a person may advance, and how (C8): on an ACTIVE schedule
 * only, the manual stage in progress ("complete", which starts the next
 * pending stage at once) or, with none in progress, the first pending stage
 * when it is manual ("start"). Never any other stage: the API advances any
 * pending manual stage, which would put two stages in progress.
 */
export function advanceTarget(
  schedule: RolloutSchedule,
): { kind: 'start' | 'complete'; stage: RolloutStage; next: RolloutStage | null } | null {
  if (schedule.status !== 'active') return null;
  const { current, next } = stageSummary(schedule);
  if (current) return current.trigger_type === 'manual' ? { kind: 'complete', stage: current, next } : null;
  if (next && next.trigger_type === 'manual') return { kind: 'start', stage: next, next: null };
  return null;
}

const pad = (n: number) => String(n).padStart(2, '0');
const LOCAL_INPUT = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/;

/** A `datetime-local` value as the instant it names in the browser's zone, or null. */
export function localInputToDate(value: string): Date | null {
  const m = LOCAL_INPUT.exec(value);
  if (!m) return null;
  // The component constructor reads local time on the date itself, so an
  // offset that changes between now and then (daylight saving) is applied.
  const d = new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], m[6] ? +m[6] : 0, 0);
  return Number.isNaN(d.getTime()) ? null : d;
}

/**
 * A `datetime-local` value as naive UTC, `YYYY-MM-DDTHH:MM:SS`, with no `Z` and
 * no offset. Stage dates are sent as naive UTC; the API takes a value with no
 * zone as UTC (`UTCDateTime`).
 */
export function localInputToNaiveUtc(value: string): string | null {
  const d = localInputToDate(value);
  return d ? d.toISOString().slice(0, 19) : null;
}

/** A stored API time as a `datetime-local` value in the browser's zone ('' if none). */
export function apiTimeToLocalInput(value: string | null | undefined): string {
  if (!value) return '';
  const d = parseApiTimestamp(value);
  if (Number.isNaN(d.getTime())) return '';
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** "Saved as Mon, Nov 2, 2026, 9:00 AM PST (17:00 UTC)." for a picker value. */
export function readBack(value: string): string | null {
  const d = localInputToDate(value);
  if (!d) return null;
  const local = new Intl.DateTimeFormat('en-US', {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    timeZoneName: 'short',
  })
    .format(d)
    .replace(/\s/g, ' ');
  return `Saved as ${local} (${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC).`;
}

function browserZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  } catch {
    return 'UTC';
  }
}

function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—';
  const d = parseApiTimestamp(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString();
}

function humanize(value: string): string {
  return value.replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase());
}

/** One stage as the form holds it. */
export interface StageDraft {
  name: string;
  target: string;
  trigger: DashboardTrigger;
  /** A `datetime-local` value, or ''. */
  start: string;
}

export const EMPTY_STAGE: StageDraft = { name: '', target: '', trigger: 'manual', start: '' };

export interface StageProblem {
  /** 0-based position in the list. */
  index: number;
  field: 'name' | 'target';
  message: string;
}

function wholePercent(value: string): number | null {
  const t = value.trim();
  if (!/^\d{1,3}$/.test(t)) return null;
  const n = Number(t);
  return n <= 100 ? n : null;
}

/**
 * The whole stage list, as it would be after a change, checked the way the
 * API checks a new schedule and more: every stage named, every target a whole
 * number from 0 to 100, and no target below the one before it, in
 * `stage_order`. The API checks the order only on create, and by list
 * position, so the form runs this before every create, add and update.
 */
export function checkStages(stages: Array<{ name: string; target: string }>): StageProblem[] {
  const problems: StageProblem[] = [];
  let previous: number | null = null;
  stages.forEach((stage, index) => {
    const n = index + 1;
    if (stage.name.trim() === '') problems.push({ index, field: 'name', message: stageNameRequired(n) });
    const target = wholePercent(stage.target);
    if (target === null) {
      problems.push({ index, field: 'target', message: TARGET_RANGE });
    } else if (previous !== null && target < previous) {
      problems.push({ index, field: 'target', message: targetBelowPrevious(n, previous) });
    }
    previous = target ?? previous;
  });
  return problems;
}

function draftFromStage(stage: RolloutStage): StageDraft {
  return {
    name: stage.name,
    target: String(stage.target_percentage),
    trigger: stage.trigger_type === 'time_based' ? 'time_based' : 'manual',
    start: apiTimeToLocalInput(stage.start_date),
  };
}

/** The create body's stage: numbered from 1 in list order, a start only on a timed stage. */
export function stageToCreate(draft: StageDraft, stageOrder: number): RolloutStageCreateRequest {
  const body: RolloutStageCreateRequest = {
    name: draft.name.trim(),
    stage_order: stageOrder,
    target_percentage: Number(draft.target.trim()),
    trigger_type: draft.trigger,
  };
  const start = draft.trigger === 'time_based' && draft.start ? localInputToNaiveUtc(draft.start) : null;
  if (start) body.start_date = start;
  return body;
}

/** The update body: exactly the fields that changed. */
export function stageChanges(stored: RolloutStage, draft: StageDraft): RolloutStageUpdateRequest {
  const body: RolloutStageUpdateRequest = {};
  const name = draft.name.trim();
  if (name !== stored.name) body.name = name;
  const target = Number(draft.target.trim());
  if (target !== stored.target_percentage) body.target_percentage = target;
  const storedInput = apiTimeToLocalInput(stored.start_date);
  if (draft.trigger !== stored.trigger_type) {
    body.trigger_type = draft.trigger;
    if (draft.trigger === 'manual') {
      if (stored.start_date) body.start_date = null;
    } else if (draft.start !== storedInput) {
      body.start_date = draft.start ? localInputToNaiveUtc(draft.start) : null;
    }
  } else if (draft.trigger === 'time_based' && draft.start !== storedInput) {
    body.start_date = draft.start ? localInputToNaiveUtc(draft.start) : null;
  }
  return body;
}

/** A pending stage the dashboard can edit: manual or time-based (never metric-based). */
function isEditable(stage: RolloutStage): boolean {
  return stage.status === 'pending' && (stage.trigger_type === 'manual' || stage.trigger_type === 'time_based');
}

// ---------------------------------------------------------------------------
// The stage fields, shared by the create form and the edit form
// ---------------------------------------------------------------------------

interface StageFieldsProps {
  idPrefix: string;
  position: number;
  draft: StageDraft;
  invalid: { name?: string; target?: string };
  disabled: boolean;
  onChange: (draft: StageDraft) => void;
  zone: string;
}

function StageFields({ idPrefix, position, draft, invalid, disabled, onChange, zone }: StageFieldsProps) {
  const nameId = `${idPrefix}-name`;
  const targetId = `${idPrefix}-target`;
  const triggerId = `${idPrefix}-trigger`;
  const startId = `${idPrefix}-start`;
  const back = draft.trigger === 'time_based' && draft.start ? readBack(draft.start) : null;
  const startDescribedBy = [`${startId}-zone`, `${startId}-help`, back ? `${startId}-readback` : '']
    .filter(Boolean)
    .join(' ');
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      <div>
        <label htmlFor={nameId} className="block text-sm font-medium text-slate-700">
          Stage {position} name
        </label>
        <input
          id={nameId}
          type="text"
          value={draft.name}
          maxLength={100}
          disabled={disabled}
          aria-invalid={invalid.name ? true : undefined}
          aria-describedby={invalid.name ? `${nameId}-error` : undefined}
          onChange={(e) => onChange({ ...draft, name: e.target.value })}
          className="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm"
          data-testid="rollout-stage-name-input"
        />
        {invalid.name && (
          <p id={`${nameId}-error`} className="mt-1 text-xs text-red-700">
            {invalid.name}
          </p>
        )}
      </div>
      <div>
        <label htmlFor={targetId} className="block text-sm font-medium text-slate-700">
          Stage {position} target (% of users)
        </label>
        <input
          id={targetId}
          type="number"
          inputMode="numeric"
          min={0}
          max={100}
          step={1}
          value={draft.target}
          disabled={disabled}
          aria-invalid={invalid.target ? true : undefined}
          aria-describedby={invalid.target ? `${targetId}-error` : undefined}
          onChange={(e) => onChange({ ...draft, target: e.target.value })}
          className="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm"
          data-testid="rollout-stage-target-input"
        />
        {invalid.target && (
          <p id={`${targetId}-error`} className="mt-1 text-xs text-red-700">
            {invalid.target}
          </p>
        )}
      </div>
      <div>
        <label htmlFor={triggerId} className="block text-sm font-medium text-slate-700">
          Stage {position} starts
        </label>
        <select
          id={triggerId}
          value={draft.trigger}
          disabled={disabled}
          onChange={(e) => onChange({ ...draft, trigger: e.target.value as DashboardTrigger })}
          className="mt-1 w-full rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm"
          data-testid="rollout-stage-trigger-input"
        >
          {TRIGGER_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </div>
      {draft.trigger === 'time_based' && (
        <div>
          <label htmlFor={startId} className="block text-sm font-medium text-slate-700">
            Stage {position} start time (optional)
          </label>
          <input
            id={startId}
            type="datetime-local"
            value={draft.start}
            disabled={disabled}
            aria-describedby={startDescribedBy}
            onChange={(e) => onChange({ ...draft, start: e.target.value })}
            className="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm"
            data-testid="rollout-stage-start-input"
          />
          <p id={`${startId}-zone`} className="mt-1 text-xs text-slate-600">
            In your time zone, {zone}.
          </p>
          {back && (
            <p id={`${startId}-readback`} className="mt-1 text-xs text-slate-600" data-testid="rollout-stage-readback">
              {back}
            </p>
          )}
          <p id={`${startId}-help`} className="mt-1 text-xs text-slate-600">
            {START_HELP}
          </p>
        </div>
      )}
    </div>
  );
}

/** "Note: Stage 3 is time-based but follows a manual stage, …" for each such stage. */
function OrderNotes({ triggers }: { triggers: string[] }) {
  const notes = triggers
    .map((trigger, i) => (i > 0 && trigger === 'time_based' && triggers[i - 1] === 'manual' ? i + 1 : null))
    .filter((n): n is number => n !== null);
  if (notes.length === 0) return null;
  return (
    <div data-testid="rollout-stage-notes">
      {notes.map((n) => (
        <p key={n} className="text-sm text-amber-800">
          <strong>Note:</strong> {timeAfterManualNote(n)}
        </p>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// The manager
// ---------------------------------------------------------------------------

type ConfirmKind = 'activate' | 'resume' | 'cancel' | 'start' | 'complete';

interface FormProblem {
  /** The id of the input to mark and focus, when the problem has one on screen. */
  inputId: string | null;
  message: string;
}

export interface RolloutScheduleManagerProps {
  flagId: string;
  /** The flag's stored rollout percentage (not the unsaved slider). */
  flagPercentage: number;
  /** The schedule the section shows (`pickSchedule`), or null. */
  schedule: RolloutSchedule | null;
  /** `canChangeFeatureFlags`: false gives the read-only view and sends nothing. */
  canChange: boolean;
  /**
   * The section's heading (focusable, `tabIndex={-1}`). Focus goes there once
   * Activate, Resume, Pause, Cancel schedule, Start stage or Complete stage has
   * landed, because the button that asked for it unmounts or changes.
   */
  headingRef?: React.RefObject<HTMLElement | null>;
  /** Reload after a change; `flagMoved` when the flag's percentage may have changed. */
  onChanged: (change: { flagMoved: boolean }) => Promise<void> | void;
}

const BUTTON =
  'px-3 py-1.5 rounded-md text-sm font-medium border transition-colors disabled:opacity-50 disabled:cursor-not-allowed';
const SECONDARY = `${BUTTON} bg-white text-slate-700 border-slate-300 hover:bg-slate-50`;
const PRIMARY = `${BUTTON} bg-blue-600 text-white border-blue-600 hover:bg-blue-700`;
const DANGER = `${BUTTON} bg-white text-red-700 border-red-300 hover:bg-red-50`;

/**
 * A flag's rollout schedule (#1456): what it is, and for ADMIN, DEVELOPER and
 * superusers the controls to create one and run it. It offers only what C8
 * allows:
 * - "Create a rollout schedule" only when no schedule is draft, active or
 *   paused (the API lets two ACTIVE schedules move one flag);
 * - stages added, edited or removed only while PENDING, and only while the
 *   schedule is draft or paused (an edit restarts a running stage's clock);
 * - no delete: Cancel covers a draft;
 * - advance only on the stage the schedule is at (`advanceTarget`);
 * - manual and time-based triggers only.
 * ANALYST and VIEWER get the schedule and its stages and nothing else.
 */
export function RolloutScheduleManager({
  flagId,
  flagPercentage,
  schedule,
  canChange,
  headingRef,
  onChanged,
}: RolloutScheduleManagerProps) {
  const uid = useId().replace(/[^A-Za-z0-9_-]/g, '');
  const zone = browserZone();

  const [mode, setMode] = useState<'create' | 'edit' | null>(null);
  const [createName, setCreateName] = useState('');
  const [createDescription, setCreateDescription] = useState('');
  const [createStages, setCreateStages] = useState<StageDraft[]>([]);
  const [editDrafts, setEditDrafts] = useState<Record<string, StageDraft>>({});
  const [newStage, setNewStage] = useState<StageDraft | null>(null);
  const [problems, setProblems] = useState<FormProblem[]>([]);
  const [confirm, setConfirm] = useState<ConfirmKind | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  /** Where focus goes next: a button that is still there, or `section` (the heading) after a change landed. */
  const [returnFocus, setReturnFocus] = useState<'create' | 'edit' | 'pause' | 'section' | ConfirmKind | null>(null);

  const createButton = useRef<HTMLButtonElement>(null);
  const editButton = useRef<HTMLButtonElement>(null);
  const activateButton = useRef<HTMLButtonElement>(null);
  const pauseButton = useRef<HTMLButtonElement>(null);
  const cancelButton = useRef<HTMLButtonElement>(null);
  const advanceButton = useRef<HTMLButtonElement>(null);
  const confirmRegion = useRef<HTMLDivElement>(null);
  const formHeading = useRef<HTMLHeadingElement>(null);
  const formAlert = useRef<HTMLDivElement>(null);
  const noticeRef = useRef<HTMLParagraphElement>(null);

  const status = schedule?.status ?? null;
  const open = status === 'draft' || status === 'active' || status === 'paused';
  const mayCreate = canChange && !open;
  const mayEdit = canChange && (status === 'draft' || status === 'paused');
  const advance = canChange && schedule ? advanceTarget(schedule) : null;
  const editing = mode === 'edit' && mayEdit;
  const creating = mode === 'create' && mayCreate;
  const busy = pending !== null;

  useEffect(() => {
    if (mode !== null) formHeading.current?.focus();
  }, [mode]);

  useEffect(() => {
    if (confirm !== null) confirmRegion.current?.focus();
  }, [confirm]);

  useEffect(() => {
    if (notice) noticeRef.current?.focus();
  }, [notice]);

  useEffect(() => {
    if (problems.length === 0) return;
    const first = problems[0];
    const field = first.inputId ? document.getElementById(first.inputId) : null;
    (field ?? formAlert.current)?.focus();
  }, [problems]);

  useEffect(() => {
    if (returnFocus === null || busy) return;
    const target: Record<string, React.RefObject<HTMLElement | null> | undefined> = {
      create: createButton,
      edit: editButton,
      activate: activateButton,
      resume: activateButton,
      pause: pauseButton,
      cancel: cancelButton,
      start: advanceButton,
      complete: advanceButton,
      section: headingRef,
    };
    target[returnFocus]?.current?.focus();
    setReturnFocus(null);
  }, [returnFocus, busy, headingRef]);

  const run = async (key: string, request: () => Promise<unknown>, flagMoved = false): Promise<boolean> => {
    setError(null);
    setNotice(null);
    setPending(key);
    try {
      await request();
    } catch (err) {
      setError(rolloutErrorCopy(err));
      setPending(null);
      return false;
    }
    try {
      await onChanged({ flagMoved });
    } finally {
      setPending(null);
    }
    return true;
  };

  // --- create ---------------------------------------------------------------
  const openCreate = () => {
    setError(null);
    setNotice(null);
    setProblems([]);
    setConfirm(null);
    setCreateName('');
    setCreateDescription('');
    setCreateStages([{ ...EMPTY_STAGE }]);
    setMode('create');
  };

  const closeForm = (which: 'create' | 'edit') => {
    setMode(null);
    setProblems([]);
    setNewStage(null);
    setEditDrafts({});
    setReturnFocus(which);
  };

  const submitCreate = async (event: React.FormEvent) => {
    event.preventDefault();
    const found: FormProblem[] = [];
    if (createName.trim() === '') found.push({ inputId: `${uid}-schedule-name`, message: SCHEDULE_NAME_REQUIRED });
    for (const p of checkStages(createStages)) {
      found.push({ inputId: `${uid}-create-${p.index}-${p.field}`, message: p.message });
    }
    setProblems(found);
    if (found.length > 0) return;
    const description = createDescription.trim();
    const ok = await run('create', () =>
      RolloutSchedulesService.create({
        name: createName.trim(),
        ...(description ? { description } : {}),
        feature_flag_id: flagId,
        max_percentage: 100,
        stages: createStages.map((draft, i) => stageToCreate(draft, i + 1)),
      }),
    );
    if (ok) {
      setMode(null);
      setNotice(CREATED);
    }
  };

  // --- edit -----------------------------------------------------------------
  const openEdit = () => {
    setError(null);
    setNotice(null);
    setProblems([]);
    setConfirm(null);
    setEditDrafts({});
    setNewStage(null);
    setMode('edit');
  };

  const stored = schedule ? sortedStages(schedule) : [];
  const draftOf = (stage: RolloutStage) => editDrafts[stage.id] ?? draftFromStage(stage);
  const storedAsChecked = stored.map((s) => ({ name: s.name, target: String(s.target_percentage) }));
  // What closing the edit form would drop: a stage changed but not saved, or
  // a stage being added. The close button says so instead of "Done".
  const unsaved =
    newStage !== null ||
    stored.some((s) => isEditable(s) && s.id in editDrafts && Object.keys(stageChanges(s, editDrafts[s.id])).length > 0);

  /** Problems of the whole list with one change applied; stored stages map to their inputs. */
  const editProblems = (list: Array<{ name: string; target: string }>, newIndex: number | null) =>
    checkStages(list).map((p): FormProblem => {
      const isNew = newIndex !== null && p.index === newIndex;
      const stage = stored[p.index];
      const inputId = isNew
        ? `${uid}-new-${p.field}`
        : stage && isEditable(stage)
          ? `${uid}-edit-${stage.id}-${p.field}`
          : null;
      return { inputId, message: p.message };
    });

  const saveStage = async (stage: RolloutStage, index: number) => {
    const draft = draftOf(stage);
    const list = storedAsChecked.map((s, i) => (i === index ? { name: draft.name, target: draft.target } : s));
    const found = editProblems(list, null);
    setProblems(found);
    if (found.length > 0) return;
    const body = stageChanges(stage, draft);
    if (Object.keys(body).length === 0) return;
    const ok = await run(`save-${stage.id}`, () => RolloutSchedulesService.updateStage(stage.id, body));
    if (ok) {
      setEditDrafts((drafts) => {
        const rest = { ...drafts };
        delete rest[stage.id];
        return rest;
      });
    }
  };

  const removeStage = async (stage: RolloutStage) => {
    setProblems([]);
    const ok = await run(`remove-${stage.id}`, () => RolloutSchedulesService.deleteStage(stage.id));
    if (ok) {
      setEditDrafts((drafts) => {
        const rest = { ...drafts };
        delete rest[stage.id];
        return rest;
      });
    }
  };

  const addStage = async () => {
    if (!schedule || !newStage) return;
    const list = [...storedAsChecked, { name: newStage.name, target: newStage.target }];
    const found = editProblems(list, list.length - 1);
    setProblems(found);
    if (found.length > 0) return;
    const order = stored.length > 0 ? Math.max(...stored.map((s) => s.stage_order)) + 1 : 1;
    const ok = await run('add', () => RolloutSchedulesService.addStage(schedule.id, stageToCreate(newStage, order)));
    if (ok) setNewStage(null);
  };

  // --- actions --------------------------------------------------------------
  const ask = (kind: ConfirmKind) => {
    setError(null);
    setNotice(null);
    setConfirm(kind);
  };

  const dismissConfirm = () => {
    const kind = confirm;
    setConfirm(null);
    if (kind) setReturnFocus(kind);
  };

  const confirmed = async () => {
    if (!schedule || !confirm) return;
    const kind = confirm;
    setConfirm(null);
    let ok = false;
    if (kind === 'activate' || kind === 'resume') {
      ok = await run(kind, () => RolloutSchedulesService.activate(schedule.id));
    } else if (kind === 'cancel') {
      ok = await run(kind, () => RolloutSchedulesService.cancel(schedule.id));
    } else if (advance) {
      ok = await run(kind, () => RolloutSchedulesService.advance(advance.stage.id), true);
    }
    // Landed: the button that asked is gone or changed, so the heading. Refused:
    // nothing changed, so the button that asked, beside the error.
    setReturnFocus(ok ? 'section' : kind);
  };

  const pause = async () => {
    if (!schedule) return;
    const ok = await run('pause', () => RolloutSchedulesService.pause(schedule.id));
    // Pause becomes Resume once it lands.
    setReturnFocus(ok ? 'section' : 'pause');
  };

  const confirmText = (): string => {
    if (!schedule || !confirm) return '';
    if (confirm === 'activate') return activateConfirm(schedule);
    if (confirm === 'resume') return resumeConfirm(schedule);
    if (confirm === 'cancel') return cancelConfirm(schedule, flagPercentage);
    if (!advance) return '';
    return advance.kind === 'start'
      ? startConfirm(advance.stage)
      : completeConfirm(advance.stage, advance.next, flagPercentage);
  };

  const confirmYes: Record<ConfirmKind, string> = {
    activate: 'Activate',
    resume: 'Resume',
    cancel: 'Cancel schedule',
    start: 'Start stage',
    complete: 'Complete stage',
  };

  const invalidFor = (prefix: string) => ({
    name: problems.find((p) => p.inputId === `${prefix}-name`)?.message,
    target: problems.find((p) => p.inputId === `${prefix}-target`)?.message,
  });

  const summary = schedule ? stageSummary(schedule) : { current: null, next: null };

  return (
    <div>
      {schedule && (
        <div>
          <p className="text-sm text-slate-700 font-medium" data-testid="rollout-schedule-name">
            {schedule.name}
          </p>
          <p className="text-xs text-slate-500 mt-0.5" data-testid="rollout-schedule-summary">
            Up to {schedule.max_percentage}%
            {summary.current ? ` · current: ${summary.current.name} → ${summary.current.target_percentage}%` : ''}
            {summary.next ? ` · next: ${summary.next.name} → ${summary.next.target_percentage}%` : ''}
          </p>
          {canChange && status === 'paused' && (
            <p className="mt-2 text-sm text-slate-700" data-testid="rollout-schedule-paused-note">
              {pausedNote(flagPercentage)}
            </p>
          )}
          <ol className="mt-3 space-y-2" data-testid="rollout-stages">
            {stored.map((stage, index) => {
              const advanceHere = advance && advance.stage.id === stage.id ? advance : null;
              return (
                <li
                  key={stage.id}
                  className="flex items-center justify-between gap-3 text-sm"
                  data-testid="rollout-stage"
                >
                  <div className="min-w-0">
                    <span className="text-slate-800">{stage.name}</span>
                    <span className="text-xs text-slate-500 ml-2">
                      {TRIGGER_LABELS[stage.trigger_type] ?? stage.trigger_type}
                      {stage.start_date ? ` · ${formatDateTime(stage.start_date)}` : ''}
                    </span>
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    <span className="tabular-nums text-slate-700">{stage.target_percentage}%</span>
                    <span
                      className={`inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-medium ${
                        STAGE_STATUS_COLORS[stage.status] ?? 'bg-slate-100 text-slate-600'
                      }`}
                    >
                      {humanize(stage.status)}
                    </span>
                    {advanceHere && (
                      <button
                        ref={advanceButton}
                        type="button"
                        onClick={() => ask(advanceHere.kind)}
                        disabled={busy || confirm !== null}
                        aria-label={`${advanceHere.kind === 'start' ? 'Start' : 'Complete'} stage ${index + 1} (${stage.name})`}
                        className={SECONDARY}
                        data-testid={advanceHere.kind === 'start' ? 'rollout-stage-start' : 'rollout-stage-complete'}
                      >
                        {advanceHere.kind === 'start' ? 'Start stage' : 'Complete stage'}
                      </button>
                    )}
                  </div>
                </li>
              );
            })}
          </ol>
        </div>
      )}

      {canChange && schedule && open && (
        <div
          role="group"
          aria-label="Run this rollout schedule"
          className="mt-4 flex flex-wrap items-center gap-2"
          data-testid="rollout-schedule-actions"
        >
          {status === 'draft' && (
            <button
              ref={activateButton}
              type="button"
              onClick={() => ask('activate')}
              disabled={busy || confirm !== null}
              className={PRIMARY}
              data-testid="rollout-schedule-activate"
            >
              {pending === 'activate' ? 'Activating…' : 'Activate'}
            </button>
          )}
          {status === 'paused' && (
            <button
              ref={activateButton}
              type="button"
              onClick={() => ask('resume')}
              disabled={busy || confirm !== null}
              className={PRIMARY}
              data-testid="rollout-schedule-resume"
            >
              {pending === 'resume' ? 'Resuming…' : 'Resume'}
            </button>
          )}
          {status === 'active' && (
            <button
              ref={pauseButton}
              type="button"
              onClick={() => void pause()}
              disabled={busy || confirm !== null}
              className={SECONDARY}
              data-testid="rollout-schedule-pause"
            >
              {pending === 'pause' ? 'Pausing…' : 'Pause'}
            </button>
          )}
          {mayEdit && (
            <button
              ref={editButton}
              type="button"
              onClick={openEdit}
              disabled={busy || editing}
              className={SECONDARY}
              data-testid="rollout-schedule-edit"
            >
              Edit stages
            </button>
          )}
          <button
            ref={cancelButton}
            type="button"
            onClick={() => ask('cancel')}
            disabled={busy || confirm !== null}
            className={DANGER}
            data-testid="rollout-schedule-cancel"
          >
            {pending === 'cancel' ? 'Cancelling…' : 'Cancel schedule'}
          </button>
        </div>
      )}

      {confirm && schedule && (
        <div
          ref={confirmRegion}
          role="dialog"
          aria-label={`Confirm ${confirmYes[confirm]}`}
          tabIndex={-1}
          onKeyDown={(event) => {
            if (event.key === 'Escape') dismissConfirm();
          }}
          className="mt-3 rounded-lg border border-amber-200 bg-amber-50 p-3 focus:outline-none focus:ring-2 focus:ring-blue-600"
          data-testid="rollout-schedule-confirm"
        >
          <p className="text-sm text-amber-900" data-testid="rollout-schedule-confirm-text">
            {confirmText()}
          </p>
          <div className="mt-3 flex flex-wrap gap-2">
            <button
              type="button"
              onClick={dismissConfirm}
              className={SECONDARY}
              data-testid="rollout-schedule-confirm-no"
            >
              Go back
            </button>
            <button
              type="button"
              onClick={() => void confirmed()}
              className={confirm === 'cancel' ? DANGER : PRIMARY}
              data-testid="rollout-schedule-confirm-yes"
            >
              {confirmYes[confirm]}
            </button>
          </div>
        </div>
      )}

      {mayCreate && !creating && (
        <div className="mt-3">
          <button
            ref={createButton}
            type="button"
            onClick={openCreate}
            disabled={busy}
            className={SECONDARY}
            data-testid="rollout-schedule-create"
          >
            Create a rollout schedule
          </button>
        </div>
      )}

      {notice && (
        <p
          ref={noticeRef}
          role="status"
          tabIndex={-1}
          className="mt-3 text-sm text-green-800 focus:outline-none"
          data-testid="rollout-schedule-notice"
        >
          {notice}
        </p>
      )}

      {error && (
        <div
          role="alert"
          className="mt-3 rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-700"
          data-testid="rollout-schedule-action-error"
        >
          {error}
        </div>
      )}

      {(creating || editing) && (
        <form
          onSubmit={(event) => {
            if (creating) {
              void submitCreate(event);
            } else {
              event.preventDefault();
            }
          }}
          aria-labelledby={`${uid}-form-heading`}
          noValidate
          className="mt-4 rounded-lg border border-slate-200 bg-slate-50 p-4 space-y-4"
          data-testid="rollout-schedule-form"
        >
          <h3
            ref={formHeading}
            id={`${uid}-form-heading`}
            tabIndex={-1}
            className="text-sm font-semibold text-slate-800 focus:outline-none"
          >
            {creating ? 'Create a rollout schedule' : 'Edit stages'}
          </h3>

          {problems.length > 0 && (
            <div
              ref={formAlert}
              role="alert"
              tabIndex={-1}
              className="rounded-md bg-red-50 border border-red-200 p-3 text-sm text-red-700 focus:outline-none"
              data-testid="rollout-schedule-form-error"
            >
              {problems[0].message}
            </div>
          )}

          {creating && (
            <>
              <div>
                <label htmlFor={`${uid}-schedule-name`} className="block text-sm font-medium text-slate-700">
                  Schedule name
                </label>
                <input
                  id={`${uid}-schedule-name`}
                  type="text"
                  value={createName}
                  maxLength={100}
                  disabled={busy}
                  aria-invalid={problems.some((p) => p.inputId === `${uid}-schedule-name`) || undefined}
                  aria-describedby={
                    problems.some((p) => p.inputId === `${uid}-schedule-name`) ? `${uid}-schedule-name-error` : undefined
                  }
                  onChange={(e) => setCreateName(e.target.value)}
                  className="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm"
                  data-testid="rollout-schedule-name-input"
                />
                {problems.some((p) => p.inputId === `${uid}-schedule-name`) && (
                  <p id={`${uid}-schedule-name-error`} className="mt-1 text-xs text-red-700">
                    {SCHEDULE_NAME_REQUIRED}
                  </p>
                )}
              </div>
              <div>
                <label htmlFor={`${uid}-schedule-description`} className="block text-sm font-medium text-slate-700">
                  Description (optional)
                </label>
                <textarea
                  id={`${uid}-schedule-description`}
                  value={createDescription}
                  rows={2}
                  disabled={busy}
                  onChange={(e) => setCreateDescription(e.target.value)}
                  className="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm"
                  data-testid="rollout-schedule-description-input"
                />
              </div>
            </>
          )}

          <fieldset className="space-y-4" aria-describedby={`${uid}-help`}>
            <legend className="text-sm font-medium text-slate-800">Stages, in order</legend>
            <p id={`${uid}-help`} className="text-xs text-slate-600">
              {FORM_HELP}
            </p>

            {creating &&
              createStages.map((draft, i) => (
                <div
                  key={i}
                  className="rounded-md border border-slate-200 bg-white p-3 space-y-2"
                  data-testid="rollout-stage-fields"
                >
                  <StageFields
                    idPrefix={`${uid}-create-${i}`}
                    position={i + 1}
                    draft={draft}
                    invalid={invalidFor(`${uid}-create-${i}`)}
                    disabled={busy}
                    zone={zone}
                    onChange={(next) => setCreateStages((list) => list.map((d, j) => (j === i ? next : d)))}
                  />
                  {createStages.length > 1 && (
                    <button
                      type="button"
                      onClick={() => setCreateStages((list) => list.filter((_, j) => j !== i))}
                      disabled={busy}
                      className={DANGER}
                      data-testid="rollout-stage-remove"
                    >
                      Remove stage {i + 1}
                      {draft.name.trim() ? ` (${draft.name.trim()})` : ''}
                    </button>
                  )}
                </div>
              ))}

            {editing &&
              stored.map((stage, i) =>
                isEditable(stage) ? (
                  <div
                    key={stage.id}
                    className="rounded-md border border-slate-200 bg-white p-3 space-y-2"
                    data-testid="rollout-stage-fields"
                  >
                    <StageFields
                      idPrefix={`${uid}-edit-${stage.id}`}
                      position={i + 1}
                      draft={draftOf(stage)}
                      invalid={invalidFor(`${uid}-edit-${stage.id}`)}
                      disabled={busy}
                      zone={zone}
                      onChange={(next) => setEditDrafts((drafts) => ({ ...drafts, [stage.id]: next }))}
                    />
                    <div className="flex flex-wrap gap-2">
                      <button
                        type="button"
                        onClick={() => void saveStage(stage, i)}
                        disabled={busy}
                        className={PRIMARY}
                        data-testid="rollout-stage-save"
                      >
                        {pending === `save-${stage.id}` ? 'Saving…' : `Save stage ${i + 1}`}
                      </button>
                      {stored.length > 1 && (
                        <button
                          type="button"
                          onClick={() => void removeStage(stage)}
                          disabled={busy}
                          className={DANGER}
                          data-testid="rollout-stage-remove"
                        >
                          Remove stage {i + 1} ({stage.name})
                        </button>
                      )}
                    </div>
                  </div>
                ) : (
                  <p key={stage.id} className="text-sm text-slate-700" data-testid="rollout-stage-fixed">
                    Stage {i + 1}: {stage.name}, {stage.target_percentage}%, {humanize(stage.status)}
                  </p>
                ),
              )}

            {editing && newStage && (
              <div className="rounded-md border border-slate-200 bg-white p-3 space-y-2" data-testid="rollout-stage-new">
                <StageFields
                  idPrefix={`${uid}-new`}
                  position={stored.length + 1}
                  draft={newStage}
                  invalid={invalidFor(`${uid}-new`)}
                  disabled={busy}
                  zone={zone}
                  onChange={setNewStage}
                />
                <div className="flex flex-wrap gap-2">
                  <button
                    type="button"
                    onClick={() => void addStage()}
                    disabled={busy}
                    className={PRIMARY}
                    data-testid="rollout-stage-add-save"
                  >
                    {pending === 'add' ? 'Adding…' : `Add stage ${stored.length + 1}`}
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      setNewStage(null);
                      setProblems([]);
                    }}
                    disabled={busy}
                    className={SECONDARY}
                    data-testid="rollout-stage-add-cancel"
                  >
                    Remove stage {stored.length + 1}
                    {newStage.name.trim() ? ` (${newStage.name.trim()})` : ''}
                  </button>
                </div>
              </div>
            )}

            <OrderNotes
              triggers={
                creating
                  ? createStages.map((d) => d.trigger)
                  : [
                      ...stored.map((s) => (isEditable(s) ? draftOf(s).trigger : s.trigger_type)),
                      ...(newStage ? [newStage.trigger] : []),
                    ]
              }
            />

            {(creating || !newStage) && (
              <button
                type="button"
                onClick={() =>
                  creating
                    ? setCreateStages((list) => [...list, { ...EMPTY_STAGE }])
                    : setNewStage({ ...EMPTY_STAGE })
                }
                disabled={busy}
                className={SECONDARY}
                data-testid="rollout-stage-add"
              >
                + Add stage
              </button>
            )}
          </fieldset>

          <div className="flex flex-wrap gap-2">
            {creating ? (
              <>
                <button type="submit" disabled={busy} className={PRIMARY} data-testid="rollout-schedule-submit">
                  {pending === 'create' ? 'Creating…' : 'Create schedule'}
                </button>
                <button
                  type="button"
                  onClick={() => closeForm('create')}
                  disabled={busy}
                  className={SECONDARY}
                  data-testid="rollout-schedule-form-cancel"
                >
                  Cancel
                </button>
              </>
            ) : (
              <button
                type="button"
                onClick={() => closeForm('edit')}
                disabled={busy}
                className={SECONDARY}
                data-testid="rollout-schedule-form-done"
              >
                {unsaved ? DISCARD_AND_CLOSE : 'Done'}
              </button>
            )}
          </div>
        </form>
      )}
    </div>
  );
}
