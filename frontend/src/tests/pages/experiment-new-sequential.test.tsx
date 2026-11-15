/**
 * The sequential-testing switch on `/experiments/new` (#1454).
 *
 * Both views: guided setup (on its Estimate step) and the single-page form
 * (inside "Analysis settings"). The switch adds `sequential_testing_enabled:
 * true` to the create request only when it is ticked, and never a
 * `sequential_testing_config`. Unticked, the request is the one pinned in
 * experiment-new-guided.test.tsx ("sends the eleven keys of the create
 * request and nothing else") and experiment-new.test.tsx (the exact body),
 * which this change leaves untouched.
 */
import fs from 'fs';
import path from 'path';
import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import axe from 'axe-core';
import NewExperimentPage, { ROLE_CANNOT_CREATE } from '@/pages/experiments/new';
import { apiFetch } from '@/services/api';
import { docsUrl } from '@/services/docs';
import { STEP_HEADINGS } from '@/components/experiments/new/Wizard';
import {
  BAYESIAN_HELP,
  BAYESIAN_LABEL,
  SEQUENTIAL_HELP,
  SEQUENTIAL_LABEL,
} from '@/components/experiments/new/AnalysisSettingsFields';
import { makeRouter, routedApi } from './helpers/apiMock';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

let mockRouter = makeRouter();
jest.mock('next/router', () => ({ useRouter: () => mockRouter }));

jest.mock('next/head', () => {
  const Head = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Head.displayName = 'MockHead';
  return Head;
});

type TestUser = { role: string; is_superuser?: boolean };
// A resolved user in every test: with no user the page offers creation to
// anyone, so a role test without one would pass for the wrong reason.
let mockUser: TestUser = { role: 'DEVELOPER', is_superuser: false };
jest.mock('@/contexts/AuthContext', () => ({
  useOptionalAuth: () => ({ user: mockUser }),
}));

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

const ADVANCED = { advanced: '' };

function routerAt(query: Record<string, string> = {}, isReady = true) {
  const search = new URLSearchParams(query).toString();
  mockRouter = makeRouter({
    pathname: '/experiments/new',
    asPath: `/experiments/new${search ? `?${search}` : ''}`,
    query,
    isReady,
  });
}

function api() {
  mockedApiFetch.mockImplementation(
    routedApi([
      { method: 'POST', path: '/api/v1/experiments', handler: () => ({ id: 'exp-9', status: 'draft' }) },
    ]) as unknown as typeof apiFetch,
  );
}

const setValue = (testId: string, value: string) =>
  fireEvent.change(screen.getByTestId(testId), { target: { value } });
const next = () => fireEvent.click(screen.getByTestId('wizard-next'));
const heading = () => screen.getByTestId('wizard-step-heading');
const sequential = () => screen.getByTestId('analysis-sequential');

/** The one POST's body as it goes over the wire (undefined dropped). */
function postedBody(): Record<string, unknown> {
  const posts = mockedApiFetch.mock.calls.filter(
    ([, o]) => (o?.method ?? 'GET').toUpperCase() === 'POST',
  );
  expect(posts).toHaveLength(1);
  expect(posts[0][0]).toBe('/api/v1/experiments');
  return JSON.parse(JSON.stringify(posts[0][1]?.json));
}

/** Guided setup, on the Estimate step, with a name, a description and a hypothesis. */
function toEstimate() {
  routerAt();
  api();
  const view = render(<NewExperimentPage />);
  next();
  setValue('experiment-name', 'Checkout redesign');
  setValue('experiment-description', 'New layout');
  setValue('experiment-hypothesis', 'Shorter checkout raises purchases');
  next();
  next();
  expect(heading()).toHaveTextContent(STEP_HEADINGS.estimate);
  return view;
}

