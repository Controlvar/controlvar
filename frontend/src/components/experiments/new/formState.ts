/**
 * The new-experiment form's state, validation and request payload.
 *
 * Pure TypeScript with no React: the page (`pages/experiments/new.tsx`) owns
 * one `useReducer` over `ExperimentFormState`, the field components render
 * slices of it, and `buildCreatePayload` turns it into the exact
 * `CreateExperimentRequest` the page sends. Keeping all of that here lets more
 * than one view of the form share it without drifting apart.
 */
import { TargetingRules } from '@/types/targeting';
import { createEmptyRules, validateRules } from '@/utils/targeting';
import {
  DesignMetric,
  DesignVariant,
  Experiment,
  ExperimentDesignUpdate,
  ExperimentMetric,
  ExperimentType,
  ExperimentVariant,
  MetricType,
  CreateExperimentRequest,
} from '@/types/experiments';
import { CorrectionMethod } from '@/types/results';
import {
  DEFAULT_CONFIDENCE_LEVEL,
  DEFAULT_CORRECTION_METHOD,
} from '@/components/results/shared/analysisSettings';
import { plannedEffectFraction, plannedEffectProblem } from './estimate';

export function generateKey(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .substring(0, 64);
}

export interface VariantFormData {
  name: string;
  description: string;
  traffic_allocation: number;
  is_control: boolean;
  /**
   * The variant's configuration as typed: a JSON object, sent as
   * `configuration`. Absent or blank means the variant has none, and the key
   * is left out of the request.
   */
  configuration_text?: string;
}

export interface MetricFormData {
  name: string;
  event_name: string;
  metric_type: MetricType;
  is_primary: boolean;
  /**
   * A decrease is the good direction (errors, uninstalls): the winner and the
   * recommendation then read a significant fall as the improvement. Absent
   * means false, the API's default.
   */
  lower_is_better?: boolean;
  /**
   * The stored fields this form does not show, kept as they were read so the
   * design edit sends them back unchanged (`buildDesignPayload`). Set only by
   * `formStateFromExperiment`; a metric added in the form has none. The
   * reducer's `changeMetric` and `setPrimary` spread the row, so it survives
   * every edit of the row.
   */
  carried?: MetricCarried;
}

/**
 * A stored metric's fields that no editor shows. Null means the experiment
 * answered with no value.
 */
export interface MetricCarried {
  description: string | null;
  aggregation_method: string | null;
  minimum_sample_size: number | null;
  /**
   * The metric's own `expected_effect`. On the metric that was primary when
   * the form was seeded it is null: that value is the experiment's planned
   * effect, held in `designPlannedEffect`, and goes to whichever metric is
   * primary on save.
   */
  expected_effect: number | null;
  event_value_path: string | null;
}

export interface ExperimentFormState {
  name: string;
  key: string;
  /** True once the user has typed in the key field; the name then stops rewriting it. */
  keyEdited: boolean;
  description: string;
  hypothesis: string;
  type: ExperimentType;
  rules: TargetingRules;
  variants: VariantFormData[];
  metrics: MetricFormData[];
  /** How the results will be judged (#580); saved with the experiment. */
  confidenceLevel: number;
  correctionMethod: CorrectionMethod;
  /** Also analyse the primary metric with Bayesian statistics (#216); saved with the experiment. */
  bayesianEnabled: boolean;
  /**
   * The minimum detectable effect typed on guided setup's Estimate step, in
   * percent and RELATIVE (10 means 5% -> 5.5%), as typed. When usable it is
   * saved as the primary metric's `expected_effect` (10 -> 0.1), and the
   * results page plans the sample for it (#1257). Blank saves nothing. It is
   * here, not in the estimate panel, so switching views keeps it.
   */
  plannedEffectPct: string;
  /**
   * The design edit's planned effect: the stored primary metric's
   * `expected_effect`, exactly as stored (null when it has none). Set only by
   * `formStateFromExperiment`, and absent when no stored metric is primary.
   * `buildDesignPayload` puts it on whichever metric is primary on save, so
   * the planned sample does not change when the primary does, as
   * `buildCreatePayload` does with `plannedEffectPct`.
   */
  designPlannedEffect?: number | null;
}

