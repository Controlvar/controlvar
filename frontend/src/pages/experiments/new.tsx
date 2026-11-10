import React, { useEffect, useReducer, useRef, useState } from 'react';
import { useRouter } from 'next/router';
import Link from 'next/link';
import { ExperimentsService } from '@/services/experiments';
import { PageTitle } from '@/components/PageTitle';
import { AdvancedForm } from '@/components/experiments/new/AdvancedForm';
import { Wizard, NEW_EXPERIMENT_PATH } from '@/components/experiments/new/Wizard';
import { navigateHard, useLeaveGuard } from '@/components/experiments/new/leaveGuard';
import {
  balanceSplitProblem,
  buildCreatePayload,
  checkTargeting,
  configurationProblems,
  createInitialFormState,
  experimentFormReducer,
  validateForm,
} from '@/components/experiments/new/formState';
import { useOptionalAuth } from '@/contexts/AuthContext';
import { useEffectivePermissions } from '@/contexts/EffectivePermissionsContext';
import { canCreateExperiment } from '@/utils/experimentPermissions';
import {
  CreateError,
  CreateView,
  describeCreateError,
  ROLE_CANNOT_CREATE,
  TARGETING_INCOMPLETE,
} from '@/components/experiments/new/createErrors';

// The form's state, validation and payload live in `formState.ts`; these stay
// importable from the page for the code and tests that already use them.
export { generateKey, validateForm, FORM_EXPERIMENT_TYPES } from '@/components/experiments/new/formState';

export { ROLE_CANNOT_CREATE, SESSION_EXPIRED_CREATE } from '@/components/experiments/new/createErrors';

const INITIAL_SNAPSHOT = JSON.stringify(createInitialFormState());

/**
 * `/experiments/new`: guided setup by default, the single-page form at
 * `?advanced`. Both views share one state, one set of validators and one
 * create request, so switching between them keeps every answer.
 */
