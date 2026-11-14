/**
 * Every dashboard permission helper against the server's own table (#891,
 * check #41).
 *
 * `fixtures/permission-table.json` is generated from the backend
 * (`ROLE_PERMISSIONS`, `AUDIT_LOG_READ_ALL_ROLES`, `_require_admin`,
 * `GRANTABLE`) and pinned to it by
 * `backend/tests/unit/core/test_dashboard_permission_table.py`, so this file
 * holds the helpers to what the API decides, not to a second hand-typed copy.
 *
 * For each helper, the pair its route checks, and for every user in
 * {ADMIN, DEVELOPER, ANALYST, VIEWER, a superuser, no role} and every
 * effective map in {none, empty, each of the eleven grantable pairs alone}:
 *
 *     expected = is_superuser || role_permissions[resource][action] has role || map holds the pair
 *
 * `canAdministerEligibility` answers a question no grant can change: its
 * answer is the role's alone, whatever the map holds. Runs in both profiles'
 * Jest.
 */
import fs from 'fs';
import path from 'path';
import { canCreateRulesetKey } from '@/components/admin/api-keys/CreateApiKeyModal';
import { readsAllAuditLogs } from '@/components/admin/audit/access';
import type { Role, UserMe } from '@/services/api';
import type { EffectivePermissions } from '@/utils/effectivePermissions';
import * as experimentPermissions from '@/utils/experimentPermissions';
import * as segmentPermissions from '@/utils/segmentPermissions';

interface PermissionTable {
  roles: Role[];
  role_permissions: Record<string, Record<string, Role[]>>;
  audit_log_read_roles: Role[];
  administer_eligibility_roles: Role[];
  grantable: Array<[string, string]>;
}

const TABLE: PermissionTable = JSON.parse(
  fs.readFileSync(path.join(__dirname, '..', 'fixtures', 'permission-table.json'), 'utf8'),
);

interface MatrixUser {
  label: string;
  role: Role | null;
  is_superuser: boolean;
}

const USERS: MatrixUser[] = [
  ...TABLE.roles.map((role) => ({ label: role, role, is_superuser: false })),
  { label: 'superuser', role: 'VIEWER', is_superuser: true },
  { label: 'no role', role: null, is_superuser: false },
];

/** A whole `UserMe`, for the helpers that take one. */
function me(user: MatrixUser): UserMe {
  return {
    id: 'u1',
    email: 'u@example.com',
    username: 'u',
    full_name: null,
    role: user.role as Role,
    is_superuser: user.is_superuser,
    is_active: true,
    auth_provider: 'local',
  };
}

type Answer = (user: MatrixUser, effective: EffectivePermissions | undefined) => boolean;

interface HelperRow {
  name: string;
  /** The pair the helper's route checks; `null` for one no grant can give. */
  pair: [string, string] | null;
  /** The built-in roles that pass without a grant. */
  roles: Role[];
  answer: Answer;
}

const rolesFor = (resource: string, action: string): Role[] => TABLE.role_permissions[resource][action];

const HELPERS: HelperRow[] = [
  {
    name: 'canCreateExperiment',
    pair: ['experiment', 'create'],
    roles: rolesFor('experiment', 'create'),
    answer: (u, e) => experimentPermissions.canCreateExperiment(u, e),
  },
  {
    name: 'canReadEligibility',
    pair: ['experiment', 'create'],
    roles: rolesFor('experiment', 'create'),
    answer: (u, e) => experimentPermissions.canReadEligibility(u, e),
  },
  {
    name: 'canChangeExperiment',
    pair: ['experiment', 'update'],
    roles: rolesFor('experiment', 'update'),
    answer: (u, e) => experimentPermissions.canChangeExperiment(u, e),
  },
  {
    name: 'canDeleteExperiment',
    pair: ['experiment', 'delete'],
    roles: rolesFor('experiment', 'delete'),
    answer: (u, e) => experimentPermissions.canDeleteExperiment(u, e),
  },
  {
    name: 'canCreateFeatureFlags',
    pair: ['feature_flag', 'create'],
    roles: rolesFor('feature_flag', 'create'),
    answer: (u, e) => experimentPermissions.canCreateFeatureFlags(u, e),
  },
  {
    name: 'canChangeFeatureFlags',
    pair: ['feature_flag', 'update'],
    roles: rolesFor('feature_flag', 'update'),
    answer: (u, e) => experimentPermissions.canChangeFeatureFlags(u, e),
  },
  {
    name: 'canDeleteFeatureFlags',
    pair: ['feature_flag', 'delete'],
    roles: rolesFor('feature_flag', 'delete'),
    answer: (u, e) => experimentPermissions.canDeleteFeatureFlags(u, e),
  },
  {
    name: 'canCreateSegments',
    pair: ['experiment', 'create'],
    roles: rolesFor('experiment', 'create'),
    answer: (u, e) => segmentPermissions.canCreateSegments(u, e),
  },
  {
    name: 'canChangeSegments',
    pair: ['experiment', 'update'],
    roles: rolesFor('experiment', 'update'),
    answer: (u, e) => segmentPermissions.canChangeSegments(u, e),
  },
  {
    name: 'canArchiveSegments',
    pair: ['experiment', 'delete'],
    roles: rolesFor('experiment', 'delete'),
    answer: (u, e) => segmentPermissions.canArchiveSegments(u, e),
  },
  {
    name: 'readsAllAuditLogs',
    pair: ['audit_log', 'read'],
    roles: TABLE.audit_log_read_roles,
    answer: (u, e) => readsAllAuditLogs(me(u), e),
  },
  {
    // Option B: a server-side key needs only FEATURE_FLAG UPDATE.
    name: 'canCreateRulesetKey',
    pair: ['feature_flag', 'update'],
    roles: rolesFor('feature_flag', 'update'),
    answer: (u, e) => canCreateRulesetKey(me(u), e),
  },
  {
    name: 'canAdministerEligibility',
    pair: null,
    roles: TABLE.administer_eligibility_roles,
    // Takes no map: a grant can never give it.
    answer: (u) => experimentPermissions.canAdministerEligibility(u),
  },
];

