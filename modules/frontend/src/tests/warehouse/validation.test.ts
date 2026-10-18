/**
 * The checks the warehouse forms make before sending anything. They mirror the
 * API's patterns; the API is still the one that decides.
 */
import {
  KEY_ENDPOINT,
  KEY_INVALID,
  KEY_NOT_SERVICE_ACCOUNT,
  KEY_TOO_LARGE,
  SNOWFLAKE_ACCOUNT_HELP,
  SNOWFLAKE_ROLE_REFUSED,
  checkBytesPerQuery,
  checkColumn,
  checkName,
  checkServiceAccountJson,
  checkSnowflakeAccount,
  checkSnowflakeRole,
  checkTable,
  gigabytesToBytes,
} from '@modules/components/warehouse/validation';
import { SERVICE_ACCOUNT_JSON } from './fixtures';

describe('service-account key', () => {
  it('accepts a service-account key', () => {
    expect(checkServiceAccountJson(SERVICE_ACCOUNT_JSON)).toBeNull();
  });

  it.each([
    ['not JSON', 'nope', KEY_INVALID],
    ['an array', '[]', KEY_INVALID],
    ['no type', JSON.stringify({ client_email: 'a', private_key: 'b', private_key_id: 'c' }), KEY_INVALID],
    ['a user credential', JSON.stringify({ type: 'authorized_user', client_id: 'x' }), KEY_NOT_SERVICE_ACCOUNT],
    ['workload identity', JSON.stringify({ type: 'external_account' }), KEY_NOT_SERVICE_ACCOUNT],
    ['no private key', JSON.stringify({ type: 'service_account', client_email: 'a', private_key_id: 'c' }), KEY_INVALID],
  ])('refuses %s', (_label, text, message) => {
    expect(checkServiceAccountJson(text)).toBe(message);
  });

  it('refuses a key that names another sign-in endpoint', () => {
    const key = { ...JSON.parse(SERVICE_ACCOUNT_JSON), token_uri: 'https://example.test/token' };
    expect(checkServiceAccountJson(JSON.stringify(key))).toBe(KEY_ENDPOINT);
    const other = { ...JSON.parse(SERVICE_ACCOUNT_JSON), universe_domain: 'example.test' };
    expect(checkServiceAccountJson(JSON.stringify(other))).toBe(KEY_ENDPOINT);
  });

  it('refuses a key over 16 KiB without parsing it', () => {
    expect(checkServiceAccountJson('x'.repeat(16 * 1024 + 1))).toBe(KEY_TOO_LARGE);
  });

  it('never repeats the key in a message', () => {
    for (const text of ['{"type":"authorized_user","secret":"S3CR3T"}', 'S3CR3T', '{"S3CR3T":1}']) {
      expect(checkServiceAccountJson(text) ?? '').not.toContain('S3CR3T');
    }
  });
});

describe('Snowflake', () => {
  it.each(['MYORG-MYACCOUNT', 'acme-prod_1'])('accepts the account %s', (v) => {
    expect(checkSnowflakeAccount(v)).toBeNull();
  });
  it.each([
    'xy12345.eu-west-1',
    'https://myorg-myaccount.snowflakecomputing.com',
    'myorg-myaccount.snowflakecomputing.com',
    'myorg-myaccount.privatelink',
    'MYORG',
  ])('refuses the account %s with the identifier help', (v) => {
    expect(checkSnowflakeAccount(v)).toBe(SNOWFLAKE_ACCOUNT_HELP);
  });
  it.each(['ACCOUNTADMIN', 'securityadmin', 'SysAdmin', 'ORGADMIN', 'USERADMIN'])('refuses the role %s', (v) => {
    expect(checkSnowflakeRole(v)).toBe(SNOWFLAKE_ROLE_REFUSED);
  });
  it('accepts a read-only role', () => {
    expect(checkSnowflakeRole('CONTROLVAR_READER')).toBeNull();
  });
});

describe('table references and columns', () => {
  it.each([
    ['snowflake', 'ANALYTICS.EXPERIMENTS.EXPOSURES'],
    ['bigquery', 'my-analytics-project.experiments.exposures'],
    ['athena', 'analytics.exposures'],
  ] as const)('accepts a %s reference', (type, ref) => {
    expect(checkTable(ref, type)).toBeNull();
  });

  // The identifier tampers the API refuses (plan SPEC 3): each is refused here too.
  it.each([
    'A.B."C"',
    'A.B.`C`',
    "A.B.C'",
    'A.B.C;',
    'A.B.C--',
    'A.B./*C',
    `A.B.C${String.fromCharCode(0x2028)}`,
    `A.B.C${String.fromCharCode(0)}`,
    ' A.B.C',
    'A.B.C ',
    'A..C',
    'A.B.C.D',
  ])('refuses the Snowflake reference %j', (ref) => {
    expect(checkTable(ref, 'snowflake')).toMatch(/^Use DATABASE\.SCHEMA\.TABLE/);
  });

  it('refuses an Athena reference with three parts or capitals', () => {
    expect(checkTable('db.schema.t', 'athena')).not.toBeNull();
    expect(checkTable('DB.t', 'athena')).not.toBeNull();
  });

  it('checks columns per dialect', () => {
    expect(checkColumn('user_id', 'athena', 'User ID column')).toBeNull();
    expect(checkColumn('UserId', 'athena', 'User ID column')).not.toBeNull();
    expect(checkColumn('USER$ID', 'snowflake', 'User ID column')).toBeNull();
    expect(checkColumn('user id', 'bigquery', 'User ID column')).not.toBeNull();
  });
});

describe('names and limits', () => {
  it('refuses empty and multi-line names', () => {
    expect(checkName('')).toBe('Name is required.');
    expect(checkName('a\nb')).toMatch(/line breaks/);
    expect(checkName('Prod analytics')).toBeNull();
  });

  it('reads GB and enforces the 10 MB floor', () => {
    expect(gigabytesToBytes('50')).toBe(50_000_000_000);
    expect(gigabytesToBytes('0.5')).toBe(500_000_000);
    expect(gigabytesToBytes('1e3')).toBeNull();
    expect(checkBytesPerQuery('0.001')).toMatch(/at least 0\.01 GB/);
    expect(checkBytesPerQuery('0.01')).toBeNull();
  });
});
