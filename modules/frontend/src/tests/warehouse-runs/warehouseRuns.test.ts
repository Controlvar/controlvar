/**
 * The pure parts of the warehouse runs service: roles, the words for each
 * refusal and failure code, and the numbers in the source strip.
 */
import { ApiError } from '@/services/api';
import {
  KNOWN_FAILURE_CODES,
  canStartRun,
  costText,
  effectiveRole,
  failureCopy,
  meanText,
  startRefusal,
  untilText,
  utcText,
} from '@modules/services/warehouseRuns';
import { ERRORS_PY, RUNNER_PY, enumCodes, literalCodes, storableCodes } from './backendCodes';
import fs from 'fs';

describe('roles (the API matrix: start a run = ADMIN, DEVELOPER; a superuser is ADMIN)', () => {
  it.each([
    [{ role: 'ADMIN' }, true],
    [{ role: 'DEVELOPER' }, true],
    [{ role: 'ANALYST' }, false],
    [{ role: 'VIEWER' }, false],
    [{ role: 'VIEWER', is_superuser: true }, true],
    [{ role: 'developer' }, true],
    [{}, false],
  ])('%j may start: %s', (user, allowed) => {
    expect(canStartRun(user)).toBe(allowed);
  });

  it('reads no user as no role', () => {
    expect(effectiveRole(null)).toBeNull();
    expect(canStartRun(undefined)).toBe(false);
  });
});

describe('failureCopy', () => {
  it('reads the backend’s code list (the parser is not vacuous)', () => {
    const members = enumCodes(fs.readFileSync(ERRORS_PY, 'utf8'));
    // WarehouseErrorCode has 23 members on main; a parser that found none
    // would make the check below pass for nothing.
    expect(members.length).toBeGreaterThanOrEqual(23);
    expect(members).toEqual(expect.arrayContaining(['auth_failed', 'internal', 'destination_not_allowed']));
    expect(literalCodes(fs.readFileSync(RUNNER_PY, 'utf8'))).toEqual(
      expect.arrayContaining(['abandoned', 'no_units', 'result_invalid']),
    );
  });

  it('has its own words for every code the backend can store on a run or a metric', () => {
    const missing = storableCodes().filter((c) => KNOWN_FAILURE_CODES.indexOf(c) === -1);
    expect(missing).toEqual([]);
  });

  it('names the warehouse, the connection and the experiment key', () => {
    const c = { warehouse: 'Snowflake', connection: 'Prod analytics', experimentKey: 'checkout-v2' };
    expect(failureCopy('auth_failed', null, c).title).toBe(
      'Snowflake did not accept the credentials of Prod analytics.',
    );
    expect(failureCopy('no_units', null, c)).toEqual({
      title: 'No exposures for checkout-v2 were found in the window.',
      fix: 'Is the experiment key in your table exactly checkout-v2?',
    });
  });

  it('says whether a stopped query was billed, per warehouse', () => {
    expect(failureCopy('bytes_limit', null, { warehouse: 'BigQuery' }).fix).toMatch(/^Nothing was run or billed\./);
    expect(failureCopy('bytes_limit', null, { warehouse: 'Amazon Athena' }).fix).toMatch(
      /^You are billed for the data Athena scanned/,
    );
  });

  it('says a deployment with no AWS identity is for an operator to fix', () => {
    const c = { warehouse: 'Amazon Redshift', connection: 'Prod analytics' };
    expect(failureCopy('aws_identity_unavailable', null, c)).toEqual({
      title: 'This deployment has no AWS identity to assume your role from, so Amazon Redshift can’t be used here.',
      fix: 'An operator must run the API with an AWS identity that may assume the role Prod analytics uses.',
    });
  });

  it('says a role that does not require its external ID is for an admin to fix', () => {
    const c = { warehouse: 'Amazon Redshift', connection: 'Prod analytics' };
    expect(failureCopy('role_trust_unsafe', null, c)).toEqual({
      title: 'The trust policy of the role Prod analytics uses doesn’t require its external ID, so the role was refused.',
      fix:
        'An admin adds a condition requiring the external ID shown in Warehouse › Connections › Prod analytics ' +
        'to the role’s trust policy.',
    });
  });

  it('falls back to the API message, then to the code', () => {
    expect(failureCopy('something_new', 'API words')).toEqual({ title: 'API words' });
    expect(failureCopy('something_new', null)).toEqual({ title: 'The analysis failed (code something_new).' });
  });
});