const STATES: Array<{ label: string; effective: EffectivePermissions | undefined }> = [
  { label: 'no map', effective: undefined },
  { label: 'an empty map', effective: {} },
  ...TABLE.grantable.map(([resource, action]) => ({
    label: `${resource}:${action}`,
    effective: { [resource]: [action] },
  })),
];

function expected(row: HelperRow, user: MatrixUser, effective: EffectivePermissions | undefined): boolean {
  if (user.is_superuser) return true;
  if (user.role !== null && row.roles.includes(user.role)) return true;
  if (row.pair === null) return false;
  const [resource, action] = row.pair;
  return Boolean(effective?.[resource]?.includes(action));
}

const CASES = HELPERS.flatMap((row) =>
  USERS.flatMap((user) =>
    STATES.map((state) => ({
      row,
      user,
      state,
      want: expected(row, user, state.effective),
    })),
  ),
);

describe('the table the matrix reads', () => {
  it('holds the eleven grantable pairs and the four built-in roles', () => {
    expect(TABLE.grantable).toHaveLength(11);
    expect(TABLE.roles).toEqual(['ADMIN', 'DEVELOPER', 'ANALYST', 'VIEWER']);
  });

  it("names each helper's pair among the grantable ones, and none for canAdministerEligibility", () => {
    const grantable = new Set(TABLE.grantable.map(([r, a]) => `${r}:${a}`));
    for (const row of HELPERS) {
      if (row.pair === null) continue;
      expect(grantable.has(row.pair.join(':'))).toBe(true);
    }
    expect(HELPERS.filter((row) => row.pair === null).map((row) => row.name)).toEqual(['canAdministerEligibility']);
  });

  it('covers every exported can… helper of the two permission modules', () => {
    // A helper added to either module without a row here would never be held
    // to the server's table.
    const exported = [...Object.keys(experimentPermissions), ...Object.keys(segmentPermissions)]
      .filter((name) => name.startsWith('can'))
      .sort();
    const covered = HELPERS.map((row) => row.name).filter((name) => exported.includes(name));
    expect(exported).toHaveLength(11);
    expect(exported).toEqual(Array.from(new Set(covered)).sort());
  });
});

describe('every helper x user x effective map (#891, check #41)', () => {
  it('evaluates exactly 13 helpers x 6 users x 13 maps = 1014 cases', () => {
    expect(HELPERS).toHaveLength(13);
    expect(USERS).toHaveLength(6);
    expect(STATES).toHaveLength(13);
    expect(CASES).toHaveLength(1014);
  });

  it('answers as the server decides, in every case', () => {
    const wrong = CASES.filter(({ row, user, state, want }) => row.answer(user, state.effective) !== want).map(
      ({ row, user, state, want }) => `${row.name}(${user.label}, ${state.label}): expected ${want}`,
    );
    expect(wrong).toEqual([]);
  });

  it('a grant admits a user the role refuses, and only its own pair does (positive control)', () => {
    const viewer = USERS.find((u) => u.label === 'VIEWER') as MatrixUser;
    expect(experimentPermissions.canCreateExperiment(viewer, undefined)).toBe(false);
    expect(experimentPermissions.canCreateExperiment(viewer, { experiment: ['create'] })).toBe(true);
    expect(experimentPermissions.canCreateExperiment(viewer, { experiment: ['update'] })).toBe(false);
    expect(experimentPermissions.canCreateExperiment(viewer, { feature_flag: ['create'] })).toBe(false);
    expect(readsAllAuditLogs(me(viewer), { audit_log: ['read'] })).toBe(true);
    expect(experimentPermissions.canAdministerEligibility(viewer)).toBe(false);
  });

  it('a map that is not a map of lists admits nothing', () => {
    const viewer = USERS.find((u) => u.label === 'VIEWER') as MatrixUser;
    const odd = { experiment: 'create' } as unknown as EffectivePermissions;
    expect(experimentPermissions.canCreateExperiment(viewer, odd)).toBe(false);
    const inherited = Object.create({ experiment: ['create'] }) as EffectivePermissions;
    expect(experimentPermissions.canCreateExperiment(viewer, inherited)).toBe(false);
  });
});
