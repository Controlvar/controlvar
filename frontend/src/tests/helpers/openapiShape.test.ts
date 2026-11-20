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

// #442: the create request's bayesian_config is the first body checked here
// with numbers and their bounds (alpha > 0, 0 < credible_level < 1).
describe('shapeErrors(BayesianConfig): numbers and their bounds', () => {
  const good = { prior_family: 'beta', alpha: 2, beta: 1, loss_threshold: 0.001, credible_level: 0.95 };

  it('accepts a known-good value', () => {
    expect(shapeErrors('BayesianConfig', good)).toEqual([]);
  });

  it.each([
    ['alpha', 0, 'not above 0'],
    ['loss_threshold', -0.5, 'not above 0'],
    ['credible_level', 1, 'not below 1'],
    ['alpha', '2', 'expected a number'],
    ['beta', null, 'expected a number'],
  ])('refuses %s = %p', (field, value, words) => {
    expect(shapeErrors('BayesianConfig', { ...good, [field]: value }).join('\n')).toContain(words);
  });

  it('refuses a prior family the API does not know', () => {
    expect(shapeErrors('BayesianConfig', { ...good, prior_family: 'cauchy' }).length).toBeGreaterThan(0);
  });
});
