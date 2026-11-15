import path from 'path';
import React from 'react';
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react';
import { UserTable } from '@/components/admin/users/UserTable';
import { AdminService } from '@/services/admin';
import { AdminUser, ROLE_COLORS, USER_ROLE_LABELS, UserListResponse, UserRole } from '@/types/admin';

jest.mock('@/services/admin');

const mockListUsers = AdminService.listUsers as jest.Mock;
const mockDeleteUser = AdminService.deleteUser as jest.Mock;

const makeUser = (overrides: Partial<AdminUser> = {}): AdminUser => ({
  id: '1',
  username: 'testuser',
  email: 'test@example.com',
  role: 'DEVELOPER',
  is_active: true,
  created_at: '2024-01-01T00:00:00Z',
  ...overrides,
});

const makeResponse = (users: AdminUser[], total?: number, skip = 0): UserListResponse => ({
  items: users,
  total: total ?? users.length,
  skip,
  limit: 50,
});

/** `count` users numbered from `start`, so each page's rows are distinct. */
const makeUsers = (start: number, count: number): AdminUser[] =>
  Array.from({ length: count }, (_, i) =>
    makeUser({ id: `u${start + i}`, username: `user${start + i}`, email: `user${start + i}@example.com` }),
  );

/** A listUsers that serves `total` users a page at a time, honouring skip/limit. */
const pagedList = (total: number) => (params?: { skip?: number; limit?: number }) => {
  const skip = params?.skip ?? 0;
  const limit = params?.limit ?? 50;
  return Promise.resolve(makeResponse(makeUsers(skip, Math.max(0, Math.min(limit, total - skip))), total, skip));
};

