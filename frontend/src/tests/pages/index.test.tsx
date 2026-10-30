import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import HomePage from '@/pages/index';
import { AuthProvider } from '@/contexts/AuthContext';
import { TOKEN_STORAGE_KEY, UserMe } from '@/services/api';
import { routeKind } from '@/utils/routes';
import { isMarketingSite } from '@/utils/site-mode';

// The platform build unless a block says otherwise: the claims sweeps below
// render both builds.
jest.mock('@/utils/site-mode', () => ({
  ...jest.requireActual('@/utils/site-mode'),
  isMarketingSite: jest.fn(),
}));
const mode = isMarketingSite as jest.Mock;

const mockReplace = jest.fn().mockResolvedValue(true);

jest.mock('next/router', () => ({
  useRouter: () => ({
    replace: mockReplace,
    push: jest.fn(),
    pathname: '/',
    asPath: '/',
    query: {},
    isReady: true,
  }),
}));

jest.mock('next/head', () => {
  const MockHead = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  MockHead.displayName = 'MockHead';
  return MockHead;
});

const mockFetch = jest.fn();
global.fetch = mockFetch;

const user: UserMe = {
  id: 'u-1',
  email: 'admin@demo.com',
  username: 'admin',
  full_name: null,
  role: 'ADMIN',
  is_superuser: true,
  is_active: true,
  auth_provider: 'local',
};

beforeEach(() => {
  mockFetch.mockReset();
  mockReplace.mockClear();
  localStorage.clear();
  mode.mockReset().mockReturnValue(false);
});