const DEFAULT_VARIANTS: VariantFormData[] = [
  { name: 'Control', description: '', traffic_allocation: 50, is_control: true },
  { name: 'Treatment', description: '', traffic_allocation: 50, is_control: false },
];

const DEFAULT_METRICS: MetricFormData[] = [
  { name: 'Conversion', event_name: 'conversion', metric_type: 'conversion', is_primary: true },
];

/** A fresh copy of the form's starting values. */
export function createInitialFormState(): ExperimentFormState {
  return {
    name: '',
    key: '',
    keyEdited: false,
    description: '',
    hypothesis: '',
    type: 'a_b',
    rules: createEmptyRules(),
    variants: DEFAULT_VARIANTS,
    metrics: DEFAULT_METRICS,
    confidenceLevel: DEFAULT_CONFIDENCE_LEVEL,
    correctionMethod: DEFAULT_CORRECTION_METHOD,
    bayesianEnabled: false,
    plannedEffectPct: '',
  };
}

export const INITIAL_FORM_STATE: ExperimentFormState = createInitialFormState();

/**
 * Types this form can fully configure.
 *
 * `split_url` needs a `split_url_config` (without it the split-URL path has no
 * URLs to route to) and `bandit` needs `optimization_type != "fixed"` (with the
 * default `fixed` the bandit scheduler skips the experiment forever, so traffic
 * never adapts). This form sends neither field, so it must not offer them —
 * see `backend/app/schemas/experiment.py::ExperimentCreate`.
 */
export const FORM_EXPERIMENT_TYPES: ExperimentType[] = ['a_b', 'mv'];

/** Sum of the variants' traffic allocations, counting a blank or invalid entry as 0. */
export function allocationTotalOf(variants: VariantFormData[]): number {
  return variants.reduce((sum, v) => sum + (Number(v.traffic_allocation) || 0), 0);
}

// --- variant configuration ---------------------------------------------------

/**
 * The most a configuration may be, in UTF-8 bytes, in this form. The API takes
 * larger ones (it limits only the whole request), so this is the dashboard's
 * own limit for a payload sent to every assigned user, not the API's.
 */
export const CONFIGURATION_MAX_BYTES = 16_384;

export const CONFIGURATION_NOT_JSON =
  'This is not valid JSON. Check for a missing quote, comma or brace.';
export const CONFIGURATION_NOT_OBJECT =
  'The configuration must be a JSON object in braces, like {"color": "blue"}.';

/** The words for a configuration over `CONFIGURATION_MAX_BYTES`. */
export function configurationTooLong(bytes: number): string {
  return (
    `This configuration is ${bytes.toLocaleString('en-US')} bytes. The dashboard accepts up to ` +
    `${CONFIGURATION_MAX_BYTES.toLocaleString('en-US')} bytes; the API accepts larger ones.`
  );
}

/**
 * The length of `text` in UTF-8 bytes, as it is sent. Counted by code point:
 * `.length` counts UTF-16 units, so "é" would count 1 instead of 2 and an
 * emoji 2 instead of 4. A lone surrogate counts 3, as the encoder sends
 * U+FFFD in its place.
 */
export function utf8ByteLength(text: string): number {
  let bytes = 0;
  for (let i = 0; i < text.length; i += 1) {
    const unit = text.charCodeAt(i);
    if (unit < 0x80) bytes += 1;
    else if (unit < 0x800) bytes += 2;
    else if (unit >= 0xd800 && unit <= 0xdbff && i + 1 < text.length) {
      const low = text.charCodeAt(i + 1);
      if (low >= 0xdc00 && low <= 0xdfff) {
        bytes += 4;
        i += 1;
      } else {
        bytes += 3;
      }
    } else bytes += 3;
  }
  return bytes;
}

