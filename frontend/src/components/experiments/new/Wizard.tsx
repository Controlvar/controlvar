import React, { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/router';
import { TargetingRuleBuilder } from '@/components/targeting';
import { PageTitle } from '@/components/PageTitle';
import { docsUrl } from '@/services/docs';
import { EXPERIMENT_TYPE_LABELS, METRIC_TYPE_LABELS } from '@/types/experiments';
import {
  buildCreatePayload,
  configurationProblems,
  ExperimentFormAction,
  ExperimentFormState,
  FORM_EXPERIMENT_TYPES,
  FORM_STEPS,
  FormStep,
  maxReachableStep,
  validateForm,
  validateStep,
} from './formState';
import { BasicInfoFields } from './BasicInfoFields';
import { VariantsEditor } from './VariantsEditor';
import { MetricsEditor } from './MetricsEditor';
import {
  currentEstimate,
  EstimatePanelState,
  INITIAL_ESTIMATE_PANEL,
  SampleSizeEstimate,
} from './SampleSizeEstimate';
import { formatEffect, plannedEffectFraction } from './estimate';
import { AnalysisSettingsFields } from './AnalysisSettingsFields';
import { describeSettings } from '@/components/results/shared/analysisSettings';
import { CreateError, footMessage } from './createErrors';
import { TargetingProblems } from './TargetingProblems';

export const NEW_EXPERIMENT_PATH = '/experiments/new';

export const STEP_LABELS: Record<FormStep, string> = {
  type: 'Type',
  details: 'Details',
  variants: 'Variants',
  estimate: 'Estimate',
  review: 'Review',
};

export const STEP_HEADINGS: Record<FormStep, string> = {
  type: 'What kind of experiment?',
  details: 'Name it and choose what to measure',
  variants: 'Set up the versions users will see',
  estimate: 'How results will be judged, and how many users you need',
  review: 'Check and create',
};

export const FRESH_LOAD_NOTICE =
  'Start from the first step. Answers are kept only in this tab, so reloading or opening a link starts over.';

const TYPE_DESCRIPTIONS: Record<string, string> = {
  a_b: 'Compare a control with one or more alternatives.',
  mv: 'Compare several versions against a control, with each user seeing one version.',
};

/** Input types where Enter means "Next" on the first three steps. */
const ENTER_ADVANCES = new Set(['text', 'number', 'email', 'search', 'url', 'tel']);

/** The step a `?step=` value names: its index, 0 when absent, -1 when unknown. */
function parseStep(raw: string | string[] | undefined): number {
  if (raw === undefined) return 0;
  return FORM_STEPS.indexOf((Array.isArray(raw) ? raw[0] : raw) as FormStep);
}

interface WizardProps {
  state: ExperimentFormState;
  dispatch: React.Dispatch<ExperimentFormAction>;
  error: CreateError | null;
  isSubmitting: boolean;
  onCreate: () => void;
  /** Clear the create error, when the user goes to fix what it names. */
  onClearError: () => void;
  /** True when this is the first view the page showed after loading. */
  freshLoad: boolean;
}

const primaryButton =
  'px-6 py-2 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-600 focus:ring-offset-2 disabled:opacity-50 disabled:cursor-not-allowed transition-colors';
const secondaryButton =
  'px-4 py-2 rounded-lg border border-slate-500 text-sm font-medium text-slate-700 hover:bg-slate-50 focus:outline-none focus:ring-2 focus:ring-blue-600 transition-colors';
const editButton =
  'text-sm font-medium text-blue-700 hover:text-blue-900 underline focus:outline-none focus:ring-2 focus:ring-blue-600 rounded';

/**
 * Guided setup: the new-experiment form asked one part at a time.
 *
 * The same state, validators and create request as the single-page form; only
 * the layout differs. The step lives in `?step=` so the browser's Back and
 * Forward move between steps. There is no `<form>` here and every button is
 * `type="button"`, so nothing is ever submitted by a key press: Enter in a
 * text or number field on the first three steps presses Next, and does
 * nothing on Estimate and Review.
 */
export function Wizard({ state, dispatch, error, isSubmitting, onCreate, onClearError, freshLoad }: WizardProps) {
  const router = useRouter();
  const rawStep = router.query.step;
  const requested = parseStep(rawStep);
  const maxReachable = maxReachableStep(state);

  // A step this view has asked the router for and not yet seen in the URL.
  const [pinned, setPinned] = useState<number | null>(null);
  const pinnedRef = useRef<number | null>(null);
  const [showFreshNotice, setShowFreshNotice] = useState(false);
  const [stepError, setStepError] = useState<string | null>(null);
  const [visitedMax, setVisitedMax] = useState(0);
  const [estimate, setEstimate] = useState<EstimatePanelState>(INITIAL_ESTIMATE_PANEL);
  const [revealProblems, setRevealProblems] = useState(0);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const firstRunRef = useRef(true);
  const shownRef = useRef<number | null>(null);
  // Set by the duplicate-key "Edit details": the next step change focuses the
  // key field rather than the step heading.
  const focusKeyRef = useRef(false);

  const current = pinned ?? (requested < 0 ? 0 : Math.min(requested, maxReachable));
  const step = FORM_STEPS[current];

  const moveTo = (index: number, how: 'push' | 'replace') => {
    pinnedRef.current = index;
    setPinned(index);
    const navigate = how === 'push' ? router.push : router.replace;
    const release = () => {
      // The route change did not happen (the browser's Back or Forward
      // cancelled it mid-flight, say): stop holding the step, so the view
      // follows the address again instead of the step that never arrived.
      if (pinnedRef.current === index) {
        pinnedRef.current = null;
        setPinned(null);
      }
    };
    navigate({ pathname: NEW_EXPERIMENT_PATH, query: { step: FORM_STEPS[index] } }, undefined, {
      shallow: true,
    }).then(
      (moved) => {
        if (moved === false) release();
      },
      release,
    );
  };

  // Keep the step in the URL honest. On the page's first load the answers are
  // empty, so any later step sends you to the first with a notice; after that,
  // Back, Forward or an edited URL can only reach a step whose earlier steps
  // are complete.
  useEffect(() => {
    if (firstRunRef.current) {
      firstRunRef.current = false;
      if (freshLoad && rawStep !== undefined && rawStep !== 'type') {
        setShowFreshNotice(true);
        moveTo(0, 'replace');
        return;
      }
    }
    if (pinnedRef.current !== null) {
      if (requested === pinnedRef.current) {
        pinnedRef.current = null;
        setPinned(null);
      }
      return;
    }
    if (requested < 0 || requested > maxReachable) {
      moveTo(requested < 0 ? 0 : maxReachable, 'replace');
    }
    // Runs when the URL's step changes; the answers are read as they stand.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rawStep]);

  // Focus the step's heading on every step change after the first.
  useEffect(() => {
    setVisitedMax((v) => Math.max(v, current));
    if (shownRef.current !== null && shownRef.current !== current) {
      const key = focusKeyRef.current ? document.getElementById('experiment-key') : null;
      focusKeyRef.current = false;
      if (key instanceof HTMLInputElement) {
        key.focus();
        key.select();
      } else {
        headingRef.current?.focus();
      }
      setStepError(null);
    }
    shownRef.current = current;
  }, [current]);

  const next = () => {
    const problem = validateStep(step, state);
    if (problem) {
      setStepError(problem);
      // A configuration problem may sit in a closed section: open it and focus it.
      if (step === 'variants' && configurationProblems(state.variants).some((p) => p !== null)) {
        setRevealProblems((n) => n + 1);
      }
      return;
    }
    setStepError(null);
    setShowFreshNotice(false);
    moveTo(current + 1, 'push');
  };

  const back = () => {
    setStepError(null);
    moveTo(current - 1, 'push');
  };

  const goTo = (index: number) => {
    if (index === current || index > maxReachable) return;
    moveTo(index, 'push');
  };

  // The key is taken: go to Details with the key field focused. The error goes
  // too, so Review does not repeat it after the key has been changed.
  const editKey = () => {
    focusKeyRef.current = true;
    onClearError();
    goTo(FORM_STEPS.indexOf('details'));
  };

  // The targeting rules stopped the create: go to the step that has the rule
  // builder. The error goes too; Create checks the rules again.
  const editTargeting = () => {
    onClearError();
    goTo(FORM_STEPS.indexOf('variants'));
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== 'Enter' || e.defaultPrevented) return;
    // An IME confirming a composition also sends Enter.
    if (e.nativeEvent.isComposing || e.keyCode === 229) return;
    if (step === 'estimate' || step === 'review') return;
    const target = e.target;
    if (!(target instanceof HTMLInputElement) || !ENTER_ADVANCES.has(target.type)) return;
    e.preventDefault();
    next();
  };

  const payload = step === 'review' ? buildCreatePayload(state) : null;
  const plannedEffect = plannedEffectFraction(state.plannedEffectPct);
  // A targeting problem is shown beside the targeting summary; anything else the error says goes below.
  const foot = footMessage(error);

  return (
    <div data-testid="guided-setup">
      <PageTitle title={`New Experiment – Step ${current + 1} of 5: ${STEP_LABELS[step]}`} />

      <nav aria-label="Guided setup steps" className="mb-6">
        <p className="text-sm text-slate-600 mb-2" data-testid="wizard-step-count">
          Step {current + 1} of {FORM_STEPS.length}
        </p>
        <ol className="flex flex-wrap gap-2" data-testid="wizard-stepper">
          {FORM_STEPS.map((s, index) => {
            const isCurrent = index === current;
            const reachable = index <= maxReachable;
            // Each step's own check, independently: a problem on one step never
            // marks another. Review is the whole form.
            const problem =
              !isCurrent && index <= visitedMax
                ? s === 'review'
                  ? validateForm(state.name, state.variants, state.metrics)
                  : validateStep(s, state)
                : null;
            const status = isCurrent
              ? 'current step'
              : problem
                ? 'needs attention'
                : index <= visitedMax
                  ? 'completed'
                  : 'not started';
            const label = (
              <>
                <span aria-hidden="true">{index + 1}</span> {STEP_LABELS[s]}
                {problem && (
                  <span aria-hidden="true" className="ml-1 text-red-700" data-testid={`wizard-step-problem-${s}`}>
                    !
                  </span>
                )}
                <span className="sr-only"> ({status})</span>
              </>
            );
            const base = 'inline-flex items-center gap-1 rounded-full px-3 py-1 text-sm min-h-[24px]';
            return (
              <li key={s} aria-current={isCurrent ? 'step' : undefined} data-testid={`wizard-step-${s}`}>
                {isCurrent || !reachable ? (
                  <span
                    className={`${base} ${
                      isCurrent ? 'bg-blue-600 text-white font-semibold' : 'border border-slate-300 text-slate-600'
                    }`}
                    aria-disabled={!isCurrent ? true : undefined}
                  >
                    {label}
                  </span>
                ) : (
                  <button
                    type="button"
                    onClick={() => goTo(index)}
                    className={`${base} border border-slate-500 text-slate-800 hover:bg-slate-100 focus:outline-none focus:ring-2 focus:ring-blue-600`}
                  >
                    {label}
                  </button>
                )}
              </li>
            );
          })}
        </ol>
      </nav>

      {showFreshNotice && step === 'type' && (
        <div
          role="status"
          className="mb-4 rounded-lg border border-blue-200 bg-blue-50 p-3 text-sm text-blue-900"
          data-testid="wizard-fresh-notice"
        >
          {FRESH_LOAD_NOTICE}
        </div>
      )}

      <div onKeyDown={onKeyDown} className="space-y-6" data-testid={`wizard-panel-${step}`}>
        <section
          aria-labelledby="wizard-step-heading"
          className="bg-white rounded-lg border border-slate-200 p-6 space-y-4"
        >
          <h2
            id="wizard-step-heading"
            ref={headingRef}
            tabIndex={-1}
            className="text-lg font-semibold text-slate-900 focus:outline-none"
            data-testid="wizard-step-heading"
          >
            {STEP_HEADINGS[step]}
          </h2>

          {step === 'type' && (
            <fieldset>
              <legend className="text-sm text-slate-700 mb-3">Choose the experiment type.</legend>
              <div className="grid gap-3 sm:grid-cols-2">
                {FORM_EXPERIMENT_TYPES.map((t) => (
                  <label
                    key={t}
                    className={`flex gap-3 items-start rounded-lg border p-4 cursor-pointer ${
                      state.type === t ? 'border-blue-600 bg-blue-50' : 'border-slate-500'
                    }`}
                  >
                    <input
                      type="radio"
                      name="wizard_experiment_type"
                      value={t}
                      checked={state.type === t}
                      onChange={() => dispatch({ type: 'setType', experimentType: t })}
                      className="mt-1"
                      data-testid={`wizard-type-${t}`}
                    />
                    <span>
                      <span className="block text-sm font-semibold text-slate-900">
                        {EXPERIMENT_TYPE_LABELS[t]}
                      </span>
                      <span className="block text-sm text-slate-600">{TYPE_DESCRIPTIONS[t]}</span>
                    </span>
                  </label>
                ))}
              </div>
              <p className="text-sm text-slate-600 mt-3" data-testid="wizard-type-note">
                <a href={docsUrl('api/split-url')} className="text-blue-700 underline hover:text-blue-900">
                  Split URL
                </a>{' '}
                and{' '}
                <a
                  href={docsUrl('api/multi-armed-bandit')}
                  className="text-blue-700 underline hover:text-blue-900"
                >
                  bandit
                </a>{' '}
                experiments need settings this flow does not ask for. Create them through the API or an
                SDK.
              </p>
            </fieldset>
          )}

          {step === 'details' && (
            <>
              <BasicInfoFields state={state} dispatch={dispatch} />
              <div className="pt-2 space-y-4" data-testid="metrics-section">
                <p className="text-sm text-slate-600">
                  Mark one metric as the primary metric; that is the one you decide on. Any other metrics
                  are tracked alongside it.
                </p>
                <MetricsEditor metrics={state.metrics} dispatch={dispatch} />
              </div>
            </>
          )}

          {step === 'variants' && (
            <>
              <VariantsEditor
                variants={state.variants}
                dispatch={dispatch}
                revealProblems={revealProblems}
              />
              <div className="pt-2">
                <TargetingRuleBuilder
                  value={state.rules}
                  onChange={(rules) => dispatch({ type: 'setRules', rules })}
                />
              </div>
            </>
          )}

          {step === 'estimate' && (
            <>
              <AnalysisSettingsFields
                confidenceLevel={state.confidenceLevel}
                correctionMethod={state.correctionMethod}
                dispatch={dispatch}
                variantCount={state.variants.length}
                bayesianEnabled={state.bayesianEnabled}
                sequentialEnabled={state.sequentialEnabled}
              />
              {/* Saved settings above; the estimate below, whose effect is saved. */}
              <section
                aria-labelledby="wizard-estimate-heading"
                className="border-t border-slate-200 pt-4 space-y-3"
              >
                <h3 id="wizard-estimate-heading" className="text-base font-semibold text-slate-900">
                  Sample size estimate (optional)
                </h3>
                <SampleSizeEstimate
                  value={estimate}
                  onChange={setEstimate}
                  mdePct={state.plannedEffectPct}
                  onMdePctChange={(plannedEffectPct) => dispatch({ type: 'setPlannedEffect', plannedEffectPct })}
                  allocations={state.variants.map((v) => Number(v.traffic_allocation) || 0)}
                  confidenceLevel={state.confidenceLevel}
                  correctionMethod={state.correctionMethod}
                />
              </section>
            </>
          )}

          {step === 'review' && payload && (
            <dl className="divide-y divide-slate-200 text-sm" data-testid="wizard-review">
              <ReviewRow title="Type" onEdit={() => goTo(0)} editLabel="Edit type">
                <span data-testid="review-type">{EXPERIMENT_TYPE_LABELS[payload.experiment_type]}</span>
              </ReviewRow>
              <ReviewRow title="Details" onEdit={() => goTo(1)} editLabel="Edit details">
                <p data-testid="review-name">
                  <span className="font-medium">Name:</span> {payload.name}
                </p>
                <p data-testid="review-key">
                  <span className="font-medium">Key:</span>{' '}
                  <code className="font-mono">{payload.key ?? '(none)'}</code>
                </p>
                {payload.description && (
                  <p data-testid="review-description">
                    <span className="font-medium">Description:</span> {payload.description}
                  </p>
                )}
                {payload.hypothesis && (
                  <p data-testid="review-hypothesis">
                    <span className="font-medium">Hypothesis:</span> {payload.hypothesis}
                  </p>
                )}
                <ul className="mt-1 list-disc pl-5" data-testid="review-metrics">
                  {payload.metrics.map((m, i) => (
                    <li key={i}>
                      {m.name} (<code className="font-mono">{m.event_name}</code>,{' '}
                      {METRIC_TYPE_LABELS[m.metric_type]}){m.is_primary ? ' — primary metric' : ''}
                    </li>
                  ))}
                </ul>
              </ReviewRow>
              <ReviewRow title="Variants" onEdit={() => goTo(2)} editLabel="Edit variants">
                <ul className="list-disc pl-5" data-testid="review-variants">
                  {payload.variants.map((v, i) => (
                    <li key={i}>
                      {v.name}: {v.traffic_allocation}%{v.is_control ? ' (control)' : ''}
                      {v.configuration && (
                        <>
                          {', configuration '}
                          <code
                            className="font-mono break-all"
                            data-testid={`review-variant-configuration-${i}`}
                          >
                            {JSON.stringify(v.configuration)}
                          </code>
                        </>
                      )}
                    </li>
                  ))}
                </ul>
                <p className="mt-1" data-testid="review-targeting">
                  {payload.targeting_rules
                    ? `Targeting: ${state.rules.groups.length} rule ${
                        state.rules.groups.length === 1 ? 'group' : 'groups'
                      }`
                    : 'Targeting: everyone'}
                </p>
                <div className="mt-2">
                  <TargetingProblems error={error}>
                    <button
                      type="button"
                      onClick={editTargeting}
                      className="mt-2 font-medium underline hover:text-red-900 focus:outline-none focus:ring-2 focus:ring-blue-600 rounded"
                      data-testid="targeting-error-edit"
                    >
                      Edit targeting
                    </button>
                  </TargetingProblems>
                </div>
              </ReviewRow>
              <ReviewRow title="Analysis" onEdit={() => goTo(3)} editLabel="Edit analysis">
                <p data-testid="review-analysis">
                  {describeSettings(
                    payload.confidence_level ?? state.confidenceLevel,
                    payload.correction_method ?? state.correctionMethod,
                  )}
                  {payload.bayesian_enabled ? ' · Bayesian analysis on' : ''}
                  {payload.sequential_testing_enabled ? ' · Sequential testing (beta) on' : ''}
                </p>
              </ReviewRow>
              <ReviewRow title="Sample size" onEdit={() => goTo(3)} editLabel="Edit sample size">
                <p data-testid="review-estimate">
                  {reviewSampleSize(
                    plannedEffect,
                    currentEstimate(
                      estimate,
                      state.plannedEffectPct,
                      state.variants.length,
                      state.confidenceLevel,
                    )?.samples_per_variant ?? null,
                  )}
                </p>
              </ReviewRow>
            </dl>
          )}

          <div role="alert" id="wizard-step-error" data-testid="step-error">
            {stepError && (
              <p className="rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-700">{stepError}</p>
            )}
          </div>

          {step === 'review' && error && foot && (
            <div
              role="alert"
              className="rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-700"
              data-testid="form-error"
            >
              {foot}
              {error.editDetails && (
                <>
                  {' '}
                  <button
                    type="button"
                    onClick={editKey}
                    className="font-medium underline hover:text-red-900 focus:outline-none focus:ring-2 focus:ring-blue-600 rounded"
                    data-testid="form-error-edit-details"
                  >
                    Edit details
                  </button>
                </>
              )}
            </div>
          )}
        </section>

        <div className="flex flex-wrap gap-3 justify-between items-center">
          <Link
            href="/experiments"
            className="px-4 py-2 rounded-lg text-sm font-medium text-slate-700 underline hover:text-slate-900"
            data-testid="wizard-cancel"
          >
            Cancel
          </Link>
          <div className="flex gap-3">
            {current > 0 && (
              <button type="button" onClick={back} className={secondaryButton} data-testid="wizard-back">
                Back
              </button>
            )}
            {step === 'review' ? (
              <button
                type="button"
                onClick={onCreate}
                disabled={isSubmitting}
                className={primaryButton}
                data-testid="wizard-create"
              >
                {isSubmitting ? 'Creating…' : 'Create Experiment'}
              </button>
            ) : (
              <button
                type="button"
                onClick={next}
                aria-describedby={stepError ? 'wizard-step-error' : undefined}
                className={primaryButton}
                data-testid="wizard-next"
              >
                Next
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

/**
 * The Review step's sample-size line: exactly what will be saved. Only the
 * effect is; an estimate number is shown only when it answers the inputs as
 * they stand.
 */
export function reviewSampleSize(plannedEffect: number | undefined, perVariant: number | null): string {
  if (plannedEffect === undefined) {
    return 'No minimum detectable effect set; the results page will plan for 5%.';
  }
  const effect = formatEffect(plannedEffect);
  if (perVariant === null) {
    return `Minimum detectable effect: ${effect} (saved). No estimate calculated for these inputs.`;
  }
  return (
    `${perVariant.toLocaleString('en-US')} users per variant to detect a ${effect} lift. ` +
    `The ${effect} is saved; the rest of the estimate is not.`
  );
}

function ReviewRow({
  title,
  onEdit,
  editLabel,
  children,
}: {
  title: string;
  onEdit: () => void;
  editLabel: string;
  children: React.ReactNode;
}) {
  return (
    <div className="py-3">
      <dt className="font-semibold text-slate-900">{title}</dt>
      <dd className="mt-1 flex gap-4 justify-between items-start text-slate-700">
        <div className="flex-1">{children}</div>
        <button
          type="button"
          onClick={onEdit}
          className={editButton}
          data-testid={`review-edit-${title.toLowerCase().replace(/\s+/g, '-')}`}
        >
          {editLabel}
        </button>
      </dd>
    </div>
  );
}
