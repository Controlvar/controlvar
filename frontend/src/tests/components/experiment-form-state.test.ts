import { TextEncoder as NodeTextEncoder } from 'util';
import {
  buildCreatePayload,
  CONFIGURATION_MAX_BYTES,
  CONFIGURATION_NOT_JSON,
  CONFIGURATION_NOT_OBJECT,
  configurationTooLong,
  parseConfiguration,
  utf8ByteLength,
  checkMetrics,
  checkName,
  checkVariants,
  createInitialFormState,
  experimentFormReducer,
  ExperimentFormAction,
  ExperimentFormState,
  FORM_STEPS,
  INITIAL_FORM_STATE,
  MetricFormData,
  validateForm,
  validateStep,
  VariantFormData,
} from '@/components/experiments/new/formState';
import { ESTIMATE_PROBLEMS } from '@/components/experiments/new/estimate';

const apply = (actions: ExperimentFormAction[], start: ExperimentFormState = createInitialFormState()) =>
  actions.reduce(experimentFormReducer, start);

describe('INITIAL_FORM_STATE', () => {
  it('holds the form defaults: two 50/50 variants, one primary conversion metric, no rules', () => {
    expect(INITIAL_FORM_STATE).toEqual({
      name: '',
      key: '',
      keyEdited: false,
      description: '',
      hypothesis: '',
      type: 'a_b',
      rules: { logical_operator: 'AND', groups: [] },
      variants: [
        { name: 'Control', description: '', traffic_allocation: 50, is_control: true },
        { name: 'Treatment', description: '', traffic_allocation: 50, is_control: false },
      ],
      metrics: [{ name: 'Conversion', event_name: 'conversion', metric_type: 'conversion', is_primary: true }],
      confidenceLevel: 0.95,
      correctionMethod: 'benjamini_hochberg',
      bayesianEnabled: false,
      sequentialEnabled: false,
      plannedEffectPct: '',
    });
  });

  it('createInitialFormState returns a fresh rules object each time', () => {
    expect(createInitialFormState().rules).not.toBe(createInitialFormState().rules);
  });
});

describe('experimentFormReducer', () => {
  it('derives the key from the name until the key is edited', () => {
    let s = apply([{ type: 'setName', name: 'Pricing Page Test' }]);
    expect(s.key).toBe('pricing_page_test');
    s = apply([{ type: 'setKey', key: 'custom-key' }, { type: 'setName', name: 'Another' }], s);
    expect(s).toMatchObject({ name: 'Another', key: 'custom-key', keyEdited: true });
  });

  it('sets description, hypothesis, type and rules', () => {
    const rules = { logical_operator: 'OR' as const, groups: [] };
    const s = apply([
      { type: 'setDescription', description: 'd' },
      { type: 'setHypothesis', hypothesis: 'h' },
      { type: 'setType', experimentType: 'mv' },
      { type: 'setRules', rules },
    ]);
    expect(s).toMatchObject({ description: 'd', hypothesis: 'h', type: 'mv' });
    expect(s.rules).toBe(rules);
  });

  it('adds a variant named after its index with 0% and no control', () => {
    const s = apply([{ type: 'addVariant' }]);
    expect(s.variants[2]).toEqual({ name: 'Variant 2', description: '', traffic_allocation: 0, is_control: false });
  });

  it('changes one variant field, moves control, and removes a variant', () => {
    let s = apply([
      { type: 'addVariant' },
      { type: 'changeVariant', index: 1, field: 'name', value: 'B' },
      { type: 'changeVariant', index: 1, field: 'traffic_allocation', value: 30 },
      { type: 'setControl', index: 2 },
    ]);
    expect(s.variants.map((v) => [v.name, v.traffic_allocation, v.is_control])).toEqual([
      ['Control', 50, false],
      ['B', 30, false],
      ['Variant 2', 0, true],
    ]);
    s = apply([{ type: 'removeVariant', index: 0 }], s);
    expect(s.variants.map((v) => v.name)).toEqual(['B', 'Variant 2']);
  });

  it('a new metric is primary only when it is the first', () => {
    let s = apply([{ type: 'addMetric' }]);
    expect(s.metrics[1]).toEqual({ name: '', event_name: '', metric_type: 'conversion', is_primary: false });
    s = apply([{ type: 'removeMetric', index: 0 }, { type: 'removeMetric', index: 0 }, { type: 'addMetric' }]);
    expect(s.metrics).toEqual([{ name: '', event_name: '', metric_type: 'conversion', is_primary: true }]);
  });

  it('removing the primary metric promotes the first remaining one', () => {
    const s = apply([{ type: 'addMetric' }, { type: 'addMetric' }, { type: 'removeMetric', index: 0 }]);
    expect(s.metrics.map((m) => m.is_primary)).toEqual([true, false]);
  });

  it('removing a non-primary metric leaves the primary where it is', () => {
    const s = apply([{ type: 'addMetric' }, { type: 'addMetric' }, { type: 'setPrimary', index: 2 }, { type: 'removeMetric', index: 0 }]);
    expect(s.metrics.map((m) => m.is_primary)).toEqual([false, true]);
  });

  it('changes one metric field and moves primary', () => {
    const s = apply([
      { type: 'addMetric' },
      { type: 'changeMetric', index: 1, field: 'event_name', value: 'purchase' },
      { type: 'changeMetric', index: 1, field: 'metric_type', value: 'revenue' },
      { type: 'setPrimary', index: 1 },
    ]);
    expect(s.metrics[1]).toEqual({ name: '', event_name: 'purchase', metric_type: 'revenue', is_primary: true });
    expect(s.metrics[0].is_primary).toBe(false);
  });

  it('never mutates the state it is given', () => {
    const start = createInitialFormState();
    const snapshot = JSON.stringify(start);
    apply(
      [
        { type: 'setName', name: 'x' },
        { type: 'addVariant' },
        { type: 'changeVariant', index: 0, field: 'name', value: 'y' },
        { type: 'setControl', index: 1 },
        { type: 'addMetric' },
        { type: 'removeMetric', index: 0 },
        { type: 'changeMetric', index: 0, field: 'name', value: 'z' },
      ],
      start,
    );
    expect(JSON.stringify(start)).toBe(snapshot);
  });
});

