/**
 * `SampleSizeResult` in types/results.ts describes exactly the response the API
 * snapshot documents (#666): the same keys, and null allowed on exactly the
 * fields the API may send as null.
 *
 * The two records below are type-checked by ts-jest: each must name every key
 * of its type and nothing else, so adding a field to the interface without
 * listing it here (or the reverse) fails to compile. The assertions then hold
 * those lists against `docs/api/openapi-v1.stable.json`.
 */
import fs from 'fs';
import path from 'path';
import { SampleSizeResult } from '@/types/results';

const REPO_ROOT = path.resolve(__dirname, '..', '..', '..', '..');
const STABLE_SNAPSHOT = path.join(REPO_ROOT, 'docs', 'api', 'openapi-v1.stable.json');

type Schema = {
  properties: Record<string, { anyOf?: { type?: string }[]; type?: string }>;
};

function snapshotSchema(): Schema {
  const spec = JSON.parse(fs.readFileSync(STABLE_SNAPSHOT, 'utf8'));
  return spec.components.schemas.SampleSizeResult as Schema;
}

type NullableKeys<T> = { [K in keyof T]-?: null extends T[K] ? K : never }[keyof T];

const EVERY_KEY: { [K in keyof SampleSizeResult]-?: true } = {
  required_sample_size_per_variant: true,
  current_sample_size_per_variant: true,
  is_adequate: true,
  achieved_power: true,
  days_to_significance: true,
  projected_completion_date: true,
  baseline_rate: true,
  mde: true,
  mde_source: true,
  confidence_level: true,
  power_target: true,
  baseline_source: true,
  baseline_users: true,
  metric_id: true,
  metric_name: true,
  metric_type: true,
  analysed_as: true,
  alpha: true,
  comparisons: true,
  correction_method: true,
  mde_absolute: true,
  unavailable_reason: true,
  guide_only_reasons: true,
};

const NULLABLE_KEYS: Record<NullableKeys<SampleSizeResult>, true> = {
  required_sample_size_per_variant: true,
  achieved_power: true,
  days_to_significance: true,
  projected_completion_date: true,
  baseline_rate: true,
  baseline_source: true,
  baseline_users: true,
  metric_id: true,
  metric_name: true,
  metric_type: true,
  mde_absolute: true,
  unavailable_reason: true,
};

describe('SampleSizeResult matches the API snapshot', () => {
  it('has exactly the properties of the snapshot schema', () => {
    expect(Object.keys(EVERY_KEY).sort()).toEqual(
      Object.keys(snapshotSchema().properties).sort()
    );
  });

  it('allows null on exactly the properties the snapshot marks nullable', () => {
    const nullableInSnapshot = Object.entries(snapshotSchema().properties)
      .filter(([, p]) => (p.anyOf ?? []).some((option) => option.type === 'null'))
      .map(([name]) => name)
      .sort();
    expect(Object.keys(NULLABLE_KEYS).sort()).toEqual(nullableInSnapshot);
  });
});
