import React, { useEffect, useRef, useState } from 'react';
import { TargetingRuleBuilder } from '@/components/targeting';
import { useSegmentList } from '@/hooks/useSegmentList';
import { docsUrl } from '@/services/docs';
import { ExperimentsService } from '@/services/experiments';
import { Experiment } from '@/types/experiments';
import { TargetingRules, isSegmentOperator } from '@/types/targeting';
import { checkTargeting } from '@/components/experiments/new/formState';
import { createEmptyRules, jsonToRules } from '@/utils/targeting';
import {
  TARGETING_SAVE_INCOMPLETE,
  SEGMENT_NOT_FOUND,
  TargetingSaveError,
  combineWord,
  describeCondition,
  describeTargetingSaveError,
  isEditableTargeting,
  isNoRules,
  targetingPayload,
} from '@/utils/experimentTargeting';

/** The states in which the API accepts a change to who can join. */
const EDITABLE_STATES = new Set(['draft', 'paused']);

/** Shown before a save while the experiment is paused (PE C12, EM condition 8). */
export const PAUSED_SAVE_NOTICE =
  'People already in the experiment keep their variant. People not yet in it, including anyone turned away before, ' +
  'are checked against the new rules after you resume. Apps using an SDK may keep an earlier answer for up to ' +
  '5 minutes. Results will combine people admitted under the old and the new rules.';

export const OUTSIDE_BUILDER_NOTE =
  'These rules were not created in the dashboard and may not be applied as written.';

export const ACTIVE_REASON = 'Pause the experiment to change who can join.';

const STATE_REASONS: Record<string, string> = {
  completed: 'Who can join cannot be changed after an experiment is completed.',
  archived: 'Who can join cannot be changed after an experiment is archived.',
};

export function roleReason(role: string | null | undefined): string {
  return `Changing who can join needs permission to change experiments, which the ADMIN and DEVELOPER roles have; you are ${role || 'signed in without a role'}.`;
}

interface TargetingSectionProps {
  experiment: Experiment;
  /**
   * `canChangeExperiment(user, effective)`: whether the user may change the
   * experiment at all (the built-in role, or a custom role or direct grant).
   */
  mayChange: boolean;
  /** True while the effective map is on its way: no reason is shown yet. */
  mayChangePending?: boolean;
  role: string | null | undefined;
  /** The page's own pause action, offered beside the ACTIVE reason. */
  onPause: () => void;
  pauseBusy: boolean;
  onSaved: (updated: Experiment) => void;
}

