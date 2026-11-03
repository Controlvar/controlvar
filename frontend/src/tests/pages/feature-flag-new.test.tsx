/**
 * New Feature Flag (#535, PR A): incomplete targeting rules are stopped before
 * the request is sent; a 422 on `targeting_rules` is shown inside the
 * Targeting Rules section, in an alert that takes focus; any other failure
 * is announced (`role="alert"`). Rules are sent only when the builder has
 * some, without the builder's ids.
 */
import React from 'react';
import axe from 'axe-core';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import NewFeatureFlagPage from '@/pages/feature-flags/new';
import { ApiError, ApiFetchOptions, apiFetch } from '@/services/api';
import { apiError, makeRouter, routedApi } from './helpers/apiMock';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

const mockRouter = makeRouter({ pathname: '/feature-flags/new' });
jest.mock('next/router', () => ({ useRouter: () => mockRouter }));

// Unset by default (no session, as before #917): the page shows the form.
const mockAuth = jest.fn();
jest.mock('@/contexts/AuthContext', () => ({ useOptionalAuth: () => mockAuth() }));

function signIn(role: string, is_superuser = false) {
  mockAuth.mockReturnValue({
    status: 'authenticated',
    user: { id: 'u1', email: 'u@example.com', username: 'u', role, is_superuser },
  });
}

jest.mock('next/head', () => {
  const Head = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Head.displayName = 'MockHead';
  return Head;
});

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