// --- validation ------------------------------------------------------------

const V = (over: Partial<VariantFormData> = {}): VariantFormData => ({
  name: 'Control',
  description: '',
  traffic_allocation: 50,
  is_control: true,
  ...over,
});
const M = (over: Partial<MetricFormData> = {}): MetricFormData => ({
  name: 'Conv',
  event_name: 'purchase',
  metric_type: 'conversion',
  is_primary: true,
  ...over,
});

const GOOD_VARIANTS = [V(), V({ name: 'B', is_control: false })];
const GOOD_METRICS = [M()];

const NAME_CASES: Array<[string, string]> = [
  ['X', 'ok'],
  ['   ', 'blank'],
];
const VARIANT_CASES: Array<[VariantFormData[], string]> = [
  [GOOD_VARIANTS, 'ok'],
  [[], 'none'],
  [[V(), V({ name: ' ', is_control: false })], 'unnamed'],
  [[V({ is_control: false }), V({ name: 'B', is_control: false })], 'no control'],
  [[V(), V({ name: 'B', is_control: false, traffic_allocation: 30 })], '80%'],
];
const METRIC_CASES: Array<[MetricFormData[], string]> = [
  [GOOD_METRICS, 'ok'],
  [[], 'none'],
  [[M({ event_name: '' })], 'no event'],
  [[M({ name: '' })], 'no name'],
  [[M({ is_primary: false })], 'no primary'],
  [[M(), M({ name: ' Conv ', event_name: 'other', is_primary: false })], 'repeated name'],
];

const TABLE = NAME_CASES.flatMap(([name, n]) =>
  VARIANT_CASES.flatMap(([variants, v]) =>
    METRIC_CASES.map(([metrics, m]) => ({
      label: `name ${n}, variants ${v}, metrics ${m}`,
      state: { ...createInitialFormState(), name, variants, metrics },
    })),
  ),
);