/** A promise and the functions that settle it, so a test decides the order answers arrive in. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

const lastListCall = () => mockListUsers.mock.calls[mockListUsers.mock.calls.length - 1][0];

beforeEach(() => {
  jest.clearAllMocks();
  jest.useFakeTimers();
  // Default: suppress window.confirm
  window.confirm = jest.fn(() => true);
});

afterEach(() => {
  jest.useRealTimers();
});

describe('UserTable', () => {
  it('renders loading state initially', () => {
    mockListUsers.mockImplementation(() => new Promise(() => {}));
    render(<UserTable />);
    // Loading skeleton should appear (animate-pulse rows)
    expect(screen.getByTestId('user-table')).toBeInTheDocument();
    expect(screen.getByTestId('loading-skeleton')).toBeInTheDocument();
  });

  it('renders user rows after data loads', async () => {
    const users = [
      makeUser({ id: '1', username: 'alice', email: 'alice@example.com' }),
      makeUser({ id: '2', username: 'bob', email: 'bob@example.com' }),
    ];
    mockListUsers.mockResolvedValue(makeResponse(users));
    render(<UserTable />);
    await waitFor(() => {
      expect(screen.getByText('alice')).toBeInTheDocument();
      expect(screen.getByText('bob')).toBeInTheDocument();
    });
  });

  it('shows username and email in each row', async () => {
    const users = [makeUser({ username: 'charlie', email: 'charlie@corp.com' })];
    mockListUsers.mockResolvedValue(makeResponse(users));
    render(<UserTable />);
    await waitFor(() => {
      expect(screen.getByText('charlie')).toBeInTheDocument();
      expect(screen.getByText('charlie@corp.com')).toBeInTheDocument();
    });
  });

  it('renders an account with no email address, marked "No email" (#342)', async () => {
    const users = [
      makeUser({ id: '1', username: 'noemail', email: null }),
      makeUser({ id: '2', username: 'dana', email: 'dana@example.com' }),
    ];
    mockListUsers.mockResolvedValue(makeResponse(users));
    render(<UserTable />);
    await waitFor(() => {
      expect(screen.getByText('noemail')).toBeInTheDocument();
    });
    const row = screen.getByText('noemail').closest('tr') as HTMLElement;
    expect(row).toHaveTextContent('No email');
    expect(screen.getByText('dana@example.com')).toBeInTheDocument();
    expect(screen.getAllByText('No email')).toHaveLength(1);
  });

  it('shows role badge with correct text', async () => {
    const users = [makeUser({ role: 'ADMIN' })];
    mockListUsers.mockResolvedValue(makeResponse(users));
    render(<UserTable />);
    await waitFor(() => {
      expect(screen.getByText('Admin')).toBeInTheDocument();
    });
  });

  it('applies ROLE_COLORS classes for every role and scans src/types in tailwind.config.js (#1297)', async () => {
    const roles: UserRole[] = ['ADMIN', 'DEVELOPER', 'ANALYST', 'VIEWER'];
    const users = roles.map((role, idx) =>
      makeUser({ id: `r-${idx}`, username: `u-${role.toLowerCase()}`, role }),
    );
    mockListUsers.mockResolvedValue(makeResponse(users));
    render(<UserTable />);
    for (const role of roles) {
      const badge = await screen.findByText(USER_ROLE_LABELS[role]);
      for (const cls of ROLE_COLORS[role].split(/\s+/)) {
        expect(badge).toHaveClass(cls);
      }
    }
    const tailwindConfig = require(path.resolve(__dirname, '..', '..', '..', 'tailwind.config.js'));
    expect(tailwindConfig.content).toEqual(
      expect.arrayContaining([expect.stringMatching(/^\.\/src\/types\/\*\*\/\*\.\{.*ts.*\}$/)]),
    );
  });

  it('shows active/inactive status badge', async () => {
    const users = [
      makeUser({ id: '1', username: 'active-user', is_active: true }),
      makeUser({ id: '2', username: 'inactive-user', is_active: false }),
    ];
    mockListUsers.mockResolvedValue(makeResponse(users));
    render(<UserTable />);
    await waitFor(() => {
      expect(screen.getByText('Active')).toBeInTheDocument();
      expect(screen.getByText('Inactive')).toBeInTheDocument();
    });
  });

  it('search input filters by name/email (calls listUsers with search param after debounce)', async () => {
    mockListUsers.mockResolvedValue(makeResponse([]));
    render(<UserTable />);
    // Wait for initial load
    await waitFor(() => expect(mockListUsers).toHaveBeenCalledTimes(1));

    const searchInput = screen.getByTestId('user-search');
    fireEvent.change(searchInput, { target: { value: 'alice' } });

    // Debounce: should not call immediately
    expect(mockListUsers).toHaveBeenCalledTimes(1);

    // Advance timers past the 300ms debounce
    act(() => {
      jest.advanceTimersByTime(300);
    });

    await waitFor(() => {
      expect(mockListUsers).toHaveBeenCalledWith(
        expect.objectContaining({ search: 'alice' })
      );
    });
  });

  it('clicking delete button calls AdminService.deleteUser with user id', async () => {
    const user = makeUser({ id: 'user-42' });
    mockListUsers.mockResolvedValue(makeResponse([user]));
    mockDeleteUser.mockResolvedValue(undefined);

    render(<UserTable />);
    await waitFor(() => screen.getByText('testuser'));

    const deleteBtn = screen.getByTestId('delete-user-user-42');
    fireEvent.click(deleteBtn);

    await waitFor(() => {
      expect(mockDeleteUser).toHaveBeenCalledWith('user-42');
    });
  });

  it('shows confirmation before delete', async () => {
    const user = makeUser({ id: 'user-99' });
    mockListUsers.mockResolvedValue(makeResponse([user]));
    mockDeleteUser.mockResolvedValue(undefined);
    const confirmMock = jest.fn(() => false);
    window.confirm = confirmMock;

    render(<UserTable />);
    await waitFor(() => screen.getByText('testuser'));

    const deleteBtn = screen.getByTestId('delete-user-user-99');
    fireEvent.click(deleteBtn);

    expect(confirmMock).toHaveBeenCalled();
    // Since confirm returns false, deleteUser should NOT be called
    expect(mockDeleteUser).not.toHaveBeenCalled();
  });

  it('shows error state if listUsers fails', async () => {
    mockListUsers.mockRejectedValue(new Error('Network error'));
    render(<UserTable />);
    await waitFor(() => {
      expect(screen.getByTestId('error-state')).toBeInTheDocument();
    });
  });

  it('renders empty state when no users', async () => {
    mockListUsers.mockResolvedValue(makeResponse([]));
    render(<UserTable />);
    await waitFor(() => {
      expect(screen.getByText(/no users found/i)).toBeInTheDocument();
    });
  });

  it('pagination shows the range on screen and the total', async () => {
    mockListUsers.mockImplementation(pagedList(42));
    render(<UserTable />);
    await waitFor(() => {
      expect(screen.getByTestId('user-page-info')).toHaveTextContent('Showing 1–42 of 42');
    });
  });

  // #651: the list pages by skip/limit; it used to send `page`, which the API
  // does not read, so every administrator saw only the first 50 users.
  it('first request asks for skip 0, limit 50', async () => {
    mockListUsers.mockImplementation(pagedList(3));
    render(<UserTable />);
    await waitFor(() => expect(mockListUsers).toHaveBeenCalledTimes(1));
    expect(mockListUsers).toHaveBeenCalledWith({ skip: 0, limit: 50, search: undefined });
  });

  it('Next and Prev move by a page, and are disabled at the ends', async () => {
    mockListUsers.mockImplementation(pagedList(120));
    render(<UserTable />);
    await waitFor(() => expect(screen.getByTestId('user-page-info')).toHaveTextContent('Showing 1–50 of 120'));
    expect(screen.getByTestId('user-page-prev')).toBeDisabled();
    expect(screen.getByTestId('user-page-next')).toBeEnabled();

    fireEvent.click(screen.getByTestId('user-page-next'));
    await waitFor(() => expect(screen.getByTestId('user-page-info')).toHaveTextContent('Showing 51–100 of 120'));
    expect(lastListCall()).toEqual(expect.objectContaining({ skip: 50, limit: 50 }));
    expect(screen.getByText('user50')).toBeInTheDocument();
    expect(screen.getByTestId('user-page-prev')).toBeEnabled();

    fireEvent.click(screen.getByTestId('user-page-next'));
    await waitFor(() => expect(screen.getByTestId('user-page-info')).toHaveTextContent('Showing 101–120 of 120'));
    expect(lastListCall()).toEqual(expect.objectContaining({ skip: 100 }));
    expect(screen.getByTestId('user-page-next')).toBeDisabled();

    fireEvent.click(screen.getByTestId('user-page-prev'));
    await waitFor(() => expect(screen.getByTestId('user-page-info')).toHaveTextContent('Showing 51–100 of 120'));
    expect(lastListCall()).toEqual(expect.objectContaining({ skip: 50 }));
  });

  it('a single page has both buttons disabled', async () => {
    mockListUsers.mockImplementation(pagedList(50));
    render(<UserTable />);
    await waitFor(() => expect(screen.getByTestId('user-page-info')).toHaveTextContent('Showing 1–50 of 50'));
    expect(screen.getByTestId('user-page-prev')).toBeDisabled();
    expect(screen.getByTestId('user-page-next')).toBeDisabled();
  });

  it('a search change goes back to the first page', async () => {
    mockListUsers.mockImplementation(pagedList(120));
    render(<UserTable />);
    await waitFor(() => expect(screen.getByTestId('user-page-next')).toBeEnabled());
    fireEvent.click(screen.getByTestId('user-page-next'));
    await waitFor(() => expect(screen.getByTestId('user-page-info')).toHaveTextContent('Showing 51–100 of 120'));

    fireEvent.change(screen.getByTestId('user-search'), { target: { value: 'user1' } });
    act(() => {
      jest.advanceTimersByTime(300);
    });
    await waitFor(() => expect(lastListCall()).toEqual({ skip: 0, limit: 50, search: 'user1' }));
    await waitFor(() => expect(screen.getByTestId('user-page-info')).toHaveTextContent('Showing 1–50 of 120'));
  });

  it('typing within the debounce sends one request, for the last value', async () => {
    mockListUsers.mockImplementation(pagedList(3));
    render(<UserTable />);
    await waitFor(() => expect(mockListUsers).toHaveBeenCalledTimes(1));
    const input = screen.getByTestId('user-search');
    fireEvent.change(input, { target: { value: 'a' } });
    act(() => {
      jest.advanceTimersByTime(100);
    });
    fireEvent.change(input, { target: { value: 'al' } });
    act(() => {
      jest.advanceTimersByTime(300);
    });
    await waitFor(() => expect(mockListUsers).toHaveBeenCalledTimes(2));
    expect(lastListCall()).toEqual(expect.objectContaining({ search: 'al' }));
  });

  // An older request answering after a newer one must not replace its rows:
  // without the sequence check, the table would show "a" results under "ab".
  it('ignores a response that is not for the latest request', async () => {
    const first = deferred<UserListResponse>();
    const second = deferred<UserListResponse>();
    mockListUsers
      .mockResolvedValueOnce(makeResponse([makeUser({ id: 'x', username: 'initial' })]))
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    render(<UserTable />);
    await waitFor(() => screen.getByText('initial'));

    const input = screen.getByTestId('user-search');
    fireEvent.change(input, { target: { value: 'a' } });
    act(() => {
      jest.advanceTimersByTime(300);
    });
    await waitFor(() => expect(mockListUsers).toHaveBeenCalledTimes(2));
    fireEvent.change(input, { target: { value: 'ab' } });
    act(() => {
      jest.advanceTimersByTime(300);
    });
    await waitFor(() => expect(mockListUsers).toHaveBeenCalledTimes(3));

    // The newer request answers first, the older one last.
    await act(async () => {
      second.resolve(makeResponse([makeUser({ id: 'ab', username: 'abigail' })]));
    });
    await act(async () => {
      first.resolve(makeResponse([makeUser({ id: 'a', username: 'aaron' })]));
    });

    expect(screen.getByText('abigail')).toBeInTheDocument();
    expect(screen.queryByText('aaron')).not.toBeInTheDocument();
  });

  it('deleting the only row on the last page steps back a page', async () => {
    let total = 101;
    mockListUsers.mockImplementation((params) => pagedList(total)(params));
    mockDeleteUser.mockImplementation(() => {
      total = 100;
      return Promise.resolve(undefined);
    });
    render(<UserTable />);
    await waitFor(() => expect(screen.getByTestId('user-page-next')).toBeEnabled());
    fireEvent.click(screen.getByTestId('user-page-next'));
    await waitFor(() => expect(screen.getByTestId('user-page-info')).toHaveTextContent('Showing 51–100 of 101'));
    fireEvent.click(screen.getByTestId('user-page-next'));
    await waitFor(() => expect(screen.getByTestId('user-page-info')).toHaveTextContent('Showing 101–101 of 101'));

    fireEvent.click(screen.getByTestId('delete-user-u100'));
    await waitFor(() => expect(mockDeleteUser).toHaveBeenCalledWith('u100'));
    await waitFor(() => expect(screen.getByTestId('user-page-info')).toHaveTextContent('Showing 51–100 of 100'));
    expect(lastListCall()).toEqual(expect.objectContaining({ skip: 50 }));
  });

  it('deleting a row on a page with others reloads the same page', async () => {
    mockListUsers.mockImplementation(pagedList(120));
    mockDeleteUser.mockResolvedValue(undefined);
    render(<UserTable />);
    await waitFor(() => expect(screen.getByTestId('user-page-next')).toBeEnabled());
    fireEvent.click(screen.getByTestId('user-page-next'));
    await waitFor(() => expect(screen.getByTestId('user-page-info')).toHaveTextContent('Showing 51–100 of 120'));
    const callsBefore = mockListUsers.mock.calls.length;

    fireEvent.click(screen.getByTestId('delete-user-u60'));
    await waitFor(() => expect(mockListUsers.mock.calls.length).toBe(callsBefore + 1));
    expect(lastListCall()).toEqual(expect.objectContaining({ skip: 50 }));
  });

  it('a search with no results names the term', async () => {
    mockListUsers.mockResolvedValue(makeResponse([]));
    render(<UserTable />);
    await waitFor(() => expect(screen.getByText(/no users found/i)).toBeInTheDocument());
    fireEvent.change(screen.getByTestId('user-search'), { target: { value: 'zed' } });
    act(() => {
      jest.advanceTimersByTime(300);
    });
    await waitFor(() => expect(screen.getByTestId('empty-state')).toHaveTextContent('No users match "zed".'));
  });

  it('the search box takes at most 100 characters and says what it searches', () => {
    mockListUsers.mockImplementation(() => new Promise(() => {}));
    render(<UserTable />);
    const input = screen.getByTestId('user-search');
    expect(input).toHaveAttribute('maxLength', '100');
    expect(input).toHaveAttribute('placeholder', 'Search by username, email, first or last name');
  });

  it('a failed delete shows an inline message on the table, never alert()', async () => {
    const alertSpy = jest.spyOn(window, 'alert').mockImplementation(() => {});
    mockListUsers.mockResolvedValue(makeResponse([makeUser({ id: 'user-7', username: 'gina' })]));
    mockDeleteUser.mockRejectedValue(new Error('Cannot delete your own user account'));
    render(<UserTable />);
    await waitFor(() => screen.getByText('gina'));
    fireEvent.click(screen.getByTestId('delete-user-user-7'));
    const message = await screen.findByTestId('delete-error-message');
    expect(message).toHaveAttribute('role', 'alert');
    expect(message).toHaveTextContent("Couldn't delete gina: Cannot delete your own user account");
    expect(alertSpy).not.toHaveBeenCalled();
    alertSpy.mockRestore();
  });

  it('each row has an Edit button, named for the user, before Delete', async () => {
    const onEdit = jest.fn();
    const alice = makeUser({ id: 'a1', username: 'alice' });
    mockListUsers.mockResolvedValue(makeResponse([alice, makeUser({ id: 'b2', username: 'bob' })]));
    render(<UserTable onEdit={onEdit} />);
    await waitFor(() => screen.getByText('alice'));
    const edit = screen.getByTestId('edit-user-a1');
    expect(edit).toHaveAccessibleName('Edit alice');
    expect(screen.getByTestId('edit-user-b2')).toHaveAccessibleName('Edit bob');
    const cell = edit.closest('td') as HTMLElement;
    const buttons = Array.from(cell.querySelectorAll('button')).map((b) => b.dataset.testid);
    expect(buttons).toStrictEqual(['edit-user-a1', 'delete-user-a1']);
    fireEvent.click(edit);
    expect(onEdit).toHaveBeenCalledWith(alice);
  });

  it('without onEdit there is no Edit button', async () => {
    mockListUsers.mockResolvedValue(makeResponse([makeUser({ id: 'a1', username: 'alice' })]));
    render(<UserTable />);
    await waitFor(() => screen.getByText('alice'));
    expect(screen.queryByTestId('edit-user-a1')).not.toBeInTheDocument();
  });

  it('shows a Superuser column, and "No role" for an account with no role', async () => {
    mockListUsers.mockResolvedValue(
      makeResponse([
        makeUser({ id: 's1', username: 'root', role: 'DEVELOPER', is_superuser: true }),
        makeUser({ id: 'n1', username: 'legacy', role: null, is_superuser: false }),
      ]),
    );
    render(<UserTable />);
    await waitFor(() => screen.getByText('root'));
    expect(screen.getByRole('columnheader', { name: 'Superuser' })).toBeInTheDocument();
    expect(screen.getByTestId('superuser-s1')).toHaveTextContent('Superuser');
    expect(screen.getByTestId('superuser-n1')).toHaveTextContent('No');
    const legacyRow = screen.getByText('legacy').closest('tr') as HTMLElement;
    expect(legacyRow).toHaveTextContent('No role');
  });

  it('a reload from the page keeps the current page and search term', async () => {
    mockListUsers.mockImplementation(pagedList(120));
    const { rerender } = render(<UserTable reloadToken={0} />);
    await waitFor(() => expect(screen.getByTestId('user-page-next')).toBeEnabled());
    fireEvent.change(screen.getByTestId('user-search'), { target: { value: 'user' } });
    act(() => {
      jest.advanceTimersByTime(300);
    });
    await waitFor(() => expect(lastListCall()).toEqual(expect.objectContaining({ search: 'user' })));
    await waitFor(() => expect(screen.getByTestId('user-page-next')).toBeEnabled());
    fireEvent.click(screen.getByTestId('user-page-next'));
    await waitFor(() => expect(screen.getByTestId('user-page-info')).toHaveTextContent('Showing 51–100 of 120'));
    const callsBefore = mockListUsers.mock.calls.length;

    rerender(<UserTable reloadToken={1} />);
    await waitFor(() => expect(mockListUsers.mock.calls.length).toBe(callsBefore + 1));
    expect(lastListCall()).toStrictEqual({ skip: 50, limit: 50, search: 'user' });
    expect(screen.getByTestId('user-search')).toHaveValue('user');
  });

  it("after a reload, focus goes to the edited row's Edit button", async () => {
    mockListUsers.mockResolvedValue(makeResponse([makeUser({ id: 'a1', username: 'alice' })]));
    const onFocusHandled = jest.fn();
    const props = { onEdit: jest.fn(), onFocusHandled };
    const { rerender } = render(<UserTable {...props} reloadToken={0} />);
    await waitFor(() => screen.getByText('alice'));
    let finish!: (v: UserListResponse) => void;
    mockListUsers.mockImplementationOnce(() => new Promise((r) => (finish = r)));

    rerender(<UserTable {...props} reloadToken={1} focusUserId="a1" />);
    // Still loading: nothing is focused yet, and the request is not reported handled.
    expect(onFocusHandled).not.toHaveBeenCalled();
    await act(async () => {
      finish(makeResponse([makeUser({ id: 'a1', username: 'alice' })]));
    });
    await waitFor(() => expect(document.activeElement).toBe(screen.getByTestId('edit-user-a1')));
    expect(onFocusHandled).toHaveBeenCalledTimes(1);
  });

  it('when the row is gone after a reload, focus goes to the search box', async () => {
    mockListUsers.mockResolvedValue(makeResponse([makeUser({ id: 'a1', username: 'alice' })]));
    const props = { onEdit: jest.fn(), onFocusHandled: jest.fn() };
    const { rerender } = render(<UserTable {...props} reloadToken={0} />);
    await waitFor(() => screen.getByText('alice'));
    mockListUsers.mockResolvedValue(makeResponse([makeUser({ id: 'b2', username: 'bob' })]));
    rerender(<UserTable {...props} reloadToken={1} focusUserId="a1" />);
    await waitFor(() => expect(document.activeElement).toBe(screen.getByTestId('user-search')));
  });

  it('renders with data-testid="user-table"', () => {
    mockListUsers.mockImplementation(() => new Promise(() => {}));
    render(<UserTable />);
    expect(screen.getByTestId('user-table')).toBeInTheDocument();
  });
});