/** A failure, announced and focused so a keyboard or screen-reader user lands on it. */
export function SaveAlert({ error }: { error: TargetingSaveError }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    ref.current?.focus();
  }, [error]);
  return (
    <div
      ref={ref}
      role="alert"
      tabIndex={-1}
      className="mt-3 rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-700 focus:outline-none focus:ring-2 focus:ring-blue-600"
      data-testid="targeting-save-error"
    >
      <p>{error.message}</p>
      {error.problems && (
        <ul className="mt-1 list-disc pl-5">
          {error.problems.map((problem, i) => (
            <li key={i}>{problem}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * The stored rules in words, for every reader. A segment condition shows the
 * segment's name once the list has loaded: the id while it loads, and the id
 * (no banner) when the list cannot be read, as for a role without segment
 * access. An id the list does not contain is shown with "(segment not found)".
 */
export function RulesSummary({ value }: { value: Record<string, unknown> }) {
  const groups = (value.groups as Record<string, unknown>[]) ?? [];
  const usesSegment = groups.some((group) =>
    ((group.conditions as Record<string, unknown>[]) ?? []).some((condition) => isSegmentOperator(condition.operator)),
  );
  const segmentList = useSegmentList(usesSegment);
  const segmentName = (id: string): string => {
    if (segmentList.state !== 'ready') return id;
    const found = segmentList.segments.find((segment) => segment.id === id);
    return found ? found.name : `${id} ${SEGMENT_NOT_FOUND}`;
  };
  const conditionList = (group: Record<string, unknown>) => (
    <ul className="mt-1 list-disc pl-5 space-y-0.5">
      {((group.conditions as Record<string, unknown>[]) ?? []).map((condition, ci) => (
        <li key={ci}>
          <code className="font-mono text-slate-800">{describeCondition(condition, segmentName)}</code>
        </li>
      ))}
    </ul>
  );

  if (groups.length === 1) {
    return (
      <div className="text-sm text-slate-700" data-testid="targeting-summary">
        <p>People who match {combineWord(groups[0].logical_operator)} of these conditions:</p>
        {conditionList(groups[0])}
      </div>
    );
  }
  return (
    <div className="text-sm text-slate-700" data-testid="targeting-summary">
      <p>People who match {combineWord(value.logical_operator)} of these groups:</p>
      <ol className="mt-1 space-y-2">
        {groups.map((group, gi) => (
          <li key={gi}>
            <p className="font-medium">
              Group {gi + 1}: {combineWord(group.logical_operator)} of these conditions
            </p>
            {conditionList(group)}
          </li>
        ))}
      </ol>
    </div>
  );
}

/**
 * "Who can join": the experiment's targeting rules, shown to every reader,
 * and changed in place by a user who may change the experiment while it is
 * draft or paused. The save sends `targeting_rules` and nothing else, which is
 * what the API accepts on a paused experiment.
 */
export function TargetingSection({
  experiment,
  mayChange,
  mayChangePending = false,
  role,
  onPause,
  pauseBusy,
  onSaved,
}: TargetingSectionProps) {
  const stored = experiment.targeting_rules;
  const empty = isNoRules(stored);
  const editable = isEditableTargeting(stored);
  const stateAllows = EDITABLE_STATES.has(experiment.status);
  const paused = experiment.status === 'paused';

  const [rules, setRules] = useState<TargetingRules | null>(null);
  const [confirmingReplace, setConfirmingReplace] = useState(false);
  const [confirmingSave, setConfirmingSave] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<TargetingSaveError | null>(null);
  const [saved, setSaved] = useState<string | null>(null);

  const editing = rules !== null;

  const startEdit = (initial: TargetingRules) => {
    setSaved(null);
    setError(null);
    setConfirmingReplace(false);
    setConfirmingSave(false);
    setRules(initial);
  };

  const cancelEdit = () => {
    setRules(null);
    setError(null);
    setConfirmingSave(false);
  };

  const send = async () => {
    if (!rules) return;
    setConfirmingSave(false);
    setSaving(true);
    setError(null);
    try {
      const updated = await ExperimentsService.update(experiment.id, {
        targeting_rules: targetingPayload(rules, stored),
      });
      setRules(null);
      setSaved(
        paused
          ? 'Saved. The new rules apply when you resume the experiment.'
          : 'Saved. The new rules apply when the experiment starts.',
      );
      onSaved(updated);
    } catch (err) {
      setError(describeTargetingSaveError(err));
    } finally {
      setSaving(false);
    }
  };

  const onSave = () => {
    if (!rules) return;
    const problems = checkTargeting(rules);
    if (problems.length > 0) {
      setError({ message: TARGETING_SAVE_INCOMPLETE, problems });
      return;
    }
    setError(null);
    if (paused) {
      setConfirmingSave(true);
      return;
    }
    void send();
  };

  let reason: React.ReactNode = null;
  if (!mayChange && mayChangePending) {
    // The effective map is on its way: neither the reason nor the controls.
    reason = null;
  } else if (!mayChange) {
    reason = (
      <p className="text-sm text-slate-600" data-testid="targeting-role-reason">
        {roleReason(role)}
      </p>
    );
  } else if (!stateAllows) {
    reason =
      experiment.status === 'active' ? (
        <div className="flex flex-wrap items-center gap-3" data-testid="targeting-state-reason">
          <p className="text-sm text-slate-600">{ACTIVE_REASON}</p>
          <button
            type="button"
            onClick={onPause}
            disabled={pauseBusy}
            className="px-3 py-1.5 rounded-md text-sm font-medium bg-white text-slate-700 border border-slate-300 hover:bg-slate-50 disabled:opacity-50"
            data-testid="targeting-pause"
          >
            {pauseBusy ? 'Pausing…' : 'Pause experiment'}
          </button>
        </div>
      ) : (
        <p className="text-sm text-slate-600" data-testid="targeting-state-reason">
          {STATE_REASONS[experiment.status] ?? 'Who can join cannot be changed in this state.'}
        </p>
      );
  }

  const canAct = mayChange && stateAllows;

  return (
    <section
      className="mt-6 bg-white rounded-lg border border-slate-200 p-5"
      aria-labelledby="targeting-heading"
      data-testid="targeting-section"
    >
      <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
        <h2 id="targeting-heading" className="text-base font-semibold text-slate-800">
          Who can join
        </h2>
        {canAct && editable && !editing && (
          <button
            type="button"
            onClick={() => startEdit(jsonToRules(empty ? null : (stored as object)))}
            className="px-3 py-1.5 rounded-md text-sm font-medium bg-blue-600 text-white hover:bg-blue-700"
            data-testid="targeting-edit"
          >
            Edit
          </button>
        )}
        {canAct && !editable && !editing && !confirmingReplace && (
          <button
            type="button"
            onClick={() => {
              setSaved(null);
              setConfirmingReplace(true);
            }}
            className="px-3 py-1.5 rounded-md text-sm font-medium bg-white text-slate-700 border border-slate-300 hover:bg-slate-50"
            data-testid="targeting-replace"
          >
            Replace rules
          </button>
        )}
      </div>

      {!editing && (
        <div data-testid="targeting-read">
          {empty ? (
            <p className="text-sm text-slate-700" data-testid="targeting-everyone">
              Everyone is eligible.
            </p>
          ) : editable ? (
            <RulesSummary value={stored as Record<string, unknown>} />
          ) : (
            <div data-testid="targeting-raw">
              <p className="text-sm text-amber-900 mb-2" data-testid="targeting-raw-note">
                {OUTSIDE_BUILDER_NOTE}{' '}
                <a
                  href={docsUrl('api/endpoints', 'targeting-rules')}
                  className="text-blue-600 hover:underline"
                  target="_blank"
                  rel="noreferrer"
                >
                  Read about targeting rules
                </a>
                .
              </p>
              <pre className="bg-slate-50 border border-slate-200 text-xs text-slate-800 rounded-md p-3 overflow-x-auto">
                {JSON.stringify(stored, null, 2)}
              </pre>
            </div>
          )}
        </div>
      )}

      {reason && <div className="mt-3">{reason}</div>}

      {confirmingReplace && (
        <div
          role="dialog"
          aria-label="Replace rules"
          className="mt-3 rounded-lg border border-amber-200 bg-amber-50 p-3"
          data-testid="targeting-replace-confirm"
        >
          <p className="text-sm text-amber-900">
            These rules will be discarded when you save new ones. Until you save, nothing changes.
          </p>
          <pre className="mt-2 bg-white border border-amber-200 text-xs text-slate-800 rounded-md p-3 overflow-x-auto">
            {JSON.stringify(stored, null, 2)}
          </pre>
          <div className="mt-3 flex gap-2">
            <button
              type="button"
              onClick={() => setConfirmingReplace(false)}
              className="px-3 py-1.5 rounded-md text-sm font-medium text-slate-600 border border-slate-300 bg-white hover:bg-slate-50"
              data-testid="targeting-replace-cancel"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={() => startEdit(createEmptyRules())}
              className="px-3 py-1.5 rounded-md text-sm font-medium bg-amber-600 text-white hover:bg-amber-700"
              data-testid="targeting-replace-yes"
            >
              Start with no rules
            </button>
          </div>
        </div>
      )}

      {editing && rules && (
        <div data-testid="targeting-editor">
          <TargetingRuleBuilder value={rules} onChange={setRules} />
          {confirmingSave ? (
            <div
              role="dialog"
              aria-label="Save rules while paused"
              className="mt-3 rounded-lg border border-amber-200 bg-amber-50 p-3"
              data-testid="targeting-paused-confirm"
            >
              <p className="text-sm text-amber-900">{PAUSED_SAVE_NOTICE}</p>
              <div className="mt-3 flex gap-2">
                <button
                  type="button"
                  onClick={() => setConfirmingSave(false)}
                  className="px-3 py-1.5 rounded-md text-sm font-medium text-slate-600 border border-slate-300 bg-white hover:bg-slate-50"
                  data-testid="targeting-paused-cancel"
                >
                  Keep editing
                </button>
                <button
                  type="button"
                  onClick={() => void send()}
                  className="px-3 py-1.5 rounded-md text-sm font-medium bg-amber-600 text-white hover:bg-amber-700"
                  data-testid="targeting-paused-yes"
                >
                  Save rules
                </button>
              </div>
            </div>
          ) : (
            <div className="mt-4 flex gap-2">
              <button
                type="button"
                onClick={cancelEdit}
                disabled={saving}
                className="px-3 py-1.5 rounded-md text-sm font-medium text-slate-600 border border-slate-300 bg-white hover:bg-slate-50 disabled:opacity-50"
                data-testid="targeting-cancel"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={onSave}
                disabled={saving}
                className="px-3 py-1.5 rounded-md text-sm font-medium bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
                data-testid="targeting-save"
              >
                {saving ? 'Saving…' : 'Save rules'}
              </button>
            </div>
          )}
        </div>
      )}

      {error && <SaveAlert error={error} />}

      {/* Always present, so the announcement is not lost to a live region that appears with its text. */}
      <p role="status" className={saved ? 'mt-3 text-sm text-green-700' : 'sr-only'} data-testid="targeting-saved">
        {saved}
      </p>
    </section>
  );
}