describe('startRefusal', () => {
  const now = new Date('2026-09-29T18:48:00Z');

  it('reads limit and resets_at from a 429 daily_run_limit_reached', () => {
    const err = new ApiError({
      status: 429,
      detail: {
        code: 'daily_run_limit_reached',
        message: 'x',
        limit: 20,
        resets_at: '2026-09-30T00:00:00Z',
      },
    });
    const r = startRefusal(err, now);
    expect(r.code).toBe('daily_run_limit_reached');
    expect(r.limit).toBe(20);
    expect(r.resetsAt).toBe('2026-09-30T00:00:00Z');
    expect(r.message).toBe(
      'Not run: this connection has reached its limit of 20 analyses per day (UTC, previews ' +
        'included). The count resets at 2026-09-30 00:00 UTC, in 5 h 12 min. An admin can change ' +
        'the limit in Warehouse › Connections.',
    );
  });

  it('keeps the API message for a code it has no words for', () => {
    const err = new ApiError({ status: 422, detail: { code: 'unknown_variant', message: 'API words' } });
    expect(startRefusal(err)).toEqual({ code: 'unknown_variant', message: 'API words' });
  });

  it('reports an unreachable API', () => {
    const err = new ApiError({ status: 0, detail: undefined, message: 'Can’t reach the API' });
    expect(startRefusal(err).code).toBe('unreachable');
  });
});

describe('times and costs', () => {
  it('formats UTC and the wait', () => {
    expect(utcText('2026-09-28T09:00:49Z')).toBe('2026-09-28 09:00 UTC');
    expect(untilText('2026-09-30T00:00:00Z', new Date('2026-09-29T23:59:30Z'))).toBe('1 min');
    expect(untilText('2026-09-30T00:00:00Z', new Date('2026-09-29T21:00:00Z'))).toBe('3 h 0 min');
    expect(untilText('nonsense')).toBeNull();
  });

  it('reports cost in the warehouse’s own terms, never a price', () => {
    expect(
      costText({ warehouse_type: 'bigquery', job_metadata: [{ statement: 'x', total_bytes_billed: 3_400_000_000 }] }),
    ).toBe('Billed 3.4 GB');
    expect(
      costText({ warehouse_type: 'athena', job_metadata: [{ statement: 'x', total_bytes_processed: 1500 }] }),
    ).toBe('Scanned 1.5 KB');
    expect(
      costText({
        warehouse_type: 'snowflake',
        job_metadata: [
          { statement: 'x', elapsed_ms: 30_000, warehouse: 'ANALYTICS_XS' },
          { statement: 'y', elapsed_ms: 18_000 },
        ],
      }),
    ).toBe('Ran 48 s on ANALYTICS_XS');
    expect(costText({ warehouse_type: 'bigquery', job_metadata: [] })).toBeNull();
    expect(costText({ warehouse_type: 'bigquery', job_metadata: null })).toBeNull();
  });
});

describe('meanText', () => {
  it.each([
    [12.3456, '12.35'],
    [10000.5, '10,000.5'],
    [-3.14159, '-3.14'],
    [0.012345, '0.0123'],
    [0, '0'],
    [null, '—'],
    [Number.NaN, '—'],
  ])('%p is %p', (value, text) => {
    expect(meanText(value as number | null)).toBe(text);
  });
});
