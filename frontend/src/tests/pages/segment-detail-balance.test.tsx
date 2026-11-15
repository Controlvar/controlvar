/**
 * A segment's page when an experiment balances its split within the segment
 * (#448): the answer to Archive, and the "Used by" panel, say what actually
 * holds the segment. Targeting rules are not the only thing that does.
 *
 * The 409's own `message` is never shown (the page words it itself, as the
 * rest of this page's tests pin), so the sentences are written out here.
 */
import React from 'react';
import { fireEvent, render, screen, within } from '@testing-library/react';
import axe from 'axe-core';
import SegmentDetailPage from '@/pages/segments/[id]';
import { ApiError, apiFetch } from '@/services/api';
import { makeRouter, routedApi } from './helpers/apiMock';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

const SEG = '3f2b9c1e-8d4a-4c6b-9e2f-1a2b3c4d5e6f';
const mockRouter = makeRouter({ pathname: '/segments/[id]', asPath: `/segments/${SEG}`, query: { id: SEG } });
jest.mock('next/router', () => ({ useRouter: () => mockRouter }));

const mockAuth = jest.fn();
jest.mock('@/contexts/AuthContext', () => ({ useOptionalAuth: () => mockAuth() }));

jest.mock('next/head', () => {
  const Head = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Head.displayName = 'MockHead';
  return Head;
});

const mocked = apiFetch as jest.MockedFunction<typeof apiFetch>;
const BASE = `/api/v1/segments/${SEG}`;
const PLANTED = 'Traceback (most recent call last): sqlalchemy.exc.PLANTED';

const SEGMENT = {
  id: SEG,
  name: 'Enterprise pilot',
  description: null,
  kind: 'id_list',
  rules: null,
  status: 'active',
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-02T00:00:00Z',
  member_count: 10,
};

function serve(usage: object, archive?: () => never) {
  mocked.mockImplementation(
    routedApi([
      { method: 'DELETE', path: BASE, handler: archive ?? (() => undefined) },
      { path: BASE, handler: () => SEGMENT },
      { path: `${BASE}/experiments`, handler: () => usage },
    ]) as unknown as typeof apiFetch,
  );
}

const inUse409 = (detail: object) => () => {
  throw new ApiError({ status: 409, detail: { code: 'segment_in_use', message: PLANTED, ...detail } });
};

async function archiveIt() {
  const view = render(<SegmentDetailPage />);
  await screen.findByTestId('segment-name-heading');
  fireEvent.click(screen.getByTestId('segment-archive'));
  fireEvent.click(screen.getByTestId('segment-archive-yes'));
  return { view, inUse: await screen.findByTestId('segment-in-use') };
}

beforeEach(() => {
  mocked.mockReset();
  mockAuth.mockReturnValue({
    status: 'authenticated',
    user: { id: 'u1', email: 'u@example.com', username: 'u', role: 'ADMIN', is_superuser: false },
  });
});

describe('the answer to Archive', () => {
  it('an experiment holds it: the text names balancing as well as targeting rules, and not the 409 message', async () => {
    serve(
      { segment_id: SEG, experiments: [], feature_flags: [] },
      inUse409({ feature_flags: [], experiments: [{ id: 'e1', key: 'pricing-page', name: 'Pricing', status: 'draft' }] }),
    );
    const { view, inUse } = await archiveIt();
    expect(inUse).toHaveTextContent(
      'Enterprise pilot is used by 1 experiment. Remove the segment from the targeting rules that name it and from the ' +
        'segments an experiment balances within, then archive it. A started experiment that balances within it lets it ' +
        'go once it is completed or archived.',
    );
    expect(within(inUse).getByRole('link', { name: 'pricing-page' })).toHaveAttribute('href', '/experiments/e1');
    expect(document.body.textContent).not.toContain('PLANTED');
    expect(screen.getByTestId('segment-archive-error')).toHaveFocus();
    const result = await axe.run(view.container, { rules: { 'color-contrast': { enabled: false } } });
    expect(result.violations).toEqual([]);
  });

  it('flags and experiments together get the same text', async () => {
    serve(
      { segment_id: SEG, experiments: [], feature_flags: [] },
      inUse409({
        feature_flags: [{ id: 'f1', key: 'checkout-v2', name: 'Checkout v2' }],
        experiments: [{ id: 'e1', key: 'pricing-page', name: 'Pricing', status: 'paused' }],
      }),
    );
    const { inUse } = await archiveIt();
    expect(inUse).toHaveTextContent('Enterprise pilot is used by 1 flag and 1 experiment. Remove the segment from the targeting rules that name it');
    expect(inUse).toHaveTextContent('segments an experiment balances within');
  });

  it('flags alone still get the targeting-rules text', async () => {
    serve(
      { segment_id: SEG, experiments: [], feature_flags: [] },
      inUse409({ feature_flags: [{ id: 'f1', key: 'checkout-v2', name: 'Checkout v2' }], experiments: [] }),
    );
    const { inUse } = await archiveIt();
    expect(inUse).toHaveTextContent(
      'Enterprise pilot is used by 1 flag. Remove the segment from their targeting rules, then archive it.',
    );
    expect(inUse.textContent).not.toContain('balances');
  });
});

describe('the Used by panel', () => {
  it('says no flag or experiment uses it, not that no targeting rules name it', async () => {
    serve({ segment_id: SEG, experiments: [], feature_flags: [] });
    render(<SegmentDetailPage />);
    expect(await screen.findByTestId('segment-usage-none')).toHaveTextContent(
      'No flag or experiment uses this segment.',
    );
  });

  it('lists an experiment that balances within the segment', async () => {
    serve({
      segment_id: SEG,
      feature_flags: [],
      experiments: [{ id: 'e1', name: 'Pricing', status: 'draft' }],
    });
    render(<SegmentDetailPage />);
    const list = await screen.findByTestId('segment-usage-list');
    expect(list).toHaveTextContent('Experiment Pricing (draft)');
    expect(screen.queryByTestId('segment-usage-none')).toBeNull();
  });
});
