/**
 * Everything the flag pages' rule builder can send is accepted, stopped in
 * the browser, or refused by the server and shown beside the rules with the
 * place it names (#535 V8, the browser half).
 *
 * `fixtures/flag-builder-outputs.json` records, for every row of
 * `flagBuilderGrid()`, what the page sends and which of the three happens.
 * This file checks the record against the page's own code: the rows are the
 * grid's rows, each `sends` is what the page would send, and a row is
 * `blocked_in_browser` exactly when the page's pre-save check stops it. The
 * server half, `backend/tests/unit/core/test_flag_builder_outputs.py`, checks
 * `accepted` and `refused_by_server` (with the path and reason) against the
 * API's targeting validator. Both read the same file, so they cannot drift.
 *
 * The counts are pinned: a new attribute, operator or value moves them, and
 * the fixture is regenerated and both halves re-run.
 */
import fs from 'fs';
import path from 'path';
import { checkTargeting } from '@/components/experiments/new/formState';
import { ApiError } from '@/services/api';
import { TARGETING_SAVE_REFUSED, describeTargetingSaveError } from '@/utils/experimentTargeting';
import { targetingToSend } from '@/utils/flagTargeting';
import { flagBuilderGrid } from '../fixtures/flagBuilderGrid';

type Outcome = 'accepted' | 'blocked_in_browser' | 'refused_by_server';

interface FixtureRow {
  name: string;
  sends: Record<string, unknown> | null;
  outcome: Outcome;
  path?: string;
  reason?: string;
}

const fixture: { counts: Record<Outcome, number>; rows: FixtureRow[] } = JSON.parse(
  fs.readFileSync(path.join(__dirname, '..', 'fixtures', 'flag-builder-outputs.json'), 'utf8'),
);

const grid = flagBuilderGrid();

describe('flag builder outputs (#535 V8)', () => {
  it('covers exactly the grid the builder offers, row for row', () => {
    expect(grid).toHaveLength(1335);
    expect(fixture.rows.map((r) => r.name)).toEqual(grid.map((r) => r.name));
  });

  it('pins how many rows fall in each category', () => {
    const counted: Record<string, number> = {};
    for (const row of fixture.rows) counted[row.outcome] = (counted[row.outcome] ?? 0) + 1;
    expect(counted).toEqual(fixture.counts);
    expect(fixture.counts).toEqual({ accepted: 641, blocked_in_browser: 262, refused_by_server: 432 });
  });

  it('records what the flag page sends for each row', () => {
    const differ = grid
      .map((row, i) => ({ name: row.name, sends: targetingToSend(row.rules, null, false) ?? null, recorded: fixture.rows[i].sends }))
      .filter((r) => JSON.stringify(r.sends) !== JSON.stringify(r.recorded))
      .map((r) => r.name);
    expect(differ).toEqual([]);
  });

  it('a row is blocked in the browser exactly when the pre-save check stops it', () => {
    const wrong = grid
      .map((row, i) => ({ name: row.name, blocked: checkTargeting(row.rules).length > 0, outcome: fixture.rows[i].outcome }))
      .filter((r) => r.blocked !== (r.outcome === 'blocked_in_browser'))
      .map((r) => `${r.name}: ${r.outcome}`);
    expect(wrong).toEqual([]);
  });

  it('every row the server refuses is shown as a refusal naming its group and condition', () => {
    const refused = fixture.rows.filter((r) => r.outcome === 'refused_by_server');
    expect(refused.length).toBe(fixture.counts.refused_by_server);
    const unshown = refused
      .map((row) => {
        const err = new ApiError({
          status: 422,
          detail: [{ loc: ['body', 'targeting_rules'], msg: `Value error, ${row.path}: ${row.reason}`, type: 'value_error' }],
        });
        return { row, shown: describeTargetingSaveError(err) };
      })
      .filter(
        ({ row, shown }) =>
          shown.message !== TARGETING_SAVE_REFUSED ||
          shown.problems?.length !== 1 ||
          !/^Group 1(, Condition 1)?: .+$/.test(shown.problems[0]) ||
          !shown.problems[0].endsWith(`: ${row.reason}`),
      )
      .map(({ row }) => row.name);
    expect(unshown).toEqual([]);
  });
});
