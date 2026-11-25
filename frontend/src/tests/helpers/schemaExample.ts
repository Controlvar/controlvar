/**
 * An example response built from the API's own schema, so a test feeds a page
 * what the API sends and not a hand-typed copy of what the page expects.
 * Reads `docs/api/openapi-v1.full.json`, which the backend smoke suite keeps
 * equal to the API.
 */
import fs from 'fs';
import path from 'path';
import { shapeErrors } from './openapiShape';

type Schema = Record<string, any>;

const REPO_ROOT = path.resolve(__dirname, '..', '..', '..', '..');
const SPEC_PATH = path.join(REPO_ROOT, 'docs', 'api', 'openapi-v1.full.json');

let spec: Schema | undefined;

function schemas(): Record<string, Schema> {
  spec ??= JSON.parse(fs.readFileSync(SPEC_PATH, 'utf8'));
  return spec!.components.schemas;
}

function build(schema: Schema, name: string): unknown {
  if (schema.$ref) {
    const ref = String(schema.$ref).replace('#/components/schemas/', '');
    return build(schemas()[ref], ref);
  }
  if (schema.enum) return schema.enum[0];
  if (schema.anyOf) {
    const branch = (schema.anyOf as Schema[]).find((b) => b.type !== 'null');
    return branch ? build(branch, name) : null;
  }
  switch (schema.type) {
    case 'object': {
      const out: Record<string, unknown> = {};
      for (const [key, prop] of Object.entries((schema.properties ?? {}) as Record<string, Schema>)) {
        out[key] = build(prop, key);
      }
      return out;
    }
    case 'array':
      return [build(schema.items, name)];
    case 'integer':
      return 3;
    case 'number':
      return 1.5;
    case 'boolean':
      return true;
    case 'string':
      // A plain string is a time when the API names it `..._at`.
      return /_at$/.test(name) ? '2026-11-20T10:00:00' : `${name}-value`;
    default:
      return null;
  }
}

/**
 * One instance of the named schema with every property filled in: the first
 * enum value, the first non-null branch of an `anyOf`. `overrides` replace
 * top-level properties, and each must be a property of the schema: a field the
 * API renamed makes the call throw instead of passing a stale name through.
 */
export function schemaExample<T = Record<string, unknown>>(
  schemaName: string,
  overrides: Record<string, unknown> = {},
): T {
  const schema = schemas()[schemaName];
  if (!schema) throw new Error(`${schemaName} is not in ${SPEC_PATH}`);
  const example = build(schema, schemaName) as Record<string, unknown>;
  for (const key of Object.keys(overrides)) {
    if (!(key in example)) throw new Error(`${schemaName} has no property ${key}`);
  }
  const result = { ...example, ...overrides };
  const errors = shapeErrors(schemaName, result);
  if (errors.length) throw new Error(`${schemaName} example is not the API's shape: ${errors.join('; ')}`);
  return result as T;
}
