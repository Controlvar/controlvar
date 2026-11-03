import React, { useId, useRef, useState, useEffect } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/router';
import { PageTitle } from '@/components/PageTitle';
import { TargetingRuleBuilder } from '@/components/targeting';
import { SEGMENT_RULES_EMPTY, segmentFormProblems } from '@/components/segments/segmentForm';
import { useOptionalAuth } from '@/contexts/AuthContext';
import { SegmentKind, SegmentsService } from '@/services/segments';
import { TargetingRules } from '@/types/targeting';
import { targetingPayload } from '@/utils/experimentTargeting';
import { ROLE_NOTE, rulesProblems, segmentErrorCopy } from '@/utils/segmentErrors';
import { canCreateSegments } from '@/utils/segmentPermissions';
import { useEffectivePermissions } from '@/contexts/EffectivePermissionsContext';
import { SEGMENT_RULES_OPERATOR_OPTIONS, createEmptyGroup } from '@/utils/targeting';

function initialRules(): TargetingRules {
  return { logical_operator: 'AND', groups: [createEmptyGroup()] };
}

export default function NewSegmentPage() {
  const router = useRouter();
  const auth = useOptionalAuth();
  const { effective, loading: permissionsLoading } = useEffectivePermissions();
  const nameId = useId();
  const descriptionId = useId();
  const [kind, setKind] = useState<SegmentKind>('rules');
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [rules, setRules] = useState<TargetingRules>(initialRules);
  const [problems, setProblems] = useState<string[]>([]);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const errorRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (saveError || problems.length > 0) errorRef.current?.focus();
  }, [saveError, problems]);

  const canCreate = canCreateSegments(auth?.user, effective);

  // Also while the effective map may still add EXPERIMENT CREATE (#891):
  // neither the notice nor the form until it answers.
  if ((auth && auth.status === 'loading') || (!canCreate && permissionsLoading)) {
    return (
      <div className="flex-1 bg-slate-50 p-8" data-testid="segment-new-loading">
        <PageTitle title="New segment" />
        <p className="text-sm text-slate-600">Loading...</p>
      </div>
    );
  }

  if (!canCreate) {
    return (
      <div className="flex-1 bg-slate-50">
        <PageTitle title="New segment" />
        <div className="max-w-3xl mx-auto px-4 py-8">
          <h1 className="text-2xl font-bold text-slate-900">New segment</h1>
          <p className="mt-4 text-sm text-slate-700" data-testid="segment-new-role-note">
            Your role can view segments but not create them. {ROLE_NOTE}
          </p>
          <Link href="/segments" className="mt-4 inline-block text-sm text-blue-700 underline">
            Back to segments
          </Link>
        </div>
      </div>
    );
  }

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setSaveError(null);
    const found = segmentFormProblems(name, kind, rules);
    setProblems(found);
    if (found.length > 0) return;
    setSaving(true);
    try {
      const created = await SegmentsService.create({
        name: name.trim(),
        description: description.trim() || undefined,
        kind,
        rules: kind === 'rules' ? targetingPayload(rules) : undefined,
      });
      await router.push(`/segments/${created.id}`);
    } catch (err) {
      setSaveError(segmentErrorCopy(err, 'create'));
      setProblems(rulesProblems(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex-1 bg-slate-50">
      <PageTitle title="New segment" />
      <div className="max-w-3xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <Link href="/segments" className="text-sm text-blue-700 hover:underline">
          ← Segments
        </Link>
        <h1 className="mt-2 text-2xl font-bold text-slate-900">New segment</h1>

        <form onSubmit={(e) => void submit(e)} className="mt-6 space-y-6" noValidate data-testid="segment-new-form">
          <fieldset className="bg-white rounded-lg border border-slate-200 p-4">
            <legend className="px-1 text-sm font-semibold text-slate-800">What defines this segment?</legend>
            <div className="space-y-3 mt-2">
              <label className="flex gap-2 text-sm text-slate-800">
                <input
                  type="radio"
                  name="segment-kind"
                  value="rules"
                  checked={kind === 'rules'}
                  onChange={() => setKind('rules')}
                  data-testid="segment-kind-rules"
                />
                <span>
                  <strong>Rules</strong>: users whose attributes match conditions you set (for example, plan is enterprise).
                </span>
              </label>
              <label className="flex gap-2 text-sm text-slate-800">
                <input
                  type="radio"
                  name="segment-kind"
                  value="id_list"
                  checked={kind === 'id_list'}
                  onChange={() => setKind('id_list')}
                  data-testid="segment-kind-id-list"
                />
                <span>
                  <strong>ID list</strong>: the users whose IDs you upload (for example, a customer export). IDs are
                  matched on <code>user_id</code>.
                </span>
              </label>
            </div>
            <p className="mt-3 text-xs text-slate-600">You can&apos;t change a segment&apos;s type after it is created.</p>
          </fieldset>

          <div className="bg-white rounded-lg border border-slate-200 p-4 space-y-4">
            <div>
              <label htmlFor={nameId} className="block text-sm font-medium text-slate-800">
                Name
              </label>
              <input
                id={nameId}
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                maxLength={128}
                className="mt-1 w-full rounded border border-slate-300 px-3 py-2 text-sm"
                data-testid="segment-name"
              />
            </div>
            <div>
              <label htmlFor={descriptionId} className="block text-sm font-medium text-slate-800">
                Description (optional)
              </label>
              <textarea
                id={descriptionId}
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                maxLength={512}
                rows={2}
                className="mt-1 w-full rounded border border-slate-300 px-3 py-2 text-sm"
                data-testid="segment-description"
              />
            </div>
          </div>

          {kind === 'rules' ? (
            <div className="bg-white rounded-lg border border-slate-200 p-4 space-y-2" data-testid="segment-rules-editor">
              <h2 className="text-lg font-semibold text-slate-900">Rules</h2>
              <TargetingRuleBuilder
                value={rules}
                onChange={setRules}
                operatorOptions={SEGMENT_RULES_OPERATOR_OPTIONS}
                emptyText={SEGMENT_RULES_EMPTY}
              />
            </div>
          ) : (
            <p className="text-sm text-slate-700" data-testid="segment-id-list-next">
              After you create the segment, upload its IDs on the segment&apos;s page.
            </p>
          )}

          {(saveError || problems.length > 0) && (
            <div
              ref={errorRef}
              tabIndex={-1}
              role="alert"
              className="rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-800"
              data-testid="segment-new-error"
            >
              <p>{saveError ?? 'Fix these before creating the segment:'}</p>
              {problems.length > 0 && (
                <ul className="mt-1 list-disc pl-5">
                  {problems.map((problem, i) => (
                    <li key={i}>{problem}</li>
                  ))}
                </ul>
              )}
            </div>
          )}

          <button
            type="submit"
            disabled={saving}
            className="px-4 py-2 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 disabled:opacity-60"
            data-testid="segment-create"
          >
            {saving ? 'Creating...' : 'Create segment'}
          </button>
        </form>
      </div>
    </div>
  );
}
