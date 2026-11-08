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
 *    action, for an active, an inactive or an archived flag: no button, no
 *    menu item, no request, and the only links that name archiving go to the
 *    docs;
 *  - an archived flag (#1748) reads "Archived", never "Off", on both pages;
 *    it has no on/off switch, which the API would refuse with 400; and each
 *    page says how to unarchive it (through the API) and links the docs. The
 *    list filters it with "Archived".
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import axe from 'axe-core';
import NewFeatureFlagPage from '@/pages/feature-flags/new';
import FeatureFlagDetailPage from '@/pages/feature-flags/[id]';
import FeatureFlagsPage from '@/pages/feature-flags/index';
import { ApiFetchOptions, apiFetch } from '@/services/api';
import { makeRouter, routedApi } from './helpers/apiMock';

/** Where the "Archived flags" section of the flag guide is, in either docs mode. */
const ARCHIVED_DOCS_HREF = /feature-flags\/create(\.md|\/)#archived-flags$/;

/** The sentence the flag page shows for an archived flag, pinned word for word. */
const ARCHIVED_NOTE_TEXT =
  'This flag is archived: every evaluation answers it as off, and it cannot be turned on until it ' +
  'is unarchived. Unarchiving is an API call for now, POST /api/v1/feature-flags/{id}/unarchive ' +
  '(beta), and the flag comes back off.';

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
  /** The verb, not the state: "Archived" (a filter, a status) is not an action. */
  const ACTION = /\b(un)?archive\b/i;

  /** Every link that names archiving goes to the docs, never to an action. */
  function linksNamingArchiveGoToTheDocs() {
    for (const link of screen.queryAllByRole('link', { name: /archiv/i })) {
      expect(link).toHaveAttribute('href', expect.stringMatching(ARCHIVED_DOCS_HREF));
    }
  }

  it.each(['active', 'inactive', 'archived'])(
    'neither page offers archive or unarchive for an %s flag',
    async (status) => {
      install(status);
      const { unmount } = render(<FeatureFlagDetailPage />);
      await screen.findByTestId('flag-key');
      expect(screen.queryAllByRole('button', { name: ACTION })).toEqual([]);
      expect(screen.queryAllByRole('menuitem', { name: /archive/i })).toEqual([]);
      linksNamingArchiveGoToTheDocs();
      unmount();

      render(<FeatureFlagsPage />);
      await screen.findByTestId('flags-table');
      expect(screen.queryAllByRole('button', { name: ACTION })).toEqual([]);
      expect(screen.queryAllByRole('option', { name: /archive/i })).toEqual([]);
      linksNamingArchiveGoToTheDocs();
      const calls = mockedApiFetch.mock.calls.map(([path]) => String(path));
      expect(calls.filter((path) => /archive|bulk-toggle/.test(path))).toEqual([]);
    },
  );
});

// ---------------------------------------------------------------------------
// #1748: an archived flag is shown as archived, with no switch the API refuses
// ---------------------------------------------------------------------------

