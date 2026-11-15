/**
 * The four workspace overlays on the shared Dialog (#369): Create Workspace
 * (the list page), Edit Workspace (the overview page), and Invite Member and
 * Remove Member (the members page).
 *
 * Each one is opened from its real button and must: be a named modal dialog,
 * make the page behind it inert, close on Escape (Create, Edit and Remove did
 * not before #369), give focus back to its button, start with focus where the
 * user starts, and pass an axe scan of the dialog node. The existing tests of
 * the pages stay where they are; Invite Member's own are in
 * workspace-members-invite.test.tsx.
 */
import React from 'react';
import { fireEvent, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import { ModulesProvider } from '@/contexts/ModulesContext';
import { APP_ROOT_ID, renderInAppRoot } from '@/tests/helpers/renderInAppRoot';
import WorkspaceOverviewPage from '@modules/pages/workspaces/[id]/index';
import WorkspaceMembersPage from '@modules/pages/workspaces/[id]/members';
import WorkspacesPage from '@modules/pages/workspaces/index';
import { workspaceService } from '@modules/services/workspaces';

jest.mock('next/router', () => ({
  useRouter: () => ({
    pathname: '/workspaces/[id]',
    asPath: '/workspaces/ws-1',
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
    list: jest.fn(),
    create: jest.fn(),
    update: jest.fn(),
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
  member_count: 2,
  created_at: '2026-10-01T00:00:00Z',
};

const MEMBERS = [
  { user_id: 'u-owner', username: 'olive', email: 'olive@example.com', role: 'OWNER', joined_at: '2026-10-01T00:00:00Z' },
  { user_id: 'u-bob', username: 'bob', email: 'bob@example.com', role: 'DEVELOPER', joined_at: '2026-10-02T00:00:00Z' },
];

beforeEach(() => {
  jest.clearAllMocks();
  (workspaceService.get as jest.Mock).mockResolvedValue(WORKSPACE);
  (workspaceService.list as jest.Mock).mockResolvedValue([WORKSPACE]);
  (workspaceService.listMembers as jest.Mock).mockResolvedValue(MEMBERS);
});

function inApp(page: React.ReactElement) {
  return renderInAppRoot(
    <ModulesProvider initial={{ profile: 'full', modules: ['workspaces'], version: 'test' }}>
      {page}
    </ModulesProvider>,
  );
}

interface Scenario {
  name: string;
  title: string;
  render: () => void;
  opener: () => Promise<HTMLElement>;
  /** What has focus when the dialog opens. */
  firstFocus: () => HTMLElement;
  /** The control that closes it without acting. */
  cancel: () => HTMLElement;
}

const SCENARIOS: Scenario[] = [
  {
    name: 'Create Workspace',
    title: 'Create Workspace',
    render: () => inApp(<WorkspacesPage />),
    opener: () => screen.findByRole('button', { name: '+ Create Workspace' }),
    firstFocus: () => screen.getByRole('textbox', { name: /^Name/ }),
    cancel: () => screen.getByRole('button', { name: 'Cancel' }),
  },
  {
    name: 'Edit Workspace',
    title: 'Edit Workspace',
    render: () => inApp(<WorkspaceOverviewPage />),
    opener: () => screen.findByRole('button', { name: 'Edit' }),
    firstFocus: () => screen.getByRole('textbox', { name: /^Name/ }),
    cancel: () => screen.getByRole('button', { name: 'Cancel' }),
  },
  {
    name: 'Invite Member',
    title: 'Invite Member',
    render: () => inApp(<WorkspaceMembersPage />),
    opener: () => screen.findByRole('button', { name: '+ Invite Member' }),
    firstFocus: () => screen.getByLabelText(/Email Address/),
    cancel: () => screen.getByRole('button', { name: 'Cancel' }),
  },
  {
    name: 'Remove Member',
    title: 'Remove Member',
    render: () => inApp(<WorkspaceMembersPage />),
    opener: async () => {
      // Two rows; the owner is the last owner and cannot be removed, so bob's is the one.
      const buttons = await screen.findAllByRole('button', { name: 'Remove' });
      return buttons.find((b) => !(b as HTMLButtonElement).disabled) as HTMLElement;
    },
    // A destructive confirm starts on Cancel.
    firstFocus: () => screen.getByRole('button', { name: 'Cancel' }),
    cancel: () => screen.getByRole('button', { name: 'Cancel' }),
  },
];

const appRoot = () => document.getElementById(APP_ROOT_ID) as HTMLElement;

describe.each(SCENARIOS)('$name on the shared Dialog (#369)', (scenario) => {
  async function open() {
    const user = userEvent.setup();
    scenario.render();
    const opener = await scenario.opener();
    await user.click(opener);
    return { user, opener };
  }

  it('opens as a modal dialog with its name', async () => {
    await open();
    const dialog = screen.getByRole('dialog', { name: scenario.title });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(document.body).toContainElement(dialog);
    expect(appRoot().contains(dialog)).toBe(false);
  });

  it('the page behind it is inert while it is open, and not after Cancel', async () => {
    const { user } = await open();
    expect(appRoot()).toHaveAttribute('inert');
    await user.click(scenario.cancel());
    expect(screen.queryByRole('heading', { name: scenario.title })).not.toBeInTheDocument();
    expect(appRoot()).not.toHaveAttribute('inert');
  });

  it('starts with focus where the user starts', async () => {
    await open();
    expect(scenario.firstFocus()).toHaveFocus();
  });

  it('Escape on document closes it, and focus returns to the button that opened it', async () => {
    const { opener } = await open();
    // By the title: the three that had no role before #369 are not found by role.
    expect(screen.getByRole('heading', { name: scenario.title })).toBeInTheDocument();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('heading', { name: scenario.title })).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it('Escape typed in the dialog closes it too, and so does Cancel, each returning focus', async () => {
    const { user, opener } = await open();
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('heading', { name: scenario.title })).not.toBeInTheDocument();
    expect(opener).toHaveFocus();

    await user.click(opener);
    await user.click(scenario.cancel());
    expect(screen.queryByRole('heading', { name: scenario.title })).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it('the dialog has no axe violations, and the rule that names it ran on it', async () => {
    await open();
    const dialog = screen.getByRole('dialog');
    const result = await axe.run(dialog, { rules: { 'color-contrast': { enabled: false } } });
    expect(result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(', ')}`)).toStrictEqual([]);
    expect(result.passes.map((p) => p.id)).toContain('aria-dialog-name');
  });
});

describe('Remove Member, a destructive confirm', () => {
  async function openRemove() {
    const user = userEvent.setup();
    inApp(<WorkspaceMembersPage />);
    const buttons = await screen.findAllByRole('button', { name: 'Remove' });
    await user.click(buttons.find((b) => !(b as HTMLButtonElement).disabled) as HTMLElement);
    return user;
  }

  it('is described by the question it asks', async () => {
    await openRemove();
    expect(screen.getByRole('dialog', { name: 'Remove Member' })).toHaveAccessibleDescription(
      'Are you sure you want to remove bob from this workspace?',
    );
  });

  it('Escape is Cancel: it never removes the member', async () => {
    await openRemove();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('heading', { name: 'Remove Member' })).not.toBeInTheDocument();
    expect(workspaceService.removeMember).not.toHaveBeenCalled();
  });

  it('Remove removes that member and closes', async () => {
    (workspaceService.removeMember as jest.Mock).mockResolvedValue(undefined);
    const user = await openRemove();
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Remove' }));
    expect(workspaceService.removeMember).toHaveBeenCalledWith('ws-1', 'u-bob');
    await screen.findByText('olive');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
});
