import React from 'react';
import { render, screen } from '@testing-library/react';
import ExperimentDetailPage from '@/pages/experiments/[id]';
import { apiFetch } from '@/services/api';
import { Experiment } from '@/types/experiments';
import { makeRouter, routedApi } from './helpers/apiMock';

// #1134. The API sends `start_date`, `end_date` and `created_at` as stored,
// UTC with no zone (`2026-10-11T05:02:00`). A browser reads that as LOCAL, so
// the page showed the UTC clock as if it were the viewer's. The suite runs in
// America/Los_Angeles (jest.global-setup.js); in UTC this could not fail.

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

jest.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 'user-1', email: 'admin@demo.com', username: 'admin', role: 'ADMIN' },
    status: 'authenticated',
  }),
}));

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

const EXPERIMENT: Experiment = {
  id: 'exp-1',
  name: 'Checkout button colour',
  key: 'checkout-button-colour',
  description: null,
  hypothesis: null,
  experiment_type: 'a_b',
  status: 'completed',
  targeting_rules: null,
  tags: null,
  owner_id: 'user-1',
  start_date: '2026-10-11T05:02:00',
  end_date: '2026-10-13T17:30:15.123456',
  created_at: '2026-10-10T03:04:05',
  updated_at: '2026-10-13T17:30:15',
  variants: [
    { id: 'v-1', name: 'Control', is_control: true, traffic_allocation: 50 },
    { id: 'v-2', name: 'Blue CTA', is_control: false, traffic_allocation: 50 },
  ],
  metrics: [
    { id: 'm-1', name: 'Purchase', event_name: 'purchase', metric_type: 'conversion', is_primary: true },
  ],
};

async function shownFor(label: string): Promise<string> {
  const row = (await screen.findByText(label)).closest('div');
  return row?.querySelector('dd')?.textContent ?? '';
}

describe('ExperimentDetailPage — times the API sends without a zone', () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
    mockedApiFetch.mockImplementation(
      routedApi([
        { path: '/api/v1/experiments/exp-1', handler: () => EXPERIMENT },
      ]) as unknown as typeof apiFetch,
    );
  });

  it('runs in a zone that is not UTC', () => {
    expect(new Date(0).getTimezoneOffset()).not.toBe(0);
    expect(new Date(Date.UTC(2026, 9, 11, 5, 2)).toLocaleString('en-US')).toBe(
      '10/10/2026, 10:02:00 PM',
    );
  });

  it('shows Started, Ended and Created as UTC converted to local time', async () => {
    render(<ExperimentDetailPage />);

    // 05:02 UTC is 22:02 the evening before in Los Angeles (UTC-7 in October):
    // "10/10/2026, 10:02:00 PM" in the en-US form, not "10/11/2026, 5:02:00 AM".
    const started = await shownFor('Started');
    expect(started).toBe(new Date('2026-10-11T05:02:00Z').toLocaleString());
    expect(started).not.toBe(new Date(2026, 9, 11, 5, 2).toLocaleString());

    expect(await shownFor('Ended')).toBe(new Date('2026-10-13T17:30:15.123Z').toLocaleString());
    expect(await shownFor('Created')).toBe(new Date('2026-10-10T03:04:05Z').toLocaleString());
  });
});
