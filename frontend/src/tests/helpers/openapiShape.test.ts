import { shapeErrors } from '@/tests/helpers/openapiShape';

// The helper is the assertion behind the roles and permissions fixtures (#891).
// If it stopped refusing bad values every fixture would pass, so each schema
// gets a known-good value and the ways it must be refused.

const grant = { resource: 'experiment', actions: ['read', 'update'] };

const role = {
  id: 'r1',
  name: 'data-scientist',
  description: null,
  is_system_role: false,
  permissions: [grant],
  created_at: '2026-11-02T10:00:00Z',
};

const effective = {
  user_id: 'u1',
  username: 'ada',
  base_role: 'developer',
  custom_roles: ['data-scientist'],
  permissions: { experiment: ['read'] },
  is_superuser: false,
};

type Case = {
  schema: string;
  good: Record<string, unknown>;
  stringPermissions: unknown;
  badEnumValue: Record<string, unknown>;
  missing: string;
};

const CASES: Case[] = [
  {
    schema: 'CustomRoleResponse',
    good: role,
    stringPermissions: 'experiment:read',
    badEnumValue: { ...role, permissions: [{ resource: 'experiment', actions: ['obliterate'] }] },
    missing: 'id',
  },
  {
    schema: 'CustomRoleCreate',
    good: { name: 'data-scientist', description: 'x', permissions: [grant] },
    stringPermissions: ['experiment:read'],
    badEnumValue: { name: 'data-scientist', permissions: [{ resource: 'experiment', actions: ['obliterate'] }] },
    missing: 'name',
  },
  {
    schema: 'CustomRoleUpdate',
    good: { description: 'x', permissions: [grant] },
    stringPermissions: ['experiment:read'],
    badEnumValue: { permissions: [{ resource: 'experiment', actions: ['obliterate'] }] },
    // No required field: an empty grant list is below its minItems instead.
    missing: '',
  },
  {
    schema: 'EffectivePermissionsResponse',
    good: effective,
    stringPermissions: 'experiment:read',
    badEnumValue: { ...effective, permissions: { experiment: 'read' } },
    missing: 'user_id',
  },
];

describe.each(CASES)('shapeErrors($schema)', (c) => {
  it('accepts a known-good value', () => {
    expect(shapeErrors(c.schema, c.good)).toEqual([]);
  });

  it('refuses permissions given as strings', () => {
    expect(shapeErrors(c.schema, { ...c.good, permissions: c.stringPermissions }).length).toBeGreaterThan(0);
  });

  it('refuses an unknown extra property', () => {
    const errors = shapeErrors(c.schema, { ...c.good, surprise: true });
    expect(errors.join('\n')).toMatch(/"surprise" is not a property/);
  });

  it('refuses a bad action (enum or type)', () => {
    expect(shapeErrors(c.schema, c.badEnumValue).length).toBeGreaterThan(0);
  });

  it('refuses a missing required field', () => {
    if (!c.missing) {
      // CustomRoleUpdate has none; a grant with no actions breaks minItems.
      const bad = { permissions: [{ resource: 'experiment', actions: [] }] };
      expect(shapeErrors(c.schema, bad).length).toBeGreaterThan(0);
      return;
    }
    const { [c.missing]: _dropped, ...rest } = c.good;
    expect(shapeErrors(c.schema, rest).join('\n')).toContain(`missing "${c.missing}"`);
  });
});