describe('validateForm and its three checks', () => {
  it('keeps every message word for word', () => {
    expect(checkName('')).toBe('Name is required.');
    expect(checkVariants([])).toBe('Add at least one variant.');
    expect(checkVariants([V({ name: '' })])).toBe('Every variant needs a name.');
    expect(checkVariants([V({ is_control: false, traffic_allocation: 100 })])).toBe(
      'One variant must be marked as control.',
    );
    expect(checkVariants([V({ traffic_allocation: 80 })])).toBe(
      'Variant allocations must add up to 100% (currently 80%).',
    );
    expect(checkMetrics([])).toBe('Add at least one metric.');
    expect(checkMetrics([M({ event_name: ' ' })])).toBe('Every metric needs a name and an event name.');
    expect(checkMetrics([M({ is_primary: false })])).toBe('Choose a primary metric.');
    expect(checkMetrics([M(), M({ is_primary: false })])).toBe('Metric names must be unique.');
  });

  it('compares metric names as they are sent: trimmed, and case-sensitive like the API', () => {
    expect(checkMetrics([M(), M({ name: '  Conv', is_primary: false })])).toBe('Metric names must be unique.');
    expect(checkMetrics([M(), M({ name: 'conv', is_primary: false })])).toBeNull();
    expect(checkMetrics([M(), M({ name: 'Revenue', event_name: 'purchase', is_primary: false })])).toBeNull();
  });

  it.each(TABLE)('$label: validateForm is the first of name, variants, metrics', ({ state }) => {
    expect(validateForm(state.name, state.variants, state.metrics)).toBe(
      checkName(state.name) ?? checkVariants(state.variants) ?? checkMetrics(state.metrics),
    );
  });
});

describe('validateStep against validateForm', () => {
  // Review re-runs the whole-form check, so it would make "every step passes"
  // true of any valid form by itself. The invariants below are about the steps
  // that come BEFORE review: those alone must catch everything validateForm does.
  const STEPS_BEFORE_REVIEW = FORM_STEPS.filter((step) => step !== 'review');
  const firstFailingStep = (state: ExperimentFormState) =>
    STEPS_BEFORE_REVIEW.map((step) => validateStep(step, state)).find((p) => p !== null) ?? null;

  it('has the five steps in order', () => {
    expect(FORM_STEPS).toEqual(['type', 'details', 'variants', 'estimate', 'review']);
  });

  it.each(TABLE)('$label: the form is valid exactly when every step before review is', ({ state }) => {
    const whole = validateForm(state.name, state.variants, state.metrics);
    const steps = STEPS_BEFORE_REVIEW.map((step) => validateStep(step, state));
    expect(whole === null).toBe(steps.every((p) => p === null));
  });

  it.each(TABLE)('$label: every step message is one validateForm can give', ({ state }) => {
    const allowed = [checkName(state.name), checkVariants(state.variants), checkMetrics(state.metrics)];
    for (const step of FORM_STEPS) {
      const problem = validateStep(step, state);
      if (problem !== null) expect(allowed).toContain(problem);
    }
  });

  it('review is the whole-form check', () => {
    for (const { state } of TABLE) {
      expect(validateStep('review', state)).toBe(validateForm(state.name, state.variants, state.metrics));
    }
  });

  // Where the form has one problem, or its problems fall in step order, the
  // first failing step names the same problem validateForm does. The single
  // exception is below: details (step 2) asks for metrics before variants
  // (step 3), while validateForm checks variants first.
  const agreeing = TABLE.filter(
    ({ state }) => !(checkName(state.name) === null && checkVariants(state.variants) && checkMetrics(state.metrics)),
  );
  it.each(agreeing)('$label: the first failing step gives validateForm’s message', ({ state }) => {
    expect(firstFailingStep(state)).toBe(validateForm(state.name, state.variants, state.metrics));
  });

  it('with a good name and both variants and metrics wrong, details reports the metrics problem first', () => {
    const disagreeing = TABLE.filter((row) => !agreeing.includes(row));
    expect(disagreeing).toHaveLength(20);
    for (const { state } of disagreeing) {
      expect(firstFailingStep(state)).toBe(checkMetrics(state.metrics));
      expect(validateForm(state.name, state.variants, state.metrics)).toBe(checkVariants(state.variants));
    }
  });
});

// --- payload -------------------------------------------------------------------

