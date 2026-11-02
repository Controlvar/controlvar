/**
 * Checks a value against a component schema of the committed full-profile
 * OpenAPI snapshot (`docs/api/openapi-v1.full.json`), which the backend smoke
 * suite keeps equal to the API. A test fixture or a request body that is not
 * what the API sends or accepts fails here, instead of passing against a mock
 * of the wrong shape (#891).
 *
 * It understands the subset those snapshots use: `$ref`, `type`, `properties`,
 * `required`, `items`, `minItems`, `enum`, `anyOf` and `additionalProperties`.
 * A keyword it does not know throws, so a schema that grows cannot be skipped
 * silently.
 */
import fs from 'fs';
import path from 'path';

type Schema = Record<string, unknown>;

const SNAPSHOT = path.resolve(__dirname, '..', '..', '..', '..', 'docs', 'api', 'openapi-v1.full.json');
const KNOWN = new Set([
  '$ref', 'type', 'properties', 'required', 'items', 'minItems', 'maxItems', 'enum', 'anyOf',
  'additionalProperties', 'title', 'description', 'default', 'format', 'maxLength', 'minLength', 'pattern',
]);

let spec: { components: { schemas: Record<string, Schema> } } | null = null;

function schemas(): Record<string, Schema> {
  if (!spec) spec = JSON.parse(fs.readFileSync(SNAPSHOT, 'utf8'));
  return spec!.components.schemas;
}

function check(value: unknown, schema: Schema, where: string, errors: string[]): void {
  for (const key of Object.keys(schema)) {
    if (!KNOWN.has(key)) throw new Error(`openapiShape does not understand "${key}" at ${where}`);
  }
  if (typeof schema.$ref === 'string') {
    const name = schema.$ref.replace('#/components/schemas/', '');
    const target = schemas()[name];
    if (!target) throw new Error(`no schema ${name} in the snapshot`);
    return check(value, target, `${where}(${name})`, errors);
  }
  if (Array.isArray(schema.anyOf)) {
    const options = schema.anyOf as Schema[];
    const failures = options.map((option) => {
      const own: string[] = [];
      check(value, option, where, own);
      return own;
    });
    if (!failures.some((f) => f.length === 0)) errors.push(`${where}: matches none of anyOf`);
    return;
  }
  if (Array.isArray(schema.enum) && !schema.enum.includes(value)) {
    errors.push(`${where}: ${JSON.stringify(value)} is not one of ${JSON.stringify(schema.enum)}`);
    return;
  }
  switch (schema.type) {
    case 'string':
      if (typeof value !== 'string') errors.push(`${where}: expected a string, got ${JSON.stringify(value)}`);
      break;
    case 'integer':
      if (!Number.isInteger(value)) errors.push(`${where}: expected an integer, got ${JSON.stringify(value)}`);
      break;
    case 'boolean':
      if (typeof value !== 'boolean') errors.push(`${where}: expected a boolean, got ${JSON.stringify(value)}`);
      break;
    case 'null':
      if (value !== null) errors.push(`${where}: expected null`);
      break;
    case 'array': {
      if (!Array.isArray(value)) {
        errors.push(`${where}: expected an array, got ${JSON.stringify(value)}`);
        break;
      }
      if (typeof schema.minItems === 'number' && value.length < schema.minItems) {
        errors.push(`${where}: fewer than ${schema.minItems} items`);
      }
      value.forEach((item, i) => check(item, (schema.items ?? {}) as Schema, `${where}[${i}]`, errors));
      break;
    }
    case 'object': {
      if (value === null || typeof value !== 'object' || Array.isArray(value)) {
        errors.push(`${where}: expected an object, got ${JSON.stringify(value)}`);
        break;
      }
      const record = value as Record<string, unknown>;
      const props = (schema.properties ?? {}) as Record<string, Schema>;
      for (const name of (schema.required ?? []) as string[]) {
        if (!(name in record)) errors.push(`${where}: missing "${name}"`);
      }
      for (const [name, item] of Object.entries(record)) {
        if (props[name]) check(item, props[name], `${where}.${name}`, errors);
        else if (schema.additionalProperties && typeof schema.additionalProperties === 'object') {
          check(item, schema.additionalProperties as Schema, `${where}.${name}`, errors);
        } else errors.push(`${where}: "${name}" is not a property of the schema`);
      }
      break;
    }
    case undefined:
      break;
    default:
      throw new Error(`openapiShape does not understand type ${String(schema.type)} at ${where}`);
  }
}

/** The ways `value` is not a `schemaName` of the snapshot; empty when it is one. */
export function shapeErrors(schemaName: string, value: unknown): string[] {
  const schema = schemas()[schemaName];
  if (!schema) throw new Error(`no schema ${schemaName} in the snapshot`);
  const errors: string[] = [];
  check(value, schema, schemaName, errors);
  return errors;
}
