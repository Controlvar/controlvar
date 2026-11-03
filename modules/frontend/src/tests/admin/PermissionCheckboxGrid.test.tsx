import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { PermissionCheckboxGrid } from '@modules/components/admin/roles/PermissionCheckboxGrid';

// Exactly the pairs the API accepts (GRANTABLE in backend/app/core/permissions.py).
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

  // W2 PR 2: what each pair lets its holder do, linked to its checkbox (#891).
  it.each([
    [
      'experiment:create',
      'Create and clone experiments. Also: read the global holdout, manage exclusion groups (not archive them), create segments.',
    ],
    ['experiment:update', 'Change, schedule, start, pause, complete and archive any experiment, and change segments.'],
    ['experiment:delete', 'Delete draft experiments, and archive segments.'],
    ['experiment:read', 'Every user already has this.'],
    ['experiment:list', 'Every user already has this.'],
    ['feature_flag:create', 'Create flags.'],
    ['feature_flag:update', 'Change any flag: on and off, targeting, values, rollout and rollout schedules.'],
    ['feature_flag:delete', 'Delete any flag.'],
    ['feature_flag:read', 'Every user already has this.'],
    ['feature_flag:list', 'Every user already has this.'],
    [
      'audit_log:read',
      "Read the audit log, including other users' entries, its statistics and each item's history. " +
        'Without it, Developer and Viewer users read only their own entries. ' +
        'It does not give the compliance audit events, reports or exports.',
    ],
  ])('describes %s, as its checkbox\'s description', (pair, text) => {
    render(<PermissionCheckboxGrid selectedPermissions={[]} onChange={jest.fn()} />);
    expect(screen.getByTestId(`permission-checkbox-${pair}`)).toHaveAccessibleDescription(text);
  });

  it('describes every pair, each with an id of its own', () => {
    render(<PermissionCheckboxGrid selectedPermissions={[]} onChange={jest.fn()} />);
    const ids = screen.getAllByRole('checkbox').map((c) => c.getAttribute('aria-describedby'));
    expect(ids.every((id) => id && document.getElementById(id)?.textContent)).toBe(true);
    expect(new Set(ids).size).toBe(ALL_PERMISSIONS.length);
  });

  it('says after the grid what Feature flags: Update also allows (D1.4)', () => {
    render(<PermissionCheckboxGrid selectedPermissions={[]} onChange={jest.fn()} />);
    const note = screen.getByTestId('permission-server-key-note');
    expect(note).toHaveTextContent(
      'With Feature flags: Update, a user can also create server-side SDK keys (API keys with the sdk:ruleset scope) ' +
        'through the API. Assigning users in a batch with such a key also needs Experiments: Update.',
    );
    // Plain text in reading order, after the last group; no live region.
    const groups = screen.getAllByRole('group');
    expect(groups[groups.length - 1].compareDocumentPosition(note) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(note).not.toHaveAttribute('aria-live');
  });

  it('never says "every entry", "every audit entry" or "both feature flags and experiments"', () => {
    const { container } = render(<PermissionCheckboxGrid selectedPermissions={[]} onChange={jest.fn()} />);
    expect(container.textContent).not.toMatch(/every (audit )?entr(y|ies)|both feature flags and experiments/i);
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
