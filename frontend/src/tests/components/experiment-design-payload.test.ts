/**
 * The design edit's seed, body and checks (#1453), as pure functions.
 *
 * `PUT /api/v1/experiments/{id}` replaces both lists in one commit, and a
 * field left out of an item is reset to its default with a 200. So the body
 * must carry every item and every stored field, every time; these pin the
 * mapping. The page-level gates (every item, every field against the OpenAPI
 * snapshot, refusals with zero requests) are in
 * tests/pages/experiment-detail-design.test.tsx.
 */
import {
  buildDesignPayload,
  checkDesign,
  checkMetrics,
  checkVariants,
  configurationText,
  experimentFormReducer,
  ExperimentFormAction,
  ExperimentFormState,
  formStateFromExperiment,
  ONE_CONTROL,
  ONE_PRIMARY,
  TWO_VARIANTS,
} from '@/components/experiments/new/formState';
import { Experiment, ExperimentMetric, ExperimentVariant } from '@/types/experiments';

const STAMPS = { experiment_id: 'exp-1', created_at: '2026-10-01T10:00:00', updated_at: '2026-10-01T10:00:00' };

const variant = (overrides: Partial<ExperimentVariant>): ExperimentVariant => ({
  id: 'v',
  name: 'V',
  description: null,
  is_control: false,
  traffic_allocation: 50,
  configuration: null,
  ...STAMPS,
  ...overrides,
});

const metric = (overrides: Partial<ExperimentMetric>): ExperimentMetric => ({
  id: 'm',
  name: 'M',
  description: null,
  event_name: 'm',
  metric_type: 'conversion',
  is_primary: false,
  aggregation_method: 'average',
  minimum_sample_size: 100,
  expected_effect: null,
  event_value_path: null,
  lower_is_better: false,
  ...STAMPS,
  ...overrides,
});

const experiment = (variants: ExperimentVariant[], metrics: ExperimentMetric[]): Experiment => ({
  id: 'exp-1',
  name: 'Checkout',
  key: 'checkout',
  experiment_type: 'a_b',
  status: 'draft',
  targeting_rules: null,
  owner_id: null,
  start_date: null,
  end_date: null,
  created_at: STAMPS.created_at,
  updated_at: STAMPS.updated_at,
  variants,
  metrics,
});

/** Every stored field set, and set away from its default. */
const FULL = experiment(
  [
    variant({
      id: 'v-1',
      name: 'Control',
      description: 'The page as it is',
      is_control: true,
      traffic_allocation: 40,
      configuration: { color: 'grey', layout: { columns: 2 } },
    }),
    variant({
      id: 'v-2',
      name: 'Blue CTA',
      description: 'A blue button',
      traffic_allocation: 60,
      configuration: { color: 'blue' },
    }),
  ],
  [
    metric({
      id: 'm-1',
      name: 'Revenue',
      description: 'Order value',
      event_name: 'purchase',
      metric_type: 'revenue',
      is_primary: true,
      aggregation_method: 'sum',
      minimum_sample_size: 500,
      expected_effect: 0.1,
      event_value_path: 'amount',
      lower_is_better: true,
    }),
    metric({
      id: 'm-2',
      name: 'Errors',
      description: 'Client errors',
      event_name: 'error',
      metric_type: 'count',
      aggregation_method: 'median',
      minimum_sample_size: 250,
      expected_effect: 0.03,
      event_value_path: 'count',
      lower_is_better: true,
    }),
  ],
);

/** A stored item as the body sends it back: the server's own fields gone. */
function asSent<T extends { id: string; experiment_id?: string; created_at?: string; updated_at?: string }>(item: T) {
  const { id, experiment_id, created_at, updated_at, ...rest } = item;
  return rest;
}

const apply = (state: ExperimentFormState, actions: ExperimentFormAction[]) =>
  actions.reduce(experimentFormReducer, state);

describe('formStateFromExperiment', () => {
  it('seeds every variant, its configuration as indented JSON and a missing description as blank', () => {
    const state = formStateFromExperiment(
      experiment([FULL.variants[0], variant({ id: 'v-2', name: 'B', traffic_allocation: 60 })], FULL.metrics),
    );
    expect(state.variants).toEqual([
      {
        name: 'Control',
        description: 'The page as it is',
        traffic_allocation: 40,
        is_control: true,
        configuration_text: '{\n  "color": "grey",\n  "layout": {\n    "columns": 2\n  }\n}',
      },
      { name: 'B', description: '', traffic_allocation: 60, is_control: false, configuration_text: '' },
    ]);
    expect(configurationText(null)).toBe('');
  });

  it('carries the fields no editor shows; the primary metric’s planned effect is held apart', () => {
    const state = formStateFromExperiment(FULL);
    expect(state.designPlannedEffect).toBe(0.1);
    expect(state.metrics).toEqual([
      {
        name: 'Revenue',
        event_name: 'purchase',
        metric_type: 'revenue',
        is_primary: true,
        lower_is_better: true,
        carried: {
          description: 'Order value',
          aggregation_method: 'sum',
          minimum_sample_size: 500,
          expected_effect: null,
          event_value_path: 'amount',
        },
      },
      {
        name: 'Errors',
        event_name: 'error',
        metric_type: 'count',
        is_primary: false,
        lower_is_better: true,
        carried: {
          description: 'Client errors',
          aggregation_method: 'median',
          minimum_sample_size: 250,
          expected_effect: 0.03,
          event_value_path: 'count',
        },
      },
    ]);
  });

  it('with no primary metric stored, holds no planned effect', () => {
    const state = formStateFromExperiment(experiment(FULL.variants, [metric({ expected_effect: 0.2 })]));
    expect(state.designPlannedEffect).toBeUndefined();
    expect(state.metrics[0].carried?.expected_effect).toBe(0.2);
  });
});

