import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { RoleTable } from '@modules/components/admin/roles/RoleTable';
import { RbacService } from '@modules/rbac';
import { CustomRole } from '@/types/admin';
import { makeRole } from './rbacFixtures';

jest.mock('@modules/rbac');

const mockListRoles = RbacService.listRoles as jest.Mock;
const mockDeleteRole = RbacService.deleteRole as jest.Mock;

const mockRoles: CustomRole[] = [
  makeRole({
    id: 'r1',
    name: 'developer-role',
    description: 'A developer role',
    permissions: [
      { resource: 'experiment', actions: ['read', 'create'] },
      { resource: 'feature_flag', actions: ['read'] },
    ],
    user_count: 4,
  }),
  makeRole({
    id: 'r2',
    name: 'analyst-role',
    description: 'An analyst role',
    permissions: [{ resource: 'audit_log', actions: ['read'] }],
    user_count: 0,
  }),
];

beforeEach(() => {
  jest.clearAllMocks();
  // Suppress window.confirm warnings in test output
  window.confirm = jest.fn(() => true);
});

describe('RoleTable', () => {
  it('renders loading state initially', () => {
    mockListRoles.mockImplementation(() => new Promise(() => {}));
    render(<RoleTable onEditRole={jest.fn()} />);
    expect(screen.getByTestId('role-table-loading')).toBeInTheDocument();
  });

  it('renders role rows after data loads', async () => {
    mockListRoles.mockResolvedValue(mockRoles);
    render(<RoleTable onEditRole={jest.fn()} />);
    await waitFor(() => {
      expect(screen.getByText('developer-role')).toBeInTheDocument();
      expect(screen.getByText('analyst-role')).toBeInTheDocument();
    });
  });

  it('shows role name and description', async () => {
    mockListRoles.mockResolvedValue(mockRoles);
    render(<RoleTable onEditRole={jest.fn()} />);
    await waitFor(() => {
      expect(screen.getByText('developer-role')).toBeInTheDocument();
      expect(screen.getByText('A developer role')).toBeInTheDocument();
      expect(screen.getByText('analyst-role')).toBeInTheDocument();
      expect(screen.getByText('An analyst role')).toBeInTheDocument();
    });
  });

  it('shows each role\'s permissions as text, and how many users have it (#891)', async () => {
    mockListRoles.mockResolvedValue(mockRoles);
    render(<RoleTable onEditRole={jest.fn()} />);
    expect(await screen.findByText('experiment: read, create · feature_flag: read')).toBeInTheDocument();
    expect(screen.getByText('audit_log: read')).toBeInTheDocument();
    const row = screen.getByText('developer-role').closest('tr')!;
    expect(row).toHaveTextContent('4');
  });

  it('names Edit and Delete by the role', async () => {
    mockListRoles.mockResolvedValue(mockRoles);
    render(<RoleTable onEditRole={jest.fn()} />);
    await screen.findByText('developer-role');
    expect(screen.getByRole('button', { name: 'Edit developer-role' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Delete analyst-role' })).toBeInTheDocument();
  });

  it('clicking edit button calls onEditRole callback', async () => {
    mockListRoles.mockResolvedValue(mockRoles);
    const onEditRole = jest.fn();
    render(<RoleTable onEditRole={onEditRole} />);
    await waitFor(() => {
      expect(screen.getByText('developer-role')).toBeInTheDocument();
    });
    const editButtons = screen.getAllByTestId('edit-role-button');
    fireEvent.click(editButtons[0]);
    expect(onEditRole).toHaveBeenCalledWith(mockRoles[0]);
  });

  it('clicking delete shows confirmation', async () => {
    (window.confirm as jest.Mock).mockReturnValue(false);
    mockListRoles.mockResolvedValue(mockRoles);
    mockDeleteRole.mockResolvedValue(undefined);
    render(<RoleTable onEditRole={jest.fn()} />);
    await waitFor(() => {
      expect(screen.getByText('developer-role')).toBeInTheDocument();
    });
    const deleteButtons = screen.getAllByTestId('delete-role-button');
    fireEvent.click(deleteButtons[0]);
    expect(window.confirm).toHaveBeenCalled();
    expect(mockDeleteRole).not.toHaveBeenCalled();
  });

  // Check #47: the confirmation says who loses the role's permissions (#891).
  it.each([
    [0, 'Delete the role "data-scientist"? No user has it. This cannot be undone.'],
    [1, 'Delete the role "data-scientist"? The 1 user who has it loses its permissions at once. This cannot be undone.'],
    [
      3,
      'Delete the role "data-scientist"? The 3 users who have it lose its permissions at once. This cannot be undone.',
    ],
  ])('the delete confirmation for a role with %i users says who is affected', async (userCount, text) => {
    (window.confirm as jest.Mock).mockReturnValue(false);
    mockListRoles.mockResolvedValue([makeRole({ name: 'data-scientist', user_count: userCount })]);
    render(<RoleTable onEditRole={jest.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Delete data-scientist' }));
    expect(window.confirm).toHaveBeenCalledTimes(1);
    expect(window.confirm).toHaveBeenCalledWith(text);
    expect(mockDeleteRole).not.toHaveBeenCalled();
  });

  it('delete calls RbacService.deleteRole', async () => {
    mockListRoles.mockResolvedValue(mockRoles);
    mockDeleteRole.mockResolvedValue(undefined);
    // mockListRoles returns the same on refetch
    mockListRoles.mockResolvedValue(mockRoles);

    render(<RoleTable onEditRole={jest.fn()} />);
    await waitFor(() => {
      expect(screen.getByText('developer-role')).toBeInTheDocument();
    });

    const deleteButtons = screen.getAllByTestId('delete-role-button');
    fireEvent.click(deleteButtons[0]);

    await waitFor(() => {
      expect(mockDeleteRole).toHaveBeenCalledWith('developer-role');
    });
    await waitFor(() => {
      expect(mockListRoles).toHaveBeenCalledTimes(2);
    });
  });

  it('shows empty state when no roles', async () => {
    mockListRoles.mockResolvedValue([]);
    render(<RoleTable onEditRole={jest.fn()} />);
    await waitFor(() => {
      expect(screen.getByTestId('role-table-empty')).toBeInTheDocument();
    });
  });

  it('shows error state if listRoles fails', async () => {
    mockListRoles.mockRejectedValue(new Error('Failed to fetch roles'));
    render(<RoleTable onEditRole={jest.fn()} />);
    await waitFor(() => {
      expect(screen.getByTestId('role-table-error')).toBeInTheDocument();
    });
  });

  it('renders with data-testid="role-table"', async () => {
    mockListRoles.mockResolvedValue(mockRoles);
    render(<RoleTable onEditRole={jest.fn()} />);
    expect(screen.getByTestId('role-table')).toBeInTheDocument();
    await screen.findByText('developer-role');
  });
});
