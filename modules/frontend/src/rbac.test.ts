/**
 * The rbac module's `@modules/rbac`. Colocated with the module it covers so it
 * leaves the tree with it — a core run never collects this file.
 */
import { RbacService } from '@modules/rbac';
import { CustomRole } from '@/types/admin';

const mockFetch = jest.fn();
global.fetch = mockFetch;

const BASE = 'http://localhost:8000';

beforeEach(() => {
  mockFetch.mockReset();
  localStorage.clear();
  process.env.NEXT_PUBLIC_API_URL = BASE;
});

afterAll(() => {
  delete process.env.NEXT_PUBLIC_API_URL;
});

function mockOk(data: unknown) {
  mockFetch.mockResolvedValueOnce({
    ok: true,
    status: 200,
    json: () => Promise.resolve(data),
  } as Response);
}

const role: CustomRole = {
  id: 'role-1',
  name: 'release-manager',
  description: 'Ships flags',
  is_system_role: false,
  permissions: [{ resource: 'feature_flag', actions: ['update'] }],
  created_at: '2026-09-01T00:00:00Z',
  user_count: 0,
};

describe('RbacService', () => {
  it('lists roles', async () => {
    mockOk([role]);
    await expect(RbacService.listRoles()).resolves.toEqual([role]);
    expect(mockFetch).toHaveBeenCalledWith(`${BASE}/api/v1/rbac/roles`, expect.any(Object));
  });

  it('creates a role with POST', async () => {
    mockOk(role);
    await RbacService.createRole({
      name: role.name,
      description: role.description,
      permissions: role.permissions,
    });
    const [url, init] = mockFetch.mock.calls[0];
    expect(url).toBe(`${BASE}/api/v1/rbac/roles`);
    expect((init as RequestInit).method).toBe('POST');
  });

  it('updates a role by name with PUT', async () => {
    mockOk(role);
    await RbacService.updateRole('release-manager', { description: 'new' });
    const [url, init] = mockFetch.mock.calls[0];
    expect(url).toBe(`${BASE}/api/v1/rbac/roles/release-manager`);
    expect((init as RequestInit).method).toBe('PUT');
  });

  it('deletes a role by name', async () => {
    mockOk(undefined);
    await RbacService.deleteRole('release-manager');
    const [url, init] = mockFetch.mock.calls[0];
    expect(url).toBe(`${BASE}/api/v1/rbac/roles/release-manager`);
    expect((init as RequestInit).method).toBe('DELETE');
  });

  it('reads effective permissions for a user', async () => {
    const answer = {
      user_id: 'u-1',
      username: 'ada',
      base_role: 'developer',
      custom_roles: [],
      permissions: { experiment: ['read'] },
      is_superuser: false,
    };
    mockOk(answer);
    await expect(RbacService.getUserPermissions('u-1')).resolves.toEqual(answer);
    expect(mockFetch).toHaveBeenCalledWith(
      `${BASE}/api/v1/rbac/users/u-1/permissions`,
      expect.any(Object),
    );
  });
});