describe('buildDesignPayload', () => {
  it('an unchanged form sends back exactly what is stored, every item and every field', () => {
    expect(buildDesignPayload(formStateFromExperiment(FULL))).toEqual({
      variants: FULL.variants.map(asSent),
      metrics: FULL.metrics.map(asSent),
    });
  });

  it('a stored null stays null for the Optional fields, and is left out for the two with defaults', () => {
    const sparse = experiment(
      [variant({ name: 'A', is_control: true }), variant({ name: 'B' })],
      [
        metric({
          name: 'Signups',
          is_primary: true,
          aggregation_method: undefined,
          minimum_sample_size: undefined,
          lower_is_better: undefined,
        }),
      ],
    );
    const body = buildDesignPayload(formStateFromExperiment(sparse));
    expect(body.variants).toEqual([
      { name: 'A', description: null, is_control: true, traffic_allocation: 50, configuration: null },
      { name: 'B', description: null, is_control: false, traffic_allocation: 50, configuration: null },
    ]);
    expect(body.metrics).toEqual([
      {
        name: 'Signups',
        description: null,
        event_name: 'm',
        metric_type: 'conversion',
        is_primary: true,
        expected_effect: null,
        event_value_path: null,
        lower_is_better: false,
      },
    ]);
    expect(body.metrics[0]).not.toHaveProperty('aggregation_method');
    expect(body.metrics[0]).not.toHaveProperty('minimum_sample_size');
  });

  it('an empty configuration object is sent as {}, not as none', () => {
    const body = buildDesignPayload(
      formStateFromExperiment(experiment([variant({ is_control: true, configuration: {} }), variant({})], FULL.metrics)),
    );
    expect(body.variants[0].configuration).toEqual({});
  });

  it('a variant and a metric added in the form carry every key, with the API defaults left to the API', () => {
    const state = apply(formStateFromExperiment(FULL), [
      { type: 'addVariant' },
      { type: 'addMetric' },
      { type: 'changeMetric', index: 2, field: 'name', value: ' Refunds ' },
      { type: 'changeMetric', index: 2, field: 'event_name', value: 'refund' },
    ]);
    const body = buildDesignPayload(state);
    expect(body.variants[2]).toEqual({
      name: 'Variant 2',
      description: null,
      is_control: false,
      traffic_allocation: 0,
      configuration: null,
    });
    expect(body.metrics[2]).toEqual({
      name: 'Refunds',
      description: null,
      event_name: 'refund',
      metric_type: 'conversion',
      is_primary: false,
      expected_effect: null,
      event_value_path: null,
      lower_is_better: false,
    });
  });

  it('every carried field survives an edit of its own row', () => {
    const state = apply(formStateFromExperiment(FULL), [
      { type: 'changeMetric', index: 1, field: 'name', value: 'Client errors' },
      { type: 'changeMetric', index: 1, field: 'lower_is_better', value: false },
    ]);
    expect(buildDesignPayload(state).metrics[1]).toEqual({
      ...asSent(FULL.metrics[1]),
      name: 'Client errors',
      lower_is_better: false,
    });
  });

  describe('the planned effect follows the primary metric (C7)', () => {
    it('moves to a new primary; the old one is left with none', () => {
      const state = apply(formStateFromExperiment(FULL), [{ type: 'setPrimary', index: 1 }]);
      const [revenue, errors] = buildDesignPayload(state).metrics;
      expect([revenue.is_primary, revenue.expected_effect]).toEqual([false, null]);
      expect([errors.is_primary, errors.expected_effect]).toEqual([true, 0.1]);
      // Nothing else on either metric moves.
      expect(revenue).toEqual({ ...asSent(FULL.metrics[0]), is_primary: false, expected_effect: null });
      expect(errors).toEqual({ ...asSent(FULL.metrics[1]), is_primary: true, expected_effect: 0.1 });
    });

    it('moves to the metric that becomes primary when the primary is removed', () => {
      const withThird = experiment(FULL.variants, [...FULL.metrics, metric({ id: 'm-3', name: 'Visits' })]);
      const state = apply(formStateFromExperiment(withThird), [{ type: 'removeMetric', index: 0 }]);
      expect(buildDesignPayload(state).metrics.map((m) => [m.name, m.is_primary, m.expected_effect])).toEqual([
        ['Errors', true, 0.1],
        ['Visits', false, null],
      ]);
    });

    it('a stored primary with no planned effect leaves the new primary with none', () => {
      const unplanned = experiment(FULL.variants, [
        { ...FULL.metrics[0], expected_effect: null },
        FULL.metrics[1],
      ]);
      const state = apply(formStateFromExperiment(unplanned), [{ type: 'setPrimary', index: 1 }]);
      expect(buildDesignPayload(state).metrics.map((m) => m.expected_effect)).toEqual([null, null]);
    });

    it('with no stored primary, the chosen primary keeps its own', () => {
      const state = apply(
        formStateFromExperiment(experiment(FULL.variants, [metric({ name: 'A', expected_effect: 0.2 })])),
        [{ type: 'setPrimary', index: 0 }],
      );
      expect(buildDesignPayload(state).metrics[0].expected_effect).toBe(0.2);
    });
  });
});

