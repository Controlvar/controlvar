/**
 * The `unavailable_reason` codes the API documents for a CUPED comparison, read
 * from the committed stable OpenAPI snapshot (the field's own description names
 * them all), so a code added there with no copy in the dashboard fails a test
 * instead of reading "Not adjusted for this comparison." for ever.
 */
import fs from 'fs';
import path from 'path';

const STABLE_SNAPSHOT = path.resolve(__dirname, '..', '..', '..', '..', 'docs', 'api', 'openapi-v1.stable.json');

export function documentedCupedReasonCodes(): string[] {
  const description: string = JSON.parse(fs.readFileSync(STABLE_SNAPSHOT, 'utf8')).components.schemas
    .CupedMetricResult.properties.unavailable_reason.description;
  const words = description.match(/\b[a-z0-9]+(?:_[a-z0-9]+)+\b/g) ?? [];
  return Array.from(new Set(words)).filter((word) => word !== 'unavailable_reason');
}