describe('an archived flag (#1748)', () => {
  function listOf(items: Array<{ id: string; key: string; status: string }>) {
    mockedApiFetch.mockImplementation(
      routedApi([
        {
          path: '/api/v1/feature-flags',
          handler: () => ({
            items: items.map(({ id, key, status }) => ({
              ...detailFlag(status),
              id,
              key,
              name: key,
              is_active: status === 'active',
            })),
            total: items.length,
            skip: 0,
            limit: 100,
          }),
        },
      ]) as unknown as typeof apiFetch,
    );
  }

  it('reads Archived on its page, with no switch, and says how to unarchive it', async () => {
    install('archived');
    render(<FeatureFlagDetailPage />);
    const pill = await screen.findByTestId('flag-status');
    expect(pill).toHaveTextContent(/^Archived$/);
    expect(pill).toHaveAttribute('data-status', 'archived');
    expect(screen.queryByRole('switch')).toBeNull();
    expect(screen.queryByTestId('flag-toggle')).toBeNull();

    const note = screen.getByTestId('flag-archived-note');
    expect(note).toHaveTextContent(ARCHIVED_NOTE_TEXT);
    expect(within(note).getByRole('link', { name: 'Read about archived flags' })).toHaveAttribute(
      'href',
      expect.stringMatching(ARCHIVED_DOCS_HREF),
    );
  });

  it('an inactive flag keeps reading Off, with its switch and no archived note', async () => {
    install('inactive');
    render(<FeatureFlagDetailPage />);
    expect(await screen.findByTestId('flag-status')).toHaveTextContent(/^Off$/);
    expect(screen.getByRole('switch')).toHaveAttribute('aria-checked', 'false');
    expect(screen.queryByTestId('flag-archived-note')).toBeNull();
  });

  it('reads Archived in the list, with no switch in its row and a link saying how to unarchive it', async () => {
    listOf([
      { id: 'flag-1', key: 'old_banner', status: 'archived' },
      { id: 'flag-2', key: 'dark_mode', status: 'inactive' },
      { id: 'flag-3', key: 'new_nav', status: 'active' },
    ]);
    render(<FeatureFlagsPage />);
    const table = await screen.findByTestId('flags-table');
    const [archived, off, on] = within(table).getAllByTestId('flag-row');

    expect(within(archived).getByTestId('flag-status-pill')).toHaveTextContent(/^Archived$/);
    expect(within(archived).queryByRole('switch')).toBeNull();
    const help = within(archived).getByRole('link', { name: /unarchive through the api/i });
    expect(help).toHaveTextContent('Unarchive through the API');
    expect(help).toHaveAccessibleName('Unarchive through the API (old_banner: read about archived flags)');
    expect(help).toHaveAttribute('href', expect.stringMatching(ARCHIVED_DOCS_HREF));

    // The other rows are as they were.
    expect(within(off).getByTestId('flag-status-pill')).toHaveTextContent(/^Off$/);
    expect(within(off).getByRole('switch')).toHaveAttribute('aria-checked', 'false');
    expect(within(on).getByTestId('flag-status-pill')).toHaveTextContent(/^On$/);
    expect(within(on).getByRole('switch')).toHaveAttribute('aria-checked', 'true');
    expect(within(off).queryByRole('link', { name: /unarchive/i })).toBeNull();
  });

  it('axe finds nothing on either page for an archived flag', async () => {
    // Scoped to what the page renders, as the other axe checks here are: the
    // pages render without the app shell's landmarks.
    const noViolations = async (container: HTMLElement) => {
      const result = await axe.run(container, { rules: { 'color-contrast': { enabled: false } } });
      expect(result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(', ')}`)).toEqual([]);
    };
    install('archived');
    const detail = render(<FeatureFlagDetailPage />);
    await screen.findByTestId('flag-archived-note');
    await noViolations(detail.container);
    detail.unmount();

    listOf([{ id: 'flag-1', key: 'old_banner', status: 'archived' }]);
    const list = render(<FeatureFlagsPage />);
    await screen.findByTestId('flags-table');
    await noViolations(list.container);
  });

  it('the list filters archived flags, asking the API for status ARCHIVED', async () => {
    listOf([{ id: 'flag-1', key: 'old_banner', status: 'archived' }]);
    render(<FeatureFlagsPage />);
    await screen.findByTestId('flags-table');
    const filter = screen.getByTestId('flag-filter-archived');
    expect(filter).toHaveTextContent(/^Archived$/);
    expect(filter).toHaveAttribute('aria-pressed', 'false');

    fireEvent.click(filter);
    await waitFor(() => expect(filter).toHaveAttribute('aria-pressed', 'true'));
    await waitFor(() =>
      expect(
        mockedApiFetch.mock.calls.some(
          ([path, options]) =>
            path === '/api/v1/feature-flags' &&
            (options as ApiFetchOptions | undefined)?.query?.status === 'ARCHIVED',
        ),
      ).toBe(true),
    );
  });
});
