/**
 * "Set weights by hand" on the Current traffic weights panel (#1957). Who is
 * offered it is the experiment page's test (experiment-detail-bandit.test.tsx,
 * one non-superuser per role); here an ADMIN uses it: on which statuses it is
 * offered, what the form opens with, the exact request (each whole percentage
 * divided by 100, keyed by the experiment's own variant ids), when Save is
 * refused, what the panel shows after a save, fixed error copy whatever the
 * server says, focus, and axe.
 */
import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import axe from 'axe-core';
import refusal from '@/tests/fixtures/demo-read-only-refusal.json';
import { BanditWeightsSection } from '@/components/experiments/BanditWeightsSection';
import {
  OVERRIDE_ALWAYS,
  OVERRIDE_ERROR_COPY,
  OVERRIDE_INTRO,
  OVERRIDE_INVALID,
  OVERRIDE_ROLE_NOTE,
  OVERRIDE_SAVED,
  OVERRIDE_STATUS_COPY,
  largestRemainder,
  overrideErrorCopy,
  parsePercent,
  prefillPercents,
} from '@/components/experiments/BanditWeightsOverride';
import { ApiError, ApiFetchOptions, apiFetch } from '@/services/api';
import { shapeErrors } from '@/tests/helpers/openapiShape';
import { BanditStatus } from '@/types/bandit';
import { Experiment, ExperimentStatus } from '@/types/experiments';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

/** A resolved ADMIN who is not a superuser: the role alone must admit them. */
const ADMIN = { role: 'ADMIN', is_superuser: false };

const EXPERIMENT: Experiment = {
  id: 'exp-1',
  name: 'Recommendation algorithm',
  key: 'recommendation-algorithm',
  description: null,
  hypothesis: null,
  experiment_type: 'bandit',
  optimization_type: 'thompson_sampling',
  status: 'active',
  targeting_rules: null,
  tags: null,
  owner_id: 'user-1',
  start_date: '2026-09-01T10:00:00Z',
  end_date: null,
  created_at: '2026-09-01T10:00:00Z',
  updated_at: '2026-09-01T10:00:00Z',
  variants: [
    { id: 'v-1', name: 'algo_v1', is_control: true, traffic_allocation: 50 },
    { id: 'v-2', name: 'algo_v2', is_control: false, traffic_allocation: 30 },
    { id: 'v-3', name: 'algo_v3', is_control: false, traffic_allocation: 20 },
  ],
  metrics: [],
};

/** After an update: plain rounding of these opens the form at 99. */
const UPDATED: BanditStatus = {
  experiment_id: 'exp-1',
  algorithm: 'thompson_sampling',
  current_weights: [
    { variant_id: 'v-1', variant_name: 'algo_v1', current_weight: 0.3333, successes: 61, pulls: 1200, conversion_rate: 0.050833 },
    { variant_id: 'v-2', variant_name: 'algo_v2', current_weight: 0.3333, successes: 90, pulls: 1000, conversion_rate: 0.09 },
    { variant_id: 'v-3', variant_name: 'algo_v3', current_weight: 0.3334, successes: 66, pulls: 1100, conversion_rate: 0.06 },
  ],
  total_pulls: 3300,
  recommendation: 'EXPLORING',
  last_updated: '2026-10-04T09:30:00+00:00',
};

/** Before the first update the server fills in an even split it is not using. */
const NEVER_UPDATED: BanditStatus = {
  ...UPDATED,
  current_weights: UPDATED.current_weights.map((w) => ({
    ...w,
    current_weight: 1 / 3,
    successes: 0,
    pulls: 0,
    conversion_rate: 0,
  })),
  total_pulls: 0,
  last_updated: null,
};