describe('HomePage (/)', () => {
  it('shows an anonymous visitor the public homepage instead of redirecting', async () => {
    render(
      <AuthProvider>
        <HomePage />
      </AuthProvider>,
    );
    // Settle first. `waitFor` around a NEGATIVE assertion resolves on its first
    // synchronous check, so it adds no waiting at all and would pass while the
    // context was still `loading` -- green even if the redirect came back.
    await screen.findByRole('link', { name: /sign in to the dashboard/i });
    expect(screen.getByTestId('public-home')).toBeInTheDocument();
    expect(mockReplace).not.toHaveBeenCalled();
  });

  it('still offers a way in', () => {
    render(
      <AuthProvider>
        <HomePage />
      </AuthProvider>,
    );
    expect(screen.getAllByRole('link', { name: /sign in/i }).length).toBeGreaterThan(0);
  });

  it('redirects authenticated users to /experiments', async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, 'tok');
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      headers: { get: () => 'application/json' },
      json: () => Promise.resolve(user),
      text: () => Promise.resolve(JSON.stringify(user)),
    } as unknown as Response);
    render(
      <AuthProvider>
        <HomePage />
      </AuthProvider>,
    );
    await waitFor(() => expect(mockReplace).toHaveBeenCalledWith('/experiments'));
  });

  it('does not redirect while the session is still loading', () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, 'tok');
    mockFetch.mockImplementation(() => new Promise(() => {}));
    render(
      <AuthProvider>
        <HomePage />
      </AuthProvider>,
    );
    expect(mockReplace).not.toHaveBeenCalled();
    // and shows the page rather than a spinner, so there is no flash.
    expect(screen.getByTestId('public-home')).toBeInTheDocument();
  });

  /**
   * The homepage came back; the claims did not.
   *
   * The landing page that used to live at `/` advertised an event volume, an
   * uptime figure and two compliance certifications for software that has
   * never been deployed. `scripts/leak_guard.py` refuses to let a tree
   * containing them be pushed, so restoring that page would also have made the
   * repository unpublishable.
   *
   * WHY THE PATTERNS ARE ASSEMBLED RATHER THAN WRITTEN OUT. There is a real
   * tension here: a test asserting the absence of a forbidden string has to
   * name that string, and the guard forbids a tracked file from containing it.
   * Spelling them out failed the gate on this very pull request -- five hits,
   * all of them this table. So each is built from fragments at run time and the
   * literal never appears in the source. The guard stays strict and the test
   * keeps its teeth. `backend/tests/unit/scripts/test_leak_guard.py` assembles
   * its fixtures for the same reason, and found out the same way.
   *
   * These mirror the CLAIM_RULES in `scripts/leak_guard.py` but are NOT shared
   * with them -- nothing links the two, so keep them in step by hand and treat
   * the leak guard as the authority. This is the fast feedback, not the gate.
   *
   * Matched against `container.textContent`, not `queryByText`: the latter sees
   * only one element's direct text children, so a claim split across nested
   * elements is invisible to it, and this page has fragmented paragraphs.
   */
  const forbidden: Array<[string[], string]> = [
    [['SOC', '2'], 'a compliance certification nobody has audited'],
    [['ISO', '27001'], 'the same'],
    [['GDPR', 'Compliant'], 'the same'],
    [['SRM', 'detection'], "in the export's list"],
    [['Enterprise', 'Edition'], 'the editions no longer exist'],
    [['Community', 'Edition'], 'the same'],
    [['licen[cs]e', 'key'], 'the platform has no such gate'],
    [['\\b\\d+(\\.\\d+)?[MB]\\+'], 'a volume for something never deployed'],
    [['99\\.9'], 'an uptime figure with no deployment behind it'],
    [['\\d+%\\s+(\\w+\\s+)?(uptime|hit rate|cache hit|pass rate|coverage)'], 'the same'],
    [['start', 'free', 'trial'], 'there is no paid tier to trial'],
    [['pricing'], 'there is no price'],
  ];

  /**
   * Claims this page used to make that were not true (T86): an SDK count that
   * `sdk/` had outgrown, local bucketing that matched the server exactly (the
   * SDKs bucket nothing; the server decides), a benchmark that runs nightly
   * described as running locally, the AWS name used as a hyphenated
   * adjective or a possessive noun.
   *
   * The fragments are concatenated, not joined with whitespace, so each entry
   * is exactly the regular expression it reads as once put together; they are
   * split only so the old wording never appears in this file. The meta
   * description is not in `textContent`; the marketing build grep covers it.
   */
  const retired: Array<[string[], string]> = [
    [['AW', 'S-\\w'], 'the AWS name is not a hyphenated adjective'],
    [['on\\s+your', '\\s+AWS'], 'nor a noun the reader owns'],
    [['four', 'teen'], 'the SDK count drifted from sdk/; the list is derived now'],
    [['byte\\s+for', '\\s+byte'], 'the SDKs do not bucket locally'],
    [['asserted\\s+on\\s+every', '\\s+local\\s+run'], 'the benchmarks run nightly, not locally'],
  ];

  /**
   * No accessibility claim (T176 condition 13): the walkthrough transcripts
   * meet WCAG 1.2.3, and the audio-description gap (1.2.5) is accepted for
   * launch, so the site names no standard it would then have to meet. Not
   * leak-guard rules; concatenated like `retired`.
   */
  const unclaimed: Array<[string[], string]> = [
    [['accessi', 'b'], 'the site makes no accessibility claim'],
    [['WC', 'AG'], 'the same'],
  ];

  /**
   * Both builds. The marketing build renders text the platform build does
   * not -- the walkthroughs section (#1290) -- and these sweeps used to render
   * the platform build only, so a claim planted in marketing-only text passed
   * all of them (measured: 47 of 47 green). `index.site-mode.test.tsx` and
   * `walkthroughs.test.tsx` pin the rest of the marketing page.
   */
  describe.each([
    ['platform build', false],
    ['marketing build', true],
  ])('%s', (_build, marketing) => {
    beforeEach(() => mode.mockReturnValue(marketing));

    const text = () =>
      render(
        <AuthProvider>
          <HomePage />
        </AuthProvider>,
      ).container.textContent ?? '';

    it.each(forbidden)('makes no claim matching %s (%s)', (parts) => {
      expect(text()).not.toMatch(new RegExp((parts as string[]).join('\\s+'), 'i'));
    });

    it.each(retired)('no longer says %s (%s)', (parts) => {
      expect(text()).not.toMatch(new RegExp((parts as string[]).join(''), 'i'));
    });

    it.each(unclaimed)('claims no accessibility standard: %s (%s)', (parts) => {
      expect(text()).not.toMatch(new RegExp((parts as string[]).join(''), 'i'));
    });

    it('renders the marketing section only in the marketing build', () => {
      expect(text().includes('Walkthroughs')).toBe(marketing);
    });
  });

  it('renders nothing but the title once authenticated, so there is no flash', async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, 'tok');
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      headers: { get: () => 'application/json' },
      json: () => Promise.resolve(user),
      text: () => Promise.resolve(JSON.stringify(user)),
    } as unknown as Response);
    render(
      <AuthProvider>
        <HomePage />
      </AuthProvider>,
    );
    await waitFor(() => expect(mockReplace).toHaveBeenCalledWith('/experiments'));
    // The marketing page must not have been painted on the way through.
    expect(screen.queryByTestId('public-home')).not.toBeInTheDocument();
  });
});

describe('_app routeKind', () => {
  it('renders /login and / without the shell', () => {
    expect(routeKind('/login')).toBe('bare');
    expect(routeKind('/')).toBe('bare');
  });

  it('keeps /sso/complete bare, so RequireAuth never carries its fragment to /login (C2b)', () => {
    expect(routeKind('/sso/complete')).toBe('bare');
  });

  it('keeps docs, the power calculator, the privacy page and error pages open', () => {
    expect(routeKind('/docs')).toBe('open');
    expect(routeKind('/docs/[...slug]')).toBe('open');
    expect(routeKind('/power-calculator')).toBe('open');
    expect(routeKind('/privacy')).toBe('open');
    expect(routeKind('/404')).toBe('open');
  });

  it('protects everything else', () => {
    expect(routeKind('/experiments')).toBe('protected');
    expect(routeKind('/experiments/[id]')).toBe('protected');
    expect(routeKind('/feature-flags')).toBe('protected');
    expect(routeKind('/admin/users')).toBe('protected');
    expect(routeKind('/results/[id]')).toBe('protected');
    expect(routeKind('/workspaces/[id]/members')).toBe('protected');
    // #1455: the exclusion-group pages need a session (RequireAuth).
    expect(routeKind('/experiments/exclusion-groups')).toBe('protected');
    expect(routeKind('/experiments/exclusion-groups/[id]')).toBe('protected');
  });
});
