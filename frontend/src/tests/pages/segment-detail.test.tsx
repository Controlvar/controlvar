/**
 * A segment's page (#440 PR D; gates-D D4, D6, D7, D13, D14, D16-D18, D21).
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import axe from 'axe-core';
import SegmentDetailPage from '@/pages/segments/[id]';
import { ApiError, ApiFetchOptions, apiFetch } from '@/services/api';
import { Segment } from '@/services/segments';
import { RULES_NOT_VALID } from '@/utils/segmentErrors';
import vocabularyRefusal from '@/tests/fixtures/targeting-vocabulary-refusal.json';
import { Route, makeRouter, routedApi } from './helpers/apiMock';

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

function signIn(role: string) {
  mockAuth.mockReturnValue({ status: 'authenticated', user: { id: 'u1', email: 'u@example.com', username: 'u', role, is_superuser: false } });
}

function idList(overrides: Partial<Segment> = {}): Segment {
  return {
    id: SEG,
    name: 'Enterprise pilot',
    description: 'CRM export',
    kind: 'id_list',
    rules: null,
    status: 'active',
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-02T00:00:00Z',
    member_count: 10,
    ...overrides,
  };
}

const VALID_RULES = {
  logical_operator: 'AND',
  groups: [{ logical_operator: 'AND', conditions: [{ attribute: 'plan', operator: 'equals', value: 'pro' }] }],
};

function rulesSegment(rules: unknown = VALID_RULES, overrides: Partial<Segment> = {}): Segment {
  return idList({ kind: 'rules', rules, member_count: null, name: 'Pro plan', ...overrides });
}

const noUsage = { segment_id: SEG, experiments: [], feature_flags: [] };

function serve(segment: Segment | (() => Segment), extra: Route[] = []) {
  mocked.mockImplementation(
    routedApi([
      ...extra,
      { path: BASE, handler: () => (typeof segment === 'function' ? segment() : segment) },
      { path: `${BASE}/experiments`, handler: () => noUsage },
    ]) as unknown as typeof apiFetch,
  );
}

const callsTo = (path: string) =>
  mocked.mock.calls.filter(([p, o]) => p === path && (o as ApiFetchOptions | undefined)?.method === 'POST');

const axeOptions: axe.RunOptions = { rules: { 'color-contrast': { enabled: false } } };
async function violations(node: Element) {
  const result = await axe.run(node, axeOptions);
  return result.violations.map((v) => `${v.id} (${v.impact})`);
}

function chooseFile(testId: string, text: string, name = 'customers.csv') {
  const input = screen.getByTestId(testId);
  const file = new File([text], name, { type: 'text/csv' });
  fireEvent.change(input, { target: { files: [file] } });
}

let confirmSpy: jest.SpyInstance;

beforeEach(() => {
  mocked.mockReset();
  signIn('DEVELOPER');
  confirmSpy = jest.spyOn(window, 'confirm').mockImplementation(() => true);
});

afterEach(() => {
  // In-page confirmation only (gates-D D7).
  expect(confirmSpy).not.toHaveBeenCalled();
  confirmSpy.mockRestore();
});

describe('loading', () => {
  it('shows a loading state, then the segment', async () => {
    serve(idList());
    render(<SegmentDetailPage />);
    expect(screen.getByTestId('segment-loading')).toHaveTextContent('Loading segment...');
    expect(await screen.findByTestId('segment-name-heading')).toHaveTextContent('Enterprise pilot');
    expect(screen.getByTestId('segment-detail-kind')).toHaveTextContent('ID list');
    expect(screen.getByTestId('segment-member-count')).toHaveTextContent('10');
    expect(await screen.findByTestId('segment-usage-none')).toBeInTheDocument();
  });

  it('a missing segment gets fixed copy, never the server text', async () => {
    mocked.mockImplementation(
      routedApi([
        {
          path: BASE,
          handler: () => {
            throw new ApiError({ status: 404, detail: PLANTED });
          },
        },
      ]) as unknown as typeof apiFetch,
    );
    const { container } = render(<SegmentDetailPage />);
    expect(await screen.findByTestId('segment-error')).toHaveTextContent('This segment no longer exists.');
    expect(document.body.textContent).not.toContain('PLANTED');
    expect(await violations(container)).toEqual([]);
  });
});

describe('adding ids from a file', () => {
  it('summarises the file, then adds it and reports the counts', async () => {
    serve(idList(), [
      {
        method: 'POST',
        path: `${BASE}/members`,
        handler: (_p, o) => {
          const sent = (o.json as { add: string[] }).add;
          return { added: sent.length - 1, already_members: 1, member_count: 12 };
        },
      },
    ]);
    const { container } = render(<SegmentDetailPage />);
    await screen.findByTestId('segment-name-heading');
    expect(await violations(container)).toEqual([]);
    chooseFile('upload-file-add', 'user_id\ncust-1\ncust-2\ncust-1\ncust-3\n' + 'x'.repeat(256) + '\n');
    const summary = await screen.findByTestId('upload-summary');
    expect(summary).toHaveTextContent('3 IDs found in customers.csv.');
    expect(summary).toHaveTextContent('1 duplicate removed.');
    expect(summary).toHaveTextContent('1 row skipped: line 6 is longer than 255 characters.');
    expect(summary).not.toHaveTextContent('xxxx');
    expect(await violations(container)).toEqual([]);
    fireEvent.click(screen.getByTestId('upload-start'));
    const done = await screen.findByTestId('upload-done');
    expect(done).toHaveTextContent('3 IDs uploaded. 2 were added and 1 was already a member. The segment now has 12 members.');
    expect(callsTo(`${BASE}/members`)).toEqual([[`${BASE}/members`, { method: 'POST', json: { add: ['cust-1', 'cust-2', 'cust-3'] } }]]);
    expect(screen.getByTestId('segment-member-count')).toHaveTextContent('12');
    expect(done).toHaveFocus();
  });

  it('stops at the first failed chunk, and Retry resends from it (D4)', async () => {
    let call = 0;
    serve(idList({ member_count: 0 }), [
      {
        method: 'POST',
        path: `${BASE}/members`,
        handler: (_p, o) => {
          call += 1;
          const sent = (o.json as { add: string[] }).add;
          if (call === 2) throw new ApiError({ status: 500, detail: PLANTED });
          return { added: sent.length, already_members: 0, member_count: call * 10 };
        },
      },
    ]);
    const { container } = render(<SegmentDetailPage />);
    await screen.findByTestId('segment-name-heading');
    const ids = Array.from({ length: 25_001 }, (_, i) => `u${i}`);
    chooseFile('upload-file-add', ids.join('\n'));
    fireEvent.click(await screen.findByTestId('upload-start'));
    const failed = await screen.findByTestId('upload-failed');
    expect(failed).toHaveTextContent('Upload stopped at chunk 2 of 3:');
    expect(failed).toHaveTextContent('10,000 IDs were added before it stopped.');
    expect(failed).toHaveTextContent('Adding the same IDs again is safe');
    expect(document.body.textContent).not.toContain('PLANTED');
    expect(callsTo(`${BASE}/members`)).toHaveLength(2);
    expect(await violations(container)).toEqual([]);

    fireEvent.click(screen.getByTestId('upload-retry'));
    const done = await screen.findByTestId('upload-done');
    const sent = callsTo(`${BASE}/members`).map(([, o]) => (o as ApiFetchOptions & { json: { add: string[] } }).json.add);
    expect(sent.map((c) => c.length)).toEqual([10_000, 10_000, 10_000, 5_001]);
    expect(sent[2]).toEqual(sent[1]);
    expect(done).toHaveTextContent('25,001 IDs uploaded. 25,001 were added');
  });

  it('refuses a file over the 1,000,000 cap and an empty file before any request (D6)', async () => {
    serve(idList({ member_count: 999_999 }));
    render(<SegmentDetailPage />);
    await screen.findByTestId('segment-name-heading');
    chooseFile('upload-file-add', '\n\n');
    expect(await screen.findByTestId('upload-file-problem')).toHaveTextContent('This file has no IDs.');
    chooseFile('upload-file-add', 'a\nb\n');
    expect(await screen.findByTestId('upload-over-cap')).toHaveTextContent(
      'This segment can hold 1,000,000 IDs. It has 999,999, so at most 1 more can be added; this file has 2.',
    );
    expect(screen.queryByTestId('upload-start')).toBeNull();
    expect(callsTo(`${BASE}/members`)).toEqual([]);
  });
});

describe('removing ids needs an in-page confirmation (D7)', () => {
  it('sends nothing until confirmed, and nothing on Cancel', async () => {
    serve(idList(), [
      { method: 'POST', path: `${BASE}/members/remove`, handler: () => ({ removed: 1, not_members: 1, member_count: 9 }) },
    ]);
    const { container } = render(<SegmentDetailPage />);
    await screen.findByTestId('segment-name-heading');
    fireEvent.click(screen.getByTestId('segment-mode-remove'));
    chooseFile('upload-file-remove', 'cust-1\ncust-9\n');
    fireEvent.click(await screen.findByTestId('upload-start'));
    const confirm = screen.getByTestId('upload-confirm');
    expect(confirm).toHaveTextContent(
      'Remove 2 IDs from Enterprise pilot? Flags and experiments that target this segment will stop matching these users. Existing experiment assignments are kept.',
    );
    expect(await violations(container)).toEqual([]);
    expect(callsTo(`${BASE}/members/remove`)).toEqual([]);
    fireEvent.click(screen.getByTestId('upload-confirm-cancel'));
    expect(callsTo(`${BASE}/members/remove`)).toEqual([]);
    fireEvent.click(screen.getByTestId('upload-start'));
    fireEvent.click(screen.getByTestId('upload-confirm-yes'));
    expect(await screen.findByTestId('upload-done')).toHaveTextContent(
      '2 IDs sent. 1 was removed and 1 was not a member. The segment now has 9 members.',
    );
    expect(callsTo(`${BASE}/members/remove`)).toEqual([
      [`${BASE}/members/remove`, { method: 'POST', json: { remove: ['cust-1', 'cust-9'] } }],
    ]);
  });
});

describe('checking a user', () => {
  it.each([
    [true, 'cust-1 is a member.'],
    [false, 'cust-1 is not a member.'],
  ])('answers is_member %s', async (isMember, text) => {
    signIn('VIEWER');
    serve(idList(), [
      { method: 'POST', path: `${BASE}/evaluate`, handler: () => ({ segment_id: SEG, segment_name: 'x', is_member: isMember }) },
    ]);
    render(<SegmentDetailPage />);
    await screen.findByTestId('segment-name-heading');
    fireEvent.change(screen.getByLabelText('User ID'), { target: { value: 'cust-1' } });
    fireEvent.click(screen.getByTestId('segment-check-submit'));
    expect(await screen.findByTestId('segment-check-answer')).toHaveTextContent(text);
    expect(callsTo(`${BASE}/evaluate`)).toEqual([[`${BASE}/evaluate`, { method: 'POST', json: { user_context: { user_id: 'cust-1' } } }]]);
  });
});

describe('archiving (D7, D17)', () => {
  it('asks in the page first, then archives and reloads', async () => {
    let status: Segment['status'] = 'active';
    serve(() => idList({ status }), [
      {
        method: 'DELETE',
        path: BASE,
        handler: () => {
          status = 'archived';
          return undefined;
        },
      },
    ]);
    const { container } = render(<SegmentDetailPage />);
    await screen.findByTestId('segment-name-heading');
    fireEvent.click(screen.getByTestId('segment-archive'));
    expect(screen.getByTestId('segment-archive-confirm')).toHaveTextContent(
      "Archive Enterprise pilot? Archived segments can't be used in targeting rules.",
    );
    expect(await violations(container)).toEqual([]);
    fireEvent.click(screen.getByTestId('segment-archive-cancel'));
    expect(mocked.mock.calls.filter(([, o]) => (o as ApiFetchOptions | undefined)?.method === 'DELETE')).toEqual([]);
    fireEvent.click(screen.getByTestId('segment-archive'));
    fireEvent.click(screen.getByTestId('segment-archive-yes'));
    expect(await screen.findByTestId('segment-archived-note')).toBeInTheDocument();
    expect(screen.getByTestId('segment-detail-status')).toHaveTextContent('Archived');
    expect(screen.queryByTestId('upload-add')).toBeNull();
    expect(screen.queryByTestId('segment-archive')).toBeNull();
  });

  it('a 409 lists the flags and experiments as links, from the body lists only', async () => {
    serve(idList(), [
      {
        method: 'DELETE',
        path: BASE,
        handler: () => {
          throw new ApiError({
            status: 409,
            detail: {
              code: 'segment_in_use',
              message: PLANTED,
              feature_flags: [{ id: 'f1', key: 'checkout-v2', name: 'Checkout v2' }],
              experiments: [
                { id: 'e1', key: 'pricing-page', name: 'Pricing', status: 'paused' },
                { id: 'e2', key: 'onboarding-v3', name: 'Onboarding', status: 'draft' },
              ],
            },
          });
        },
      },
    ]);
    const { container } = render(<SegmentDetailPage />);
    await screen.findByTestId('segment-name-heading');
    fireEvent.click(screen.getByTestId('segment-archive'));
    fireEvent.click(screen.getByTestId('segment-archive-yes'));
    const inUse = await screen.findByTestId('segment-in-use');
    expect(inUse).toHaveTextContent(
      'Enterprise pilot is used by 1 flag and 2 experiments. Remove the segment from their targeting rules, then archive it.',
    );
    const links = within(inUse).getAllByRole('link');
    expect(links.map((a) => [a.textContent, a.getAttribute('href')])).toEqual([
      ['checkout-v2', '/feature-flags/f1'],
      ['pricing-page', '/experiments/e1'],
      ['onboarding-v3', '/experiments/e2'],
    ]);
    expect(document.body.textContent).not.toContain('PLANTED');
    expect(screen.getByTestId('segment-archive-error')).toHaveFocus();
    expect(await violations(container)).toEqual([]);
  });
});

describe('rules segments', () => {
  it('saves changed rules with PUT and the dashboard shape', async () => {
    serve(rulesSegment(), [{ method: 'PUT', path: BASE, handler: (_p, o) => rulesSegment((o.json as { rules: unknown }).rules) }]);
    const { container } = render(<SegmentDetailPage />);
    await screen.findByTestId('segment-name-heading');
    expect(screen.getByTestId('segment-rules-save')).toBeDisabled();
    expect(await violations(container)).toEqual([]);
    fireEvent.change(screen.getByLabelText('Group 1, condition 1 value'), { target: { value: 'enterprise' } });
    fireEvent.click(screen.getByTestId('segment-rules-save'));
    expect(await screen.findByTestId('segment-rules-saved')).toHaveTextContent('Rules saved.');
    const puts = mocked.mock.calls.filter(([, o]) => (o as ApiFetchOptions | undefined)?.method === 'PUT');
    expect(puts).toEqual([
      [
        BASE,
        {
          method: 'PUT',
          json: {
            rules: {
              logical_operator: 'AND',
              groups: [{ logical_operator: 'AND', conditions: [{ attribute: 'plan', operator: 'equals', value: 'enterprise' }] }],
            },
          },
        },
      ],
    ]);
  });

  it('an unknown distance unit (#1204) refused on save is shown as one line in the builder words', async () => {
    serve(rulesSegment(), [
      {
        method: 'PUT',
        path: BASE,
        handler: () => {
          throw new ApiError({ status: 422, detail: [{ loc: ['body', 'rules'], msg: vocabularyRefusal.msg }] });
        },
      },
    ]);
    render(<SegmentDetailPage />);
    await screen.findByTestId('segment-name-heading');
    fireEvent.change(screen.getByLabelText('Group 1, condition 1 value'), { target: { value: 'enterprise' } });
    fireEvent.click(screen.getByTestId('segment-rules-save'));
    const error = await screen.findByTestId('segment-rules-error');
    expect(within(error).getAllByRole('listitem').map((li) => li.textContent)).toEqual([vocabularyRefusal.shown]);
  });

  it('the preview says so honestly when no assignment carries attributes', async () => {
    serve(rulesSegment(), [
      { method: 'POST', path: `${BASE}/preview`, handler: () => ({ estimated_percentage: 0, sample_size: 0, matched: 0 }) },
    ]);
    render(<SegmentDetailPage />);
    await screen.findByTestId('segment-name-heading');
    fireEvent.click(screen.getByTestId('segment-preview'));
    expect(await screen.findByTestId('segment-preview-result')).toHaveTextContent(
      "No recent assignments carry user attributes, so the size can't be estimated yet. The segment still works: it is evaluated against each request's attributes.",
    );
  });

  it('a preview with a sample gives the share', async () => {
    serve(rulesSegment(), [
      { method: 'POST', path: `${BASE}/preview`, handler: () => ({ estimated_percentage: 34.2, sample_size: 2150, matched: 735 }) },
    ]);
    render(<SegmentDetailPage />);
    await screen.findByTestId('segment-name-heading');
    fireEvent.click(screen.getByTestId('segment-preview'));
    expect(await screen.findByTestId('segment-preview-result')).toHaveTextContent('About 34% of 2,150 recently assigned users match.');
  });

  it.each([
    ['the legacy shape', { operator: 'and', conditions: [{ attribute: 'plan', operator: 'eq', value: 'pro' }] }],
    ['a legacy operator', { groups: [{ conditions: [{ attribute: 'plan', operator: 'eq', value: 'pro' }] }] }],
    ['no groups', { groups: [] }],
    ['an empty group', { groups: [{ conditions: [] }] }],
    ['groups as text', { groups: 'x' }],
  ])('rules in %s are "not valid", shown as stored, and can be replaced (D18)', async (_label, rules) => {
    serve(rulesSegment(rules));
    const { container } = render(<SegmentDetailPage />);
    await screen.findByTestId('segment-name-heading');
    expect(screen.getByTestId('segment-rules-invalid')).toHaveTextContent(RULES_NOT_VALID);
    expect(screen.getByTestId('segment-rules-json')).toBeInTheDocument();
    expect(await violations(container)).toEqual([]);
    fireEvent.click(screen.getByTestId('segment-rules-replace'));
    expect(screen.getByTestId('segment-rules-replace-confirm')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('segment-rules-replace-yes'));
    expect(screen.getByLabelText('Group 1, condition 1 attribute')).toBeInTheDocument();
  });

  it('valid rules are not marked', async () => {
    serve(rulesSegment());
    render(<SegmentDetailPage />);
    await screen.findByTestId('segment-name-heading');
    expect(screen.queryByTestId('segment-rules-invalid')).toBeNull();
  });
});

describe('roles (D13)', () => {
  it.each(['ANALYST', 'VIEWER'])('%s sees no control that changes a segment', async (role) => {
    signIn(role);
    serve(idList());
    const { container, unmount } = render(<SegmentDetailPage />);
    await screen.findByTestId('segment-name-heading');
    for (const id of ['segment-mode-add', 'segment-mode-remove', 'upload-add', 'upload-remove', 'segment-archive']) {
      expect(screen.queryByTestId(id)).toBeNull();
    }
    expect(screen.getByTestId('segment-role-note')).toHaveTextContent('ADMIN and DEVELOPER');
    expect(screen.getByTestId('segment-check-user')).toBeInTheDocument();
    expect(await violations(container)).toEqual([]);
    unmount();

    serve(rulesSegment({ groups: [] }));
    render(<SegmentDetailPage />);
    await screen.findByTestId('segment-name-heading');
    expect(screen.queryByTestId('segment-rules-replace')).toBeNull();
    expect(screen.queryByTestId('segment-rules-save')).toBeNull();
  });

  it.each(['ANALYST', 'VIEWER'])('%s reads valid rules in a read-only builder', async (role) => {
    signIn(role);
    serve(rulesSegment());
    render(<SegmentDetailPage />);
    await screen.findByTestId('segment-name-heading');
    expect(screen.getByLabelText('Group 1, condition 1 attribute')).toBeDisabled();
    expect(screen.queryByTestId('segment-rules-save')).toBeNull();
    expect(screen.getByTestId('segment-preview')).toBeInTheDocument();
  });

  it('a DEVELOPER (not a superuser) gets every control', async () => {
    serve(idList());
    render(<SegmentDetailPage />);
    await screen.findByTestId('segment-name-heading');
    for (const id of ['segment-mode-add', 'segment-mode-remove', 'upload-add', 'segment-archive']) {
      expect(screen.getByTestId(id)).toBeInTheDocument();
    }
    expect(screen.queryByTestId('segment-role-note')).toBeNull();
  });
});

describe('a member route refusal is fixed copy (D14)', () => {
  it('409 on the members route', async () => {
    serve(idList(), [
      {
        method: 'POST',
        path: `${BASE}/members`,
        handler: () => {
          throw new ApiError({ status: 409, detail: PLANTED });
        },
      },
    ]);
    render(<SegmentDetailPage />);
    await screen.findByTestId('segment-name-heading');
    chooseFile('upload-file-add', 'a\n');
    fireEvent.click(await screen.findByTestId('upload-start'));
    const failed = await screen.findByTestId('upload-failed');
    expect(failed).toHaveTextContent("This segment's IDs cannot be changed: it is archived, or it is a rules segment.");
    expect(document.body.textContent).not.toContain('PLANTED');
    await act(async () => undefined);
  });
});

describe('the usage list', () => {
  it('links each flag and experiment by name', async () => {
    mocked.mockImplementation(
      routedApi([
        { path: BASE, handler: () => idList() },
        {
          path: `${BASE}/experiments`,
          handler: () => ({
            segment_id: SEG,
            feature_flags: [{ id: 'f1', name: 'Checkout v2' }],
            experiments: [{ id: 'e1', name: 'Pricing', status: 'active' }],
          }),
        },
      ]) as unknown as typeof apiFetch,
    );
    render(<SegmentDetailPage />);
    const list = await screen.findByTestId('segment-usage-list');
    await waitFor(() => expect(within(list).getAllByRole('link')).toHaveLength(2));
    expect(within(list).getByRole('link', { name: 'Checkout v2' })).toHaveAttribute('href', '/feature-flags/f1');
    expect(list).toHaveTextContent('Experiment Pricing (active)');
  });

  it('a failed usage lookup shows an error, never "none" (#888)', async () => {
    mocked.mockImplementation(
      routedApi([
        { path: BASE, handler: () => idList() },
        {
          path: `${BASE}/experiments`,
          handler: () => {
            throw new ApiError({ status: 500, detail: PLANTED });
          },
        },
      ]) as unknown as typeof apiFetch,
    );
    render(<SegmentDetailPage />);
    const failed = await screen.findByTestId('segment-usage-error');
    expect(failed).toHaveAttribute('role', 'alert');
    expect(screen.queryByTestId('segment-usage-none')).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain('PLANTED');
  });
});
