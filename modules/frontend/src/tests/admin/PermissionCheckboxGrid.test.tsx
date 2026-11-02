import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { PermissionCheckboxGrid } from '@modules/components/admin/roles/PermissionCheckboxGrid';

// Exactly the pairs the API accepts (GRANTABLE in modules/backend/app/schemas/rbac.py).
const ALL_PERMISSIONS = [
  'experiment:create',
  'experiment:read',
  'experiment:update',
  'experiment:delete',
  'experiment:list',
  'feature_flag:create',
  'feature_flag:read',
  'feature_flag:update',
  'feature_flag:delete',
  'feature_flag:list',
  'audit_log:read',
];

describe('PermissionCheckboxGrid', () => {
  it('renders the three groups the API accepts, and no others', () => {
    render(<PermissionCheckboxGrid selectedPermissions={[]} onChange={jest.fn()} />);
    expect(screen.getAllByRole('group').map((g) => g.querySelector('legend')?.textContent)).toEqual([
      'Experiments',
      'Feature flags',
      'Audit log',
    ]);
  });

  it('labels the audit log checkbox "Read the audit log"', () => {
    render(<PermissionCheckboxGrid selectedPermissions={[]} onChange={jest.fn()} />);
    expect(screen.getByRole('checkbox', { name: 'Read the audit log' })).toBe(
      screen.getByTestId('permission-checkbox-audit_log:read'),
    );
  });

  it('offers exactly the pairs the API accepts', () => {
    render(<PermissionCheckboxGrid selectedPermissions={[]} onChange={jest.fn()} />);
    const offered = screen
      .getAllByRole('checkbox')
      .map((c) => c.getAttribute('data-testid')!.replace('permission-checkbox-', ''));
    expect(offered).toEqual(ALL_PERMISSIONS);
  });

  it('renders individual permission checkboxes', () => {
    render(
      <PermissionCheckboxGrid
        selectedPermissions={[]}
        onChange={jest.fn()}
      />
    );
    const checkboxes = screen.getAllByRole('checkbox');
    expect(checkboxes.length).toBe(ALL_PERMISSIONS.length);
  });

  it('checked state matches selectedPermissions prop', () => {
    const selected = ['experiment:read', 'feature_flag:read'];
    render(
      <PermissionCheckboxGrid
        selectedPermissions={selected}
        onChange={jest.fn()}
      />
    );
    const readExperimentsCheckbox = screen.getByTestId('permission-checkbox-experiment:read');
    const createExperimentsCheckbox = screen.getByTestId('permission-checkbox-experiment:create');
    const readUsersCheckbox = screen.getByTestId('permission-checkbox-feature_flag:read');

    expect(readExperimentsCheckbox).toBeChecked();
    expect(createExperimentsCheckbox).not.toBeChecked();
    expect(readUsersCheckbox).toBeChecked();
  });

  it('clicking a checkbox calls onChange with updated permissions array', () => {
    const onChange = jest.fn();
    render(
      <PermissionCheckboxGrid
        selectedPermissions={[]}
        onChange={onChange}
      />
    );
    const checkbox = screen.getByTestId('permission-checkbox-experiment:read');
    fireEvent.click(checkbox);
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it('checking a permission adds it to the array', () => {
    const onChange = jest.fn();
    render(
      <PermissionCheckboxGrid
        selectedPermissions={[]}
        onChange={onChange}
      />
    );
    const checkbox = screen.getByTestId('permission-checkbox-experiment:read');
    fireEvent.click(checkbox);
    expect(onChange).toHaveBeenCalledWith(
      expect.arrayContaining(['experiment:read'])
    );
  });

  it('unchecking a permission removes it from the array', () => {
    const onChange = jest.fn();
    render(
      <PermissionCheckboxGrid
        selectedPermissions={['experiment:read', 'feature_flag:read']}
        onChange={onChange}
      />
    );
    const checkbox = screen.getByTestId('permission-checkbox-experiment:read');
    fireEvent.click(checkbox);
    const result = onChange.mock.calls[0][0] as string[];
    expect(result).not.toContain('experiment:read');
    expect(result).toContain('feature_flag:read');
  });

  it('renders read-only when readOnly=true (checkboxes disabled)', () => {
    render(
      <PermissionCheckboxGrid
        selectedPermissions={['experiment:read']}
        onChange={jest.fn()}
        readOnly={true}
      />
    );
    const checkboxes = screen.getAllByRole('checkbox');
    checkboxes.forEach((checkbox) => {
      expect(checkbox).toBeDisabled();
    });
  });

  it('renders with data-testid="permission-checkbox-grid"', () => {
    render(
      <PermissionCheckboxGrid
        selectedPermissions={[]}
        onChange={jest.fn()}
      />
    );
    expect(screen.getByTestId('permission-checkbox-grid')).toBeInTheDocument();
  });
});