/** Guided setup to Review and Create, with the two analysis boxes as given; the body sent. */
async function createThroughGuided(bayesian: boolean, seq: boolean): Promise<Record<string, unknown>> {
  const view = toEstimate();
  if (bayesian) fireEvent.click(screen.getByTestId('analysis-bayesian'));
  if (seq) fireEvent.click(sequential());
  next();
  expect(heading()).toHaveTextContent(STEP_HEADINGS.review);
  fireEvent.click(screen.getByTestId('wizard-create'));
  await waitFor(() => expect(mockRouter.push).toHaveBeenCalledWith('/experiments/exp-9'));
  const body = postedBody();
  view.unmount();
  mockedApiFetch.mockReset();
  return body;
}

/** The eleven keys experiment-new-guided.test.tsx pins for this request. */
const ELEVEN_KEYS = [
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
  'bayesian_enabled',
];

beforeEach(() => {
  mockedApiFetch.mockReset();
  mockUser = { role: 'DEVELOPER', is_superuser: false };
  routerAt();
});

describe('the sequential testing checkbox', () => {
  it.each([
    ['guided setup', false],
    ['the single-page form', true],
  ])('%s: is off by default, labelled, explains itself, and links to how it works', (_view, advanced) => {
    if (advanced) {
      routerAt(ADVANCED);
      render(<NewExperimentPage />);
    } else {
      toEstimate();
    }
    const box = screen.getByLabelText(SEQUENTIAL_LABEL);
    expect(box).toBe(sequential());
    expect(box).toHaveAttribute('type', 'checkbox');
    expect(box).not.toBeChecked();
    expect(box).toHaveAccessibleDescription(`${SEQUENTIAL_HELP} How sequential testing works`);
    const help = document.getElementById('analysis-sequential-help');
    expect(help).not.toBeNull();
    expect(within(help as HTMLElement).getByRole('link', { name: 'How sequential testing works' })).toHaveAttribute(
      'href',
      docsUrl('api/sequential-testing', 'turning-it-on'),
    );
  });

  it('says it is beta, compares the control with the first treatment on the primary metric, and has its own 5% level', () => {
    expect(SEQUENTIAL_LABEL).toBe('Also test the primary metric sequentially (beta)');
    expect(SEQUENTIAL_HELP).toBe(
      'Check results as often as you like, and stop early once the evidence is strong. It compares ' +
        'the control with the first treatment only, on the primary metric, at its own 5% significance ' +
        'level, not the confidence level above. Saved with the experiment.',
    );
  });

  it('sits after the Bayesian block and leaves that block as it was', () => {
    toEstimate();
    const bayesianBlock = screen.getByTestId('analysis-bayesian').parentElement as HTMLElement;
    // Video 9 frames `div:has(> #analysis-bayesian)`: that div holds only the
    // Bayesian checkbox and its words.
    expect(bayesianBlock.tagName).toBe('DIV');
    expect(bayesianBlock.children).toHaveLength(2);
    expect(bayesianBlock.textContent).toBe(`${BAYESIAN_LABEL}${BAYESIAN_HELP} How Bayesian analysis works`);
    expect(within(bayesianBlock).queryByTestId('analysis-sequential')).not.toBeInTheDocument();
    const sequentialBlock = bayesianBlock.nextElementSibling as HTMLElement;
    expect(sequentialBlock).not.toBeNull();
    expect(within(sequentialBlock).getByTestId('analysis-sequential')).toBe(sequential());
    expect(sequentialBlock.nextElementSibling).toBeNull();
  });
});

