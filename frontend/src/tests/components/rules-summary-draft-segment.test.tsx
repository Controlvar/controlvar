/**
 * #1778: the new-experiment Review (RulesSummary in `draft` mode) shows a
 * segment condition's name, not its id: the id while names load or when the
 * list cannot be read, the id with "(segment not found)" for a segment the
 * list lacks. A condition with no value yet is not looked up.
 */
import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import { RulesSummary } from '@/components/experiments/RulesSummary';
import { SEGMENT_NOT_FOUND, VALUE_NOT_SET } from '@/utils/experimentTargeting';
import { apiFetch } from '@/services/api';
import { apiError, routedApi } from '../pages/helpers/apiMock';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

const SEGMENT_ID = '3f2504e0-4f89-41d3-9a0c-0305e82c3301';
const OTHER_ID = '9b2c1f7a-1d3e-4a5b-8c6d-7e8f9a0b1c2d';
const SEGMENT = {
  id: SEGMENT_ID,
  name: 'Beta testers',
  kind: 'rules',
  rules: null,
  status: 'active',
  created_at: '2026-09-01T10:00:00Z',
  updated_at: '2026-09-01T10:00:00Z',
};

const rules = (value: unknown) => ({
  logical_operator: 'AND',
  groups: [
    {
      logical_operator: 'AND',
      conditions: [{ attribute: 'segment', operator: 'in_segment', value }],
    },
  ],
});

const segmentCalls = () => mockedApiFetch.mock.calls.filter(([path]) => String(path).startsWith('/api/v1/segments'));

function install(segments: () => unknown) {
  mockedApiFetch.mockImplementation(
    routedApi([{ path: '/api/v1/segments', handler: segments }]) as unknown as typeof apiFetch,
  );
}

beforeEach(() => mockedApiFetch.mockReset());

describe('a segment condition in the Review (draft)', () => {
  it('shows the segment name once the list has loaded', async () => {
    install(() => [SEGMENT]);
    render(<RulesSummary value={rules(SEGMENT_ID)} draft />);
    const summary = screen.getByTestId('targeting-summary');
    await waitFor(() => expect(summary).toHaveTextContent('segment is in segment Beta testers'));
    expect(summary).not.toHaveTextContent(SEGMENT_ID);
  });

  it('shows the id, not "unknown", while the names load', async () => {
    let release: (value: unknown) => void = () => undefined;
    install(() => new Promise((resolve) => (release = resolve)));
    render(<RulesSummary value={rules(SEGMENT_ID)} draft />);
    const summary = screen.getByTestId('targeting-summary');
    await waitFor(() => expect(segmentCalls().length).toBe(1));
    expect(summary).toHaveTextContent(`segment is in segment ${SEGMENT_ID}`);
    expect(summary).not.toHaveTextContent(/unknown|not found/i);
    release([SEGMENT]);
    await waitFor(() => expect(summary).toHaveTextContent('Beta testers'));
  });

  it('shows the id with "(segment not found)" for a segment the list lacks', async () => {
    install(() => [SEGMENT]);
    render(<RulesSummary value={rules(OTHER_ID)} draft />);
    const summary = screen.getByTestId('targeting-summary');
    await waitFor(() => expect(summary).toHaveTextContent(`${OTHER_ID} ${SEGMENT_NOT_FOUND}`));
  });

  it('shows the id and no banner when the list cannot be read', async () => {
    install(() => {
      throw apiError(403, 'Not enough permissions');
    });
    render(<RulesSummary value={rules(SEGMENT_ID)} draft />);
    const summary = screen.getByTestId('targeting-summary');
    await waitFor(() => expect(segmentCalls().length).toBe(1));
    expect(summary).toHaveTextContent(`segment is in segment ${SEGMENT_ID}`);
    expect(summary).not.toHaveTextContent(/not found/i);
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('does not look up a segment condition with no value yet', () => {
    install(() => [SEGMENT]);
    render(<RulesSummary value={rules('')} draft />);
    expect(screen.getByTestId('targeting-summary')).toHaveTextContent(`segment is in segment ${VALUE_NOT_SET}`);
    expect(segmentCalls().length).toBe(0);
  });

  it('makes no segment request without a segment condition', () => {
    install(() => [SEGMENT]);
    const plain = {
      logical_operator: 'AND',
      groups: [
        {
          logical_operator: 'AND',
          conditions: [{ attribute: 'user.plan', operator: 'equals', value: 'pro' }],
        },
      ],
    };
    render(<RulesSummary value={plain} draft />);
    expect(segmentCalls().length).toBe(0);
  });
});
