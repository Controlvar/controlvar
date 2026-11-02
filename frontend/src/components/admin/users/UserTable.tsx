import React, { useCallback, useEffect, useRef, useState } from 'react';
import { AdminService } from '@/services/admin';
import { DEFAULT_REQUEST_TIMEOUT_MS, isApiError } from '@/services/api';
import { AdminUser, ROLE_COLORS, USER_ROLE_LABELS, UserListResponse } from '@/types/admin';

/**
 * A delete that timed out may still have been carried out, so it is not
 * reported as "Couldn't delete" (#1040).
 */
export function deleteTimedOutMessage(username: string): string {
  return (
    `The API did not answer within ${DEFAULT_REQUEST_TIMEOUT_MS / 1000} seconds, so "${username}" ` +
    'may or may not have been deleted. Reload the list and check before you try again.'
  );
}

/** Rows per page. The API's `limit` maximum is 100. */
export const USER_PAGE_SIZE = 50;
/** The API refuses a longer `search` with a 422. */
export const USER_SEARCH_MAX_LENGTH = 100;
const SEARCH_DEBOUNCE_MS = 300;

export interface UserTableProps {
  /** Opens the Edit dialog for a row. Without it the row has no Edit button. */
  onEdit?: (user: AdminUser) => void;
  /**
   * Bump to reload the list. The current page and search term are kept, so an
   * edit does not send the administrator back to the first page.
   */
  reloadToken?: number;
  /**
   * After the list has (re)loaded, move focus to this user's Edit button, or to
   * the search box when the row is no longer listed; then `onFocusHandled`.
   */
  focusUserId?: string | null;
  onFocusHandled?: () => void;
}

