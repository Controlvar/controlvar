/**
 * Creating a segment (#440 PR D; gates-D D13, D14, D20, D21).
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import axe from 'axe-core';
import NewSegmentPage from '@/pages/segments/new';
import { ApiError, apiFetch } from '@/services/api';
import vocabularyRefusal from '@/tests/fixtures/targeting-vocabulary-refusal.json';
import { makeRouter, routedApi } from './helpers/apiMock';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

const mockRouter = makeRouter({ pathname: '/segments/new', asPath: '/segments/new' });
jest.mock('next/router', () => ({ useRouter: () => mockRouter }));

const mockAuth = jest.fn();
jest.mock('@/contexts/AuthContext', () => ({ useOptionalAuth: () => mockAuth() }));

jest.mock('next/head', () => {
  const Head = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Head.displayName = 'MockHead';
  return Head;
});

const mocked = apiFetch as jest.MockedFunction<typeof apiFetch>;

function signIn(role: string) {
  mockAuth.mockReturnValue({ status: 'authenticated', user: { id: 'u1', email: 'u@example.com', username: 'u', role, is_superuser: false } });
}

const axeOptions: axe.RunOptions = { rules: { 'color-contrast': { enabled: false } } };
async function violations(node: Element) {
  const result = await axe.run(node, axeOptions);
  return result.violations.map((v) => `${v.id} (${v.impact})`);
}

const posts = () => mocked.mock.calls.filter(([, o]) => (o as { method?: string } | undefined)?.method === 'POST');

beforeEach(() => {
  mocked.mockReset();
  mockRouter.push.mockClear();
  signIn('DEVELOPER');
  mocked.mockImplementation(
    routedApi([{ method: 'POST', path: '/api/v1/segments', handler: () => ({ id: 'new-seg' }) }]) as unknown as typeof apiFetch,
  );
});

describe('NewSegmentPage', () => {
  it('creates a rules segment with the dashboard rule shape and opens it', async () => {
    const { container } = render(<NewSegmentPage />);
    expect(await violations(container)).toEqual([]);
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Pro plan' } });
    fireEvent.change(screen.getByLabelText('Group 1, condition 1 attribute'), { target: { value: 'plan' } });
    fireEvent.change(screen.getByLabelText('Group 1, condition 1 value'), { target: { value: 'pro' } });
    // A segment cannot contain a segment: no segment operator on offer.
    fireEvent.change(screen.getByLabelText('Group 1, condition 1 attribute'), { target: { value: 'segment' } });
    const ops = within(screen.getByLabelText('Group 1, condition 1 operator')).getAllByRole('option').map((o) => o.getAttribute('value'));
    expect(ops).not.toContain('in_segment');
    fireEvent.change(screen.getByLabelText('Group 1, condition 1 attribute'), { target: { value: 'plan' } });
    fireEvent.click(screen.getByTestId('segment-create'));
    await waitFor(() => expect(mockRouter.push).toHaveBeenCalledWith('/segments/new-seg'));
    expect(posts()).toEqual([
      [
        '/api/v1/segments',
        {
          method: 'POST',
          json: {
            name: 'Pro plan',
            description: undefined,
            kind: 'rules',
            rules: {
              logical_operator: 'AND',
              groups: [{ logical_operator: 'AND', conditions: [{ attribute: 'plan', operator: 'equals', value: 'pro' }] }],
            },
          },
        },
      ],
    ]);
  });

  it('creates an id list with no rules', async () => {
    const { container } = render(<NewSegmentPage />);
    fireEvent.click(screen.getByTestId('segment-kind-id-list'));
    expect(screen.getByTestId('segment-id-list-next')).toBeInTheDocument();
    expect(await violations(container)).toEqual([]);
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Enterprise pilot' } });
    fireEvent.change(screen.getByLabelText('Description (optional)'), { target: { value: 'CRM export' } });
    fireEvent.click(screen.getByTestId('segment-create'));
    await waitFor(() => expect(mockRouter.push).toHaveBeenCalledWith('/segments/new-seg'));
    expect(posts()).toEqual([
      ['/api/v1/segments', { method: 'POST', json: { name: 'Enterprise pilot', description: 'CRM export', kind: 'id_list', rules: undefined } }],
    ]);
  });

  it('stops a short name and an empty condition before sending', async () => {
    render(<NewSegmentPage />);
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'x' } });
    fireEvent.click(screen.getByTestId('segment-create'));
    const error = await screen.findByTestId('segment-new-error');
    expect(error).toHaveTextContent('Name: 2 to 128 characters.');
    expect(error).toHaveTextContent('Group 1, Condition 1: attribute is required');
    expect(posts()).toEqual([]);
  });

  it('a refusal shows fixed copy and the rules problems in the builder words, never the server text', async () => {
    mocked.mockImplementation(
      routedApi([
        {
          method: 'POST',
          path: '/api/v1/segments',
          handler: () => {
            throw new ApiError({
              status: 422,
              detail: [
                { loc: ['body', 'rules'], msg: 'Value error, groups[0].conditions[0].value: must be text' },
                { loc: ['body', 'name'], msg: 'PLANTED' },
              ],
            });
          },
        },
      ]) as unknown as typeof apiFetch,
    );
    const { container } = render(<NewSegmentPage />);
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Pro plan' } });
    fireEvent.change(screen.getByLabelText('Group 1, condition 1 attribute'), { target: { value: 'plan' } });
    fireEvent.change(screen.getByLabelText('Group 1, condition 1 value'), { target: { value: 'pro' } });
    fireEvent.click(screen.getByTestId('segment-create'));
    const error = await screen.findByTestId('segment-new-error');
    expect(error).toHaveTextContent('The segment was not accepted.');
    expect(error).toHaveTextContent('Group 1, Condition 1: must be text');
    expect(document.body.textContent).not.toContain('PLANTED');
    expect(await violations(container)).toEqual([]);
  });

  it('an unknown distance unit (#1204) is shown as one line in the builder words', async () => {
    mocked.mockImplementation(
      routedApi([
        {
          method: 'POST',
          path: '/api/v1/segments',
          handler: () => {
            throw new ApiError({ status: 422, detail: [{ loc: ['body', 'rules'], msg: vocabularyRefusal.msg }] });
          },
        },
      ]) as unknown as typeof apiFetch,
    );
    render(<NewSegmentPage />);
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Bay Area' } });
    fireEvent.change(screen.getByLabelText('Group 1, condition 1 attribute'), { target: { value: 'plan' } });
    fireEvent.change(screen.getByLabelText('Group 1, condition 1 value'), { target: { value: 'pro' } });
    fireEvent.click(screen.getByTestId('segment-create'));
    const error = await screen.findByTestId('segment-new-error');
    expect(error).toHaveTextContent('The segment was not accepted.');
    expect(within(error).getAllByRole('listitem').map((li) => li.textContent)).toEqual([vocabularyRefusal.shown]);
  });

  it('a 500 shows fixed copy, not its body', async () => {
    mocked.mockImplementation(
      routedApi([
        {
          method: 'POST',
          path: '/api/v1/segments',
          handler: () => {
            throw new ApiError({ status: 500, detail: 'Traceback PLANTED' });
          },
        },
      ]) as unknown as typeof apiFetch,
    );
    render(<NewSegmentPage />);
    fireEvent.click(screen.getByTestId('segment-kind-id-list'));
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Pilot' } });
    fireEvent.click(screen.getByTestId('segment-create'));
    expect(await screen.findByTestId('segment-new-error')).toHaveTextContent('Something went wrong on the server.');
    expect(document.body.textContent).not.toContain('PLANTED');
  });

  it.each(['ANALYST', 'VIEWER'])('%s gets the role note and no form (D13)', async (role) => {
    signIn(role);
    const { container } = render(<NewSegmentPage />);
    expect(screen.getByTestId('segment-new-role-note')).toHaveTextContent('ADMIN and DEVELOPER');
    expect(screen.queryByTestId('segment-new-form')).toBeNull();
    expect(screen.queryByTestId('segment-create')).toBeNull();
    expect(await violations(container)).toEqual([]);
  });
});
