/**
 * The flag's key, and archiving, in the dashboard (the flag guide's Step 2 and
 * "Archived flags").
 *
 * The pages run as in the browser; the API is mocked at `apiFetch`.
 *
 * What is pinned:
 *  - the key is filled in from the name, and an edited key is what is sent;
 *  - after the flag is created, the dashboard shows the key and offers no way
 *    to change it: the detail page has no key field, and what it sends never
 *    carries `key`;
 *  - neither the list nor the detail page offers an archive or unarchive
 *    action, for an active, an inactive or an archived flag.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import NewFeatureFlagPage from '@/pages/feature-flags/new';
import FeatureFlagDetailPage from '@/pages/feature-flags/[id]';
import FeatureFlagsPage from '@/pages/feature-flags/index';
import { ApiFetchOptions, apiFetch } from '@/services/api';
import { makeRouter, routedApi } from './helpers/apiMock';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

const mockRouter = makeRouter({ pathname: '/feature-flags/[id]', query: { id: 'flag-1' } });
jest.mock('next/router', () => ({ useRouter: () => mockRouter }));

const mockAuth = jest.fn();
jest.mock('@/contexts/AuthContext', () => ({ useOptionalAuth: () => mockAuth() }));

jest.mock('next/head', () => {
  const Head = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Head.displayName = 'MockHead';
  return Head;
});

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

let sent: Array<{ method: string; path: string; body: unknown }>;

function detailFlag(status: string) {
  return {
    id: 'flag-1',
    key: 'checkout_v2',
    name: 'Checkout v2',
    description: 'New checkout flow',
    status,
    rollout_percentage: 25,
    rules: null,
    owner_id: 'user-1',
    created_at: '2026-09-01T10:00:00Z',
    updated_at: '2026-09-05T10:00:00Z',
  };
}

function install(status = 'inactive') {
  const record = (method: string) => (path: string, options: ApiFetchOptions) => {
    sent.push({ method, path, body: options.json });
    return { ...detailFlag(status), rollout_percentage: 50 };
  };
  mockedApiFetch.mockImplementation(
    routedApi([
      { path: '/api/v1/feature-flags/flag-1', handler: () => detailFlag(status) },
      {
        path: '/api/v1/feature-flags',
        handler: () => ({
          items: [{ ...detailFlag(status), is_active: status === 'active' }],
          total: 1,
          skip: 0,
          limit: 100,
        }),
      },
      {
        path: '/api/v1/rollout-schedules',
        handler: () => ({ items: [], total: 0, skip: 0, limit: 50 }),
      },
      {
        path: '/api/v1/safety/feature-flags/flag-1/check',
        handler: () => ({ feature_flag_id: 'flag-1', is_healthy: true, metrics: [], last_checked: null }),
      },
      { method: 'PUT', path: /^\/api\/v1\/feature-flags\/flag-1$/, handler: record('PUT') },
      { method: 'POST', path: /^\/api\/v1\/feature-flags/, handler: record('POST') },
    ]) as unknown as typeof apiFetch,
  );
}

beforeEach(() => {
  mockedApiFetch.mockReset();
  mockAuth.mockReturnValue({
    status: 'authenticated',
    user: { id: 'u1', email: 'root@example.com', username: 'root', role: 'ADMIN', is_superuser: true },
  });
  sent = [];
});

describe('the key', () => {
  it('is filled in from the name, and an edited key is the one sent', async () => {
    install();
    render(<NewFeatureFlagPage />);
    fireEvent.change(screen.getByTestId('flag-name-input'), { target: { value: 'New Checkout Flow' } });
    expect(screen.getByTestId('flag-key-input')).toHaveValue('new_checkout_flow');

    fireEvent.change(screen.getByTestId('flag-key-input'), { target: { value: 'new-checkout-flow' } });
    fireEvent.change(screen.getByTestId('flag-name-input'), { target: { value: 'New Checkout Flow 2' } });
    expect(screen.getByTestId('flag-key-input')).toHaveValue('new-checkout-flow');

    fireEvent.click(screen.getByTestId('submit-flag'));
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0].body).toMatchObject({ key: 'new-checkout-flow', name: 'New Checkout Flow 2' });
  });

  it('cannot be changed in the dashboard once the flag exists', async () => {
    install();
    render(<FeatureFlagDetailPage />);
    expect(await screen.findByTestId('flag-key')).toHaveTextContent('checkout_v2');
    const fields = Array.from(document.querySelectorAll('input, textarea, select')) as HTMLInputElement[];
    expect(fields.some((field) => field.value === 'checkout_v2')).toBe(false);
    expect(screen.queryByLabelText(/^key/i)).toBeNull();

    // A save from the page leaves the key out.
    fireEvent.change(screen.getByTestId('rollout-percentage'), { target: { value: '60' } });
    fireEvent.click(screen.getByTestId('save-flag'));
    await waitFor(() => expect(sent.filter((r) => r.method === 'PUT')).toHaveLength(1));
    const [put] = sent.filter((r) => r.method === 'PUT');
    expect(put.body).toEqual({ rollout_percentage: 60 });
  });
});

describe('archiving', () => {
  it.each(['active', 'inactive', 'archived'])(
    'neither page offers archive or unarchive for an %s flag',
    async (status) => {
      install(status);
      const { unmount } = render(<FeatureFlagDetailPage />);
      await screen.findByTestId('flag-key');
      expect(screen.queryAllByRole('button', { name: /archive/i })).toEqual([]);
      expect(screen.queryAllByRole('link', { name: /archive/i })).toEqual([]);
      expect(screen.queryAllByRole('menuitem', { name: /archive/i })).toEqual([]);
      unmount();

      render(<FeatureFlagsPage />);
      await screen.findByTestId('flags-table');
      expect(screen.queryAllByRole('button', { name: /archive/i })).toEqual([]);
      expect(screen.queryAllByRole('link', { name: /archive/i })).toEqual([]);
      expect(screen.queryAllByRole('option', { name: /archive/i })).toEqual([]);
      const calls = mockedApiFetch.mock.calls.map(([path]) => String(path));
      expect(calls.filter((path) => /archive|bulk-toggle/.test(path))).toEqual([]);
    },
  );
});