describe('the create request', () => {
  it('guided setup sends a twelfth key, sequential_testing_enabled: true, only when it is ticked, and no config', async () => {
    const off = await createThroughGuided(true, false);
    expect(Object.keys(off).sort()).toEqual([...ELEVEN_KEYS].sort());

    const on = await createThroughGuided(true, true);
    expect(Object.keys(on).sort()).toEqual([...ELEVEN_KEYS, 'sequential_testing_enabled'].sort());
    expect(on.sequential_testing_enabled).toBe(true);
    expect('sequential_testing_config' in on).toBe(false);
    const { sequential_testing_enabled: _added, ...rest } = on;
    expect(rest).toStrictEqual(off);
  });

  it('ticked then unticked sends no sequential key', async () => {
    toEstimate();
    fireEvent.click(sequential());
    fireEvent.click(sequential());
    expect(sequential()).not.toBeChecked();
    next();
    fireEvent.click(screen.getByTestId('wizard-create'));
    await waitFor(() => expect(mockRouter.push).toHaveBeenCalledWith('/experiments/exp-9'));
    expect('sequential_testing_enabled' in postedBody()).toBe(false);
  });

  it('the single-page form sends sequential_testing_enabled: true when it is ticked, and no config', async () => {
    routerAt(ADVANCED);
    api();
    render(<NewExperimentPage />);
    setValue('experiment-name', 'Checkout redesign');
    fireEvent.click(sequential());
    fireEvent.click(screen.getByTestId('submit-experiment'));
    await waitFor(() => expect(mockRouter.push).toHaveBeenCalledWith('/experiments/exp-9'));
    const body = postedBody();
    expect(body.sequential_testing_enabled).toBe(true);
    expect('sequential_testing_config' in body).toBe(false);
    expect('bayesian_enabled' in body).toBe(false);
  });

  it('the answer carries over between the views', () => {
    routerAt(ADVANCED);
    const view = render(<NewExperimentPage />);
    fireEvent.click(sequential());
    routerAt();
    view.rerender(<NewExperimentPage />);
    next();
    setValue('experiment-name', 'Checkout redesign');
    next();
    next();
    expect(sequential()).toBeChecked();
  });
});

describe('Review and the Analysis settings summary say when it is on', () => {
  async function toReview(bayesian: boolean, seq: boolean) {
    toEstimate();
    if (bayesian) fireEvent.click(screen.getByTestId('analysis-bayesian'));
    if (seq) fireEvent.click(sequential());
    next();
    expect(heading()).toHaveTextContent(STEP_HEADINGS.review);
  }

  it('Review: the Analysis row ends "Sequential testing (beta) on"', async () => {
    await toReview(false, true);
    expect(screen.getByTestId('review-analysis').textContent).toBe(
      '95% confidence · Benjamini-Hochberg correction · Sequential testing (beta) on',
    );
  });

  it('Review: with Bayesian analysis on too, both are named, Bayesian first', async () => {
    await toReview(true, true);
    expect(screen.getByTestId('review-analysis').textContent).toBe(
      '95% confidence · Benjamini-Hochberg correction · Bayesian analysis on · Sequential testing (beta) on',
    );
  });

  it('Review: says nothing about sequential testing when it is off', async () => {
    await toReview(true, false);
    expect(screen.getByTestId('review-analysis').textContent).toBe(
      '95% confidence · Benjamini-Hochberg correction · Bayesian analysis on',
    );
  });

  it('the single-page form: the collapsed summary says "Sequential testing (beta) on" when it is on', () => {
    routerAt(ADVANCED);
    render(<NewExperimentPage />);
    const summary = screen.getByTestId('analysis-settings-summary');
    expect(summary).not.toHaveTextContent(/sequential/i);
    fireEvent.click(sequential());
    expect(summary.textContent).toBe(
      'Analysis settings: 95% confidence, Benjamini-Hochberg correction, Sequential testing (beta) on',
    );
    fireEvent.click(screen.getByTestId('analysis-bayesian'));
    expect(summary.textContent).toBe(
      'Analysis settings: 95% confidence, Benjamini-Hochberg correction, Bayesian analysis on, ' +
        'Sequential testing (beta) on',
    );
  });
});

