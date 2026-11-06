/**
 * #1778: a segment condition in the experiment page's "Who can join" summary
 * shows the segment's name, not its id: the id while names load, the id with
 * "(segment not found)" for a segment the list lacks, and the id with no error
 * banner for a reader who cannot list segments.
 */
import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import ExperimentDetailPage from '@/pages/experiments/[id]';
import { SEGMENT_NOT_FOUND, describeCondition } from '@/utils/experimentTargeting';
import { apiFetch } from '@/services/api';
import { Experiment } from '@/types/experiments';
import { apiError, makeRouter, routedApi } from './helpers/apiMock';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

const mockRouter = makeRouter({ pathname: '/experiments/[id]', query: { id: 'exp-1' } });
jest.mock('next/router', () => ({ useRouter: () => mockRouter }));

jest.mock('next/head', () => {
  const Head = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Head.displayName = 'MockHead';
  return Head;
});

const mockUseAuth = jest.fn();
jest.mock('@/contexts/AuthContext', () => ({ useAuth: () => mockUseAuth() }));

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

const SEGMENT_ID = '3f2504e0-4f89-41d3-9a0c-0305e82c3301';
const OTHER_ID = '9b2c1f7a-1d3e-4a5b-8c6d-7e8f9a0b1c2d';

const experiment = (segmentId: string): Experiment =>
  ({
    id: 'exp-1',
    name: 'Checkout button colour',
    key: 'checkout-button-colour',
    description: null,
    hypothesis: null,
    experiment_type: 'a_b',
    status: 'draft',
    targeting_rules: {
      logical_operator: 'AND',
      groups: [
        {
          logical_operator: 'AND',
          conditions: [{ attribute: 'segment', operator: 'in_segment', value: segmentId }],
        },
      ],
    },
    tags: null,
    owner_id: 'user-1',
    start_date: null,
    end_date: null,
    created_at: '2026-09-01T10:00:00Z',
    updated_at: '2026-09-01T10:00:00Z',
    variants: [
      { id: 'v-1', name: 'Control', is_control: true, traffic_allocation: 50 },
      { id: 'v-2', name: 'Blue CTA', is_control: false, traffic_allocation: 50 },
    ],
    metrics: [{ id: 'm-1', name: 'Purchase', event_name: 'purchase', metric_type: 'conversion', is_primary: true }],
  }) as Experiment;

const SEGMENT = {
  id: SEGMENT_ID,
  name: 'Beta testers',
  kind: 'rules',
  rules: null,
  status: 'active',
  created_at: '2026-09-01T10:00:00Z',
  updated_at: '2026-09-01T10:00:00Z',
};

function install(segmentId: string, segments: () => unknown) {
  mockedApiFetch.mockImplementation(
    routedApi([
      { path: '/api/v1/experiments/exp-1', handler: () => experiment(segmentId) },
      { path: '/api/v1/segments', handler: segments },
    ]) as unknown as typeof apiFetch,
  );
}

async function summaryText() {
  await screen.findByTestId('experiment-detail');
  return screen.getByTestId('targeting-summary');
}

beforeEach(() => {
  mockedApiFetch.mockReset();
  mockUseAuth.mockReturnValue({
    user: { id: 'user-1', email: 'admin@demo.com', username: 'admin', role: 'ADMIN' },
    status: 'authenticated',
  });
});

describe('a segment condition on the experiment page', () => {
  it('shows the segment name once the list has loaded', async () => {
    install(SEGMENT_ID, () => [SEGMENT]);
    render(<ExperimentDetailPage />);
    const summary = await summaryText();
    await waitFor(() => expect(summary).toHaveTextContent('segment is in segment Beta testers'));
    expect(summary).not.toHaveTextContent(SEGMENT_ID);
  });

  it('shows the id, not "unknown", while the names load', async () => {
    let release: (value: unknown) => void = () => undefined;
    install(SEGMENT_ID, () => new Promise((resolve) => (release = resolve)));
    render(<ExperimentDetailPage />);
    const summary = await summaryText();
    expect(summary).toHaveTextContent(`segment is in segment ${SEGMENT_ID}`);
    expect(summary).not.toHaveTextContent(/unknown|not found/i);
    release([SEGMENT]);
    await waitFor(() => expect(summary).toHaveTextContent('Beta testers'));
  });

  it('shows the id with "(segment not found)" for a segment the list lacks', async () => {
    install(OTHER_ID, () => [SEGMENT]);
    render(<ExperimentDetailPage />);
    const summary = await summaryText();
    await waitFor(() => expect(summary).toHaveTextContent(`${OTHER_ID} ${SEGMENT_NOT_FOUND}`));
  });

  it('shows the id and no error banner to a reader who cannot list segments', async () => {
    install(SEGMENT_ID, () => {
      throw apiError(403, 'Not enough permissions');
    });
    mockUseAuth.mockReturnValue({
      user: { id: 'user-2', email: 'viewer@demo.com', username: 'viewer', role: 'VIEWER' },
      status: 'authenticated',
    });
    render(<ExperimentDetailPage />);
    const summary = await summaryText();
    await waitFor(() =>
      expect(mockedApiFetch.mock.calls.some(([path]) => String(path).startsWith('/api/v1/segments'))).toBe(true),
    );
    expect(summary).toHaveTextContent(`segment is in segment ${SEGMENT_ID}`);
    expect(summary).not.toHaveTextContent(/not found/i);
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('renders a segment name as text, never as HTML', async () => {
    const name = '<img src=x onerror=alert(1)>';
    install(SEGMENT_ID, () => [{ ...SEGMENT, name }]);
    render(<ExperimentDetailPage />);
    const summary = await summaryText();
    await waitFor(() => expect(summary).toHaveTextContent(name));
    expect(summary.querySelector('img')).toBeNull();
  });

  it('makes no segment request for rules without a segment condition', async () => {
    install(SEGMENT_ID, () => [SEGMENT]);
    mockedApiFetch.mockImplementation(
      routedApi([
        {
          path: '/api/v1/experiments/exp-1',
          handler: () => ({
            ...experiment(SEGMENT_ID),
            targeting_rules: {
              logical_operator: 'AND',
              groups: [{ logical_operator: 'AND', conditions: [{ attribute: 'user.plan', operator: 'equals', value: 'pro' }] }],
            },
          }),
        },
      ]) as unknown as typeof apiFetch,
    );
    render(<ExperimentDetailPage />);
    await summaryText();
    expect(mockedApiFetch.mock.calls.some(([path]) => String(path).startsWith('/api/v1/segments'))).toBe(false);
  });
});

describe('describeCondition', () => {
  const condition = { attribute: 'segment', operator: 'not_in_segment', value: SEGMENT_ID };

  it('shows the id without a name resolver', () => {
    expect(describeCondition(condition)).toBe(`segment is not in segment ${SEGMENT_ID}`);
  });

  it('uses the resolver for a segment operator only', () => {
    expect(describeCondition(condition, () => 'Beta testers')).toBe('segment is not in segment Beta testers');
    expect(describeCondition({ attribute: 'user.plan', operator: 'equals', value: 'pro' }, () => 'X')).toBe(
      'user.plan equals pro',
    );
  });
});
