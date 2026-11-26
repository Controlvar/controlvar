/**
 * The conversion window's result fields (beta, #1934) in types/results.ts are
 * the ones the API snapshot documents: the same names, integers, and null
 * allowed. A field renamed on either side fails here rather than rendering as
 * undefined, which the page reads as "no window" and says nothing.
 */
import fs from 'fs';
import path from 'path';
import { MetricResult, VariantResult } from '@/types/results';

const REPO_ROOT = path.resolve(__dirname, '..', '..', '..', '..');
const STABLE_SNAPSHOT = path.join(REPO_ROOT, 'docs', 'api', 'openapi-v1.stable.json');

type Props = Record<string, { anyOf?: { type?: string }[]; type?: string }>;

function properties(schema: string): Props {
  const spec = JSON.parse(fs.readFileSync(STABLE_SNAPSHOT, 'utf8')) as {
    components: { schemas: Record<string, { properties: Props }> };
  };
  return spec.components.schemas[schema].properties;
}

const types = (p: Props[string] | undefined) => (p?.anyOf ?? [p ?? {}]).map((o) => o.type).sort();

// Each name must be a key of its interface: `tsc --noEmit` (the lint job)
// refuses a renamed field here. Jest alone may not, since its transform of an
// unchanged test file is cached.
const METRIC_KEY: keyof MetricResult = 'conversion_window_hours';
const VARIANT_KEY: keyof VariantResult = 'open_window_users';

it('MetricResult.conversion_window_hours and VariantResult.open_window_users are nullable integers in the snapshot', () => {
  expect(types(properties('MetricResult')[METRIC_KEY])).toEqual(['integer', 'null']);
  expect(types(properties('VariantResult')[VARIANT_KEY])).toEqual(['integer', 'null']);
});
