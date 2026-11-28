/**
 * "Check a user id" on the global-holdouts page (#2276): `GET
 * /api/v1/holdout/check/{user_id}`.
 *
 * Shown to the people the route accepts (the experiment create check): ADMIN,
 * DEVELOPER and superusers. ANALYST and VIEWER load the page, see no control
 * and send nothing. Each case renders the page module with `apiFetch` mocked
 * and a RESOLVED user.
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import axe from 'axe-core';
import HoldoutsPage from '@/pages/experiments/holdouts';
import { ApiFetchOptions, apiFetch } from '@/services/api';
import { UNREACHABLE } from '@/components/experiments/ExperimentManageSection';
import { HOLDOUT_COPY } from '@/utils/holdouts';
import { apiError, makeRouter, routedApi } from './helpers/apiMock';
import { shapeErrors } from '../helpers/openapiShape';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

const mockRouter = makeRouter({ pathname: '/experiments/holdouts', asPath: '/experiments/holdouts' });
jest.mock('next/router', () => ({ useRouter: () => mockRouter }));

jest.mock('next/head', () => {
  const Head = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Head.displayName = 'MockHead';
  return Head;
});

const mockAuth = jest.fn();
jest.mock('@/contexts/AuthContext', () => ({
  useOptionalAuth: () => mockAuth(),
  useAuth: () => mockAuth(),
}));

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

const CHECK = '/api/v1/holdout/check/';
const PLANTED = 'Traceback (most recent call last): PLANTED-check-7e1';

type Reply = (path: string, options: ApiFetchOptions) => unknown;

const ACTIVE_HOLDOUT = {
  id: 'h-q4',
  name: 'Q4 holdout',
  description: null,
  holdout_percentage: 5,
  is_active: true,
  owner_id: null,
  activated_at: '2026-10-01T09:00:00',
  deactivated_at: null,
  created_at: '2026-09-01T10:00:00',
  updated_at: '2026-09-01T10:00:00',
};

function serve(check: Reply) {
  mockedApiFetch.mockImplementation(
    routedApi([
      { path: '/api/v1/holdout/all', handler: () => ({ items: [ACTIVE_HOLDOUT], total: 1 }) },
      { path: '/api/v1/holdout', handler: () => ACTIVE_HOLDOUT },
      { path: new RegExp(`^${CHECK}`), handler: check },
    ]) as unknown as typeof apiFetch,
  );
}

const answer = (user_id: string, is_in_holdout: boolean, bucket: number, holdout_percentage = 5) => ({
  user_id,
  is_in_holdout,
  holdout_percentage,
  bucket,
});

function signIn(role: string, superuser = false) {
  mockAuth.mockReturnValue({
    user: { id: 'u-1', email: `${role.toLowerCase()}@demo.com`, username: role.toLowerCase(), role, is_superuser: superuser },
    status: 'authenticated',
  });
}

const toTheRoute = () => mockedApiFetch.mock.calls.filter(([p]) => String(p).startsWith(CHECK));
const type = (value: string) => fireEvent.change(screen.getByTestId('holdout-check-user'), { target: { value } });
const submit = () => fireEvent.click(screen.getByTestId('holdout-check-submit'));

async function open() {
  render(<HoldoutsPage />);
  await screen.findByTestId('holdouts-page');
}

beforeEach(() => {
  mockedApiFetch.mockReset();
  signIn('ADMIN');
  serve((path) => answer(decodeURIComponent(path.slice(CHECK.length)), true, 3));
});

describe('who sees the check', () => {
  it.each([
    ['superuser VIEWER', 'VIEWER', true],
    ['ADMIN', 'ADMIN', false],
    ['DEVELOPER', 'DEVELOPER', false],
  ])('%s sees it, and the witness is on the loaded page', async (_name, role, superuser) => {
    signIn(role, superuser);
    await open();
    expect(screen.getByTestId('holdout-check-form')).toBeInTheDocument();
    expect(screen.getByLabelText('User id')).toBe(screen.getByTestId('holdout-check-user'));
  });

  it.each([['ANALYST'], ['VIEWER']])('%s loads the page, sees no check, and sends nothing', async (role) => {
    signIn(role);
    render(<HoldoutsPage />);
    await screen.findByTestId('holdouts-refused');
    expect(screen.queryByTestId('holdout-check-form')).toBeNull();
    expect(toTheRoute()).toEqual([]);
    expect(mockedApiFetch).not.toHaveBeenCalled();
  });
});

describe('the field and the button', () => {
  it('Check is disabled while the field is empty, and enabled with anything in it, a space included', async () => {
    await open();
    expect(screen.getByTestId('holdout-check-submit')).toBeDisabled();
    type(' ');
    expect(screen.getByTestId('holdout-check-submit')).toBeEnabled();
    type('');
    expect(screen.getByTestId('holdout-check-submit')).toBeDisabled();
    fireEvent.submit(screen.getByTestId('holdout-check-form'));
    expect(toTheRoute()).toEqual([]);
  });

  it('sends the id exactly as typed: capitals and spaces kept, nothing trimmed', async () => {
    await open();
    type(' User-ABC 1 ');
    submit();
    await screen.findByTestId('holdout-check-answer');
    expect(toTheRoute()).toHaveLength(1);
    expect(toTheRoute()[0][0]).toBe(`${CHECK}${encodeURIComponent(' User-ABC 1 ')}`);
    expect(decodeURIComponent(String(toTheRoute()[0][0]).slice(CHECK.length))).toBe(' User-ABC 1 ');
  });

  it('an id with a slash is refused at the field before anything is sent', async () => {
    await open();
    type('a/b');
    submit();
    expect(await screen.findByTestId('holdout-check-error')).toHaveTextContent(HOLDOUT_COPY.checkIdSlash);
    expect(screen.getByTestId('holdout-check-user')).toHaveAttribute('aria-invalid', 'true');
    expect(toTheRoute()).toEqual([]);
  });
});

describe('the ids . and .. are refused like a slash', () => {
  it.each(['.', '..'])('the id %s is refused at the field before anything is sent', async (id) => {
    await open();
    type(id);
    submit();
    expect(await screen.findByTestId('holdout-check-error')).toHaveTextContent(HOLDOUT_COPY.checkIdSlash);
    expect(screen.getByTestId('holdout-check-user')).toHaveAttribute('aria-invalid', 'true');
    expect(toTheRoute()).toEqual([]);
  });
});

describe('the answer', () => {
  it('names the id, the bucket and the holdout size when the user is in it', async () => {
    serve(() => answer('u-17', true, 3));
    await open();
    type('u-17');
    submit();
    expect(await screen.findByTestId('holdout-check-answer')).toHaveTextContent(
      'u-17 is in the global holdout (bucket 3; the holdout keeps 5%).',
    );
  });

  it('says "is not in" when the bucket is outside it', async () => {
    serve(() => answer('u-18', false, 62));
    await open();
    type('u-18');
    submit();
    expect(await screen.findByTestId('holdout-check-answer')).toHaveTextContent(
      'u-18 is not in the global holdout (bucket 62; the holdout keeps 5%).',
    );
  });

  it('says none is active when the holdout size is 0', async () => {
    serve(() => answer('u-19', false, 0, 0));
    await open();
    type('u-19');
    submit();
    expect(await screen.findByTestId('holdout-check-answer')).toHaveTextContent(
      'u-19 is not in the global holdout: none is active.',
    );
  });

  it('the answers used here fit the API, and one that does not is caught', () => {
    expect(shapeErrors('HoldoutCheckResponse', answer('u', true, 3))).toEqual([]);
    expect(shapeErrors('HoldoutCheckResponse', { ...answer('u', true, 3), bucket: '3' })).not.toEqual([]);
  });

  it('editing the field clears the answer, so it never sits beside another id', async () => {
    await open();
    type('u-1');
    submit();
    await screen.findByTestId('holdout-check-answer');
    type('u-12');
    expect(screen.queryByTestId('holdout-check-answer')).toBeNull();
  });

  it('a slow answer for an id that was edited away is not shown', async () => {
    let release: (value: unknown) => void = () => undefined;
    serve(() => new Promise((resolve) => (release = resolve)));
    await open();
    type('u-1');
    submit();
    await waitFor(() => expect(toTheRoute()).toHaveLength(1));
    type('u-2');
    await act(async () => {
      release(answer('u-1', true, 3));
    });
    expect(screen.queryByTestId('holdout-check-answer')).toBeNull();
    expect(screen.getByTestId('holdout-check-submit')).toBeEnabled();
  });
});

describe('a refusal shows fixed copy, never the server detail', () => {
  it.each([
    [0, UNREACHABLE],
    [403, HOLDOUT_COPY.checkRefused],
    [404, HOLDOUT_COPY.checkNotFound],
    [422, HOLDOUT_COPY.checkInvalid],
    [500, HOLDOUT_COPY.checkFailed],
  ])('a %s', async (status, copy) => {
    serve(() => {
      throw apiError(status, PLANTED);
    });
    await open();
    type('u-1');
    submit();
    const alert = await screen.findByTestId('holdout-check-error');
    expect(alert).toHaveTextContent(copy);
    expect(alert).toHaveAttribute('role', 'alert');
    expect(document.body.textContent).not.toContain('PLANTED');
    expect(screen.getByTestId('holdout-check-user').getAttribute('aria-describedby')).toContain('holdout-check-error');
  });
});

describe('accessibility', () => {
  it('the field has a label and help, and the form has no violations in each state', async () => {
    await open();
    const form = () => screen.getByTestId('holdout-check-form').parentElement as HTMLElement;
    const clean = async () => {
      const result = await axe.run(form(), { rules: { 'color-contrast': { enabled: false } } });
      expect(result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(', ')}`)).toEqual([]);
    };
    await clean();
    type('u-1');
    submit();
    await screen.findByTestId('holdout-check-answer');
    await clean();
    type('a/b');
    submit();
    await screen.findByTestId('holdout-check-error');
    await clean();
    expect(screen.getByTestId('holdout-check-user').getAttribute('aria-describedby')).toBe(
      'holdout-check-help holdout-check-error',
    );
  });
});