/** The PUT's answer: the new weights, with the counts at 0 until the next update. */
const SAVED_ANSWER: BanditStatus = {
  experiment_id: 'exp-1',
  algorithm: 'thompson_sampling',
  current_weights: [
    { variant_id: 'v-1', variant_name: 'algo_v1', current_weight: 0.33, successes: 0, pulls: 0, conversion_rate: 0 },
    { variant_id: 'v-2', variant_name: 'algo_v2', current_weight: 0.33, successes: 0, pulls: 0, conversion_rate: 0 },
    { variant_id: 'v-3', variant_name: 'algo_v3', current_weight: 0.34, successes: 0, pulls: 0, conversion_rate: 0 },
  ],
  total_pulls: 0,
  recommendation: 'EXPLORING',
  last_updated: '2026-10-05T12:00:00+00:00',
};

const PLANTED = 'Traceback (most recent call last): sqlalchemy.exc.PLANTED-1957';

type PutHandler = (path: string, options: ApiFetchOptions) => unknown;

function install(status: BanditStatus, put: PutHandler = () => SAVED_ANSWER) {
  mockedApiFetch.mockImplementation((async (path: string, options: ApiFetchOptions = {}) => {
    const method = (options.method ?? 'GET').toUpperCase();
    if (method === 'GET' && path === '/api/v1/bandit/exp-1') return status;
    if (method === 'PUT' && path === '/api/v1/bandit/exp-1/weights') return put(path, options);
    throw new ApiError({ status: 404, detail: `no mock for ${method} ${path}` });
  }) as unknown as typeof apiFetch);
}

const calls = (method: string) =>
  mockedApiFetch.mock.calls.filter(([, options]) => ((options?.method as string) ?? 'GET').toUpperCase() === method);

async function renderPanel(overrides: Partial<Experiment> = {}, user: object | null = ADMIN) {
  return render(<BanditWeightsSection experiment={{ ...EXPERIMENT, ...overrides }} user={user} />);
}

async function openForm(overrides: Partial<Experiment> = {}) {
  await renderPanel(overrides);
  const toggle = await screen.findByRole('button', { name: 'Set weights by hand' });
  fireEvent.click(toggle);
  const form = await screen.findByTestId('bandit-weights-override');
  return { toggle, form };
}

const inputsOf = (form: HTMLElement) => within(form).getAllByRole('spinbutton') as HTMLInputElement[];

function enter(form: HTMLElement, values: string[]) {
  inputsOf(form).forEach((input, i) => fireEvent.change(input, { target: { value: values[i] } }));
}

const saveButton = (form: HTMLElement) => within(form).getByTestId('bandit-override-save');

async function axeViolations(container: HTMLElement) {
  const result = await axe.run(container, { rules: { 'color-contrast': { enabled: false } } });
  return result.violations.map((v) => v.id);
}

beforeEach(() => {
  mockedApiFetch.mockReset();
});

describe('the fixtures are what the API sends and accepts', () => {
  it.each([
    ['after an update', UPDATED],
    ['before the first update', NEVER_UPDATED],
    ["the PUT's answer", SAVED_ANSWER],
  ])('%s', (_label, status) => {
    expect(shapeErrors('BanditStatusResponse', status)).toEqual([]);
  });
});

