/**
 * The signed-in user's effective permissions (#891, S8, check #41).
 *
 * - asked for only when `/api/v1/modules` lists rbac, never in the core
 *   profile, and never for a superuser;
 * - a failed request leaves the role lists only (fail closed);
 * - asked again on navigation, keeping the last answer meanwhile, so a revoke
 *   reaches the dashboard by the next page;
 * - one user's answer is never shown for another;
 * - with a delayed answer a page shows neither the refusal note nor the
 *   control, then the control.
 *
 * Runs in both profiles' Jest: `@modules/rbac` is the real module in the full
 * profile and the throwing stub in the core one; both are spied on here.
 */
import React from 'react';
import { act, render, screen, waitFor } from '@testing-library/react';
import { RbacService } from '@modules/rbac';
import {
  EffectivePermissionsProvider,
  ROLE_LISTS_ONLY,
  useEffectivePermissions,
} from '@/contexts/EffectivePermissionsContext';
import { ModulesProvider, __resetModulesCache } from '@/contexts/ModulesContext';
import FeatureFlagsPage from '@/pages/feature-flags/index';
import { apiFetch } from '@/services/api';
import { CORE_PROFILE, ModulesInfo, ModulesService } from '@/services/modules';
import { FLAG_ROLE_NOTE } from '@/utils/experimentPermissions';
import { makeRouter, routedApi } from '../pages/helpers/apiMock';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

const mockRouter = makeRouter({ pathname: '/feature-flags', asPath: '/feature-flags' });
jest.mock('next/router', () => ({ useRouter: () => mockRouter }));

jest.mock('next/head', () => {
  const Head = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Head.displayName = 'MockHead';
  return Head;
});

const mockAuth = jest.fn();
jest.mock('@/contexts/AuthContext', () => ({ useOptionalAuth: () => mockAuth() }));

const FULL: ModulesInfo = { profile: 'full', modules: ['rbac'], version: 'test' };

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;
let getUserPermissions: jest.SpyInstance;

function signIn(role: string | null, id = 'u1', is_superuser = false) {
  mockAuth.mockReturnValue({
    status: 'authenticated',
    user: { id, email: `${id}@example.com`, username: id, role, is_superuser },
  });
}

function answer(permissions: Record<string, string[]>) {
  return {
    user_id: 'u1',
    username: 'u1',
    base_role: 'viewer',
    custom_roles: ['flag-editor'],
    permissions,
    is_superuser: false,
  };
}

/** A promise the test settles by hand. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (err: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** Shows what the hook answers. */
function Probe() {
  const { effective, loading } = useEffectivePermissions();
  return (
    <p data-testid="probe">
      {loading ? 'loading' : 'settled'} {effective === undefined ? 'none' : JSON.stringify(effective)}
    </p>
  );
}

function renderProbe(modules: ModulesInfo, navigationKey = '/a') {
  const ui = (key: string) => (
    <ModulesProvider initial={modules}>
      <EffectivePermissionsProvider navigationKey={key}>
        <Probe />
      </EffectivePermissionsProvider>
    </ModulesProvider>
  );
  const view = render(ui(navigationKey));
  return { ...view, navigate: (key: string) => view.rerender(ui(key)) };
}

beforeEach(() => {
  __resetModulesCache();
  mockAuth.mockReset();
  mockedApiFetch.mockReset();
  getUserPermissions = jest.spyOn(RbacService, 'getUserPermissions');
});

afterEach(() => {
  getUserPermissions.mockRestore();
});

