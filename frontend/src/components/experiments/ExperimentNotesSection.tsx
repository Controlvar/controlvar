import React, { useState } from 'react';
import { isApiError } from '@/services/api';
import { demoRefusalMessage } from '@/utils/demoMode';
import { ExperimentsService } from '@/services/experiments';
import { Experiment } from '@/types/experiments';

/**
 * Notes and learnings on an experiment (#2274): the `notes` and `learnings`
 * keys of its metadata, read from `experiment_metadata` and saved with
 * `POST /experiments/{id}/metadata`.
 *
 * - The route merges the keys it is sent into the stored metadata, so a save
 *   sends only the key whose text changed and every other key stays as it is.
 * - Any other key set through the API is shown read-only, as stored.
 * - A `notes` or `learnings` value that is not text (the API stores any JSON)
 *   is shown read-only too, and has no field: a save would overwrite it.
 * - Everyone who opens the page reads the notes; the fields are offered to
 *   `canChangeExperiment`, the check the route makes (`mayChange`).
 * - Every failure is fixed copy chosen by the answer's status, never the
 *   server's `detail`.
 */

export const NOTES_HEADING = 'Notes and learnings';
export const NOTES_LABEL = 'Notes';
export const LEARNINGS_LABEL = 'Learnings';
export const NOTES_EMPTY = 'No notes yet.';
export const NOTES_SAVE = 'Save notes';
export const NOTES_SAVING = 'Saving…';
export const NOTES_SAVED = 'Notes saved.';
export const NOTES_HELP =
  'Visible to everyone who can open this experiment. Saved with the experiment and kept through every status.';
export const OTHER_KEYS_HEADING = 'Other metadata';

/** The same sentence as ExperimentManageSection's UNREACHABLE (pinned equal in Jest). */
const UNREACHABLE = 'Could not reach the server. Check your connection and try again.';

export const NOTES_ERROR_COPY: Record<number, string> & { fallback: string } = {
  0: UNREACHABLE,
  403: 'Changing an experiment needs permission to change experiments, which the ADMIN and DEVELOPER roles have.',
  404: 'This experiment no longer exists.',
  422: 'The text was not accepted. It may contain a character that cannot be stored.',
  fallback: 'Could not save the notes. Try again.',
};

/** A time-out shows the dashboard's own time-out sentence, not the status-0 entry (#1040). */
export function notesErrorCopy(err: unknown): string {
  if (!isApiError(err)) return NOTES_ERROR_COPY.fallback;
  if (err.timedOut) return err.message;
  const demo = demoRefusalMessage(err);
  if (demo) return demo;
  return NOTES_ERROR_COPY[err.status] ?? NOTES_ERROR_COPY.fallback;
}

type TextKey = 'notes' | 'learnings';
const TEXT_KEYS: TextKey[] = ['notes', 'learnings'];

/** The text stored under a key: '' when absent, null when it is something other than text. */
function storedText(metadata: Record<string, unknown>, key: TextKey): string | null {
  const value = metadata[key];
  if (value === undefined || value === null) return '';
  return typeof value === 'string' ? value : null;
}

/** A stored value as the page shows it: text as it is, anything else as JSON. */
export function metadataValueText(value: unknown): string {
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2) ?? String(value);
  } catch {
    return String(value);
  }
}

interface ExperimentNotesSectionProps {
  experiment: Experiment;
  /** Whether the user may change the experiment (`canChangeExperiment`). */
  mayChange: boolean;
  /** Called with the API's answer after a save. */
  onSaved: (updated: Experiment) => void;
}

const ERROR_ID = 'notes-error';
const HELP_ID = 'notes-help';
const FIELD = 'mt-1 w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800';