describe('who sees the switch', () => {
  const cases: Array<[string, TestUser, boolean]> = [
    ['ADMIN', { role: 'ADMIN', is_superuser: false }, true],
    ['DEVELOPER', { role: 'DEVELOPER', is_superuser: false }, true],
    ['ANALYST', { role: 'ANALYST', is_superuser: false }, false],
    ['VIEWER', { role: 'VIEWER', is_superuser: false }, false],
    ['superuser VIEWER', { role: 'VIEWER', is_superuser: true }, true],
  ];

  it.each(cases)('the single-page form: %s', (_label, user, allowed) => {
    mockUser = user;
    routerAt(ADVANCED);
    render(<NewExperimentPage />);
    if (allowed) {
      expect(screen.getByTestId('analysis-sequential')).not.toBeChecked();
      expect(screen.queryByTestId('create-not-allowed')).not.toBeInTheDocument();
    } else {
      expect(screen.getByTestId('create-not-allowed')).toHaveTextContent(ROLE_CANNOT_CREATE);
      expect(screen.queryByTestId('analysis-sequential')).not.toBeInTheDocument();
    }
    // Showing (or refusing) the switch sends nothing.
    expect(mockedApiFetch.mock.calls).toEqual([]);
  });

  it.each(cases)('guided setup: %s', (_label, user, allowed) => {
    mockUser = user;
    routerAt();
    render(<NewExperimentPage />);
    if (allowed) {
      next();
      setValue('experiment-name', 'Checkout redesign');
      next();
      next();
      expect(heading()).toHaveTextContent(STEP_HEADINGS.estimate);
      fireEvent.click(screen.getByTestId('analysis-sequential'));
      expect(sequential()).toBeChecked();
    } else {
      expect(screen.getByTestId('create-not-allowed')).toHaveTextContent(ROLE_CANNOT_CREATE);
      expect(screen.queryByTestId('guided-setup')).not.toBeInTheDocument();
      expect(screen.queryByTestId('analysis-sequential')).not.toBeInTheDocument();
    }
    // Reaching the switch and ticking it sends nothing: only Create does.
    expect(mockedApiFetch.mock.calls).toEqual([]);
  });

  it.each([
    ['guided setup', {}],
    ['the single-page form', ADVANCED],
  ])('%s: not shown while the page is loading', (_view, query) => {
    routerAt(query, false);
    render(<NewExperimentPage />);
    expect(screen.getByTestId('new-experiment-loading')).toBeInTheDocument();
    expect(screen.queryByTestId('analysis-sequential')).not.toBeInTheDocument();
  });
});

describe('accessibility (axe-core in jsdom; colour contrast is not computable here)', () => {
  const axeOptions: axe.RunOptions = { rules: { 'color-contrast': { enabled: false } } };

  async function violations() {
    const container = document.querySelector('body > div');
    if (!container) throw new Error('nothing rendered');
    const result = await axe.run(container, axeOptions);
    return result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(', ')}`);
  }

  it('the Estimate step with both boxes ticked has no axe violations', async () => {
    toEstimate();
    fireEvent.click(screen.getByTestId('analysis-bayesian'));
    fireEvent.click(sequential());
    expect(await violations()).toEqual([]);
  });

  it('the single-page form with Analysis settings open and the box ticked has no axe violations', async () => {
    routerAt(ADVANCED);
    render(<NewExperimentPage />);
    (screen.getByTestId('analysis-settings-details') as HTMLDetailsElement).open = true;
    fireEvent.click(sequential());
    expect(await violations()).toEqual([]);
  });

  it('the help is tied to the box, and the box is inside the fieldset with its legend', () => {
    toEstimate();
    expect(sequential()).toHaveAttribute('aria-describedby', 'analysis-sequential-help');
    const fieldset = screen.getByTestId('analysis-settings');
    expect(fieldset.tagName).toBe('FIELDSET');
    expect(within(fieldset).getByTestId('analysis-sequential')).toBe(sequential());
  });

  it('AnalysisSettingsFields.tsx uses no low-contrast text colour', () => {
    const source = fs.readFileSync(
      path.join(__dirname, '..', '..', 'components', 'experiments', 'new', 'AnalysisSettingsFields.tsx'),
      'utf8',
    );
    expect(source.match(/\btext-(slate-[1-4]00|red-400|red-500)\b/g)).toBeNull();
  });
});
