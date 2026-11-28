/**
 * /admin/scheduler: "Send a test notification through the scheduler's webhook"
 * (#2277). `POST /scheduler/notify/test` depends on the superuser flag alone, so
 * the button is shown to superusers whatever their role and to nobody else.
 * The real page, AdminService and a mocked `apiFetch`.
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import axe from 'axe-core';
import { SchedulerHealthPage } from '@/pages/admin/scheduler';
import { ApiError, apiFetch } from '@/services/api';
import { DEMO_READ_ONLY_CODE } from '@/utils/demoMode';
import {
  WEBHOOK_BUTTON,
  WEBHOOK_ERROR,
  WEBHOOK_FAILED,
  WEBHOOK_HELP,
  WEBHOOK_REFUSED,
  WEBHOOK_SENT,
  WEBHOOK_SKIPPED,
} from '@/components/admin/scheduler/SchedulerWebhookTest';
import { apiError, routedApi } from './helpers/apiMock';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

jest.mock('@/components/admin/AdminLayout', () => ({
  AdminLayout: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

const mockUseAuth = jest.fn();
jest.mock('@/contexts/AuthContext', () => ({ useAuth: () => mockUseAuth() }));

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

const HEALTH = '/api/v1/scheduler/health';
const TEST = '/api/v1/scheduler/notify/test';
const PLANTED = 'Traceback (most recent call last): PLANTED-webhook-5e2';

const as = (role: string, extra: Record<string, unknown> = {}) => ({
  id: 'user-1',
  email: `${role.toLowerCase()}@demo.com`,
  username: role.toLowerCase(),
  role,
  is_superuser: false,
  ...extra,
});
const signIn = (user: Record<string, unknown>) => mockUseAuth.mockReturnValue({ user, status: 'authenticated' });

type Answer = () => unknown | Promise<unknown>;

function install(answer: Answer) {
  mockedApiFetch.mockImplementation(
    routedApi([
      { path: HEALTH, handler: () => [] },
      { method: 'POST', path: TEST, handler: () => answer() },
    ]) as unknown as typeof apiFetch,
  );
}

const posts = () => mockedApiFetch.mock.calls.filter(([, o]) => (o?.method ?? 'GET').toUpperCase() === 'POST');
const toTheRoute = () => mockedApiFetch.mock.calls.filter(([url]) => String(url).split('?')[0] === TEST);

async function renderPage(answer: Answer = () => ({ status: 'sent', message: PLANTED, sent: true })) {
  install(answer);
  const view = render(<SchedulerHealthPage />);
  await screen.findByTestId('scheduler-empty');
  return view;
}

const click = async () => {
  await act(async () => {
    fireEvent.click(screen.getByTestId('scheduler-test-webhook'));
  });
};

beforeEach(() => {
  mockedApiFetch.mockReset();
  signIn(as('ADMIN', { is_superuser: true }));
});

describe('who is offered the test button', () => {
  const offered: [string, Record<string, unknown>][] = [
    ['superuser VIEWER', as('VIEWER', { is_superuser: true })],
    ['superuser ADMIN', as('ADMIN', { is_superuser: true })],
  ];
  const refused: [string, Record<string, unknown>][] = [
    ['ADMIN who is not a superuser', as('ADMIN')],
    ['DEVELOPER', as('DEVELOPER')],
    ['ANALYST', as('ANALYST')],
    ['VIEWER', as('VIEWER')],
  ];

  it.each(offered)('%s sees the button with its witness', async (_name, user) => {
    signIn(user);
    await renderPage();
    expect(screen.getByTestId('scheduler-test-webhook')).toHaveTextContent(WEBHOOK_BUTTON);
  });

  it.each(refused)('%s loads the page, sees no button, and sends nothing to the route', async (_name, user) => {
    signIn(user);
    await renderPage();
    expect(screen.getByTestId('scheduler-health-page')).toBeInTheDocument();
    expect(screen.queryByTestId('scheduler-test-webhook')).toBeNull();
    expect(toTheRoute()).toEqual([]);
    expect(posts()).toEqual([]);
  });
});

describe('sending', () => {
  it('posts once, with no body, and says what the route answered, never its own message', async () => {
    await renderPage();
    await click();
    expect(posts()).toHaveLength(1);
    const [url, options] = posts()[0];
    expect(url).toBe(TEST);
    expect(options?.json).toBeUndefined();
    const result = screen.getByTestId('scheduler-webhook-result');
    expect(result).toHaveTextContent(WEBHOOK_SENT);
    expect(result).toHaveAttribute('role', 'status');
    expect(document.body.textContent).not.toContain('PLANTED');
  });

  it.each([
    ['skipped', WEBHOOK_SKIPPED, 'status'],
    ['failed', WEBHOOK_FAILED, 'alert'],
  ])('an answer of %s shows its own sentence', async (status, copy, role) => {
    await renderPage(() => ({ status, message: PLANTED, sent: false }));
    await click();
    const result = screen.getByTestId('scheduler-webhook-result');
    expect(result).toHaveTextContent(copy);
    expect(result).toHaveAttribute('role', role);
    expect(document.body.textContent).not.toContain('PLANTED');
  });

  it('an answer it does not understand is an error, not a success', async () => {
    await renderPage(() => ({ status: 'queued', message: PLANTED, sent: true }));
    await click();
    expect(screen.getByTestId('scheduler-webhook-result')).toHaveTextContent(WEBHOOK_ERROR);
  });

  it.each([
    [403, WEBHOOK_REFUSED],
    [500, WEBHOOK_ERROR],
  ])('a %s shows fixed copy, never the server detail', async (status, copy) => {
    await renderPage(() => {
      throw apiError(status, PLANTED);
    });
    await click();
    expect(screen.getByTestId('scheduler-webhook-result')).toHaveTextContent(copy);
    expect(document.body.textContent).not.toContain('PLANTED');
  });

  it("the read-only demo's refusal shows its own sentence", async () => {
    await renderPage(() => {
      throw new ApiError({
        status: 403,
        detail: { code: DEMO_READ_ONLY_CODE, message: 'This demo is read-only.' },
      });
    });
    await click();
    expect(screen.getByTestId('scheduler-webhook-result')).toHaveTextContent('This demo is read-only.');
    expect(screen.getByTestId('scheduler-webhook-result')).not.toHaveTextContent(WEBHOOK_REFUSED);
  });

  it('a status 0 shows the dashboard\'s own sentence', async () => {
    const err = apiError(0, 'fetch failed');
    await renderPage(() => {
      throw err;
    });
    await click();
    expect(screen.getByTestId('scheduler-webhook-result')).toHaveTextContent(err.message);
  });

  it('sends one request at a time: a second click while one is out sends nothing, and the button comes back after', async () => {
    let release: (answer: unknown) => void = () => {};
    const pending = new Promise((resolve) => {
      release = resolve;
    });
    await renderPage(() => pending);
    await click();
    const button = screen.getByTestId('scheduler-test-webhook');
    expect(button).toBeDisabled();
    expect(button).toHaveTextContent('Sending...');
    await click();
    expect(posts()).toHaveLength(1);
    expect(screen.queryByTestId('scheduler-webhook-result')).toBeNull();
    await act(async () => {
      release({ status: 'sent', message: '', sent: true });
    });
    await waitFor(() => expect(screen.getByTestId('scheduler-webhook-result')).toHaveTextContent(WEBHOOK_SENT));
    expect(screen.getByTestId('scheduler-test-webhook')).toBeEnabled();
    expect(posts()).toHaveLength(1);
  });

  it('a second send clears the first answer while it is out', async () => {
    let calls = 0;
    let release: (answer: unknown) => void = () => {};
    await renderPage(() => {
      calls += 1;
      if (calls === 1) return { status: 'failed', message: '', sent: false };
      return new Promise((resolve) => {
        release = resolve;
      });
    });
    await click();
    expect(screen.getByTestId('scheduler-webhook-result')).toHaveTextContent(WEBHOOK_FAILED);
    await click();
    expect(screen.queryByTestId('scheduler-webhook-result')).toBeNull();
    await act(async () => {
      release({ status: 'sent', message: '', sent: true });
    });
    await waitFor(() => expect(screen.getByTestId('scheduler-webhook-result')).toHaveTextContent(WEBHOOK_SENT));
  });
});

describe('accessibility', () => {
  it('the button is named and described by its help line, and axe finds nothing', async () => {
    await renderPage();
    await click();
    const button = screen.getByRole('button', { name: WEBHOOK_BUTTON });
    expect(button).toHaveAttribute('aria-describedby', 'webhook-help');
    expect(screen.getByTestId('scheduler-webhook-help')).toHaveTextContent(WEBHOOK_HELP);
    const result = await axe.run(screen.getByTestId('scheduler-health-page'), {
      rules: { 'color-contrast': { enabled: false } },
    });
    expect(result.violations.map((v) => v.id)).toEqual([]);
  });
});
