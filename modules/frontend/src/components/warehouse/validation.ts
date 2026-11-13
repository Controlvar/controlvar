/**
 * Checks the warehouse forms make before anything is sent. They mirror the
 * API's own patterns (modules/backend/app/schemas/warehouse_*.py and the
 * identifier gate) so a mistake is named next to its field; the API applies
 * them again and is the one that decides. Nothing here makes a network call.
 */
import type { SourceKind, WarehouseType } from '@modules/services/warehouse';

export type FieldErrors = Record<string, string>;

/** A control character (U+0000-U+001F, U+007F) or a line/paragraph separator (U+2028, U+2029). */
function hasControlCharacter(value: string): boolean {
  for (let i = 0; i < value.length; i += 1) {
    const c = value.charCodeAt(i);
    if (c < 0x20 || c === 0x7f || c === 0x2028 || c === 0x2029) return true;
  }
  return false;
}

export function checkName(value: string, what = 'Name'): string | null {
  if (!value.trim()) return `${what} is required.`;
  if (value.length > 200) return `${what} must be 200 characters or fewer.`;
  if (hasControlCharacter(value)) return `${what} can't contain line breaks or control characters.`;
  return null;
}

// -- connections --------------------------------------------------------------

export const SNOWFLAKE_ACCOUNT = /^[A-Za-z][A-Za-z0-9]*-[A-Za-z0-9_]{1,255}$/;
export const SNOWFLAKE_OBJECT = /^[A-Za-z_][A-Za-z0-9_$]{0,254}$/;
export const SNOWFLAKE_ADMIN_ROLES = ['ACCOUNTADMIN', 'SECURITYADMIN', 'SYSADMIN', 'ORGADMIN', 'USERADMIN'];
export const BIGQUERY_PROJECT = /^[a-z][a-z0-9-]{4,28}[a-z0-9]$/;
export const BIGQUERY_LOCATION = /^[A-Za-z]+(-[a-z]+[0-9]*)*$/;
export const AWS_REGION = /^[a-z]{2}(-[a-z]+)+-[0-9]{1,2}$/;
/** A role under the IAM path /controlvar-warehouse/ (Amazon Athena and Amazon Redshift). */
export const ROLE_ARN = /^arn:aws:iam::[0-9]{12}:role\/controlvar-warehouse\/[A-Za-z0-9+=,.@_-]{1,64}$/;
export const ROLE_ARN_HELP =
  'Use a role under the IAM path /controlvar-warehouse/, such as arn:aws:iam::123456789012:role/controlvar-warehouse/controlvar-reader.';
export const ATHENA_WORKGROUP = /^[A-Za-z0-9._-]{1,128}$/;
export const ATHENA_DATABASE = /^[a-z0-9_]{1,255}$/;
export const REDSHIFT_WORKGROUP = /^[a-z0-9-]{3,64}$/;
export const REDSHIFT_CLUSTER = /^[a-z][a-z0-9-]{0,62}$/;
export const REDSHIFT_DATABASE = /^[a-z_][a-z0-9_$]{0,126}$/;
export const REDSHIFT_DB_USER = /^[a-z][a-z0-9_+.@-]{0,63}$/;
export const REDSHIFT_WORKGROUP_HELP = 'Use 3-64 lower-case letters, digits and hyphens.';
export const REDSHIFT_CLUSTER_HELP = 'Use a lower-case letter, then lower-case letters, digits and hyphens (up to 63).';
export const REDSHIFT_DATABASE_HELP = 'Use lower-case letters, digits, _ and $, starting with a letter or _.';
export const REDSHIFT_DB_USER_HELP =
  'Use lower-case letters, digits and _ + . @ -, starting with a letter (up to 64).';
export const MAX_SERVICE_ACCOUNT_JSON_CHARS = 16 * 1024;
/** Athena's workgroup cutoff cannot be below 10 MB; the API applies the same floor to BigQuery. */
export const MIN_BYTES_PER_QUERY = 10_000_000;

export const SNOWFLAKE_ACCOUNT_HELP =
  'Use the organisation-account identifier (Snowsight › Admin › Accounts), e.g. MYORG-MYACCOUNT.';
export const SNOWFLAKE_ROLE_REFUSED =
  "Use a role made for this connection with read-only grants. ACCOUNTADMIN, SECURITYADMIN, SYSADMIN, ORGADMIN and USERADMIN aren't accepted.";