export type ConfigurationParse =
  | { ok: true; value: Record<string, unknown> | undefined }
  | { ok: false; problem: string };

/**
 * A variant's configuration text read the way the API will: blank is none
 * (`value` undefined); otherwise it must be a JSON object within
 * `CONFIGURATION_MAX_BYTES`. Arrays, strings, numbers, booleans and `null`
 * are refused here because the API refuses them too (422).
 */
export function parseConfiguration(text: string | undefined): ConfigurationParse {
  if (text === undefined || text.trim() === '') return { ok: true, value: undefined };
  const bytes = utf8ByteLength(text);
  if (bytes > CONFIGURATION_MAX_BYTES) return { ok: false, problem: configurationTooLong(bytes) };
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return { ok: false, problem: CONFIGURATION_NOT_JSON };
  }
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return { ok: false, problem: CONFIGURATION_NOT_OBJECT };
  }
  return { ok: true, value: parsed as Record<string, unknown> };
}

/** Each variant's configuration problem, or null where there is none. */
export function configurationProblems(variants: VariantFormData[]): (string | null)[] {
  return variants.map((v) => {
    const result = parseConfiguration(v.configuration_text);
    return result.ok ? null : result.problem;
  });
}

// --- validation --------------------------------------------------------------
//
// `validateForm` is the client-side mirror of the `ExperimentCreate`
// validators and reports the FIRST problem, in the order name, variants,
// metrics. The three checks below are that function split at its seams, with
// the same messages in the same order, so a view that validates one part at a
// time reports exactly the words the whole-form check would.

export function checkName(name: string): string | null {
  if (!name.trim()) return 'Name is required.';
  return null;
}

export function checkVariants(variants: VariantFormData[]): string | null {
  if (variants.length === 0) return 'Add at least one variant.';
  if (variants.some((v) => !v.name.trim())) return 'Every variant needs a name.';
  if (!variants.some((v) => v.is_control)) return 'One variant must be marked as control.';
  const total = allocationTotalOf(variants);
  if (total !== 100) return `Variant allocations must add up to 100% (currently ${total}%).`;
  const problems = configurationProblems(variants);
  const first = problems.findIndex((p) => p !== null);
  if (first >= 0) {
    return `The configuration of “${variants[first].name.trim()}” needs fixing: ${problems[first]}`;
  }
  return null;
}

export function checkMetrics(metrics: MetricFormData[]): string | null {
  if (metrics.length === 0) return 'Add at least one metric.';
  if (metrics.some((m) => !m.name.trim() || !m.event_name.trim())) {
    return 'Every metric needs a name and an event name.';
  }
  // The API stores one metric per name per experiment and refuses a repeat.
  const names = metrics.map((m) => m.name.trim());
  if (new Set(names).size !== names.length) return 'Metric names must be unique.';
  if (!metrics.some((m) => m.is_primary)) return 'Choose a primary metric.';
  return null;
}

/** Client-side mirror of the `ExperimentCreate` validators; returns the first problem. */
export function validateForm(
  name: string,
  variants: VariantFormData[],
  metrics: MetricFormData[],
): string | null {
  return checkName(name) ?? checkVariants(variants) ?? checkMetrics(metrics);
}

/**
 * Every problem with the targeting rules, in the builder's words, or [] when
 * they can be sent. No groups means no targeting and is always fine.
 *
 * `validateRules` finds a blank attribute, operator or value (the empty
 * condition "+ Add Group" starts with); a group whose conditions have all been
 * removed is caught here, because the API refuses that too. The API is the
 * real check: anything it refuses beyond these comes back as a 422 and is
 * shown in the same place (`describeCreateError`).
 */
export function checkTargeting(rules: TargetingRules): string[] {
  if (rules.groups.length === 0) return [];
  const empty = rules.groups.flatMap((group, gi) =>
    group.conditions.length === 0 ? [`Group ${gi + 1}: add a condition or remove the group`] : [],
  );
  return [...empty, ...validateRules(rules).errors];
}