describe('buildCreatePayload', () => {
  // The same inputs as the page test "POSTs an ExperimentCreate payload"
  // (experiment-new.test.tsx), and the same expected body.
  const pageTestState = () =>
    apply([
      { type: 'setName', name: 'Pricing Page Test' },
      { type: 'setDescription', description: 'Does the new layout convert?' },
      { type: 'setType', experimentType: 'mv' },
      { type: 'changeVariant', index: 1, field: 'name', value: 'New layout' },
      { type: 'changeMetric', index: 0, field: 'name', value: 'Purchase' },
      { type: 'changeMetric', index: 0, field: 'event_name', value: 'purchase' },
      { type: 'addMetric' },
      { type: 'changeMetric', index: 1, field: 'name', value: 'Revenue' },
      { type: 'changeMetric', index: 1, field: 'event_name', value: 'purchase' },
      { type: 'changeMetric', index: 1, field: 'metric_type', value: 'revenue' },
    ]);

  it('matches the page test literal', () => {
    expect(buildCreatePayload(pageTestState())).toEqual({
      name: 'Pricing Page Test',
      key: 'pricing_page_test',
      description: 'Does the new layout convert?',
      hypothesis: undefined,
      experiment_type: 'mv',
      targeting_rules: null,
      variants: [
        { name: 'Control', description: undefined, is_control: true, traffic_allocation: 50 },
        { name: 'New layout', description: undefined, is_control: false, traffic_allocation: 50 },
      ],
      metrics: [
        { name: 'Purchase', event_name: 'purchase', metric_type: 'conversion', is_primary: true },
        { name: 'Revenue', event_name: 'purchase', metric_type: 'revenue', is_primary: false },
      ],
      confidence_level: 0.95,
      correction_method: 'benjamini_hochberg',
    });
  });

  it('sends the confidence level and correction chosen (#580)', () => {
    const s = apply(
      [
        { type: 'setConfidenceLevel', confidenceLevel: 0.9 },
        { type: 'setCorrectionMethod', correctionMethod: 'none' },
      ],
      pageTestState(),
    );
    expect(buildCreatePayload(s)).toMatchObject({ confidence_level: 0.9, correction_method: 'none' });
  });

  it('sends exactly these ten keys and nothing else', () => {
    // toEqual ignores keys whose value is undefined; this does not.
    expect(Object.keys(buildCreatePayload(pageTestState())).sort()).toEqual(
      [
        'name',
        'key',
        'description',
        'hypothesis',
        'experiment_type',
        'targeting_rules',
        'variants',
        'metrics',
        'confidence_level',
        'correction_method',
      ].sort(),
    );
  });

  it('trims every text field and drops empty optional ones', () => {
    const s = apply([
      { type: 'setName', name: '  Spaced  ' },
      { type: 'setKey', key: '  my_key  ' },
      { type: 'setDescription', description: '   ' },
      { type: 'setHypothesis', hypothesis: '  We believe  ' },
      { type: 'changeVariant', index: 0, field: 'description', value: '  base  ' },
      { type: 'changeMetric', index: 0, field: 'name', value: '  Conv  ' },
      { type: 'changeMetric', index: 0, field: 'event_name', value: '  buy  ' },
    ]);
    const p = buildCreatePayload(s);
    expect(p).toMatchObject({ name: 'Spaced', key: 'my_key', description: undefined, hypothesis: 'We believe' });
    expect(p.variants[0].description).toBe('base');
    expect(p.metrics[0]).toMatchObject({ name: 'Conv', event_name: 'buy' });
  });

  it('falls back to a generated key, and to none when the name has no usable characters', () => {
    const edited = apply([{ type: 'setName', name: 'Checkout CTA' }, { type: 'setKey', key: '' }]);
    expect(buildCreatePayload(edited).key).toBe('checkout_cta');
    expect(buildCreatePayload(apply([{ type: 'setName', name: '!!!' }])).key).toBeUndefined();
  });

  it('sends targeting rules only when there is at least one group, and an unreadable allocation as 0', () => {
    const rules = {
      logical_operator: 'AND' as const,
      groups: [{ id: 'g1', logical_operator: 'AND' as const, conditions: [] }],
    };
    const s = apply([
      { type: 'setRules', rules },
      { type: 'changeVariant', index: 1, field: 'traffic_allocation', value: Number.NaN },
    ]);
    const p = buildCreatePayload(s);
    expect(p.targeting_rules).toBe(rules);
    expect(p.variants[1].traffic_allocation).toBe(0);
  });
});

// --- the planned effect (#1257) -------------------------------------------------

