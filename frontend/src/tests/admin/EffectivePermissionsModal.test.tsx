import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { EffectivePermissionsModal } from '@/components/admin/users/EffectivePermissionsModal';
import { RbacService } from '@modules/rbac';
import { shapeErrors } from '@/tests/helpers/openapiShape';

jest.mock('@modules/rbac');

const mockGetUserPermissions = RbacService.getUserPermissions as jest.Mock;

// Custom roles and direct grants take effect (#891): the dialog says what the
// permission checks allow, and what no custom role or grant can give.
const ACCESS_NOTE =
  'What this user can do: their built-in role, plus their custom roles and unexpired direct grants. ' +
  "Managing users, custom roles and grants, other users' API keys, holdouts and safety settings is not " +
  'listed: it depends on the built-in role or the superuser flag, never on a custom role or grant.';

/**
 * What `GET /api/v1/rbac/users/{id}/permissions` answers: an object map from a
 * resource to its actions. Checked against the snapshot's
 * `EffectivePermissionsResponse` so the mock cannot drift from the API.
 */
function answer(
  permissions: Record<string, string[]>,
  overrides: Partial<{ base_role: string; custom_roles: string[]; is_superuser: boolean }> = {},
) {
  const body = {
    user_id: 'user-123',
    username: 'ada',
    base_role: 'analyst',
    custom_roles: ['data-scientist'],
    permissions,
    is_superuser: false,
    ...overrides,
  };
  expect(shapeErrors('EffectivePermissionsResponse', body)).toEqual([]);
  return body;
}

const lines = (testId: string) =>
  Array.from(screen.getByTestId(testId).querySelectorAll('li')).map((li) => li.textContent);

beforeEach(() => {
  jest.clearAllMocks();
});

describe('EffectivePermissionsModal', () => {
  const defaultProps = {
    isOpen: true,
    userId: 'user-123',
    userEmail: 'user@example.com',
    onClose: jest.fn(),
  };

  it('renders modal when isOpen=true', async () => {
    mockGetUserPermissions.mockResolvedValue(answer({}));
    render(<EffectivePermissionsModal {...defaultProps} />);
    expect(screen.getByTestId('effective-permissions-modal')).toBeInTheDocument();
    await waitFor(() => {
      expect(mockGetUserPermissions).toHaveBeenCalledWith('user-123');
    });
  });

  it('shows the built-in role and the custom roles that take effect (#891)', async () => {
    mockGetUserPermissions.mockResolvedValue(answer({ experiment: ['list', 'read'] }));
    render(<EffectivePermissionsModal {...defaultProps} />);
    await screen.findByTestId('permissions-summary');
    expect(lines('permissions-summary')).toEqual(['Built-in role: Analyst', 'Custom roles: data-scientist']);
  });

  it('says "None" with no custom role, and that a superuser passes every check', async () => {
    mockGetUserPermissions.mockResolvedValue(
      answer({ experiment: ['create'] }, { base_role: 'viewer', custom_roles: [], is_superuser: true }),
    );
    render(<EffectivePermissionsModal {...defaultProps} />);
    await screen.findByTestId('permissions-summary');
    expect(lines('permissions-summary')).toEqual([
      'Built-in role: Viewer',
      'Custom roles: None',
      'Superuser: passes every permission check',
    ]);
  });

  it("renders the API's object map, one line per resource, and the audit log in words (#891)", async () => {
    mockGetUserPermissions.mockResolvedValue(
      answer({
        experiment: ['create', 'read', 'update'],
        feature_flag: ['read'],
        audit_log: ['read'],
      }),
    );
    render(<EffectivePermissionsModal {...defaultProps} />);
    await screen.findByTestId('permissions-list');
    expect(lines('permissions-list')).toEqual([
      'Experiments: create, read, update',
      'Feature flags: read',
      "Audit log: read, including other users' entries",
    ]);
  });

  it('says "own entries only" when the API lists no audit_log (D1.3)', async () => {
    mockGetUserPermissions.mockResolvedValue(answer({ experiment: ['list', 'read'], feature_flag: ['list', 'read'] }));
    render(<EffectivePermissionsModal {...defaultProps} />);
    await screen.findByTestId('permissions-list');
    expect(screen.getByTestId('permissions-audit-log')).toHaveTextContent('Audit log: own entries only');
  });

  it('hides a resource whose action list is empty (#891)', async () => {
    mockGetUserPermissions.mockResolvedValue(answer({ experiment: [], feature_flag: ['read'] }));
    render(<EffectivePermissionsModal {...defaultProps} />);
    await screen.findByTestId('permissions-list');
    expect(lines('permissions-list')).toEqual(['Feature flags: read', 'Audit log: own entries only']);
  });

  it('shows loading while fetching', () => {
    mockGetUserPermissions.mockImplementation(() => new Promise(() => {}));
    render(<EffectivePermissionsModal {...defaultProps} />);
    expect(screen.getByTestId('permissions-loading')).toBeInTheDocument();
  });

  it('shows error if fetch fails', async () => {
    mockGetUserPermissions.mockRejectedValue(new Error('Forbidden'));
    render(<EffectivePermissionsModal {...defaultProps} />);
    await waitFor(() => {
      expect(screen.getByTestId('permissions-error')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('permissions-list')).toBeNull();
  });

  it('is titled "Effective permissions" and says, above the lists, what it shows and what it cannot (#891)', async () => {
    mockGetUserPermissions.mockResolvedValue(answer({ experiment: ['read', 'create'] }));
    render(<EffectivePermissionsModal {...defaultProps} />);
    const dialog = screen.getByRole('dialog', { name: 'Effective permissions' });
    expect(dialog).toHaveAccessibleDescription(ACCESS_NOTE);
    const list = await screen.findByTestId('permissions-list');
    const note = screen.getByTestId('permissions-access-note');
    expect(note).toHaveTextContent(ACCESS_NOTE);
    // Above the lists, so it is read before the permissions are.
    expect(note.compareDocumentPosition(list) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.queryByText('Recorded Permissions')).not.toBeInTheDocument();
  });

  it('closes on Escape, and the close button has a name and a readable colour', async () => {
    mockGetUserPermissions.mockResolvedValue(answer({}));
    const onClose = jest.fn();
    render(<EffectivePermissionsModal {...defaultProps} onClose={onClose} />);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
    const x = screen.getByTestId('permissions-close-button');
    expect(x).toHaveAccessibleName('Close');
    expect(x.className).not.toMatch(/slate-400/);
    await screen.findByTestId('permissions-list');
  });

  it('renders with data-testid="effective-permissions-modal"', async () => {
    mockGetUserPermissions.mockResolvedValue(answer({}));
    render(<EffectivePermissionsModal {...defaultProps} />);
    expect(screen.getByTestId('effective-permissions-modal')).toBeInTheDocument();
    await waitFor(() => {
      expect(mockGetUserPermissions).toHaveBeenCalledWith('user-123');
    });
  });
});
