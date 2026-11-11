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
 *  - archiving (#442): the flag's page offers "Archive" on an active or an
 *    inactive flag and "Unarchive" on an archived one, to ADMIN and DEVELOPER
 *    (signed in as themselves, not as superusers); ANALYST and VIEWER get no
 *    such control and the role note. Archive asks first, in the shared
 *    Dialog: no request until it is confirmed there, Cancel has focus first,
 *    and Escape is Cancel. The request is one bulk toggle, and a 200 whose
 *    result says `success: false` is a refusal. Unarchive asks nothing. Every
 *    failure reads fixed copy, never the server's words;
 *  - an archived flag (#1748) reads "Archived", never "Off", on both pages;
 *    it has no on/off switch, which the API would refuse with 400, and in its
 *    place each page says to unarchive it before turning it on; the flag page
 *    links the docs. The list filters it with "Archived".
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import axe from 'axe-core';
import NewFeatureFlagPage from '@/pages/feature-flags/new';
import FeatureFlagDetailPage from '@/pages/feature-flags/[id]';
import FeatureFlagsPage from '@/pages/feature-flags/index';
import { ApiFetchOptions, apiFetch } from '@/services/api';
import { renderInAppRoot } from '@/tests/helpers/renderInAppRoot';
import { shapeErrors } from '@/tests/helpers/openapiShape';
import { apiError, makeRouter, routedApi } from './helpers/apiMock';

/** Where the "Archived flags" section of the flag guide is, in either docs mode. */
const ARCHIVED_DOCS_HREF = /feature-flags\/create(\.md|\/)#archived-flags$/;

/** The sentence both pages show for an archived flag where its switch would be, word for word. */
const ARCHIVED_NOTE_TEXT = 'This flag is archived. Unarchive it before turning it on.';

/** The role note, as the three flag pages show it (utils/experimentPermissions.ts). */
const ROLE_NOTE =
  'Feature flags are created and changed with the feature flag permissions, which the ADMIN and DEVELOPER roles have.';

/** The archive Dialog's sentence (PLAN-v1.1 DESIGN E1), word for word. */
const ARCHIVE_DESCRIPTION =
  'It stops serving: every evaluation answers it as off, with its off value. You can unarchive it later.';

const ARCHIVE_REFUSED = 'Could not archive this flag.';
const UNARCHIVED = 'Unarchived. The flag is off; turn it on when you are ready.';

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

  /** Signed in as the role itself: a superuser would pass every check (CLAUDE.md). */
  function signIn(role: string) {
    mockAuth.mockReturnValue({
      status: 'authenticated',
      user: { id: 'u1', email: 'u@example.com', username: 'u', role, is_superuser: false },
    });
  }

  /** `FeatureFlagRead`, as `POST /feature-flags/{id}/unarchive` answers. */
  function readFlag(status: string) {
    return {
      id: 'flag-1',
      key: 'checkout_v2',
      name: 'Checkout v2',
      description: 'New checkout flow',
      status,
      is_active: status === 'active',
      rollout_percentage: 25,
      targeting_rules: null,
      default_value: false,
      value_type: 'boolean',
      on_value: true,
      off_value: false,
      tags: [],
      owner_id: 'user-1',
      created_at: '2026-09-01T10:00:00Z',
      updated_at: '2026-09-06T10:00:00Z',
    };
  }

  /** `BulkToggleResponse` for one flag: archived, or refused with the server's own words. */
  function bulkAnswer(success: boolean) {
    return {
      total: 1,
      succeeded: success ? 1 : 0,
      failed: success ? 0 : 1,
      results: [
        success
          ? { flag_id: 'flag-1', flag_key: 'checkout_v2', success: true, error: null, old_status: 'INACTIVE', new_status: 'ARCHIVED' }
          : {
              flag_id: 'flag-1',
              flag_key: 'checkout_v2',
              success: false,
              error: 'Not enough permissions to change this feature flag',
              old_status: null,
              new_status: null,
            },
      ],
      audit_log_ids: success ? ['log-1'] : [],
    };
  }

  type Answer = (path: string, options: ApiFetchOptions) => unknown;

  /** The flag page's reads, plus the two archiving routes (archive, then unarchive, by default succeed). */
  function installArchiving(status: string, answers: { bulk?: Answer; unarchive?: Answer } = {}) {
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
        { path: '/api/v1/rollout-schedules', handler: () => ({ items: [], total: 0, skip: 0, limit: 50 }) },
        {
          path: '/api/v1/safety/feature-flags/flag-1/check',
          handler: () => ({ feature_flag_id: 'flag-1', is_healthy: true, metrics: [], last_checked: null }),
        },
        {
          method: 'POST',
          path: '/api/v1/feature-flags/bulk-toggle',
          handler: answers.bulk ?? (() => bulkAnswer(true)),
        },
        {
          method: 'POST',
          path: '/api/v1/feature-flags/flag-1/unarchive',
          handler: answers.unarchive ?? (() => readFlag('inactive')),
        },
      ]) as unknown as typeof apiFetch,
    );
  }

  /** Every archive or unarchive request the page made. */
  const archiveCalls = () =>
    mockedApiFetch.mock.calls.filter(([path]) => /archive|bulk-toggle/.test(String(path)));

  /** The element's whole text, which must be the sentence and nothing else. */
  const textOf = (el: HTMLElement) => (el.textContent ?? '').trim();

  async function openArchiveDialog() {
    fireEvent.click(await screen.findByRole('button', { name: 'Archive' }));
    return screen.getByRole('dialog', { name: 'Archive Checkout v2?' });
  }

  it('the mocks are what the API sends and accepts', () => {
    expect(shapeErrors('BulkToggleResponse', bulkAnswer(true))).toEqual([]);
    expect(shapeErrors('BulkToggleResponse', bulkAnswer(false))).toEqual([]);
    expect(shapeErrors('FeatureFlagRead', readFlag('inactive'))).toEqual([]);
  });

  it.each(['active', 'inactive'])(
    'ADMIN and DEVELOPER see Archive on an %s flag and Unarchive on an archived one',
    async (status) => {
      for (const role of ['ADMIN', 'DEVELOPER']) {
        signIn(role);
        installArchiving(status);
        const first = render(<FeatureFlagDetailPage />);
        const control = await screen.findByTestId('flag-archive');
        expect(screen.getByRole('group', { name: 'Manage this flag' })).toContainElement(control);
        expect(within(control).getByRole('button', { name: 'Archive' })).toBeEnabled();
        expect(screen.queryAllByRole('button', { name: /^unarchive$/i })).toEqual([]);
        expect(screen.queryByTestId('flag-role-note')).toBeNull();
        first.unmount();

        installArchiving('archived');
        const second = render(<FeatureFlagDetailPage />);
        const again = await screen.findByTestId('flag-archive');
        expect(within(again).getByRole('button', { name: 'Unarchive' })).toBeEnabled();
        expect(screen.queryAllByRole('button', { name: /^archive$/i })).toEqual([]);
        second.unmount();
      }
      // Offering is not doing: no page load sent either request.
      expect(archiveCalls()).toEqual([]);
    },
  );

  it('ANALYST and VIEWER see no archive control and the role note', async () => {
    for (const role of ['ANALYST', 'VIEWER']) {
      for (const status of ['active', 'inactive', 'archived']) {
        signIn(role);
        installArchiving(status);
        const { unmount } = render(<FeatureFlagDetailPage />);
        expect(textOf(await screen.findByTestId('flag-role-note'))).toBe(ROLE_NOTE);
        expect(screen.getByTestId('flag-key')).toHaveTextContent('checkout_v2');
        expect(screen.queryByTestId('flag-archive')).toBeNull();
        expect(screen.queryByRole('group', { name: 'Manage this flag' })).toBeNull();
        expect(screen.queryAllByRole('button', { name: ACTION })).toEqual([]);
        unmount();
      }
    }
    expect(archiveCalls()).toEqual([]);
  });

  it('no bulk-toggle call before Archive is confirmed in the dialog', async () => {
    signIn('DEVELOPER');
    installArchiving('active');
    renderInAppRoot(<FeatureFlagDetailPage />);
    const opener = await screen.findByRole('button', { name: 'Archive' });

    // Opening the dialog sends nothing, and Escape is Cancel.
    let dialog = await openArchiveDialog();
    await act(async () => undefined);
    expect(archiveCalls()).toEqual([]);
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toHaveFocus();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(opener).toHaveFocus();
    expect(archiveCalls()).toEqual([]);

    // So is Cancel.
    dialog = await openArchiveDialog();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(opener).toHaveFocus();
    expect(archiveCalls()).toEqual([]);

    // Archive in the dialog sends one request, the documented body.
    dialog = await openArchiveDialog();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Archive' }));
    await waitFor(() => expect(archiveCalls()).toHaveLength(1));
    const [[path, options]] = archiveCalls();
    expect(path).toBe('/api/v1/feature-flags/bulk-toggle');
    expect(options?.method).toBe('POST');
    expect(options?.json).toEqual({ flag_ids: ['flag-1'], action: 'archive' });
    expect(shapeErrors('BulkToggleRequest', options?.json)).toEqual([]);
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(archiveCalls()).toHaveLength(1);
  });

  it('the dialog asks "Archive {name}?" in these words, Cancel first, and axe finds nothing in it', async () => {
    signIn('ADMIN');
    installArchiving('inactive');
    renderInAppRoot(<FeatureFlagDetailPage />);
    const dialog = await openArchiveDialog();
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(dialog).toHaveAccessibleDescription(ARCHIVE_DESCRIPTION);
    expect(within(dialog).getAllByRole('button').map(textOf)).toEqual(['Cancel', 'Archive']);
    const result = await axe.run(dialog, { rules: { 'color-contrast': { enabled: false } } });
    expect(result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(', ')}`)).toEqual([]);
  });

  it('confirming archives the flag: it reads Archived, with no switch, and offers Unarchive', async () => {
    signIn('DEVELOPER');
    installArchiving('active');
    renderInAppRoot(<FeatureFlagDetailPage />);
    expect(await screen.findByTestId('flag-status')).toHaveTextContent(/^On$/);
    expect(screen.getByRole('switch')).toHaveAttribute('aria-checked', 'true');

    const dialog = await openArchiveDialog();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Archive' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());

    expect(screen.getByTestId('flag-status')).toHaveTextContent(/^Archived$/);
    expect(screen.queryByRole('switch')).toBeNull();
    expect(screen.getByTestId('flag-archived-note')).toHaveTextContent(ARCHIVED_NOTE_TEXT);
    const unarchive = within(screen.getByTestId('flag-archive')).getByRole('button', { name: 'Unarchive' });
    await waitFor(() => expect(unarchive).toHaveFocus());
    await waitFor(() => expect(textOf(screen.getByTestId('flag-manage-status'))).toBe('Archived Checkout v2.'));
  });

  it('a 200 whose result says success: false reads "Could not archive this flag." and the flag stays as it was', async () => {
    signIn('ADMIN');
    installArchiving('inactive', { bulk: () => bulkAnswer(false) });
    renderInAppRoot(<FeatureFlagDetailPage />);
    const dialog = await openArchiveDialog();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Archive' }));

    expect(textOf(await within(dialog).findByRole('alert'))).toBe(ARCHIVE_REFUSED);
    expect(screen.queryByText(/Not enough permissions/)).toBeNull();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('dialog')).toBeNull();

    expect(screen.getByTestId('flag-status')).toHaveTextContent(/^Off$/);
    expect(screen.getByRole('switch')).toHaveAttribute('aria-checked', 'false');
    expect(screen.queryByTestId('flag-archived-note')).toBeNull();
    expect(within(screen.getByTestId('flag-archive')).getByRole('button', { name: 'Archive' })).toBeInTheDocument();
    expect(textOf(screen.getByTestId('flag-manage-status'))).toBe('');
  });

  it.each([
    [403, 'Archiving a flag needs permission to change feature flags, which the ADMIN and DEVELOPER roles have.'],
    [422, ARCHIVE_REFUSED],
    [500, ARCHIVE_REFUSED],
  ])("a %s from the archive request reads fixed copy, never the server's words", async (status, copy) => {
    signIn('ADMIN');
    installArchiving('inactive', {
      bulk: () => {
        throw apiError(status, 'server words that must not be shown');
      },
    });
    renderInAppRoot(<FeatureFlagDetailPage />);
    const dialog = await openArchiveDialog();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Archive' }));
    expect(textOf(await within(dialog).findByRole('alert'))).toBe(copy);
    expect(screen.queryByText(/server words/)).toBeNull();
    expect(screen.getByTestId('flag-status')).toHaveTextContent(/^Off$/);
  });

  it('Unarchive asks nothing, sends one POST /unarchive, and the flag reads Off with its switch back', async () => {
    signIn('DEVELOPER');
    installArchiving('archived');
    renderInAppRoot(<FeatureFlagDetailPage />);
    expect(screen.queryByRole('switch')).toBeNull();
    fireEvent.click(await screen.findByRole('button', { name: 'Unarchive' }));
    expect(screen.queryByRole('dialog')).toBeNull();

    await waitFor(() => expect(textOf(screen.getByTestId('flag-manage-status'))).toBe(UNARCHIVED));
    expect(archiveCalls().map(([path, options]) => `${options?.method} ${path}`)).toEqual([
      'POST /api/v1/feature-flags/flag-1/unarchive',
    ]);
    expect(screen.getByTestId('flag-status')).toHaveTextContent(/^Off$/);
    expect(screen.getByRole('switch')).toHaveAttribute('aria-checked', 'false');
    expect(screen.queryByTestId('flag-archived-note')).toBeNull();
    await waitFor(() =>
      expect(within(screen.getByTestId('flag-archive')).getByRole('button', { name: 'Archive' })).toHaveFocus(),
    );
  });

  it.each([
    [403, 'Unarchiving a flag needs permission to change feature flags, which the ADMIN and DEVELOPER roles have.'],
    [404, 'This flag no longer exists.'],
    [500, 'Could not unarchive this flag.'],
  ])("a %s from the unarchive request reads fixed copy, never the server's words", async (status, copy) => {
    signIn('ADMIN');
    installArchiving('archived', {
      unarchive: () => {
        throw apiError(status, 'server words that must not be shown');
      },
    });
    renderInAppRoot(<FeatureFlagDetailPage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Unarchive' }));
    expect(textOf(await screen.findByTestId('flag-manage-error'))).toBe(copy);
    expect(screen.queryByText(/server words/)).toBeNull();
    expect(screen.getByTestId('flag-status')).toHaveTextContent(/^Archived$/);
    expect(textOf(screen.getByTestId('flag-manage-status'))).toBe('');
    // A refused unarchive leaves the way to try again.
    await act(async () => undefined);
    expect(within(screen.getByTestId('flag-archive')).getByRole('button', { name: 'Unarchive' })).toBeEnabled();
  });
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

  it('reads Archived on its page, with no switch, and says to unarchive it before turning it on', async () => {
    install('archived');
    render(<FeatureFlagDetailPage />);
    const pill = await screen.findByTestId('flag-status');
    expect(pill).toHaveTextContent(/^Archived$/);
    expect(pill).toHaveAttribute('data-status', 'archived');
    expect(screen.queryByRole('switch')).toBeNull();
    expect(screen.queryByTestId('flag-toggle')).toBeNull();

    const note = screen.getByTestId('flag-archived-note');
    expect(note).toHaveTextContent(`${ARCHIVED_NOTE_TEXT} Read about archived flags.`, { normalizeWhitespace: true });
    expect(note.textContent).not.toMatch(/API call|\/unarchive/);
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

  it('reads Archived in the list, with no switch in its row and a note to unarchive it before turning it on', async () => {
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
    const help = within(archived).getByTestId('flag-archived-help-old_banner');
    expect((help.textContent ?? '').trim()).toBe(ARCHIVED_NOTE_TEXT);
    // Unarchive is on the flag's page; the row does not send anyone to the API.
    expect(within(archived).queryByRole('link', { name: /unarchive/i })).toBeNull();
    expect(within(archived).queryByRole('button', { name: /archive/i })).toBeNull();

    // The other rows are as they were.
    expect(within(off).getByTestId('flag-status-pill')).toHaveTextContent(/^Off$/);
    expect(within(off).getByRole('switch')).toHaveAttribute('aria-checked', 'false');
    expect(within(on).getByTestId('flag-status-pill')).toHaveTextContent(/^On$/);
    expect(within(on).getByRole('switch')).toHaveAttribute('aria-checked', 'true');
    expect(within(off).queryByTestId('flag-archived-help-dark_mode')).toBeNull();
    expect(within(on).queryByTestId('flag-archived-help-new_nav')).toBeNull();
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