describe('when the map is asked for', () => {
  it('full profile: asks once for the signed-in user, and serves the map', async () => {
    signIn('VIEWER');
    getUserPermissions.mockResolvedValue(answer({ feature_flag: ['create', 'read'] }));
    renderProbe(FULL);
    expect(screen.getByTestId('probe')).toHaveTextContent('loading none');
    await waitFor(() =>
      expect(screen.getByTestId('probe')).toHaveTextContent('settled {"feature_flag":["create","read"]}'),
    );
    expect(getUserPermissions).toHaveBeenCalledTimes(1);
    expect(getUserPermissions).toHaveBeenCalledWith('u1');
  });

  it('core profile: no RBAC request, and the role lists only', async () => {
    signIn('VIEWER');
    renderProbe(CORE_PROFILE);
    expect(screen.getByTestId('probe')).toHaveTextContent('settled none');
    await act(async () => {
      await Promise.resolve();
    });
    expect(getUserPermissions).not.toHaveBeenCalled();
    expect(mockedApiFetch.mock.calls.filter(([p]) => String(p).includes('/api/v1/rbac/'))).toEqual([]);
  });

  it('a superuser is never asked about: every check passes before any map', () => {
    signIn('VIEWER', 'su', true);
    renderProbe(FULL);
    expect(screen.getByTestId('probe')).toHaveTextContent('settled none');
    expect(getUserPermissions).not.toHaveBeenCalled();
  });

  it('nobody signed in: nothing is asked', () => {
    mockAuth.mockReturnValue({ status: 'anonymous', user: null });
    renderProbe(FULL);
    expect(screen.getByTestId('probe')).toHaveTextContent('settled none');
    expect(getUserPermissions).not.toHaveBeenCalled();
  });

  it('while the modules probe is outstanding: pending, and nothing asked yet', async () => {
    signIn('VIEWER');
    const probe = deferred<ModulesInfo>();
    const get = jest.spyOn(ModulesService, 'get').mockReturnValue(probe.promise);
    getUserPermissions.mockResolvedValue(answer({ experiment: ['create'] }));
    render(
      <ModulesProvider>
        <EffectivePermissionsProvider navigationKey="/a">
          <Probe />
        </EffectivePermissionsProvider>
      </ModulesProvider>,
    );
    expect(screen.getByTestId('probe')).toHaveTextContent('loading none');
    expect(getUserPermissions).not.toHaveBeenCalled();
    await act(async () => {
      probe.resolve(FULL);
    });
    await waitFor(() => expect(screen.getByTestId('probe')).toHaveTextContent('settled {"experiment":["create"]}'));
    get.mockRestore();
  });

  it('outside the provider: the role lists only', () => {
    signIn('VIEWER');
    render(<Probe />);
    expect(screen.getByTestId('probe')).toHaveTextContent('settled none');
    expect(ROLE_LISTS_ONLY).toEqual({ effective: undefined, loading: false });
  });
});

describe('a failed or odd answer gives the role lists only (fail closed)', () => {
  it('a rejected request', async () => {
    signIn('VIEWER');
    getUserPermissions.mockRejectedValue(new Error('boom'));
    renderProbe(FULL);
    await waitFor(() => expect(screen.getByTestId('probe')).toHaveTextContent('settled none'));
  });

  it('an answer whose permissions is not a map of lists', async () => {
    signIn('VIEWER');
    getUserPermissions.mockResolvedValue({ ...answer({}), permissions: ['experiment:create'] });
    renderProbe(FULL);
    await waitFor(() => expect(screen.getByTestId('probe')).toHaveTextContent('settled none'));
  });
});

describe('asked again on navigation (S8)', () => {
  it('a revoke reaches the dashboard by the next page, with no pending state between', async () => {
    signIn('VIEWER');
    const second = deferred<ReturnType<typeof answer>>();
    getUserPermissions
      .mockResolvedValueOnce(answer({ experiment: ['create'] }))
      .mockReturnValueOnce(second.promise);
    const view = renderProbe(FULL, '/experiments');
    await waitFor(() => expect(screen.getByTestId('probe')).toHaveTextContent('settled {"experiment":["create"]}'));

    view.navigate('/experiments/new');
    expect(getUserPermissions).toHaveBeenCalledTimes(2);
    // The last answer stays while the new one is on its way.
    expect(screen.getByTestId('probe')).toHaveTextContent('settled {"experiment":["create"]}');
    await act(async () => {
      second.resolve(answer({}));
    });
    expect(screen.getByTestId('probe')).toHaveTextContent('settled {}');
  });

  it('the same page re-rendered asks nothing more', async () => {
    signIn('VIEWER');
    getUserPermissions.mockResolvedValue(answer({}));
    const view = renderProbe(FULL, '/a');
    await waitFor(() => expect(screen.getByTestId('probe')).toHaveTextContent('settled {}'));
    view.navigate('/a');
    expect(getUserPermissions).toHaveBeenCalledTimes(1);
  });

  it("one user's map is never shown for the next user", async () => {
    signIn('VIEWER', 'u1');
    const forU2 = deferred<ReturnType<typeof answer>>();
    getUserPermissions.mockResolvedValueOnce(answer({ audit_log: ['read'] })).mockReturnValueOnce(forU2.promise);
    const view = renderProbe(FULL);
    await waitFor(() => expect(screen.getByTestId('probe')).toHaveTextContent('settled {"audit_log":["read"]}'));

    signIn('VIEWER', 'u2');
    view.navigate('/a');
    expect(screen.getByTestId('probe')).toHaveTextContent('loading none');
    expect(getUserPermissions).toHaveBeenLastCalledWith('u2');
    await act(async () => {
      forU2.resolve(answer({}));
    });
    expect(screen.getByTestId('probe')).toHaveTextContent('settled {}');
  });
});