describe('where it is offered', () => {
  it.each(['draft', 'active', 'paused'] as const)('an ADMIN sees it beside Refresh on a %s experiment', async (status) => {
    install(UPDATED);
    await renderPanel({ status });
    const panel = await screen.findByTestId('bandit-weights');
    await within(panel).findByTestId('bandit-weights-table');
    expect(within(panel).getAllByRole('button').map((b) => b.textContent)).toEqual(['Refresh', 'Set weights by hand']);
    const toggle = within(panel).getByRole('button', { name: 'Set weights by hand' });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByTestId('bandit-weights-role-note')).toBeNull();
  });

  it.each(['completed', 'archived'] as const)(
    'nobody new is assigned on a %s experiment: no control and no role note, for anyone',
    async (status) => {
      for (const user of [ADMIN, { role: 'VIEWER', is_superuser: true }, { role: 'VIEWER', is_superuser: false }]) {
        install(UPDATED);
        const view = await renderPanel({ status }, user);
        const panel = await screen.findByTestId('bandit-weights');
        await within(panel).findByTestId('bandit-weights-table');
        expect(within(panel).getAllByRole('button').map((b) => b.textContent)).toEqual(['Refresh']);
        expect(screen.queryByTestId('bandit-weights-role-note')).toBeNull();
        expect(panel.textContent).not.toContain(OVERRIDE_ROLE_NOTE);
        view.unmount();
      }
    },
  );

  it('not while the weights are loading, and not after they failed to load', async () => {
    mockedApiFetch.mockReturnValue(new Promise(() => undefined));
    const loading = await renderPanel();
    expect(screen.getByTestId('bandit-weights-loading')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Set weights by hand' })).toBeNull();
    expect(screen.queryByTestId('bandit-weights-override')).toBeNull();
    loading.unmount();

    mockedApiFetch.mockReset();
    mockedApiFetch.mockRejectedValue(new ApiError({ status: 500, detail: PLANTED }));
    await renderPanel();
    await screen.findByTestId('bandit-weights-error');
    expect(screen.queryByRole('button', { name: 'Set weights by hand' })).toBeNull();
    expect(screen.queryByTestId('bandit-weights-override')).toBeNull();
  });

  it('opening it sends nothing', async () => {
    install(UPDATED);
    await openForm();
    expect(calls('PUT')).toEqual([]);
    expect(calls('GET')).toHaveLength(1);
  });
});

describe('the form', () => {
  it('opens inline with its copy and one labelled input per variant, focus on the first', async () => {
    install(UPDATED);
    const { toggle, form } = await openForm();
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(toggle).toHaveAttribute('aria-controls', form.id);
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(within(form).getByRole('heading', { level: 3, name: 'Set weights by hand' })).toBeInTheDocument();
    expect(form.textContent).toContain(OVERRIDE_INTRO);
    expect(within(form).getByTestId('bandit-override-always')).toHaveTextContent(OVERRIDE_ALWAYS);
    const inputs = inputsOf(form);
    expect(inputs.map((i) => i.getAttribute('data-variant-id'))).toEqual(['v-1', 'v-2', 'v-3']);
    expect(within(form).getByLabelText('algo_v1 (%)')).toBe(inputs[0]);
    expect(within(form).getByLabelText('algo_v3 (%)')).toBe(inputs[2]);
    expect(document.activeElement).toBe(inputs[0]);
  });

  it.each(['draft', 'active', 'paused'] as const)('says what happens next on a %s experiment', async (status) => {
    install(UPDATED);
    const { form } = await openForm({ status });
    expect(within(form).getByTestId('bandit-override-status-copy').textContent).toBe(OVERRIDE_STATUS_COPY[status]);
  });

  it('the copy per status is exactly the agreed sentences', () => {
    expect(OVERRIDE_STATUS_COPY).toEqual({
      active:
        'The bandit replaces these weights at its next update, within minutes (every 5 minutes by default). Use this to correct the split for a short time, not to set it.',
      draft:
        'The bandit does not update a draft. From the start, new users follow these weights until its first update replaces them, within minutes.',
      paused:
        'The bandit does not update a paused experiment. When it resumes, new users follow these weights until its next update replaces them, within minutes.',
    });
    expect(OVERRIDE_ALWAYS).toBe(
      "People already in the experiment keep their variant. A variant at 0% gets no new users. After you save, Pulls and Successes read 0 until the bandit's next update counts them again; no assignment or event is lost.",
    );
    expect(OVERRIDE_ROLE_NOTE).toBe("Setting a bandit's weights by hand needs the ADMIN role.");
  });

  it("before the first update it opens with each variant's allocation, not the server's even split", async () => {
    install(NEVER_UPDATED);
    const { form } = await openForm();
    expect(inputsOf(form).map((i) => i.value)).toEqual(['50', '30', '20']);
    expect(within(form).getByRole('status')).toHaveTextContent(/^Total: 100%\.$/);
    expect(saveButton(form)).toBeEnabled();
  });

  it('after an update it opens with the weights as whole percentages that add up to 100', async () => {
    install(UPDATED);
    const { form } = await openForm();
    expect(inputsOf(form).map((i) => i.value)).toEqual(['33', '33', '34']);
    expect(within(form).getByRole('status')).toHaveTextContent(/^Total: 100%\.$/);
    expect(saveButton(form)).toBeEnabled();
  });

  it('prefill and rounding, directly', () => {
    expect(largestRemainder([100 / 3, 100 / 3, 100 / 3])).toEqual([34, 33, 33]);
    expect(largestRemainder([33.33, 33.33, 33.34])).toEqual([33, 33, 34]);
    expect(largestRemainder([12.5, 87.5])).toEqual([13, 87]);
    expect(largestRemainder([0, 100])).toEqual([0, 100]);
    // A variant the stored weights do not name opens at 0; the rest still add up to 100.
    const stale: BanditStatus = {
      ...UPDATED,
      current_weights: [
        { ...UPDATED.current_weights[0], current_weight: 0.25 },
        { ...UPDATED.current_weights[1], current_weight: 0.5 },
        { ...UPDATED.current_weights[2], variant_id: 'v-old', current_weight: 0.25 },
      ],
    };
    expect(prefillPercents(EXPERIMENT.variants, stale)).toEqual(['33', '67', '0']);
    expect(parsePercent('0')).toBe(0);
    expect(parsePercent('100')).toBe(100);
    for (const refused of ['', ' ', '-1', '12.5', '101', '1e2', 'abc']) expect(parsePercent(refused)).toBeNull();
  });

  it('the running total is announced, with the reason Save is refused', async () => {
    install(UPDATED);
    const { form } = await openForm();
    const total = within(form).getByRole('status');
    expect(total.textContent).toBe('Total: 100%.');
    enter(form, ['33', '33', '33']);
    expect(total.textContent).toBe('Total: 99%. The weights must add up to 100%.');
    enter(form, ['40', '40', '30']);
    expect(total.textContent).toBe('Total: 110%. The weights must add up to 100%.');
  });
});