/** Routes POST /feature-flags/; returns every body sent. */
function install(onPost?: (body: Record<string, unknown>) => unknown) {
  const posts: Record<string, unknown>[] = [];
  mockedApiFetch.mockImplementation(
    routedApi([
      {
        method: 'POST',
        path: '/api/v1/feature-flags',
        handler: (_p: string, options: ApiFetchOptions) => {
          const body = options.json as Record<string, unknown>;
          posts.push(body);
          if (onPost) return onPost(body);
          return { id: 'flag-9', ...body, created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-01T00:00:00Z' };
        },
      },
    ]) as unknown as typeof apiFetch,
  );
  return posts;
}

const refusal = () =>
  new ApiError({
    status: 422,
    detail: [
      {
        loc: ['body', 'targeting_rules'],
        msg: 'Value error, groups[0].conditions[0].attribute: attribute may contain only letters, digits, underscores and dots',
        type: 'value_error',
      },
    ],
  });

function section() {
  return screen.getByTestId('flag-targeting-section');
}

function fillName() {
  fireEvent.change(screen.getByTestId('flag-name-input'), { target: { value: 'Dark mode' } });
}

function addCondition(attribute: string, value: string) {
  const s = section();
  fireEvent.click(within(s).getByRole('button', { name: '+ Add Group' }));
  fireEvent.change(within(s).getByLabelText('Group 1, condition 1 attribute'), { target: { value: attribute } });
  fireEvent.change(within(s).getByLabelText('Group 1, condition 1 value'), { target: { value } });
}

function submit() {
  fireEvent.click(screen.getByTestId('submit-flag'));
}

beforeEach(() => {
  mockedApiFetch.mockReset();
  mockAuth.mockReset();
  mockRouter.push.mockClear();
});

describe('NewFeatureFlagPage', () => {
  it('sends no targeting_rules when the builder has none', async () => {
    const posts = install();
    render(<NewFeatureFlagPage />);
    fillName();
    submit();
    await waitFor(() => expect(mockRouter.push).toHaveBeenCalledWith('/feature-flags/flag-9'));
    expect(posts).toHaveLength(1);
    expect(posts[0]).not.toHaveProperty('targeting_rules');
    expect(posts[0]).toMatchObject({ name: 'Dark mode', key: 'dark_mode', is_active: false, rollout_percentage: 0 });
  });

  it('sends the rules in the dashboard shape, without the builder ids', async () => {
    const posts = install();
    render(<NewFeatureFlagPage />);
    fillName();
    addCondition('user.plan', 'pro');
    submit();
    await waitFor(() => expect(mockRouter.push).toHaveBeenCalled());
    expect(posts[0].targeting_rules).toEqual({
      logical_operator: 'AND',
      groups: [{ logical_operator: 'AND', conditions: [{ attribute: 'user.plan', operator: 'equals', value: 'pro' }] }],
    });
  });

  it('stops unfinished rules before sending, in a focused alert in the section', async () => {
    const posts = install();
    render(<NewFeatureFlagPage />);
    fillName();
    fireEvent.click(within(section()).getByRole('button', { name: '+ Add Group' }));
    submit();

    const alert = await within(section()).findByRole('alert');
    expect(alert).toHaveTextContent('Finish or remove these conditions before saving:');
    expect(within(alert).getAllByRole('listitem').map((li) => li.textContent)).toEqual([
      'Group 1, Condition 1: attribute is required',
      'Group 1, Condition 1: value is required for operator "equals"',
    ]);
    await waitFor(() => expect(alert).toHaveFocus());
    expect(posts).toEqual([]);
    expect(mockRouter.push).not.toHaveBeenCalled();
  });

  it('shows a 422 on targeting_rules in the section, focused, as a list', async () => {
    install(() => {
      throw refusal();
    });
    render(<NewFeatureFlagPage />);
    fillName();
    addCondition('plan-tier', 'pro');
    submit();

    const alert = await within(section()).findByRole('alert');
    expect(alert).toHaveTextContent('The rules were not accepted:');
    expect(within(alert).getAllByRole('listitem').map((li) => li.textContent)).toEqual([
      'Group 1, Condition 1: attribute may contain only letters, digits, underscores and dots',
    ]);
    await waitFor(() => expect(alert).toHaveFocus());
    expect(screen.queryByTestId('create-error')).toBeNull();
    expect(mockRouter.push).not.toHaveBeenCalled();
  });

  it('announces any other failure with role="alert"', async () => {
    install(() => {
      throw apiError(409, 'A feature flag with this key already exists');
    });
    render(<NewFeatureFlagPage />);
    fillName();
    submit();
    const box = (await screen.findByText('A feature flag with this key already exists')).closest('div');
    expect(box).toHaveAttribute('role', 'alert');
    expect(box).toHaveTextContent('A feature flag with this key already exists');
    expect(within(section()).queryByRole('alert')).toBeNull();
  });

  it('offers version operators for an attribute outside the suggestions', () => {
    install();
    render(<NewFeatureFlagPage />);
    fireEvent.click(within(section()).getByRole('button', { name: '+ Add Group' }));
    fireEvent.change(within(section()).getByLabelText('Group 1, condition 1 attribute'), {
      target: { value: 'os_version' },
    });
    const operator = within(section()).getByLabelText('Group 1, condition 1 operator');
    expect(within(operator).getByRole('option', { name: 'version >=' })).toBeInTheDocument();
  });
});

describe('who may create flags (#917)', () => {
  it.each(['ANALYST', 'VIEWER'])('%s gets a notice instead of the form, and nothing is sent', (role) => {
    signIn(role);
    const posts = install();
    render(<NewFeatureFlagPage />);

    expect(screen.getByTestId('flag-new-role-note')).toHaveTextContent(
      'Your role can view feature flags but not create them. Feature flags are created and changed with the feature flag permissions, which the ADMIN and DEVELOPER roles have.',
    );
    expect(screen.getByRole('link', { name: 'Back to feature flags' })).toHaveAttribute('href', '/feature-flags');
    expect(screen.queryByTestId('submit-flag')).toBeNull();
    expect(screen.queryByTestId('flag-name-input')).toBeNull();
    expect(screen.queryByTestId('flag-key-input')).toBeNull();
    expect(screen.queryByTestId('flag-targeting-section')).toBeNull();
    expect(screen.queryByTestId('rollout-percentage')).toBeNull();
    expect(document.querySelector('form')).toBeNull();
    expect(posts).toEqual([]);
    expect(mockedApiFetch).not.toHaveBeenCalled();
    expect(mockRouter.push).not.toHaveBeenCalled();
  });

  it.each([
    ['ADMIN', false],
    ['DEVELOPER', false],
    ['VIEWER', true],
  ])('%s (superuser: %s) gets the form', (role, is_superuser) => {
    signIn(role, is_superuser);
    install();
    render(<NewFeatureFlagPage />);
    expect(screen.getByTestId('submit-flag')).toBeInTheDocument();
    expect(screen.getByTestId('flag-name-input')).toBeInTheDocument();
    expect(screen.queryByTestId('flag-new-role-note')).toBeNull();
  });
});

describe('NewFeatureFlagPage accessibility (axe-core in jsdom; colour contrast is not computable here)', () => {
  const axeOptions: axe.RunOptions = { rules: { 'color-contrast': { enabled: false } } };

  async function violations(node: Element) {
    const result = await axe.run(node, axeOptions);
    return result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(', ')}`);
  }

  it('has no violations with a refusal shown', async () => {
    install(() => {
      throw refusal();
    });
    const { container } = render(<NewFeatureFlagPage />);
    fillName();
    addCondition('plan-tier', 'pro');
    submit();
    await within(section()).findByRole('alert');
    expect(await violations(container)).toEqual([]);
  });

  it('has no violations with incomplete rules stopped', async () => {
    install();
    const { container } = render(<NewFeatureFlagPage />);
    fillName();
    fireEvent.click(within(section()).getByRole('button', { name: '+ Add Group' }));
    submit();
    await within(section()).findByRole('alert');
    expect(await violations(container)).toEqual([]);
  });

  it('has no violations with another failure shown', async () => {
    install(() => {
      throw apiError(500, 'Failed to create feature flag');
    });
    const { container } = render(<NewFeatureFlagPage />);
    fillName();
    submit();
    await screen.findByText('Failed to create feature flag');
    expect(await violations(container)).toEqual([]);
  });

  it('has no violations with the role notice shown', async () => {
    signIn('VIEWER');
    install();
    const { container } = render(<NewFeatureFlagPage />);
    screen.getByTestId('flag-new-role-note');
    expect(await violations(container)).toEqual([]);
  });
});