describe('checkDesign', () => {
  const base = () => formStateFromExperiment(FULL);
  const check = (actions: ExperimentFormAction[], start: ExperimentFormState = base()) =>
    checkDesign(apply(start, actions));

  it('accepts the stored design', () => {
    expect(checkDesign(base())).toBeNull();
  });

  it('refuses one variant, saying an experiment needs two to start', () => {
    expect(check([{ type: 'removeVariant', index: 1 }])).toEqual({
      message: TWO_VARIANTS,
      field: { list: 'variants', index: -1, field: 'add' },
    });
    expect(TWO_VARIANTS).toBe('An experiment needs at least two variants to start.');
  });

  // Guided setup's own words: each expected message is what checkVariants or
  // checkMetrics itself returns for the same lists, never a copy of it.
  it.each<[string, ExperimentFormAction[], 'variants' | 'metrics', object]>([
    ['a blank variant name', [{ type: 'changeVariant', index: 1, field: 'name', value: ' ' }], 'variants', { index: 1, field: 'name' }],
    ['no control', [{ type: 'changeVariant', index: 0, field: 'is_control', value: false }], 'variants', { index: 0, field: 'is_control' }],
    ['a total other than 100', [{ type: 'changeVariant', index: 1, field: 'traffic_allocation', value: 50 }], 'variants', { index: 0, field: 'traffic_allocation' }],
    ['a configuration that is not an object', [{ type: 'changeVariant', index: 1, field: 'configuration_text', value: '[1]' }], 'variants', { index: 1, field: 'configuration' }],
    ['a blank metric name', [{ type: 'changeMetric', index: 1, field: 'name', value: '' }], 'metrics', { index: 1, field: 'name' }],
    ['a blank event name', [{ type: 'changeMetric', index: 1, field: 'event_name', value: '' }], 'metrics', { index: 1, field: 'event_name' }],
    ['a repeated metric name', [{ type: 'changeMetric', index: 1, field: 'name', value: ' Revenue ' }], 'metrics', { index: 1, field: 'name' }],
    ['no metric', [{ type: 'removeMetric', index: 1 }, { type: 'removeMetric', index: 0 }], 'metrics', { index: -1, field: 'add' }],
  ])('refuses %s with guided setup’s words', (_name, actions, list, field) => {
    const state = apply(base(), actions);
    const expected = list === 'variants' ? checkVariants(state.variants) : checkMetrics(state.metrics);
    expect(expected).not.toBeNull();
    expect(checkDesign(state)).toEqual({ message: expected, field: { list, ...field } });
  });

  it('refuses no primary metric with guided setup’s words', () => {
    const noPrimary = formStateFromExperiment(experiment(FULL.variants, [metric({ name: 'A' }), metric({ name: 'B' })]));
    expect(checkDesign(noPrimary)).toEqual({
      message: checkMetrics(noPrimary.metrics),
      field: { list: 'metrics', index: 0, field: 'is_primary' },
    });
    expect(checkMetrics(noPrimary.metrics)).toBe('Choose a primary metric.');
  });

  it('refuses two controls and two primaries, which only a stored design can hold', () => {
    const twoControls = formStateFromExperiment(
      experiment([variant({ name: 'A', is_control: true }), variant({ name: 'B', is_control: true })], FULL.metrics),
    );
    expect(checkDesign(twoControls)).toEqual({
      message: ONE_CONTROL,
      field: { list: 'variants', index: 0, field: 'is_control' },
    });
    const twoPrimaries = formStateFromExperiment(
      experiment(FULL.variants, [metric({ name: 'A', is_primary: true }), metric({ name: 'B', is_primary: true })]),
    );
    expect(checkDesign(twoPrimaries)).toEqual({
      message: ONE_PRIMARY,
      field: { list: 'metrics', index: 0, field: 'is_primary' },
    });
    // Choosing one in the form clears either.
    expect(checkDesign(apply(twoControls, [{ type: 'setControl', index: 1 }]))).toBeNull();
    expect(checkDesign(apply(twoPrimaries, [{ type: 'setPrimary', index: 1 }]))).toBeNull();
  });

  it('checks the variants before the metrics, as guided setup does', () => {
    const problem = check([
      { type: 'changeVariant', index: 1, field: 'name', value: '' },
      { type: 'changeMetric', index: 1, field: 'name', value: '' },
    ]);
    expect(problem?.field.list).toBe('variants');
  });
});