export function ExperimentNotesSection({ experiment, mayChange, onSaved }: ExperimentNotesSectionProps) {
  const metadata = experiment.experiment_metadata ?? {};
  const stored: Record<TextKey, string | null> = {
    notes: storedText(metadata, 'notes'),
    learnings: storedText(metadata, 'learnings'),
  };
  const [draft, setDraft] = useState<Record<TextKey, string>>({
    notes: stored.notes ?? '',
    learnings: stored.learnings ?? '',
  });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<{ message: string; status: number | null } | null>(null);
  const [saved, setSaved] = useState(false);

  const editable = TEXT_KEYS.filter((key) => stored[key] !== null);
  const changed = editable.filter((key) => draft[key] !== stored[key]);
  const otherKeys = Object.keys(metadata).filter(
    (key) => !(TEXT_KEYS as string[]).includes(key) || stored[key as TextKey] === null,
  );
  const hasText = TEXT_KEYS.some((key) => (stored[key] ?? '') !== '');
  const empty = !hasText && otherKeys.length === 0;

  const edit = (key: TextKey, value: string) => {
    setDraft((previous) => ({ ...previous, [key]: value }));
    setSaved(false);
    setError(null);
  };

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (changed.length === 0) return;
    const body: Record<string, string> = {};
    for (const key of changed) body[key] = draft[key];
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      const updated = await ExperimentsService.updateMetadata(experiment.id, body);
      onSaved(updated);
      setSaved(true);
    } catch (err) {
      setError({ message: notesErrorCopy(err), status: isApiError(err) ? err.status : null });
    } finally {
      setSaving(false);
    }
  };

  const label = (key: TextKey) => (key === 'notes' ? NOTES_LABEL : LEARNINGS_LABEL);

  return (
    <section
      className="mt-6 bg-white rounded-lg border border-slate-200 p-5"
      aria-labelledby="notes-heading"
      data-testid="experiment-notes"
    >
      <h2 id="notes-heading" className="text-sm font-semibold text-slate-800">
        {NOTES_HEADING}
      </h2>

      {empty && (
        <p className="mt-2 text-sm text-slate-600" data-testid="experiment-notes-empty">
          {NOTES_EMPTY}
        </p>
      )}

      {!mayChange &&
        TEXT_KEYS.filter((key) => (stored[key] ?? '') !== '').map((key) => (
          <div key={key} className="mt-3">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">{label(key)}</h3>
            <p className="mt-1 text-sm text-slate-700 whitespace-pre-line break-words" data-testid={`experiment-notes-${key}`}>
              {stored[key]}
            </p>
          </div>
        ))}

      {mayChange && (
        <form
          onSubmit={(event) => void save(event)}
          noValidate
          className="mt-3 space-y-4"
          data-testid="experiment-notes-form"
        >
          {editable.map((key) => (
            <div key={key}>
              <label htmlFor={`notes-${key}`} className="block text-sm font-medium text-slate-700">
                {label(key)}
              </label>
              <textarea
                id={`notes-${key}`}
                rows={key === 'notes' ? 5 : 3}
                value={draft[key]}
                onChange={(event) => edit(key, event.target.value)}
                disabled={saving}
                aria-invalid={error?.status === 422 || undefined}
                aria-describedby={[error ? ERROR_ID : null, HELP_ID].filter(Boolean).join(' ')}
                className={FIELD}
                data-testid={`notes-${key}`}
              />
            </div>
          ))}
          <p id={HELP_ID} className="text-xs text-slate-600" data-testid={HELP_ID}>
            {NOTES_HELP}
          </p>
          {error && (
            <p
              id={ERROR_ID}
              role="alert"
              className="rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-700"
              data-testid={ERROR_ID}
            >
              {error.message}
            </p>
          )}
          {saved && (
            <p role="status" className="text-sm text-green-800" data-testid="notes-saved">
              {NOTES_SAVED}
            </p>
          )}
          <button
            type="submit"
            disabled={saving || changed.length === 0}
            className="px-3 py-1.5 rounded-md text-sm font-medium bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
            data-testid="notes-save"
          >
            {saving ? NOTES_SAVING : NOTES_SAVE}
          </button>
        </form>
      )}

      {otherKeys.length > 0 && (
        <div className="mt-5 pt-4 border-t border-slate-100" data-testid="experiment-notes-other">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">{OTHER_KEYS_HEADING}</h3>
          <dl className="mt-2 space-y-2 text-sm">
            {otherKeys.map((key) => (
              <div key={key}>
                <dt className="font-mono text-xs text-slate-600">{key}</dt>
                <dd
                  className="text-slate-700 whitespace-pre-wrap break-words"
                  data-testid={`experiment-notes-other-${key}`}
                >
                  {metadataValueText(metadata[key])}
                </dd>
              </div>
            ))}
          </dl>
        </div>
      )}
    </section>
  );
}