describe('Save', () => {
  it("sends each whole percentage divided by 100, keyed by the experiment's own variant ids", async () => {
    install(UPDATED);
    const { form } = await openForm();
    enter(form, ['33', '33', '34']);
    fireEvent.click(saveButton(form));
    await waitFor(() => expect(calls('PUT')).toHaveLength(1));
    const [path, options] = calls('PUT')[0];
    expect(path).toBe('/api/v1/bandit/exp-1/weights');
    expect(options).toEqual({ method: 'PUT', json: { weights: { 'v-1': 0.33, 'v-2': 0.33, 'v-3': 0.34 } } });
    const weights = (options?.json as { weights: Record<string, number> }).weights;
    expect(Object.keys(weights).sort()).toEqual(EXPERIMENT.variants.map((v) => v.id).sort());
    expect(Math.abs(Object.values(weights).reduce((a, b) => a + b, 0) - 1)).toBeLessThanOrEqual(1e-6);
    expect(shapeErrors('BanditUpdateRequest', options?.json)).toEqual([]);
  });

  it('a variant may be set to 0; every variant is still sent', async () => {
    install(UPDATED);
    const { form } = await openForm();
    enter(form, ['0', '100', '0']);
    fireEvent.click(saveButton(form));
    await waitFor(() => expect(calls('PUT')).toHaveLength(1));
    expect(calls('PUT')[0][1]?.json).toEqual({ weights: { 'v-1': 0, 'v-2': 1, 'v-3': 0 } });
  });

  it('the keys come from the experiment, not from a stale read of the weights', async () => {
    install({
      ...UPDATED,
      current_weights: [
        UPDATED.current_weights[0],
        UPDATED.current_weights[1],
        { ...UPDATED.current_weights[2], variant_id: 'v-old', variant_name: 'removed' },
      ],
    });
    const { form } = await openForm();
    enter(form, ['20', '30', '50']);
    fireEvent.click(saveButton(form));
    await waitFor(() => expect(calls('PUT')).toHaveLength(1));
    const weights = (calls('PUT')[0][1]?.json as { weights: Record<string, number> }).weights;
    expect(weights).toEqual({ 'v-1': 0.2, 'v-2': 0.3, 'v-3': 0.5 });
  });

  it.each([
    ['a total of 99', ['33', '33', '33'], []],
    ['a total of 101', ['34', '34', '33'], []],
    ['12.5', ['12.5', '50', '37.5'], [0, 2]],
    ['-1', ['-1', '51', '50'], [0]],
    ['a blank', ['', '50', '50'], [0]],
    ['101 in one input', ['101', '0', '0'], [0]],
  ])('Save stays disabled and nothing is sent for %s', async (_label, values, invalid) => {
    install(UPDATED);
    const { form } = await openForm();
    enter(form, values);
    expect(saveButton(form)).toBeDisabled();
    fireEvent.click(saveButton(form));
    fireEvent.submit(form);
    expect(calls('PUT')).toEqual([]);
    const inputs = inputsOf(form);
    inputs.forEach((input, i) => {
      if (invalid.includes(i)) {
        expect(input).toHaveAttribute('aria-invalid', 'true');
        const describedBy = input.getAttribute('aria-describedby') as string;
        expect(document.getElementById(describedBy)).toHaveTextContent(OVERRIDE_INVALID);
      } else {
        expect(input).not.toHaveAttribute('aria-invalid');
      }
    });
  });

  it('after Save the panel shows the answer, says so, and gives focus back to the button', async () => {
    install(UPDATED);
    const { toggle, form } = await openForm();
    enter(form, ['33', '33', '34']);
    fireEvent.click(saveButton(form));
    await waitFor(() => expect(screen.queryByTestId('bandit-weights-override')).toBeNull());
    const rows = within(screen.getByTestId('bandit-weights-table')).getAllByTestId('bandit-weight-row');
    expect(rows.map((r) => within(r).getByTestId('bandit-weight-share').textContent)).toEqual(['33.0%', '33.0%', '34.0%']);
    expect(rows.map((r) => within(r).getByTestId('bandit-weight-pulls').textContent)).toEqual(['0', '0', '0']);
    expect(screen.getByTestId('bandit-weights-saved')).toHaveTextContent(OVERRIDE_SAVED);
    expect(screen.getByTestId('bandit-weights-saved')).toHaveAttribute('role', 'status');
    expect(document.activeElement).toBe(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    // Drawn from the PUT's answer: the weights are not read again.
    expect(calls('GET')).toHaveLength(1);
  });

  it('shows Saving… and takes no more input while the request is in flight', async () => {
    install(UPDATED, () => new Promise(() => undefined));
    const { form } = await openForm();
    fireEvent.click(saveButton(form));
    await waitFor(() => expect(saveButton(form)).toHaveTextContent('Saving…'));
    expect(saveButton(form)).toBeDisabled();
    expect(within(form).getByTestId('bandit-override-cancel')).toBeDisabled();
    inputsOf(form).forEach((input) => expect(input).toBeDisabled());
    fireEvent.submit(form);
    expect(calls('PUT')).toHaveLength(1);
  });
});

describe('Cancel and Escape', () => {
  it('Cancel closes the form, sends nothing and gives focus back to the button', async () => {
    install(UPDATED);
    const { toggle, form } = await openForm();
    enter(form, ['10', '10', '80']);
    fireEvent.click(within(form).getByTestId('bandit-override-cancel'));
    expect(screen.queryByTestId('bandit-weights-override')).toBeNull();
    expect(document.activeElement).toBe(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(calls('PUT')).toEqual([]);
  });

  it('Escape does the same', async () => {
    install(UPDATED);
    const { toggle, form } = await openForm();
    fireEvent.keyDown(inputsOf(form)[1], { key: 'Escape' });
    expect(screen.queryByTestId('bandit-weights-override')).toBeNull();
    expect(document.activeElement).toBe(toggle);
    expect(calls('PUT')).toEqual([]);
  });
});

describe('a failed save shows fixed copy, never the server text', () => {
  const cases: [string, () => unknown, string][] = [
    ['a 403', () => new ApiError({ status: 403, detail: `${PLANTED} ADMIN role required` }), OVERRIDE_ERROR_COPY[403]],
    ['a 404', () => new ApiError({ status: 404, detail: `Experiment ${PLANTED} not found` }), OVERRIDE_ERROR_COPY[404]],
    ['a 422', () => new ApiError({ status: 422, detail: [{ msg: PLANTED, loc: ['body'] }] }), OVERRIDE_ERROR_COPY[422]],
    ['a 500', () => new ApiError({ status: 500, detail: PLANTED }), OVERRIDE_ERROR_COPY.server],
    ['a 503', () => new ApiError({ status: 503, detail: PLANTED }), OVERRIDE_ERROR_COPY.server],
    ['a 409', () => new ApiError({ status: 409, detail: PLANTED }), OVERRIDE_ERROR_COPY.fallback],
    [
      'a browser exception',
      () => new TypeError("Cannot read properties of undefined (reading 'PLANTED-1957')"),
      OVERRIDE_ERROR_COPY.fallback,
    ],
  ];

  it.each(cases)('%s', async (_label, make, expected) => {
    install(UPDATED, () => {
      throw make();
    });
    const { form } = await openForm();
    fireEvent.click(saveButton(form));
    expect((await within(form).findByTestId('bandit-override-error')).textContent).toBe(expected);
    const body = document.body.textContent ?? '';
    expect(body).not.toContain('PLANTED');
    expect(body).not.toContain('Traceback');
    // The form stays open to try again, and the table still shows the weights it read.
    expect(saveButton(form)).toBeEnabled();
    expect(saveButton(form)).toHaveTextContent('Save weights');
    expect(screen.getAllByTestId('bandit-weight-pulls')[0]).toHaveTextContent('1,200');
  });

  it("the read-only demo's refusal shows the API's own sentence, asked first", async () => {
    install(UPDATED, () => {
      throw new ApiError({ status: refusal.status, detail: refusal.body.detail });
    });
    const { form } = await openForm();
    fireEvent.click(saveButton(form));
    expect((await within(form).findByTestId('bandit-override-error')).textContent).toBe(refusal.body.detail.message);
  });

  it("no answer at all (unreachable or timed out) shows the dashboard's own sentence", () => {
    const sentence = 'The API could not be reached. Check that it is running.';
    expect(overrideErrorCopy(new ApiError({ status: 0, detail: sentence, message: sentence }))).toBe(sentence);
  });
});

describe('a11y (axe-core in jsdom; colour contrast is not computable here)', () => {
  it('the open form', async () => {
    install(UPDATED);
    const { form } = await openForm();
    enter(form, ['12.5', '50', '37.5']);
    expect(await axeViolations(document.body)).toEqual([]);
  });

  it('a failed save', async () => {
    install(UPDATED, () => {
      throw new ApiError({ status: 500, detail: PLANTED });
    });
    const { form } = await openForm();
    fireEvent.click(saveButton(form));
    await within(form).findByTestId('bandit-override-error');
    expect(await axeViolations(document.body)).toEqual([]);
  });

  it('after a save', async () => {
    install(UPDATED);
    const { form } = await openForm();
    fireEvent.click(saveButton(form));
    await screen.findByText(OVERRIDE_SAVED);
    expect(await axeViolations(document.body)).toEqual([]);
  });

  it.each(['completed', 'draft'] as ExperimentStatus[])('the closed panel (%s)', async (status) => {
    install(UPDATED);
    const { container } = await renderPanel({ status });
    await screen.findByTestId('bandit-weights-table');
    expect(await axeViolations(container)).toEqual([]);
  });
});
