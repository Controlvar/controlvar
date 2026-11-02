import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { EffectivePermissionsModal } from '@/components/admin/users/EffectivePermissionsModal';
import { RbacService } from '@modules/rbac';
import { shapeErrors } from '@/tests/helpers/openapiShape';

jest.mock('@modules/rbac');

const mockGetUserPermissions = RbacService.getUserPermissions as jest.Mock;

// No permission check reads custom roles or direct grants yet (#891), so the
// dialog must not present the list as what the user can do.
const ACCESS_NOTE =
  "This list adds the user's custom roles and direct grants to their built-in role. " +
  'Custom roles and direct permission grants are recorded, but they do not change what ' +
  "anyone can do yet: each user's built-in role decides.";

/**
 * What `GET /api/v1/rbac/users/{id}/permissions` answers: an object map from a
 * resource to its actions. Checked against the snapshot's
 * `EffectivePermissionsResponse` so the mock cannot drift from the API.
 */
function answer(permissions: Record<string, string[]>) {
  const body = {
    user_id: 'user-123',
    username: 'ada',
    base_role: 'developer',
    custom_roles: ['data-scientist'],
    permissions,
    is_superuser: false,
  };
  expect(shapeErrors('EffectivePermissionsResponse', body)).toEqual([]);
  return body;
}

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

  it('renders the API\'s object map, one line per resource (#891)', async () => {
    mockGetUserPermissions.mockResolvedValue(
      answer({
        experiment: ['create', 'read', 'update'],
        feature_flag: ['read'],
        audit_log: ['read'],
      }),
    );
    render(<EffectivePermissionsModal {...defaultProps} />);
    const list = await screen.findByTestId('permissions-list');
    const lines = Array.from(list.querySelectorAll('li')).map((li) => li.textContent);
    expect(lines).toEqual([
      'Experiments: create, read, update',
      'Feature flags: read',
      'Audit log: read',
    ]);
  });

  it('hides a resource whose action list is empty (#891)', async () => {
    mockGetUserPermissions.mockResolvedValue(
      answer({ experiment: [], feature_flag: ['read'] }),
    );
    render(<EffectivePermissionsModal {...defaultProps} />);
    const list = await screen.findByTestId('permissions-list');
    const lines = Array.from(list.querySelectorAll('li')).map((li) => li.textContent);
    expect(lines).toEqual(['Feature flags: read']);
  });

  it('says so when there are no permissions', async () => {
    mockGetUserPermissions.mockResolvedValue(answer({}));
    render(<EffectivePermissionsModal {...defaultProps} />);
    expect(await screen.findByText('No permissions assigned.')).toBeInTheDocument();
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
  });

  it('says, above the list, that custom roles and direct grants do not change access yet (#891)', async () => {
    mockGetUserPermissions.mockResolvedValue(answer({ experiment: ['read', 'create'] }));
    render(<EffectivePermissionsModal {...defaultProps} />);
    const dialog = screen.getByRole('dialog', { name: 'Recorded Permissions' });
    expect(dialog).toHaveAccessibleDescription(ACCESS_NOTE);
    const list = await screen.findByTestId('permissions-list');
    const note = screen.getByTestId('permissions-access-note');
    expect(note).toHaveTextContent(ACCESS_NOTE);
    // Above the list, so it is read before the permissions are.
    expect(note.compareDocumentPosition(list) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.queryByText('Effective Permissions')).not.toBeInTheDocument();
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
    await screen.findByText('No permissions assigned.');
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