/** The parts of the form, in the order a step-by-step view asks for them. */
export const FORM_STEPS = ['type', 'details', 'variants', 'estimate', 'review'] as const;
export type FormStep = (typeof FORM_STEPS)[number];

/**
 * The first problem with the part of the form one step asks for, or null.
 *
 * `type` always has a valid value and cannot fail. `estimate` fails only on a
 * minimum detectable effect that is typed but cannot be saved (blank is fine;
 * the rest of that step is not sent). `review` is the whole-form check, and
 * every other message is one `validateForm` can return. The single-page form
 * does not ask for the effect, so `validateForm` does not check it, and
 * `buildCreatePayload` sends only a usable one.
 */
export function validateStep(step: FormStep, state: ExperimentFormState): string | null {
  switch (step) {
    case 'type':
      return null;
    case 'details':
      return checkName(state.name) ?? checkMetrics(state.metrics);
    case 'variants':
      return checkVariants(state.variants);
    case 'estimate':
      return plannedEffectProblem(state.plannedEffectPct);
    case 'review':
      return validateForm(state.name, state.variants, state.metrics);
  }
}

/**
 * The furthest step (an index into `FORM_STEPS`) a step-by-step view may show:
 * the first step that still has a problem, or `review` when none has. A fresh
 * form reaches `details`, because a new experiment has no name yet.
 */
export function maxReachableStep(state: ExperimentFormState): number {
  const review = FORM_STEPS.indexOf('review');
  for (let i = 0; i < review; i += 1) {
    if (validateStep(FORM_STEPS[i], state) !== null) return i;
  }
  return review;
}

// --- payload -----------------------------------------------------------------

/**
 * The `POST /api/v1/experiments` body for the form as it stands.
 *
 * A usable planned effect goes on the primary metric only, as
 * `expected_effect`; with none, no metric carries the key at all.
 */
export function buildCreatePayload(state: ExperimentFormState): CreateExperimentRequest {
  const { name, key, description, hypothesis, type, rules, variants, metrics } = state;
  const plannedEffect = plannedEffectFraction(state.plannedEffectPct);
  const payload: CreateExperimentRequest = {
    name: name.trim(),
    key: (key || generateKey(name)).trim() || undefined,
    description: description.trim() || undefined,
    hypothesis: hypothesis.trim() || undefined,
    experiment_type: type,
    targeting_rules: rules.groups.length > 0 ? (rules as unknown as Record<string, unknown>) : null,
    variants: variants.map((v) => {
      const variant: CreateExperimentRequest['variants'][number] = {
        name: v.name.trim(),
        description: v.description.trim() || undefined,
        is_control: v.is_control,
        traffic_allocation: Number(v.traffic_allocation) || 0,
      };
      // Validation refuses a bad configuration before this is called; blank is none.
      const configuration = parseConfiguration(v.configuration_text);
      if (configuration.ok && configuration.value !== undefined) {
        variant.configuration = configuration.value;
      }
      return variant;
    }),
    metrics: metrics.map((m) => {
      const metric: CreateExperimentRequest['metrics'][number] = {
        name: m.name.trim(),
        event_name: m.event_name.trim(),
        metric_type: m.metric_type,
        is_primary: m.is_primary,
      };
      if (m.is_primary && plannedEffect !== undefined) metric.expected_effect = plannedEffect;
      // Sent only when true; without it the API stores false.
      if (m.lower_is_better === true) metric.lower_is_better = true;
      return metric;
    }),
    confidence_level: state.confidenceLevel,
    correction_method: state.correctionMethod,
  };
  if (state.bayesianEnabled) payload.bayesian_enabled = true;
  return payload;
}

