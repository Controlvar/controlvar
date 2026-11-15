/**
 * The balanced-groups types in types/results.ts describe exactly the schemas
 * the API snapshot documents for `GET /results/{id}/balanced-groups` (#448):
 * the same keys, and null allowed on exactly the fields the API may send as
 * null. A field renamed on either side fails here rather than rendering as
 * undefined on the Breakdowns tab.
 *
 * Each record below must name every key of its type and nothing else, so a
 * field added to an interface without being listed here (or the reverse) fails
 * to compile under `tsc --noEmit` (which CI runs over the tests); Jest itself
 * does not type-check this file, so there a key left out of a record fails only
 * the assertions below, which hold those lists against
 * `docs/api/openapi-v1.stable.json`.
 */
import fs from 'fs';
import path from 'path';
import { BalancedGroupsEstimate, BalancedGroupsResponse, MergedGroup } from '@/types/results';

const REPO_ROOT = path.resolve(__dirname, '..', '..', '..', '..');
const STABLE_SNAPSHOT = path.join(REPO_ROOT, 'docs', 'api', 'openapi-v1.stable.json');

type Schema = {
  properties: Record<string, { anyOf?: { type?: string }[]; type?: string }>;
};

function schema(name: string): Schema {
  const spec = JSON.parse(fs.readFileSync(STABLE_SNAPSHOT, 'utf8'));
  return spec.components.schemas[name] as Schema;
}

function nullableIn(name: string): string[] {
  return Object.entries(schema(name).properties)
    .filter(([, p]) => (p.anyOf ?? []).some((option) => option.type === 'null'))
    .map(([key]) => key)
    .sort();
}

type NullableKeys<T> = { [K in keyof T]-?: null extends T[K] ? K : never }[keyof T];
type Every<T> = { [K in keyof T]-?: true };
type Nullable<T> = Record<NullableKeys<T>, true>;

const RESPONSE_KEYS: Every<BalancedGroupsResponse> = {
  experiment_id: true,
  confidence_level: true,
  correction_method: true,
  groups: true,
  overall: true,
  reason: true,
  merged_groups: true,
  users_outside_blocks: true,
  unknown_share: true,
  engine_version: true,
};
const RESPONSE_NULLABLE: Nullable<BalancedGroupsResponse> = {
  overall: true,
  reason: true,
  unknown_share: true,
};

const ESTIMATE_KEYS: Every<BalancedGroupsEstimate> = {
  variant_id: true,
  variant_name: true,
  absolute_difference: true,
  absolute_difference_ci: true,
  standard_error: true,
  relative_improvement_pct: true,
  p_value: true,
  adjusted_p_value: true,
  is_significant: true,
};
const ESTIMATE_NULLABLE: Nullable<BalancedGroupsEstimate> = {
  absolute_difference: true,
  absolute_difference_ci: true,
  standard_error: true,
  relative_improvement_pct: true,
  p_value: true,
  adjusted_p_value: true,
};

const MERGED_KEYS: Every<MergedGroup> = { group: true, into: true };
const MERGED_NULLABLE: Nullable<MergedGroup> = {};

const CASES: [string, Record<string, true>, Record<string, true>][] = [
  ['BalancedGroupsResponse', RESPONSE_KEYS, RESPONSE_NULLABLE],
  ['BalancedGroupsEstimate', ESTIMATE_KEYS, ESTIMATE_NULLABLE],
  ['MergedGroup', MERGED_KEYS, MERGED_NULLABLE],
];

describe('the balanced-groups types match the API snapshot', () => {
  it.each(CASES)('%s has exactly the properties of the snapshot schema', (name, keys) => {
    expect(Object.keys(keys).sort()).toEqual(Object.keys(schema(name).properties).sort());
  });

  it.each(CASES)('%s allows null on exactly the properties the snapshot marks nullable', (name, _k, nullable) => {
    expect(Object.keys(nullable).sort()).toEqual(nullableIn(name));
  });

  it('the reasons for no estimate are the snapshot\'s', () => {
    const reasons: Record<NonNullable<BalancedGroupsResponse['reason']>, true> = {
      mean_metric: true,
      sequential_testing: true,
      too_few_users: true,
    };
    const reason = schema('BalancedGroupsResponse').properties.reason as {
      anyOf?: { enum?: string[] }[];
    };
    const values = (reason.anyOf ?? []).flatMap((option) => option.enum ?? []).sort();
    expect(values).toEqual(Object.keys(reasons).sort());
  });
});