describe('no flash: neither note nor control while the map is on its way, then the control', () => {
  function serveFlags() {
    mockedApiFetch.mockImplementation(
      routedApi([
        { path: '/api/v1/feature-flags', handler: () => ({ items: [], total: 0, skip: 0, limit: 100 }) },
      ]) as unknown as typeof apiFetch,
    );
  }

  function renderFlags(modules: ModulesInfo) {
    return render(
      <ModulesProvider initial={modules}>
        <EffectivePermissionsProvider navigationKey="/feature-flags">
          <FeatureFlagsPage />
        </EffectivePermissionsProvider>
      </ModulesProvider>,
    );
  }

  it('a VIEWER granted feature_flag (create) sees nothing, then "+ New Flag"', async () => {
    signIn('VIEWER');
    serveFlags();
    const pending = deferred<ReturnType<typeof answer>>();
    getUserPermissions.mockReturnValue(pending.promise);
    renderFlags(FULL);
    await screen.findByTestId('flags-empty');
    expect(screen.queryByTestId('new-flag-btn')).toBeNull();
    expect(screen.queryByTestId('flags-role-note')).toBeNull();

    await act(async () => {
      pending.resolve(answer({ feature_flag: ['create'] }));
    });
    expect(screen.getByTestId('new-flag-btn')).toBeInTheDocument();
    expect(screen.queryByTestId('flags-role-note')).toBeNull();
  });

  it('a VIEWER with no grant sees nothing, then the note', async () => {
    signIn('VIEWER');
    serveFlags();
    const pending = deferred<ReturnType<typeof answer>>();
    getUserPermissions.mockReturnValue(pending.promise);
    renderFlags(FULL);
    await screen.findByTestId('flags-empty');
    expect(screen.queryByTestId('flags-role-note')).toBeNull();

    await act(async () => {
      pending.resolve(answer({}));
    });
    expect(screen.getByTestId('flags-role-note')).toHaveTextContent(FLAG_ROLE_NOTE);
    expect(screen.queryByTestId('new-flag-btn')).toBeNull();
  });

  it('a failed request gives the role lists: the note, never the control', async () => {
    signIn('VIEWER');
    serveFlags();
    getUserPermissions.mockRejectedValue(new Error('down'));
    renderFlags(FULL);
    expect(await screen.findByTestId('flags-role-note')).toHaveTextContent(FLAG_ROLE_NOTE);
    expect(screen.queryByTestId('new-flag-btn')).toBeNull();
  });

  it('a DEVELOPER never waits: the control at once, whatever the map', async () => {
    signIn('DEVELOPER');
    serveFlags();
    getUserPermissions.mockReturnValue(new Promise(() => undefined));
    renderFlags(FULL);
    expect(screen.getByTestId('new-flag-btn')).toBeInTheDocument();
    await screen.findByTestId('flags-empty');
  });

  it('core profile: the note at once, and no RBAC request', async () => {
    signIn('VIEWER');
    serveFlags();
    renderFlags(CORE_PROFILE);
    expect(screen.getByTestId('flags-role-note')).toHaveTextContent(FLAG_ROLE_NOTE);
    await screen.findByTestId('flags-empty');
    expect(getUserPermissions).not.toHaveBeenCalled();
  });
});