// --- design edit: a draft's variants and metrics (#1453) ---------------------
//
// `PUT /api/v1/experiments/{id}` REPLACES both lists in one commit: a variant
// or a metric left out is deleted, and a field left out of an item is reset to
// its default, with a 200. So the experiment page's design edit always sends
// every item, and every stored field of each, including the ones no editor
// shows (`MetricFormData.carried`).

/** A stored configuration as the variant editor shows it: indented JSON, or blank for none. */
export function configurationText(configuration: Record<string, unknown> | null | undefined): string {
  return configuration == null ? '' : JSON.stringify(configuration, null, 2);
}

function variantFromStored(variant: ExperimentVariant): VariantFormData {
  return {
    name: variant.name,
    description: variant.description ?? '',
    traffic_allocation: variant.traffic_allocation,
    is_control: variant.is_control,
    configuration_text: configurationText(variant.configuration),
  };
}

function metricFromStored(metric: ExperimentMetric, holdsPlannedEffect: boolean): MetricFormData {
  return {
    name: metric.name,
    event_name: metric.event_name,
    metric_type: metric.metric_type as MetricType,
    is_primary: metric.is_primary,
    lower_is_better: metric.lower_is_better === true,
    carried: {
      description: metric.description ?? null,
      aggregation_method: metric.aggregation_method ?? null,
      minimum_sample_size: metric.minimum_sample_size ?? null,
      expected_effect: holdsPlannedEffect ? null : (metric.expected_effect ?? null),
      event_value_path: metric.event_value_path ?? null,
    },
  };
}

/**
 * The form state the design edit starts from: the experiment's variants and
 * metrics as stored, in the order the API listed them. The first primary
 * metric's `expected_effect` becomes `designPlannedEffect`.
 */
export function formStateFromExperiment(experiment: Experiment): ExperimentFormState {
  const planned = experiment.metrics.findIndex((m) => m.is_primary);
  const state: ExperimentFormState = {
    ...createInitialFormState(),
    name: experiment.name,
    variants: experiment.variants.map(variantFromStored),
    metrics: experiment.metrics.map((m, i) => metricFromStored(m, i === planned)),
  };
  if (planned >= 0) state.designPlannedEffect = experiment.metrics[planned].expected_effect ?? null;
  return state;
}

/**
 * The design edit's body: exactly `{variants, metrics}`, both complete.
 *
 * - A variant carries all five `VariantBase` fields. A blank description and
 *   no configuration are sent as null.
 * - A metric carries what the editor shows, plus `carried` as it was read.
 *   `aggregation_method` and `minimum_sample_size` are left out when they
 *   have no value (the API refuses null there and stores its default); the
 *   Optional fields are sent as null.
 * - The primary metric's `expected_effect` is `designPlannedEffect`, so it
 *   moves to a new primary, as `buildCreatePayload` puts the planned effect on
 *   the primary only. With no stored primary, each metric keeps its own.
 *
 * Names and event names are trimmed, as on create. Validation
 * (`checkDesign`) refuses a configuration that does not parse before this is
 * called.
 */
export function buildDesignPayload(state: ExperimentFormState): ExperimentDesignUpdate {
  const variants = state.variants.map((v): DesignVariant => {
    const configuration = parseConfiguration(v.configuration_text);
    return {
      name: v.name.trim(),
      description: v.description === '' ? null : v.description,
      is_control: v.is_control,
      traffic_allocation: Number(v.traffic_allocation) || 0,
      configuration: configuration.ok && configuration.value !== undefined ? configuration.value : null,
    };
  });
  const metrics = state.metrics.map((m): DesignMetric => {
    const carried = m.carried;
    const takesPlan = m.is_primary && state.designPlannedEffect !== undefined;
    const metric: DesignMetric = {
      name: m.name.trim(),
      description: carried?.description ?? null,
      event_name: m.event_name.trim(),
      metric_type: m.metric_type,
      is_primary: m.is_primary,
      expected_effect: takesPlan ? (state.designPlannedEffect ?? null) : (carried?.expected_effect ?? null),
      event_value_path: carried?.event_value_path ?? null,
      lower_is_better: m.lower_is_better === true,
    };
    if (carried?.aggregation_method != null) metric.aggregation_method = carried.aggregation_method;
    if (carried?.minimum_sample_size != null) metric.minimum_sample_size = carried.minimum_sample_size;
    return metric;
  });
  return { variants, metrics };
}

