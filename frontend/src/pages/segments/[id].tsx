import React, { useCallback, useEffect, useId, useRef, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/router';
import { PageTitle } from '@/components/PageTitle';
import { TargetingRuleBuilder } from '@/components/targeting';
import { IdUpload } from '@/components/segments/IdUpload';
import { KIND_LABELS, STATUS_COLORS, STATUS_LABELS } from '@/components/segments/segmentLabels';
import { SEGMENT_RULES_EMPTY, previewText, segmentRulesProblems } from '@/components/segments/segmentForm';
import { useOptionalAuth } from '@/contexts/AuthContext';
import { Segment, SegmentUsage, SegmentsService } from '@/services/segments';
import { TargetingRules } from '@/types/targeting';
import { isEditableTargeting, targetingPayload } from '@/utils/experimentTargeting';
import { rulesFromStored, sameJson } from '@/utils/flagTargeting';
import { formatCount } from '@/utils/segmentIds';
import {
  ROLE_NOTE,
  RULES_NOT_VALID,
  SegmentInUse,
  rulesProblems,
  segmentErrorCopy,
  segmentInUse,
  usageCount,
} from '@/utils/segmentErrors';
import { canArchiveSegments, canChangeSegments } from '@/utils/segmentPermissions';
import { useEffectivePermissions } from '@/contexts/EffectivePermissionsContext';
import { useDemoMode } from '@/contexts/AuthConfigContext';
import { DemoNote } from '@/components/DemoNote';
import { DEMO_SEGMENTS_ACTION } from '@/utils/demoMode';
import { segmentRulesAreValid } from '@/utils/segmentRules';
import { SEGMENT_RULES_OPERATOR_OPTIONS, createEmptyGroup } from '@/utils/targeting';

const button =
  'px-3 py-1.5 rounded-md text-sm font-medium border border-slate-300 bg-white text-slate-700 hover:bg-slate-50 disabled:opacity-60';
const primary = 'px-3 py-1.5 rounded-md text-sm font-medium bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-60';

function RulesSection({
  segment,
  canEdit,
  canEstimate,
  onSaved,
}: {
  segment: Segment;
  canEdit: boolean;
  /** Offer "Estimate size" (not in a read-only demo). */
  canEstimate: boolean;
  onSaved: (segment: Segment) => void;
}) {
  const stored = segment.rules;
  const valid = segmentRulesAreValid(stored);
  const editable = valid && isEditableTargeting(stored, SEGMENT_RULES_OPERATOR_OPTIONS);
  const [rules, setRules] = useState<TargetingRules | null>(() => (editable ? rulesFromStored(stored) : null));
  const [confirmingReplace, setConfirmingReplace] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [problems, setProblems] = useState<string[]>([]);
  const [saved, setSaved] = useState(false);
  const [preview, setPreview] = useState<string | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [previewing, setPreviewing] = useState(false);

  const baseline = editable ? targetingPayload(rulesFromStored(stored)) : null;
  const changed = rules !== null && (baseline === null || !sameJson(targetingPayload(rules), baseline));

  const save = async () => {
    if (!rules) return;
    setSaved(false);
    setSaveError(null);
    const found = segmentRulesProblems(rules);
    setProblems(found);
    if (found.length > 0) return;
    setSaving(true);
    try {
      const updated = await SegmentsService.updateRules(segment.id, targetingPayload(rules));
      setSaved(true);
      onSaved(updated);
    } catch (err) {
      setSaveError(segmentErrorCopy(err, 'save-rules'));
      setProblems(rulesProblems(err));
    } finally {
      setSaving(false);
    }
  };

  const estimate = async () => {
    const source = rules ? targetingPayload(rules) : null;
    if (!source || !rules || segmentRulesProblems(rules).length > 0) {
      setPreview(null);
      setPreviewError('Finish the conditions before estimating the size.');
      return;
    }
    setPreviewing(true);
    setPreviewError(null);
    try {
      setPreview(previewText(await SegmentsService.preview(segment.id, segment.name, source)));
    } catch (err) {
      setPreview(null);
      setPreviewError(segmentErrorCopy(err, 'preview'));
    } finally {
      setPreviewing(false);
    }
  };

  return (
    <section className="bg-white rounded-lg border border-slate-200 p-6 space-y-4" data-testid="segment-rules-section">
      <h2 className="text-lg font-semibold text-slate-900">Rules</h2>

      {!valid && (
        <p className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-800" data-testid="segment-rules-invalid">
          {RULES_NOT_VALID}
        </p>
      )}
      {valid && !editable && rules === null && (
        <p className="text-sm text-slate-700" data-testid="segment-rules-not-editable">
          These rules use a form the editor can&apos;t show, so they are shown as stored.
        </p>
      )}

      {rules === null && (
        <>
          <pre className="bg-slate-50 border border-slate-200 text-xs text-slate-800 rounded-md p-3 overflow-x-auto" data-testid="segment-rules-json">
            {JSON.stringify(stored, null, 2)}
          </pre>
          {canEdit && !confirmingReplace && (
            <button type="button" className={button} onClick={() => setConfirmingReplace(true)} data-testid="segment-rules-replace">
              Replace rules
            </button>
          )}
          {canEdit && confirmingReplace && (
            <div
              role="group"
              aria-label="Replace rules"
              className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900"
              data-testid="segment-rules-replace-confirm"
            >
              <p>The stored rules are discarded when you save new ones. Until you save, nothing changes.</p>
              <div className="mt-3 flex gap-2">
                <button type="button" className={button} onClick={() => setConfirmingReplace(false)} data-testid="segment-rules-replace-cancel">
                  Cancel
                </button>
                <button
                  type="button"
                  className={primary}
                  onClick={() => {
                    setRules({ logical_operator: 'AND', groups: [createEmptyGroup()] });
                    setConfirmingReplace(false);
                  }}
                  data-testid="segment-rules-replace-yes"
                >
                  Start new rules
                </button>
              </div>
            </div>
          )}
        </>
      )}

      {rules !== null && (
        <TargetingRuleBuilder
          value={rules}
          onChange={(next) => {
            setRules(next);
            setSaved(false);
          }}
          readOnly={!canEdit}
          operatorOptions={SEGMENT_RULES_OPERATOR_OPTIONS}
          emptyText={SEGMENT_RULES_EMPTY}
        />
      )}

      {(saveError || problems.length > 0) && (
        <div role="alert" className="rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-800" data-testid="segment-rules-error">
          <p>{saveError ?? 'Fix these before saving:'}</p>
          {problems.length > 0 && (
            <ul className="mt-1 list-disc pl-5">
              {problems.map((p, i) => (
                <li key={i}>{p}</li>
              ))}
            </ul>
          )}
        </div>
      )}
      {saved && (
        <p role="status" className="text-sm text-green-800" data-testid="segment-rules-saved">
          Rules saved.
        </p>
      )}

      <div className="flex flex-wrap gap-2">
        {canEdit && rules !== null && (
          <button type="button" className={primary} onClick={() => void save()} disabled={saving || !changed} data-testid="segment-rules-save">
            {saving ? 'Saving...' : 'Save rules'}
          </button>
        )}
        {rules !== null && canEstimate && (
          <button type="button" className={button} onClick={() => void estimate()} disabled={previewing} data-testid="segment-preview">
            {previewing ? 'Estimating...' : 'Estimate size'}
          </button>
        )}
      </div>
      {preview && (
        <p role="status" className="text-sm text-slate-700" data-testid="segment-preview-result">
          {preview}
        </p>
      )}
      {previewError && (
        <p role="alert" className="text-sm text-red-800" data-testid="segment-preview-error">
          {previewError}
        </p>
      )}
    </section>
  );
}

function CheckUser({ segment }: { segment: Segment }) {
  const inputId = useId();
  const [userId, setUserId] = useState('');
  const [answer, setAnswer] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const check = async (event: React.FormEvent) => {
    event.preventDefault();
    setAnswer(null);
    setError(null);
    const value = userId;
    if (value.trim() === '' || value.length > 255) {
      setError('Enter a user ID of 1 to 255 characters.');
      return;
    }
    setBusy(true);
    try {
      const result = await SegmentsService.checkUser(segment.id, value);
      setAnswer(result.is_member ? `${value} is a member.` : `${value} is not a member.`);
    } catch (err) {
      setError(segmentErrorCopy(err, 'check'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={(e) => void check(e)} className="space-y-2" data-testid="segment-check-user">
      <h3 className="text-sm font-semibold text-slate-800">Check a user</h3>
      <div className="flex gap-2 items-end flex-wrap">
        <div>
          <label htmlFor={inputId} className="block text-xs font-medium text-slate-700">
            User ID
          </label>
          <input
            id={inputId}
            type="text"
            value={userId}
            onChange={(e) => setUserId(e.target.value)}
            className="mt-1 rounded border border-slate-300 px-2 py-1 text-sm"
            data-testid="segment-check-input"
          />
        </div>
        <button type="submit" className={button} disabled={busy} data-testid="segment-check-submit">
          Check
        </button>
      </div>
      {answer && (
        <p role="status" className="text-sm text-slate-800" data-testid="segment-check-answer">
          {answer}
        </p>
      )}
      {error && (
        <p role="alert" className="text-sm text-red-800" data-testid="segment-check-error">
          {error}
        </p>
      )}
    </form>
  );
}

function MembersSection({
  segment,
  canEdit,
  canCheck,
  onMemberCount,
}: {
  segment: Segment;
  canEdit: boolean;
  /** Offer "Check a user" (not in a read-only demo). */
  canCheck: boolean;
  onMemberCount: (count: number) => void;
}) {
  const count = segment.member_count ?? 0;
  const [mode, setMode] = useState<'add' | 'remove'>('add');
  const [running, setRunning] = useState(false);
  return (
    <section className="bg-white rounded-lg border border-slate-200 p-6 space-y-4" data-testid="segment-members-section">
      <h2 className="text-lg font-semibold text-slate-900">IDs</h2>
      <p className="text-sm text-slate-800">
        <span data-testid="segment-member-count">{formatCount(count)}</span> member{count === 1 ? '' : 's'}. IDs are matched
        exactly on <code>user_id</code>.
      </p>
      {canEdit && (
        <div className="space-y-3">
          <div className="flex gap-2" role="group" aria-label="Add or remove IDs">
            <button
              type="button"
              aria-pressed={mode === 'add'}
              className={button}
              disabled={running}
              onClick={() => setMode('add')}
              data-testid="segment-mode-add"
            >
              Add IDs
            </button>
            <button
              type="button"
              aria-pressed={mode === 'remove'}
              className={button}
              disabled={running}
              onClick={() => setMode('remove')}
              data-testid="segment-mode-remove"
            >
              Remove IDs
            </button>
          </div>
          <IdUpload
            key={mode}
            segmentId={segment.id}
            segmentName={segment.name}
            memberCount={count}
            mode={mode}
            onMemberCount={onMemberCount}
            onRunningChange={setRunning}
          />
        </div>
      )}
      {canCheck && <CheckUser segment={segment} />}
    </section>
  );
}

function UsageSection({ segmentId }: { segmentId: string }) {
  const [usage, setUsage] = useState<SegmentUsage | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    SegmentsService.usage(segmentId)
      .then((u) => live && setUsage(u))
      .catch((err) => live && setError(segmentErrorCopy(err, 'usage')));
    return () => {
      live = false;
    };
  }, [segmentId]);

  const flags = usage?.feature_flags ?? [];
  const experiments = usage?.experiments ?? [];
  return (
    <section className="bg-white rounded-lg border border-slate-200 p-6 space-y-2" data-testid="segment-usage">
      <h2 className="text-lg font-semibold text-slate-900">Used by</h2>
      {!usage && !error && <p className="text-sm text-slate-600">Loading...</p>}
      {error && (
        <p role="alert" className="text-sm text-red-800" data-testid="segment-usage-error">
          {error}
        </p>
      )}
      {usage && flags.length === 0 && experiments.length === 0 && (
        <p className="text-sm text-slate-700" data-testid="segment-usage-none">
          No flag or experiment uses this segment.
        </p>
      )}
      {usage && (flags.length > 0 || experiments.length > 0) && (
        <ul className="text-sm list-disc pl-5 space-y-1" data-testid="segment-usage-list">
          {flags.map((f) => (
            <li key={`f-${f.id}`}>
              Flag{' '}
              <Link href={`/feature-flags/${f.id}`} className="text-blue-700 underline">
                {f.name}
              </Link>
            </li>
          ))}
          {experiments.map((e) => (
            <li key={`e-${e.id}`}>
              Experiment{' '}
              <Link href={`/experiments/${e.id}`} className="text-blue-700 underline">
                {e.name}
              </Link>
              {e.status ? ` (${e.status})` : ''}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function InUseList({ name, inUse }: { name: string; inUse: SegmentInUse }) {
  return (
    <div data-testid="segment-in-use">
      <p>
        {name} is used by {usageCount(inUse.flags.length, inUse.experiments.length)}.{' '}
        {inUse.experiments.length > 0
          ? 'Remove the segment from the targeting rules that name it and from the segments an experiment balances ' +
            'within, then archive it. A started experiment that balances within it lets it go once it is completed ' +
            'or archived.'
          : 'Remove the segment from their targeting rules, then archive it.'}
      </p>
      <ul className="mt-1 list-disc pl-5">
        {inUse.flags.map((f) => (
          <li key={`f-${f.id}`}>
            Flag{' '}
            <Link href={`/feature-flags/${f.id}`} className="underline">
              {f.key ?? f.name}
            </Link>
          </li>
        ))}
        {inUse.experiments.map((e) => (
          <li key={`e-${e.id}`}>
            Experiment{' '}
            <Link href={`/experiments/${e.id}`} className="underline">
              {e.key ?? e.name}
            </Link>
            {e.status ? ` (${e.status})` : ''}
          </li>
        ))}
      </ul>
    </div>
  );
}

function ArchiveSection({ segment, onArchived }: { segment: Segment; onArchived: () => void }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [inUse, setInUse] = useState<SegmentInUse | null>(null);
  const errorRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (error || inUse) errorRef.current?.focus();
  }, [error, inUse]);

  const archive = async () => {
    setBusy(true);
    setError(null);
    setInUse(null);
    try {
      await SegmentsService.archive(segment.id);
      setConfirming(false);
      onArchived();
    } catch (err) {
      setConfirming(false);
      const lists = segmentInUse(err);
      if (lists) setInUse(lists);
      else setError(segmentErrorCopy(err, 'archive'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="bg-white rounded-lg border border-slate-200 p-6 space-y-3" data-testid="segment-archive-section">
      <h2 className="text-lg font-semibold text-slate-900">Archive</h2>
      {!confirming && (
        <button type="button" className={button} onClick={() => setConfirming(true)} data-testid="segment-archive">
          Archive segment
        </button>
      )}
      {confirming && (
        <div
          role="group"
          aria-labelledby="segment-archive-question"
          className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900"
          data-testid="segment-archive-confirm"
        >
          <p id="segment-archive-question">
            Archive {segment.name}? Archived segments can&apos;t be used in targeting rules. To restore it, set its status to
            active through the API.
          </p>
          <div className="mt-3 flex gap-2">
            <button type="button" className={button} onClick={() => setConfirming(false)} data-testid="segment-archive-cancel">
              Cancel
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => void archive()}
              className="px-3 py-1.5 rounded-md text-sm font-medium bg-red-700 text-white hover:bg-red-800 disabled:opacity-60"
              data-testid="segment-archive-yes"
            >
              Archive
            </button>
          </div>
        </div>
      )}
      {(error || inUse) && (
        <div
          ref={errorRef}
          tabIndex={-1}
          role="alert"
          className="rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-800"
          data-testid="segment-archive-error"
        >
          {inUse ? <InUseList name={segment.name} inUse={inUse} /> : <p>{error}</p>}
        </div>
      )}
    </section>
  );
}

export default function SegmentDetailPage() {
  const router = useRouter();
  const rawId = router.query.id;
  const id = typeof rawId === 'string' ? rawId : undefined;
  const auth = useOptionalAuth();
  const { effective, loading: permissionsLoading } = useEffectivePermissions();
  // Rules and members ask EXPERIMENT UPDATE, Archive EXPERIMENT DELETE, each
  // from the role or the effective map (#891). The note waits for the map.
  const canChange = canChangeSegments(auth?.user, effective);
  const canArchive = canArchiveSegments(auth?.user, effective);
  const changePending = !canChange && permissionsLoading;
  // A read-only demo: the demo note for the role note, and neither "Check a
  // user" nor "Estimate size" (reads the API takes as POSTs, which it refuses).
  const demo = useDemoMode();
  const [segment, setSegment] = useState<Segment | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [version, setVersion] = useState(0);

  const load = useCallback(async () => {
    if (!id) return;
    setLoading(true);
    setError(null);
    try {
      setSegment(await SegmentsService.get(id));
      setVersion((v) => v + 1);
    } catch (err) {
      setError(segmentErrorCopy(err, 'load-segment'));
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  const archived = segment?.status === 'archived';
  const canEdit = canChange && !archived;
  const mayArchive = canArchive && !archived;

  return (
    <div className="flex-1 bg-slate-50">
      <PageTitle title={segment ? segment.name : 'Segment'} />
      <div className="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 py-8 space-y-6">
        <Link href="/segments" className="text-sm text-blue-700 hover:underline">
          ← Segments
        </Link>

        {loading && !segment && (
          <p className="text-sm text-slate-600" data-testid="segment-loading">
            Loading segment...
          </p>
        )}

        {!loading && error && (
          <div role="alert" className="rounded-lg bg-red-50 border border-red-200 p-4 text-sm text-red-800 flex items-center justify-between gap-4" data-testid="segment-error">
            <span>{error}</span>
            <button type="button" className={button} onClick={() => void load()} data-testid="segment-retry">
              Retry
            </button>
          </div>
        )}

        {segment && (
          <>
            <header data-testid="segment-detail">
              <h1 className="text-2xl font-bold text-slate-900" data-testid="segment-name-heading">
                {segment.name}
              </h1>
              <div className="mt-2 flex gap-2 items-center text-sm text-slate-700">
                <span data-testid="segment-detail-kind">{KIND_LABELS[segment.kind] ?? segment.kind}</span>
                <span
                  className={`inline-flex px-2 py-0.5 rounded-full text-xs font-medium ${STATUS_COLORS[segment.status] ?? ''}`}
                  data-testid="segment-detail-status"
                >
                  {STATUS_LABELS[segment.status] ?? segment.status}
                </span>
              </div>
              {segment.description && <p className="mt-2 text-sm text-slate-700">{segment.description}</p>}
              {demo ? (
                <DemoNote action={DEMO_SEGMENTS_ACTION} className="mt-2 text-sm text-slate-600" />
              ) : (
                !canChange &&
                !changePending && (
                  <p className="mt-2 text-sm text-slate-600" data-testid="segment-role-note">
                    {ROLE_NOTE}
                  </p>
                )
              )}
              {archived && (
                <p className="mt-2 text-sm text-amber-900" data-testid="segment-archived-note">
                  This segment is archived. It can&apos;t be used in targeting rules, and it can&apos;t be changed here.
                </p>
              )}
              {segment.status === 'inactive' && (
                <p className="mt-2 text-sm text-slate-700" data-testid="segment-inactive-note">
                  This segment is inactive. It can&apos;t be used in targeting rules until its status is set to active through
                  the API.
                </p>
              )}
            </header>

            {segment.kind === 'rules' ? (
              <RulesSection
                key={`rules-${version}`}
                segment={segment}
                canEdit={canEdit}
                canEstimate={!demo}
                onSaved={(updated) => setSegment({ ...segment, ...updated })}
              />
            ) : (
              <MembersSection
                segment={segment}
                canEdit={canEdit}
                canCheck={!demo}
                onMemberCount={(count) => setSegment((s) => (s ? { ...s, member_count: count } : s))}
              />
            )}

            <UsageSection segmentId={segment.id} />

            {mayArchive && <ArchiveSection segment={segment} onArchived={() => void load()} />}
          </>
        )}
      </div>
    </div>
  );
}