export const SNOWFLAKE_OBJECT_HELP = 'Use letters, digits, _ and $, starting with a letter or _.';
export const BIGQUERY_PROJECT_HELP =
  "Use the billing project's ID (6-30 lower-case letters, digits and hyphens), e.g. my-analytics-project.";
export const BIGQUERY_LOCATION_HELP = 'Use a BigQuery location such as US, EU or europe-west2.';
export const KEY_INVALID =
  "The service-account key isn't valid. Paste the whole JSON key file downloaded from IAM & Admin > Service accounts > Keys.";
export const KEY_NOT_SERVICE_ACCOUNT =
  "This is not a service-account key. Create a service account and download a JSON key for it (IAM & Admin > Service accounts > Keys); user credentials and workload identity configurations aren't supported.";
export const KEY_ENDPOINT =
  "The key names a sign-in endpoint other than Google's (https://oauth2.googleapis.com/token). Only keys for Google Cloud's public endpoints are supported.";
export const KEY_TOO_LARGE = 'The key file is larger than 16 KiB. Paste the JSON key file itself.';

/** The service-account key checks the API makes, without repeating the key. */
export function checkServiceAccountJson(text: string): string | null {
  if (!text.trim()) return 'Paste the service-account JSON key, or choose the key file.';
  if (text.length > MAX_SERVICE_ACCOUNT_JSON_CHARS) return KEY_TOO_LARGE;
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch {
    return KEY_INVALID;
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) return KEY_INVALID;
  const key = data as Record<string, unknown>;
  if (key.type !== 'service_account') {
    return typeof key.type === 'string' ? KEY_NOT_SERVICE_ACCOUNT : KEY_INVALID;
  }
  for (const field of ['client_email', 'private_key', 'private_key_id']) {
    if (typeof key[field] !== 'string' || !(key[field] as string)) return KEY_INVALID;
  }
  if ('token_uri' in key && key.token_uri !== 'https://oauth2.googleapis.com/token') return KEY_ENDPOINT;
  if ('universe_domain' in key && key.universe_domain !== 'googleapis.com') return KEY_ENDPOINT;
  return null;
}

export function checkSnowflakeAccount(value: string): string | null {
  if (!value.trim()) return 'Account is required.';
  return SNOWFLAKE_ACCOUNT.test(value) ? null : SNOWFLAKE_ACCOUNT_HELP;
}

export function checkSnowflakeObject(value: string, what: string): string | null {
  if (!value.trim()) return `${what} is required.`;
  return SNOWFLAKE_OBJECT.test(value) ? null : SNOWFLAKE_OBJECT_HELP;
}

export function checkSnowflakeRole(value: string): string | null {
  const shape = checkSnowflakeObject(value, 'Role');
  if (shape) return shape;
  return SNOWFLAKE_ADMIN_ROLES.includes(value.toUpperCase()) ? SNOWFLAKE_ROLE_REFUSED : null;
}

export function checkPattern(value: string, pattern: RegExp, what: string, help: string): string | null {
  if (!value.trim()) return `${what} is required.`;
  return pattern.test(value) ? null : help;
}

export function checkInteger(value: string, what: string, min: number, max: number): string | null {
  if (!/^[0-9]+$/.test(value.trim())) return `${what} must be a whole number.`;
  const n = Number(value);
  if (n < min || n > max) return `${what} must be between ${min.toLocaleString('en-GB')} and ${max.toLocaleString('en-GB')}.`;
  return null;
}

/** Gigabytes (10^9 bytes) as typed -> bytes, or null when it is not a number. */
export function gigabytesToBytes(value: string): number | null {
  if (!/^[0-9]+(\.[0-9]+)?$/.test(value.trim())) return null;
  return Math.round(Number(value) * 1e9);
}

export function checkBytesPerQuery(value: string): string | null {
  const bytes = gigabytesToBytes(value);
  if (bytes === null) return 'Enter the limit in GB, e.g. 50.';
  if (bytes < MIN_BYTES_PER_QUERY) return 'The limit must be at least 0.01 GB (10 MB).';
  return null;
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return '—';
  if (bytes >= 1e12) return `${(bytes / 1e12).toLocaleString('en-GB', { maximumFractionDigits: 2 })} TB`;
  if (bytes >= 1e9) return `${(bytes / 1e9).toLocaleString('en-GB', { maximumFractionDigits: 2 })} GB`;
  return `${(bytes / 1e6).toLocaleString('en-GB', { maximumFractionDigits: 2 })} MB`;
}

