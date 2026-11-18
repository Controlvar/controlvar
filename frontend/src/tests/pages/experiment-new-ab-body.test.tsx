/**
 * The default A/B create request is the one main sent, byte for byte (#1956,
 * EMM-6), through both views of `/experiments/new`.
 *
 * Adding the Bandit type must not touch the A/B body: no `optimization_type`
 * key, not even "fixed", no key moved, no value changed. Before this file only
 * the multivariate body was pinned literally (experiment-new-guided.test.tsx's
 * PARITY_LITERAL) and the guided A/B body by its key names
 * (experiment-new-sequential.test.tsx's ELEVEN_KEYS), so a changed value, a
 * reordered key or any change to the single-page form's A/B body passed every
 * test. AB_LITERAL (helpers/abCreateBody.ts) was captured from main's form by
 * these steps, and this file was green on main before the Bandit type was
 * added on top of it.
 */
import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import NewExperimentPage from '@/pages/experiments/new';
import { apiFetch } from '@/services/api';
import { STEP_HEADINGS } from '@/components/experiments/new/Wizard';
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

/** The one create request, as the string that goes over the wire. */
function sentBody(): string {
  const posts = mockedApiFetch.mock.calls.filter(([, o]) => (o?.method ?? 'GET').toUpperCase() === 'POST');
  expect(posts).toHaveLength(1);
  expect(posts[0][0]).toBe('/api/v1/experiments');
  return JSON.stringify(posts[0][1]?.json);
}

beforeEach(() => {
  mockedApiFetch.mockReset();
});

describe('the default A/B create request is byte for byte the one main sent', () => {
  it('the single-page form', async () => {
    routerAt({ advanced: '' });
    api();
    render(<NewExperimentPage />);
    expect((screen.getByTestId('experiment-type') as HTMLSelectElement).value).toBe('a_b');
    setValue('experiment-name', AB_NAME);
    fireEvent.click(screen.getByTestId('submit-experiment'));
    await waitFor(() => expect(mockRouter.push).toHaveBeenCalledWith('/experiments/exp-9'));
    expect(sentBody()).toBe(AB_LITERAL);
  });

  it('guided setup', async () => {
    routerAt();
    api();
    render(<NewExperimentPage />);
    expect(screen.getByTestId('wizard-type-a_b')).toBeChecked();
    next();
    setValue('experiment-name', AB_NAME);
    next();
    next();
    next(); // the estimate is optional
    expect(screen.getByTestId('wizard-step-heading')).toHaveTextContent(STEP_HEADINGS.review);
    fireEvent.click(screen.getByTestId('wizard-create'));
    await waitFor(() => expect(mockRouter.push).toHaveBeenCalledWith('/experiments/exp-9'));
    expect(sentBody()).toBe(AB_LITERAL);
  });

  it('the literal is a whole A/B create request, so a match is not vacuous', () => {
    const body = JSON.parse(AB_LITERAL) as Record<string, unknown>;
    expect(body.name).toBe(AB_NAME);
    expect(body.experiment_type).toBe('a_b');
    expect(Array.isArray(body.variants)).toBe(true);
    expect(Array.isArray(body.metrics)).toBe(true);
    expect('optimization_type' in body).toBe(false);
  });
});