export function UserTable({
  onEdit,
  reloadToken: externalReloadToken = 0,
  focusUserId = null,
  onFocusHandled,
}: UserTableProps = {}) {
  const [data, setData] = useState<UserListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // `search` is what is in the box; `term` is what the list was asked for,
  // set once typing pauses.
  const [search, setSearch] = useState('');
  const [term, setTerm] = useState('');
  const [page, setPage] = useState(0);
  const [reloadToken, setReloadToken] = useState(0);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const editButtons = useRef(new Map<string, HTMLButtonElement>());
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Each request takes a number; only the latest one may write the table, so
  // a slow answer to an older search can never replace a newer one.
  const latestRequest = useRef(0);

  // The parent's reload token the list on screen was loaded for. Focus is moved
  // only once it matches, so it lands on the reloaded rows, not on ones about to
  // be replaced by the loading state.
  const [loadedFor, setLoadedFor] = useState(externalReloadToken);

  const fetchUsers = useCallback(async (pageIndex: number, searchTerm: string, token: number) => {
    const request = ++latestRequest.current;
    setLoading(true);
    setError(null);
    try {
      const result = await AdminService.listUsers({
        skip: pageIndex * USER_PAGE_SIZE,
        limit: USER_PAGE_SIZE,
        search: searchTerm || undefined,
      });
      if (request !== latestRequest.current) return;
      setData(result);
    } catch (err) {
      if (request !== latestRequest.current) return;
      setError((err as Error).message || 'Failed to load users');
    } finally {
      if (request === latestRequest.current) {
        setLoading(false);
        setLoadedFor(token);
      }
    }
  }, []);

  useEffect(() => {
    fetchUsers(page, term, externalReloadToken);
  }, [fetchUsers, page, term, reloadToken, externalReloadToken]);

  useEffect(() => {
    if (!focusUserId || loading || loadedFor !== externalReloadToken) return;
    const button = editButtons.current.get(focusUserId);
    if (button) {
      button.focus();
    } else {
      searchRef.current?.focus();
    }
    onFocusHandled?.();
  }, [focusUserId, loading, loadedFor, externalReloadToken, data, onFocusHandled]);

  useEffect(
    () => () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    },
    [],
  );

  const handleSearchChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const value = e.target.value;
    setSearch(value);
    if (debounceRef.current) {
      clearTimeout(debounceRef.current);
    }
    debounceRef.current = setTimeout(() => {
      // A new search starts again from the first page.
      setPage(0);
      setTerm(value.trim());
    }, SEARCH_DEBOUNCE_MS);
  };

  const handleDelete = async (user: AdminUser) => {
    const confirmed = window.confirm(
      `Are you sure you want to delete user "${user.username}"? This action cannot be undone.`
    );
    if (!confirmed) return;
    setDeleteError(null);
    try {
      await AdminService.deleteUser(user.id);
    } catch (err) {
      setDeleteError(
        isApiError(err) && err.timedOut
          ? deleteTimedOutMessage(user.username)
          : `Couldn't delete ${user.username}: ${(err as Error).message}`,
      );
      return;
    }
    // Deleting the only row on a later page would leave that page empty;
    // step back to the one before it instead.
    if (page > 0 && data && data.items.length === 1) {
      setPage(page - 1);
    } else {
      setReloadToken((n) => n + 1);
    }
  };

  const shownFrom = data ? data.skip + 1 : 0;
  const shownTo = data ? data.skip + data.items.length : 0;
  const hasPrevious = page > 0;
  const hasNext = data ? shownTo < data.total : false;

  return (
    <div data-testid="user-table" className="bg-white rounded-lg border border-slate-200 overflow-hidden">
      {/* Search */}
      <div className="p-4 border-b border-slate-200">
        <input
          data-testid="user-search"
          ref={searchRef}
          type="text"
          placeholder="Search by username, email, first or last name"
          aria-label="Search users"
          maxLength={USER_SEARCH_MAX_LENGTH}
          value={search}
          onChange={handleSearchChange}
          className="w-full px-3 py-2 border border-slate-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
      </div>

      {deleteError && (
        <div
          role="alert"
          data-testid="delete-error-message"
          className="mx-4 mt-4 p-3 bg-red-50 border border-red-200 rounded-md text-sm text-red-700"
        >
          {deleteError}
        </div>
      )}

      {/* Loading Skeleton */}
      {loading && (
        <div data-testid="loading-skeleton" className="divide-y divide-slate-100">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="flex items-center gap-4 px-6 py-4 animate-pulse">
              <div className="h-4 bg-slate-200 rounded w-1/4" />
              <div className="h-4 bg-slate-200 rounded w-1/3" />
              <div className="h-5 bg-slate-200 rounded w-16" />
              <div className="h-5 bg-slate-200 rounded w-12" />
            </div>
          ))}
        </div>
      )}

      {/* Error State */}
      {!loading && error && (
        <div
          data-testid="error-state"
          className="p-8 text-center text-red-600"
        >
          <p className="font-medium">Failed to load users</p>
          <p className="text-sm mt-1 text-red-500">{error}</p>
        </div>
      )}

      {/* Empty State */}
      {!loading && !error && data && data.items.length === 0 && (
        <div data-testid="empty-state" className="p-8 text-center text-slate-500">
          <p>{term ? `No users match "${term}".` : 'No users found'}</p>
        </div>
      )}

      {/* Table */}
      {!loading && !error && data && data.items.length > 0 && (
        <>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-slate-50 text-slate-600 uppercase text-xs tracking-wide">
                <tr>
                  <th className="px-6 py-3 text-left">Username</th>
                  <th className="px-6 py-3 text-left">Email</th>
                  <th className="px-6 py-3 text-left">Role</th>
                  <th className="px-6 py-3 text-left">Superuser</th>
                  <th className="px-6 py-3 text-left">Status</th>
                  <th className="px-6 py-3 text-left">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {data.items.map((user) => (
                  <tr key={user.id} className="hover:bg-slate-50">
                    <td className="px-6 py-4 font-medium text-slate-900">{user.username}</td>
                    <td className="px-6 py-4 text-slate-600">
                      {user.email ?? <span className="text-slate-400 italic">No email</span>}
                    </td>
                    <td className="px-6 py-4">
                      {user.role ? (
                        <span
                          className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${ROLE_COLORS[user.role]}`}
                        >
                          {USER_ROLE_LABELS[user.role]}
                        </span>
                      ) : (
                        <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium border border-dashed border-slate-300 text-slate-600">
                          No role
                        </span>
                      )}
                    </td>
                    <td className="px-6 py-4" data-testid={`superuser-${user.id}`}>
                      {user.is_superuser ? (
                        <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-amber-100 text-amber-900">
                          Superuser
                        </span>
                      ) : (
                        <span className="text-slate-600">No</span>
                      )}
                    </td>
                    <td className="px-6 py-4">
                      {user.is_active ? (
                        <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-green-100 text-green-800">
                          Active
                        </span>
                      ) : (
                        <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-slate-100 text-slate-600">
                          Inactive
                        </span>
                      )}
                    </td>
                    <td className="px-6 py-4 space-x-4 whitespace-nowrap">
                      {onEdit && (
                        <button
                          type="button"
                          data-testid={`edit-user-${user.id}`}
                          aria-label={`Edit ${user.username}`}
                          ref={(el) => {
                            if (el) editButtons.current.set(user.id, el);
                            else editButtons.current.delete(user.id);
                          }}
                          onClick={() => onEdit(user)}
                          className="text-blue-700 hover:text-blue-900 text-sm font-medium"
                        >
                          Edit
                        </button>
                      )}
                      <button
                        type="button"
                        data-testid={`delete-user-${user.id}`}
                        aria-label={`Delete ${user.username}`}
                        onClick={() => handleDelete(user)}
                        className="text-red-600 hover:text-red-800 text-sm font-medium"
                      >
                        Delete
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Pagination */}
          <div className="px-6 py-3 border-t border-slate-200 flex items-center justify-between gap-4 text-sm text-slate-500">
            <span data-testid="user-page-info">
              Showing {shownFrom}–{shownTo} of {data.total}
            </span>
            <div className="flex gap-2">
              <button
                type="button"
                data-testid="user-page-prev"
                onClick={() => setPage((p) => Math.max(0, p - 1))}
                disabled={!hasPrevious}
                className="px-3 py-1.5 border border-slate-300 rounded text-slate-700 hover:bg-slate-50 disabled:opacity-40 disabled:cursor-not-allowed"
              >
                Prev
              </button>
              <button
                type="button"
                data-testid="user-page-next"
                onClick={() => setPage((p) => p + 1)}
                disabled={!hasNext}
                className="px-3 py-1.5 border border-slate-300 rounded text-slate-700 hover:bg-slate-50 disabled:opacity-40 disabled:cursor-not-allowed"
              >
                Next
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