/** The design edit's own refusals; every other one is guided setup's (`checkVariants`, `checkMetrics`). */
export const TWO_VARIANTS = 'An experiment needs at least two variants to start.';
export const ONE_CONTROL = 'Only one variant can be the control.';
export const ONE_PRIMARY = 'Only one metric can be the primary metric.';

/** The control a refusal is about, so the form can move focus there. */
export type DesignField =
  | { list: 'variants' | 'metrics'; index: -1; field: 'add' }
  | { list: 'variants'; index: number; field: 'name' | 'traffic_allocation' | 'is_control' | 'configuration' }
  | { list: 'metrics'; index: number; field: 'name' | 'event_name' | 'is_primary' };

export interface DesignProblem {
  message: string;
  field: DesignField;
}

function indexesWhere<T>(items: T[], test: (item: T) => boolean): number[] {
  return items.flatMap((item, i) => (test(item) ? [i] : []));
}

/** The field `checkVariants`' first problem is about, found in the order it checks. */
function variantProblemField(variants: VariantFormData[]): DesignField {
  const unnamed = variants.findIndex((v) => !v.name.trim());
  if (unnamed >= 0) return { list: 'variants', index: unnamed, field: 'name' };
  if (!variants.some((v) => v.is_control)) return { list: 'variants', index: 0, field: 'is_control' };
  if (allocationTotalOf(variants) !== 100) return { list: 'variants', index: 0, field: 'traffic_allocation' };
  const configuration = configurationProblems(variants).findIndex((p) => p !== null);
  return { list: 'variants', index: Math.max(configuration, 0), field: 'configuration' };
}

/** The field `checkMetrics`' first problem is about, found in the order it checks. */
function metricProblemField(metrics: MetricFormData[]): DesignField {
  if (metrics.length === 0) return { list: 'metrics', index: -1, field: 'add' };
  const blank = metrics.findIndex((m) => !m.name.trim() || !m.event_name.trim());
  if (blank >= 0) {
    return { list: 'metrics', index: blank, field: metrics[blank].name.trim() ? 'event_name' : 'name' };
  }
  const names = metrics.map((m) => m.name.trim());
  const repeat = names.findIndex((name, i) => names.indexOf(name) !== i);
  if (repeat >= 0) return { list: 'metrics', index: repeat, field: 'name' };
  return { list: 'metrics', index: 0, field: 'is_primary' };
}

/**
 * The design edit's first problem, or null when it can be sent. In order:
 * at least two variants (Start refuses fewer, and a scheduled draft would be
 * started without that check), guided setup's variant checks, exactly one
 * control, guided setup's metric checks, exactly one primary. The API accepts
 * one variant, no metric, two controls and two primaries, so only this stops
 * them.
 */
export function checkDesign(state: ExperimentFormState): DesignProblem | null {
  const { variants, metrics } = state;
  if (variants.length < 2) return { message: TWO_VARIANTS, field: { list: 'variants', index: -1, field: 'add' } };
  const variantProblem = checkVariants(variants);
  if (variantProblem !== null) return { message: variantProblem, field: variantProblemField(variants) };
  const controls = indexesWhere(variants, (v) => v.is_control);
  if (controls.length > 1) {
    return { message: ONE_CONTROL, field: { list: 'variants', index: controls[0], field: 'is_control' } };
  }
  const metricProblem = checkMetrics(metrics);
  if (metricProblem !== null) return { message: metricProblem, field: metricProblemField(metrics) };
  const primaries = indexesWhere(metrics, (m) => m.is_primary);
  if (primaries.length > 1) {
    return { message: ONE_PRIMARY, field: { list: 'metrics', index: primaries[0], field: 'is_primary' } };
  }
  return null;
}

