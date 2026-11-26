/**
 * The new-experiment form's conversion window (beta, #1934), through both
 * views of `/experiments/new`.
 *
 * What is pinned:
 *  - each metric row has a box labelled "Conversion window in hours (beta,
 *    optional)", blank by default; blank sends no `conversion_window_hours`
 *    key, so the default A/B body is the one sent before (AB_LITERAL), and a
 *    number is sent as a number, never as text;
 *  - the help under the box (its description, not a live region) says what a
 *    number means in days;
 *  - a window that cannot be saved is refused with the metric named, nothing
 *    is sent, and the box takes focus, marked invalid and described by the
 *    refusal;
 *  - guided setup's Estimate and Review say so when a window is set, and say
 *    nothing when it is not;
 *  - the box has guided setup's border and focus ring, not the row's
 *    lower-contrast ones (colour contrast itself is not computable in jsdom).
 */
import React from 'react';
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react';
import axe from 'axe-core';
import NewExperimentPage from '@/pages/experiments/new';
import { apiFetch } from '@/services/api';
import { STEP_HEADINGS } from '@/components/experiments/new/Wizard';
import { wizardInputClass } from '@/components/experiments/new/fieldStyles';
import {
  WINDOW_HELP_BLANK,
  WINDOW_LABEL,
  windowEstimateLine,
  windowHelp,
  windowRefusal,
} from '@/utils/conversionWindow';
import { makeRouter, routedApi } from './helpers/apiMock';
import { AB_LITERAL, AB_NAME } from './helpers/abCreateBody';

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

// A resolved, non-superuser DEVELOPER: with no user the page offers creation
// to anyone, so the form would render for the wrong reason.
jest.mock('@/contexts/AuthContext', () => ({
  useOptionalAuth: () => ({ user: { role: 'DEVELOPER', is_superuser: false } }),
}));

jest.mock('@/components/experiments/new/leaveGuard', () => ({
  ...jest.requireActual('@/components/experiments/new/leaveGuard'),
  navigateHard: jest.fn(),
}));

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