export function formatSeconds(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—';
  if (seconds < 120) return `${seconds} s`;
  if (seconds < 7200) return `${Math.round(seconds / 60)} min`;
  return `${(seconds / 3600).toLocaleString('en-GB', { maximumFractionDigits: 1 })} h`;
}

// -- sources ------------------------------------------------------------------

interface Dialect {
  /** How the table reference is written, for the help text. */
  shape: string;
  example: string;
  parts: number;
  part: (index: number) => RegExp;
  column: RegExp;
  columnHelp: string;
}

const SNOWFLAKE_PART = /^[A-Za-z_][A-Za-z0-9_$]{0,254}$/;
const BIGQUERY_PART = /^[A-Za-z_][A-Za-z0-9_]{0,1023}$/;
const ATHENA_PART = /^[a-z0-9_]{1,255}$/;
/**
 * An unquoted Redshift identifier. Looser than the API may be: the API's
 * identifier gate decides, and this only names a mistake next to its field.
 */
const REDSHIFT_PART = /^[A-Za-z_][A-Za-z0-9_$]{0,126}$/;

export const DIALECTS: Record<WarehouseType, Dialect> = {
  snowflake: {
    shape: 'DATABASE.SCHEMA.TABLE',
    example: 'ANALYTICS.EXPERIMENTS.EXPOSURES',
    parts: 3,
    part: () => SNOWFLAKE_PART,
    column: SNOWFLAKE_PART,
    columnHelp: SNOWFLAKE_OBJECT_HELP,
  },
  bigquery: {
    shape: 'project.dataset.table',
    example: 'my-analytics-project.experiments.exposures',
    parts: 3,
    part: (i) => (i === 0 ? BIGQUERY_PROJECT : BIGQUERY_PART),
    column: BIGQUERY_PART,
    columnHelp: 'Use letters, digits and _, starting with a letter or _.',
  },
  athena: {
    shape: 'database.table',
    example: 'analytics.exposures',
    parts: 2,
    part: () => ATHENA_PART,
    column: ATHENA_PART,
    columnHelp: 'Use lower-case letters, digits and _.',
  },
  redshift: {
    shape: 'schema.table',
    example: 'analytics.exposures',
    parts: 2,
    part: () => REDSHIFT_PART,
    column: REDSHIFT_PART,
    columnHelp: 'Use letters, digits, _ and $, starting with a letter or _.',
  },
};

export function checkTable(value: string, type: WarehouseType): string | null {
  const d = DIALECTS[type];
  if (!value.trim()) return 'Table or view is required.';
  const parts = value.split('.');
  if (parts.length !== d.parts || parts.some((p, i) => !d.part(i).test(p))) {
    return `Use ${d.shape} (${d.parts} parts, e.g. ${d.example}), with no quotes or spaces.`;
  }
  return null;
}

export function checkColumn(value: string, type: WarehouseType, what: string): string | null {
  if (!value.trim()) return `${what} is required.`;
  return DIALECTS[type].column.test(value) ? null : DIALECTS[type].columnHelp;
}

export const LITERAL = /^[A-Za-z0-9 _.:@/+-]{1,256}$/;
export const LITERAL_HELP =
  'Use letters, digits, spaces and . _ : @ / + - (up to 256 characters). Quotes and other punctuation are refused, not escaped.';
export const MAX_FILTERS = 5;
export const MAX_IN_VALUES = 50;

export const ASSIGNMENT_COLUMNS = [
  { role: 'unit_id', label: 'User ID column', help: 'The ID your SDK sends for each user.' },
  { role: 'experiment_key', label: 'Experiment key column', help: 'The experiment key, exactly as the SDK sends it.' },
  { role: 'variant', label: 'Variant column', help: 'The variant name each user was assigned.' },
  { role: 'exposed_at', label: 'Assignment time column', help: 'A timestamp column: when the user was assigned.' },
] as const;

export const METRIC_COLUMNS = [
  { role: 'unit_id', label: 'User ID column', help: 'The same user ID as the assignment source.' },
  { role: 'event_at', label: 'Event time column', help: 'A timestamp column: when the event happened.' },
] as const;

export function sourceKindLabel(kind: SourceKind): string {
  return kind === 'assignment' ? 'assignment source' : 'metric source';
}