// --- reducer -----------------------------------------------------------------

export type ExperimentFormAction =
  | { type: 'setName'; name: string }
  | { type: 'setKey'; key: string }
  | { type: 'setDescription'; description: string }
  | { type: 'setHypothesis'; hypothesis: string }
  | { type: 'setType'; experimentType: ExperimentType }
  | { type: 'setRules'; rules: TargetingRules }
  | { type: 'setConfidenceLevel'; confidenceLevel: number }
  | { type: 'setCorrectionMethod'; correctionMethod: CorrectionMethod }
  | { type: 'setBayesianEnabled'; bayesianEnabled: boolean }
  | { type: 'setPlannedEffect'; plannedEffectPct: string }
  | { type: 'addVariant' }
  | { type: 'removeVariant'; index: number }
  | {
      type: 'changeVariant';
      index: number;
      field: keyof VariantFormData;
      value: string | number | boolean;
    }
  | { type: 'setControl'; index: number }
  | { type: 'addMetric' }
  | { type: 'removeMetric'; index: number }
  | { type: 'changeMetric'; index: number; field: keyof MetricFormData; value: string | boolean }
  | { type: 'setPrimary'; index: number };

export function experimentFormReducer(
  state: ExperimentFormState,
  action: ExperimentFormAction,
): ExperimentFormState {
  switch (action.type) {
    case 'setName':
      return {
        ...state,
        name: action.name,
        key: state.keyEdited ? state.key : generateKey(action.name),
      };
    case 'setKey':
      return { ...state, key: action.key, keyEdited: true };
    case 'setDescription':
      return { ...state, description: action.description };
    case 'setHypothesis':
      return { ...state, hypothesis: action.hypothesis };
    case 'setType':
      return { ...state, type: action.experimentType };
    case 'setRules':
      return { ...state, rules: action.rules };
    case 'setConfidenceLevel':
      return { ...state, confidenceLevel: action.confidenceLevel };
    case 'setCorrectionMethod':
      return { ...state, correctionMethod: action.correctionMethod };
    case 'setBayesianEnabled':
      return { ...state, bayesianEnabled: action.bayesianEnabled };
    case 'setPlannedEffect':
      return { ...state, plannedEffectPct: action.plannedEffectPct };

    case 'addVariant':
      return {
        ...state,
        variants: [
          ...state.variants,
          {
            name: `Variant ${state.variants.length}`,
            description: '',
            traffic_allocation: 0,
            is_control: false,
          },
        ],
      };
    case 'removeVariant':
      return { ...state, variants: state.variants.filter((_, i) => i !== action.index) };
    case 'changeVariant':
      return {
        ...state,
        variants: state.variants.map((v, i) =>
          i === action.index ? { ...v, [action.field]: action.value } : v,
        ),
      };
    case 'setControl':
      return {
        ...state,
        variants: state.variants.map((v, i) => ({ ...v, is_control: i === action.index })),
      };

    case 'addMetric':
      return {
        ...state,
        metrics: [
          ...state.metrics,
          {
            name: '',
            event_name: '',
            metric_type: 'conversion',
            is_primary: state.metrics.length === 0,
          },
        ],
      };
    case 'removeMetric': {
      const next = state.metrics.filter((_, i) => i !== action.index);
      if (next.length > 0 && !next.some((m) => m.is_primary)) {
        next[0] = { ...next[0], is_primary: true };
      }
      return { ...state, metrics: next };
    }
    case 'changeMetric':
      return {
        ...state,
        metrics: state.metrics.map((m, i) =>
          i === action.index ? { ...m, [action.field]: action.value } : m,
        ),
      };
    case 'setPrimary':
      return {
        ...state,
        metrics: state.metrics.map((m, i) => ({ ...m, is_primary: i === action.index })),
      };
  }
}