export default function NewExperimentPage() {
  const router = useRouter();
  const auth = useOptionalAuth();
  const { effective, loading: permissionsLoading } = useEffectivePermissions();
  const [state, dispatch] = useReducer(experimentFormReducer, undefined, createInitialFormState);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<CreateError | null>(null);
  const [revealProblems, setRevealProblems] = useState(0);
  const submittingRef = useRef(false);
  // Cleared synchronously before the page navigates away after a create, so
  // the unsaved-answers warning never fires for work that was saved.
  const guardArmedRef = useRef(true);
  // Whether a view has been shown since the page loaded (see Wizard `freshLoad`).
  const shownViewRef = useRef(false);

  const dirty = JSON.stringify(state) !== INITIAL_SNAPSHOT;
  useLeaveGuard(dirty, guardArmedRef);

  useEffect(() => {
    if (router.isReady) shownViewRef.current = true;
  }, [router.isReady]);

  // A refusal of the balance setting answers what was sent: once the setting or
  // the split changes it is stale, and goes. What the same answer said about
  // other fields stays.
  useEffect(() => {
    setError((prev) => {
      if (!prev || !prev.balance) return prev;
      return prev.otherMessage ? { message: prev.otherMessage } : null;
    });
  }, [state.balance, state.variants]);

  const submit = async (view: CreateView) => {
    if (submittingRef.current) return;
    setError(null);

    const problem = validateForm(state.name, state.variants, state.metrics);
    if (problem) {
      setError({ message: problem });
      if (configurationProblems(state.variants).some((p) => p !== null)) {
        setRevealProblems((n) => n + 1);
      }
      return;
    }

    // A blank condition ("+ Add Group" starts with one) would be refused by
    // the API; say so at the rules before sending anything.
    const targeting = checkTargeting(state.rules);
    if (targeting.length > 0) {
      setError({ message: TARGETING_INCOMPLETE, targeting });
      return;
    }

    // A split the API would refuse for a balanced experiment: said at the split, and nothing is sent.
    const splitRefusal = balanceSplitProblem(state);
    if (splitRefusal !== null) {
      setError({ message: splitRefusal, balance: { message: splitRefusal, field: 'split' } });
      return;
    }

    submittingRef.current = true;
    setIsSubmitting(true);
    let createdId: string;
    let sentKey: string | undefined;
    try {
      // No login redirect on a 401: that would discard every answer. The
      // session copy says what to do instead, and the answers stay.
      const payload = buildCreatePayload(state);
      sentKey = payload.key;
      const created = await ExperimentsService.create(payload, { redirectOn401: false });
      createdId = created.id;
    } catch (err) {
      setError(
        describeCreateError(err, sentKey, view, {
          balance: state.balance,
          allocations: state.variants.map((v) => Number(v.traffic_allocation) || 0),
        }),
      );
      submittingRef.current = false;
      setIsSubmitting(false);
      return;
    }

    // Created. Nothing below may report a failure to create, and nothing may
    // ask whether to leave: the answers are saved.
    guardArmedRef.current = false;
    const target = `/experiments/${createdId}`;
    let moved = false;
    try {
      moved = (await router.push(target)) !== false;
    } catch {
      moved = false;
    }
    if (!moved) navigateHard(target);
  };

  const handleAdvancedSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    void submit('advanced');
  };

  const allowed = canCreateExperiment(auth?.user, effective);

  // Nothing reads the query before the router has it: a static export renders
  // first with an empty one, which would drop `?advanced` and `?step`. Nor is
  // the notice shown while the effective map may still add EXPERIMENT CREATE
  // (#891): neither the notice nor the form until it answers.
  if (!router.isReady || (!allowed && permissionsLoading)) {
    return (
      <div className="flex-1 bg-slate-50">
        <PageTitle title="New Experiment" />
        <p className="sr-only" data-testid="new-experiment-loading">
          Loading
        </p>
      </div>
    );
  }

  // `?advanced` carries no value, so it parses to "" — check for the key.
  const advanced = 'advanced' in router.query;
  const freshLoad = !shownViewRef.current;

  return (
    <div className="flex-1 bg-slate-50">
      {(advanced || !allowed) && <PageTitle title="New Experiment" />}
      <div className="max-w-3xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {/* Header */}
        <div className="flex items-center gap-4 mb-6">
          <Link href="/experiments" className="text-slate-500 hover:text-slate-700 text-sm">
            &larr; Experiments
          </Link>
          <h1 className="text-2xl font-bold text-slate-900">New Experiment</h1>
        </div>

        {!allowed ? (
          <div
            role="status"
            className="rounded-lg border border-slate-200 bg-white p-6 text-sm text-slate-700 space-y-3"
            data-testid="create-not-allowed"
          >
            <p>{ROLE_CANNOT_CREATE}</p>
            <p>
              <Link href="/experiments" className="text-blue-700 underline hover:text-blue-900">
                Back to experiments
              </Link>
            </p>
          </div>
        ) : advanced ? (
          <>
            <p className="mb-4 text-sm text-slate-600">
              <Link
                href={{ pathname: NEW_EXPERIMENT_PATH }}
                shallow
                className="text-blue-700 underline hover:text-blue-900"
                data-testid="switch-to-guided"
              >
                Switch to guided setup
              </Link>{' '}
              Your answers carry over.
            </p>
            <AdvancedForm
              state={state}
              dispatch={dispatch}
              error={error}
              isSubmitting={isSubmitting}
              onSubmit={handleAdvancedSubmit}
              revealProblems={revealProblems}
            />
          </>
        ) : (
          <>
            <p className="mb-4 text-sm text-slate-600">
              <Link
                href={`${NEW_EXPERIMENT_PATH}?advanced`}
                shallow
                className="text-blue-700 underline hover:text-blue-900"
                data-testid="switch-to-advanced"
              >
                Use the single-page form (advanced)
              </Link>{' '}
              Your answers carry over.
            </p>
            <Wizard
              state={state}
              dispatch={dispatch}
              error={error}
              isSubmitting={isSubmitting}
              onCreate={() => void submit('guided')}
              onClearError={() => setError(null)}
              freshLoad={freshLoad}
            />
          </>
        )}
      </div>
    </div>
  );
}