function routerAt(query: Record<string, string> = {}) {
  const search = new URLSearchParams(query).toString();
  mockRouter = makeRouter({
    pathname: '/experiments/new',
    asPath: `/experiments/new${search ? `?${search}` : ''}`,
    query,
    isReady: true,
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
const posts = () => mockedApiFetch.mock.calls.filter(([, o]) => (o?.method ?? 'GET').toUpperCase() === 'POST');

/** The one create request, as the string that goes over the wire. */
async function sentBody(): Promise<string> {
  await waitFor(() => expect(mockRouter.push).toHaveBeenCalledWith('/experiments/exp-9'));
  expect(posts()).toHaveLength(1);
  expect(posts()[0][0]).toBe('/api/v1/experiments');
  return JSON.stringify(posts()[0][1]?.json);
}

/** The element ids an element is described by, in order. */
const describedBy = (el: HTMLElement) => (el.getAttribute('aria-describedby') ?? '').split(' ').filter(Boolean);
const descriptionText = (el: HTMLElement) =>
  describedBy(el).map((id) => document.getElementById(id)?.textContent ?? `(no #${id})`);

/** Guided setup to Details, with a name. */
function toGuidedDetails() {
  routerAt();
  render(<NewExperimentPage />);
  next();
  setValue('experiment-name', AB_NAME);
}

/** A second metric, "Refund", on row 2. */
function addRefund() {
  fireEvent.click(screen.getByTestId('add-metric'));
  setValue('metric-name-1', 'Refund');
  setValue('metric-event-1', 'refund');
}

beforeEach(() => {
  mockedApiFetch.mockReset();
  api();
});

describe('blank sends no window, so the body is the one sent before', () => {
  it('the single-page form', async () => {
    routerAt({ advanced: '' });
    render(<NewExperimentPage />);
    expect(screen.getByTestId('metric-window-0')).toHaveValue('');
    setValue('experiment-name', AB_NAME);
    fireEvent.click(screen.getByTestId('submit-experiment'));
    expect(await sentBody()).toBe(AB_LITERAL);
  });

  it('guided setup', async () => {
    toGuidedDetails();
    expect(screen.getByTestId('metric-window-0')).toHaveValue('');
    next();
    next();
    next();
    expect(screen.getByTestId('wizard-step-heading')).toHaveTextContent(STEP_HEADINGS.review);
    fireEvent.click(screen.getByTestId('wizard-create'));
    expect(await sentBody()).toBe(AB_LITERAL);
  });
});

describe('168 is sent as the number 168, on that metric only', () => {
  const expectWindowSent = async () => {
    const body = await sentBody();
    const metrics = (JSON.parse(body) as { metrics: Record<string, unknown>[] }).metrics;
    expect(metrics[0]).toStrictEqual({
      name: 'Conversion',
      event_name: 'conversion',
      metric_type: 'conversion',
      is_primary: true,
      conversion_window_hours: 168,
    });
    expect(metrics[1]).not.toHaveProperty('conversion_window_hours');
    expect(body).toContain('"conversion_window_hours":168}');
  };

  it('the single-page form', async () => {
    routerAt({ advanced: '' });
    render(<NewExperimentPage />);
    setValue('experiment-name', AB_NAME);
    setValue('metric-window-0', '168');
    addRefund();
    fireEvent.click(screen.getByTestId('submit-experiment'));
    await expectWindowSent();
  });

  it('guided setup', async () => {
    toGuidedDetails();
    setValue('metric-window-0', ' 168 ');
    addRefund();
    next();
    next();
    next();
    fireEvent.click(screen.getByTestId('wizard-create'));
    await expectWindowSent();
  });
});

describe('a window that cannot be saved is refused on its own row, and its box takes focus', () => {
  const REFUSED = ['0', '8761', '1.5', '7d', '1e2'];

  it.each(REFUSED)('the single-page form refuses "%s" on row 2 of 2', async (typed) => {
    routerAt({ advanced: '' });
    render(<NewExperimentPage />);
    setValue('experiment-name', AB_NAME);
    addRefund();
    setValue('metric-window-1', typed);
    fireEvent.click(screen.getByTestId('submit-experiment'));
    const error = await screen.findByTestId('form-error');
    expect(error).toHaveTextContent(windowRefusal('Refund'));
    const box = screen.getByTestId('metric-window-1');
    expect(box).toHaveFocus();
    expect(box).toHaveAttribute('aria-invalid', 'true');
    expect(describedBy(box)[0]).toBe(error.id);
    expect(screen.getByTestId('metric-window-0')).not.toHaveAttribute('aria-invalid');
    expect(posts()).toEqual([]);
  });

  it.each(REFUSED)('guided setup refuses "%s" on row 2 of 2, and stays on Details', (typed) => {
    toGuidedDetails();
    addRefund();
    setValue('metric-window-1', typed);
    next();
    expect(screen.getByTestId('wizard-step-heading')).toHaveTextContent(STEP_HEADINGS.details);
    const error = screen.getByTestId('step-error');
    expect(error).toHaveTextContent(windowRefusal('Refund'));
    const box = screen.getByTestId('metric-window-1');
    expect(box).toHaveFocus();
    expect(box).toHaveAttribute('aria-invalid', 'true');
    expect(describedBy(box)[0]).toBe(error.id);
    expect(posts()).toEqual([]);
  });

  it('a fixed window is no longer marked, and Next moves on', () => {
    toGuidedDetails();
    addRefund();
    setValue('metric-window-1', '7d');
    next();
    setValue('metric-window-1', '7');
    expect(screen.getByTestId('metric-window-1')).not.toHaveAttribute('aria-invalid');
    next();
    expect(screen.getByTestId('wizard-step-heading')).toHaveTextContent(STEP_HEADINGS.variants);
  });
});

describe('the box', () => {
  it('has a visible label, numeric input, and guided setup’s border and focus ring', () => {
    routerAt({ advanced: '' });
    render(<NewExperimentPage />);
    const box = screen.getByLabelText(WINDOW_LABEL) as HTMLInputElement;
    expect(box).toBe(screen.getByTestId('metric-window-0'));
    expect(document.querySelector(`label[for="${box.id}"]`)).toHaveTextContent(WINDOW_LABEL);
    expect(box.type).toBe('text');
    expect(box.inputMode).toBe('numeric');
    // Not the row's smallInputClass: its slate-300 border and 1px blue-400 ring are under 3:1 on slate-50.
    for (const cls of wizardInputClass.split(' ')) expect(box.classList).toContain(cls);
    expect(box.classList).not.toContain('border-slate-300');
    expect(box.classList).not.toContain('focus:ring-blue-400');
  });

  it('is described by its help, which says what the number means and is not a live region', () => {
    routerAt({ advanced: '' });
    render(<NewExperimentPage />);
    const box = screen.getByTestId('metric-window-0');
    expect(descriptionText(box)).toEqual([WINDOW_HELP_BLANK]);
    setValue('metric-window-0', '168');
    expect(descriptionText(box)).toEqual([windowHelp(168)]);
    expect(windowHelp(168)).toContain('7 days (168 hours)');
    setValue('metric-window-0', '7');
    expect(descriptionText(box)).toEqual([windowHelp(7)]);
    expect(windowHelp(7)).toContain('the 7 hours from');
    const help = screen.getByTestId('metric-window-help-0');
    expect(help).not.toHaveAttribute('aria-live');
    expect(help).not.toHaveAttribute('role');
    // Text that cannot be saved has no help until Create says why.
    setValue('metric-window-0', '7d');
    expect(screen.queryByTestId('metric-window-help-0')).toBeNull();
    expect(box).not.toHaveAttribute('aria-describedby');
  });

  it('has no axe violations with a refusal shown', async () => {
    routerAt({ advanced: '' });
    render(<NewExperimentPage />);
    setValue('experiment-name', AB_NAME);
    addRefund();
    setValue('metric-window-0', '168');
    setValue('metric-window-1', '0');
    fireEvent.click(screen.getByTestId('submit-experiment'));
    await screen.findByTestId('form-error');
    const section = screen.getByTestId('metrics-section');
    let result: axe.AxeResults | undefined;
    await act(async () => {
      result = await axe.run(
        { include: [section], exclude: [['[data-testid^="remove-metric"]']] },
        { rules: { 'color-contrast': { enabled: false } } },
      );
    });
    if (!result) throw new Error('axe did not run');
    expect(result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(', ')}`)).toStrictEqual([]);
  });
});

describe('guided setup’s Estimate and Review', () => {
  it('say so when the primary metric has a window', () => {
    toGuidedDetails();
    setValue('metric-window-0', '168');
    next();
    next();
    expect(screen.getByTestId('estimate-window-line')).toHaveTextContent(windowEstimateLine(168));
    next();
    expect(screen.getByTestId('review-metrics').querySelector('li')?.textContent).toBe(
      'Conversion (conversion, Conversion) — primary metric, conversion window 7 days',
    );
  });

  it('say nothing of a window when none is set', () => {
    toGuidedDetails();
    next();
    next();
    expect(screen.queryByTestId('estimate-window-line')).toBeNull();
    next();
    expect(screen.getByTestId('review-metrics').querySelector('li')?.textContent).toBe(
      'Conversion (conversion, Conversion) — primary metric',
    );
    expect(screen.getByTestId('review-metrics').textContent).not.toMatch(/window/);
  });

  it('a window on another metric only: Review names it there, and the estimate says nothing', () => {
    toGuidedDetails();
    addRefund();
    setValue('metric-window-1', '36');
    next();
    next();
    expect(screen.queryByTestId('estimate-window-line')).toBeNull();
    next();
    const items = screen.getByTestId('review-metrics').querySelectorAll('li');
    expect(items[0].textContent).toBe('Conversion (conversion, Conversion) — primary metric');
    expect(items[1].textContent).toBe('Refund (refund, Conversion), conversion window 36 hours');
  });
});