describe('the minimum detectable effect from the Estimate step', () => {
  const twoMetrics = (plannedEffectPct: string) =>
    apply([
      { type: 'setName', name: 'Checkout' },
      { type: 'changeMetric', index: 0, field: 'name', value: 'Purchase' },
      { type: 'changeMetric', index: 0, field: 'event_name', value: 'purchase' },
      { type: 'addMetric' },
      { type: 'changeMetric', index: 1, field: 'name', value: 'Refund' },
      { type: 'changeMetric', index: 1, field: 'event_name', value: 'refund' },
      { type: 'setPlannedEffect', plannedEffectPct },
    ]);

  it('is saved as the primary metric’s expected_effect, as a fraction, and on no other metric', () => {
    const p = buildCreatePayload(twoMetrics('10'));
    expect(p.metrics).toStrictEqual([
      { name: 'Purchase', event_name: 'purchase', metric_type: 'conversion', is_primary: true, expected_effect: 0.1 },
      { name: 'Refund', event_name: 'refund', metric_type: 'conversion', is_primary: false },
    ]);
  });

  it('follows the primary metric when another one is made primary', () => {
    const s = apply([{ type: 'setPrimary', index: 1 }], twoMetrics('7'));
    const p = buildCreatePayload(s);
    expect(p.metrics.map((m) => [m.name, m.is_primary, m.expected_effect])).toEqual([
      ['Purchase', false, undefined],
      ['Refund', true, 0.07],
    ]);
    expect(Object.keys(p.metrics[0])).not.toContain('expected_effect');
  });

  it.each(['', '   ', 'abc', '0.05', '-5', '100', '150'])(
    'sends nothing for %j: the body is byte for byte the one without an effect',
    (typed) => {
      const without = JSON.stringify(buildCreatePayload(twoMetrics('')));
      expect(JSON.stringify(buildCreatePayload(twoMetrics(typed)))).toBe(without);
      expect(without).not.toContain('expected_effect');
    },
  );

  it('the Estimate step refuses an effect that is typed but cannot be saved, and accepts blank', () => {
    const at = (typed: string) => validateStep('estimate', apply([{ type: 'setPlannedEffect', plannedEffectPct: typed }]));
    expect(at('')).toBeNull();
    expect(at('0.1')).toBeNull();
    expect(at('99.9')).toBeNull();
    expect(at('abc')).toBe(ESTIMATE_PROBLEMS.mde);
    expect(at('0.05')).toBe(ESTIMATE_PROBLEMS.mde);
    expect(at('100')).toBe(ESTIMATE_PROBLEMS.mdeHigh);
  });
});

// --- variant configuration (#442) ---------------------------------------------

/** A JSON object of exactly `bytes` UTF-8 bytes whose last character is `last`. */
function objectOfBytes(bytes: number, last: string): string {
  const head = '{"a":"';
  const tail = '"}';
  const lastBytes = utf8ByteLength(last);
  return head + 'x'.repeat(bytes - head.length - tail.length - lastBytes) + last + tail;
}

describe('utf8ByteLength', () => {
  it.each([
    ['', 0],
    ['abc', 3],
    ['é', 2],
    ['✓', 3],
    ['😀', 4],
    ['a😀é', 7],
    ['\ud800', 3], // a lone surrogate is sent as U+FFFD
  ])('%j is %i bytes', (text, bytes) => {
    expect(utf8ByteLength(text)).toBe(bytes);
  });

  it('agrees with Node’s UTF-8 encoder', () => {
    const encoder = new NodeTextEncoder();
    for (const text of ['plain', 'Café ✓', '😀😀', '{"k":"日本語"}']) {
      expect(utf8ByteLength(text)).toBe(encoder.encode(text).length);
    }
  });
});

