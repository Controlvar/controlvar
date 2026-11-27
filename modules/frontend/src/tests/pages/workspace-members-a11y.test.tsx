/**
 * The members page's role select names the member it changes (WCAG 4.1.2),
 * and the four workspace pages use no grey lighter than slate-500 for text
 * (slate-400 is 2.56:1 on white; jsdom cannot measure contrast, so the
 * source is the pin).
 */
import fs from 'fs';
import path from 'path';
import React from 'react';
import { screen } from '@testing-library/react';
import axe from 'axe-core';
import { ModulesProvider } from '@/contexts/ModulesContext';
import { renderInAppRoot } from '@/tests/helpers/renderInAppRoot';
import WorkspaceMembersPage from '@modules/pages/workspaces/[id]/members';
import { workspaceService } from '@modules/services/workspaces';

jest.mock('next/router', () => ({
  useRouter: () => ({
    pathname: '/workspaces/[id]/members',
    asPath: '/workspaces/ws-1/members',
    query: { id: 'ws-1' },
    isReady: true,
    push: jest.fn(),
    replace: jest.fn(),
  }),
}));

jest.mock('next/head', () => {
  const MockHead = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  MockHead.displayName = 'MockHead';
  return MockHead;
});

jest.mock('@modules/services/workspaces', () => ({
  workspaceService: {
    get: jest.fn(),
    listMembers: jest.fn(),
    sendInvite: jest.fn(),
    updateMember: jest.fn(),
    removeMember: jest.fn(),
  },
}));

const WORKSPACE = {
  id: 'ws-1',
  name: 'Growth',
  slug: 'growth',
  description: 'Growth squad',
  is_active: true,
  member_count: 3,
  created_at: '2026-10-01T00:00:00Z',
};

const MEMBERS = [
  { user_id: 'u-owner', username: 'olive', email: 'olive@example.com', role: 'OWNER', joined_at: '2026-10-01T00:00:00Z' },
  { user_id: 'u-bob', username: 'bob', email: 'bob@example.com', role: 'DEVELOPER', joined_at: '2026-10-02T00:00:00Z' },
  { user_id: 'u-cy', username: 'cy', email: 'cy@example.com', role: 'VIEWER', joined_at: '2026-10-03T00:00:00Z' },
];

beforeEach(() => {
  jest.clearAllMocks();
  (workspaceService.get as jest.Mock).mockResolvedValue(WORKSPACE);
  (workspaceService.listMembers as jest.Mock).mockResolvedValue(MEMBERS);
});

describe("a member's role select", () => {
  it('is named for the member, and axe finds no unnamed control on the page', async () => {
    const { container } = renderInAppRoot(
      <ModulesProvider initial={{ profile: 'full', modules: ['workspaces'], version: 'test' }}>
        <WorkspaceMembersPage />
      </ModulesProvider>,
    );
    await screen.findByText('bob');
    expect(screen.getByRole('combobox', { name: 'Role for bob' })).toHaveValue('DEVELOPER');
    expect(screen.getByRole('combobox', { name: 'Role for cy' })).toHaveValue('VIEWER');
    const result = await axe.run(container, {
      runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa'] },
      rules: { 'color-contrast': { enabled: false } },
    });
    expect(result.violations.map((v) => `${v.id}: ${v.nodes.length}`)).toEqual([]);
    // The rule that names a select ran on both selects: a scan that skipped
    // it would pass on nothing.
    expect(result.passes.find((p) => p.id === 'select-name')?.nodes).toHaveLength(2);
  });
});

describe('the workspace pages use no slate-400 text', () => {
  const PAGES = [
    'index.tsx',
    'new.tsx',
    path.join('[id]', 'index.tsx'),
    path.join('[id]', 'members.tsx'),
  ];
  const DIR = path.join(__dirname, '..', '..', 'pages', 'workspaces');

  it.each(PAGES)('%s', (file) => {
    const source = fs.readFileSync(path.join(DIR, file), 'utf8');
    expect(source.match(/(?<!placeholder:)\btext-slate-400\b/g)).toBeNull();
    // The pattern reads this file's greys: a file with none would pass vacuously.
    expect(source).toMatch(/\btext-slate-[5-9]00\b/);
  });
});