describe('parseConfiguration', () => {
  it('reads blank and whitespace as no configuration', () => {
    expect(parseConfiguration(undefined)).toEqual({ ok: true, value: undefined });
    expect(parseConfiguration('')).toEqual({ ok: true, value: undefined });
    expect(parseConfiguration('  \n\t')).toEqual({ ok: true, value: undefined });
  });

  it('accepts any JSON object, including {} and nesting', () => {
    expect(parseConfiguration('{}')).toEqual({ ok: true, value: {} });
    expect(parseConfiguration(' {"a": {"b": [1, 2]}, "c": null} ')).toEqual({
      ok: true,
      value: { a: { b: [1, 2] }, c: null },
    });
  });

  it.each([
    ['[]', CONFIGURATION_NOT_OBJECT],
    ['[1, 2]', CONFIGURATION_NOT_OBJECT],
    ['"blue"', CONFIGURATION_NOT_OBJECT],
    ['1', CONFIGURATION_NOT_OBJECT],
    ['true', CONFIGURATION_NOT_OBJECT],
    ['null', CONFIGURATION_NOT_OBJECT],
    ['{"a": 1,}', CONFIGURATION_NOT_JSON],
    ["{'a': 1}", CONFIGURATION_NOT_JSON],
    ['{"a": 1', CONFIGURATION_NOT_JSON],
    ['blue', CONFIGURATION_NOT_JSON],
  ])('refuses %s', (text, problem) => {
    expect(parseConfiguration(text)).toEqual({ ok: false, problem });
  });

  it('never shows the parser’s own message', () => {
    const result = parseConfiguration('{"a": 1,}');
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.problem).not.toMatch(/unexpected|token|position|SyntaxError/i);
    }
  });

  describe('the byte limit is counted in UTF-8 bytes, not characters', () => {
    it('accepts exactly CONFIGURATION_MAX_BYTES bytes', () => {
      const text = objectOfBytes(CONFIGURATION_MAX_BYTES, 'x');
      expect(utf8ByteLength(text)).toBe(CONFIGURATION_MAX_BYTES);
      expect(parseConfiguration(text).ok).toBe(true);
    });

    it('accepts exactly the limit when it ends in a two-byte character', () => {
      const text = objectOfBytes(CONFIGURATION_MAX_BYTES, 'é');
      expect(utf8ByteLength(text)).toBe(CONFIGURATION_MAX_BYTES);
      expect(text.length).toBe(CONFIGURATION_MAX_BYTES - 1);
      expect(parseConfiguration(text).ok).toBe(true);
    });

    it('refuses one byte over, even when it is fewer characters than the limit', () => {
      // The last 'x' of an exact-limit text becomes 'é': one byte more, the same character count.
      const exact = objectOfBytes(CONFIGURATION_MAX_BYTES, 'x');
      const over = exact.slice(0, -3) + 'é' + exact.slice(-2);
      expect(over.length).toBe(CONFIGURATION_MAX_BYTES);
      expect(utf8ByteLength(over)).toBe(CONFIGURATION_MAX_BYTES + 1);
      expect(parseConfiguration(over)).toEqual({
        ok: false,
        problem: configurationTooLong(CONFIGURATION_MAX_BYTES + 1),
      });
    });

    it('says the limit is the dashboard’s and that the API accepts more', () => {
      expect(configurationTooLong(16_385)).toBe(
        'This configuration is 16,385 bytes. The dashboard accepts up to 16,384 bytes; the API accepts larger ones.',
      );
    });
  });
});

describe('variant configuration in validation and the payload', () => {
  const withConfig = (texts: (string | undefined)[]) =>
    apply(texts.map((value, index) => ({ type: 'changeVariant', index, field: 'configuration_text', value: value ?? '' }) as ExperimentFormAction));

  it('checkVariants names the variant and the problem, after the allocation check', () => {
    const s = withConfig(['', '[1]']);
    expect(checkVariants(s.variants)).toBe(
      `The configuration of “Treatment” needs fixing: ${CONFIGURATION_NOT_OBJECT}`,
    );
    const both = apply([{ type: 'changeVariant', index: 0, field: 'traffic_allocation', value: 10 }], s);
    expect(checkVariants(both.variants)).toMatch(/add up to 100%/);
    expect(validateStep('variants', s)).toBe(checkVariants(s.variants));
  });

  it('sends each configuration as the object typed, and leaves the key out when blank', () => {
    const typed = '{"copy": "Café ✓", "layout": {"columns": 2, "tags": ["a", "b"]}, "on": true, "ratio": 1.5, "none": null}';
    const p = buildCreatePayload(withConfig(['   ', typed]));
    expect('configuration' in p.variants[0]).toBe(false);
    expect(p.variants[1].configuration).toStrictEqual(JSON.parse(typed));
    expect(typeof p.variants[1].configuration).toBe('object');
  });

  it('sends {} as {}, not as no configuration', () => {
    expect(buildCreatePayload(withConfig(['{}'])).variants[0].configuration).toStrictEqual({});
  });

  it('sends bayesian_enabled: true only when it is turned on', () => {
    const off = buildCreatePayload(createInitialFormState());
    expect('bayesian_enabled' in off).toBe(false);
    const on = buildCreatePayload(apply([{ type: 'setBayesianEnabled', bayesianEnabled: true }]));
    expect(on.bayesian_enabled).toBe(true);
    expect('bayesian_config' in on).toBe(false);
  });

  it('sends sequential_testing_enabled: true only when it is turned on, and never a config', () => {
    const off = buildCreatePayload(createInitialFormState());
    expect('sequential_testing_enabled' in off).toBe(false);
    const on = buildCreatePayload(apply([{ type: 'setSequentialEnabled', sequentialEnabled: true }]));
    expect(on.sequential_testing_enabled).toBe(true);
    expect('sequential_testing_config' in on).toBe(false);
    const offAgain = buildCreatePayload(
      apply([
        { type: 'setSequentialEnabled', sequentialEnabled: true },
        { type: 'setSequentialEnabled', sequentialEnabled: false },
      ]),
    );
    expect('sequential_testing_enabled' in offAgain).toBe(false);
  });
});
