/**
 * The dashboard's side of a flag's split (#2012): what it checks before a
 * request, in the API's words (backend/app/core/flag_values.py), what it sends,
 * how it reads the API's 422 by place, the impact line, the bounded preview,
 * and the words it uses.
 *
 * The impact line is held to the server's own table of weight changes,
 * `RE_WEIGHTINGS` in backend/tests/unit/services/test_flag_split_serving.py,
 * read from that file: each pair of weights, the moves the server's ground-truth
 * copy counted over user-0 .. user-19999 of the key "my-flag", and the line
 * must name exactly those moves.
 */
import fs from 'fs';
import path from 'path';
import { ApiError } from '@/services/api';
import type { FlagSplitEntry } from '@/services/featureFlags';
import {
  AT_LEAST_NOTE,
  MAX_SPLIT_VALUES,
  PREVIEW_CHARS,
  SERVED_VALUES_TOO_LARGE,
  SplitRow,
  VALUES_EQUAL_MESSAGE,
  WEIGHT_MESSAGE,
  checkSplitRows,
  describeSplit,
  impactText,
  isRuleValueMessage,
  newSplitRows,
  preview,
  previewText,
  ruleValueRefusal,
  rowsFromSplit,
  splitMoves,
  splitRefusals,
  splitToSend,
  spreadEvenly,
  totalText,
  weightsTotalMessage,
} from '@/utils/flagSplit';
import { promoteSummary, notCopiedNote } from '@/utils/flagPromotion';
import { SHARED_VALUE_SETTINGS_NOTE } from '@/utils/flagEnvironments';
import { flagTargetingToSend, isEditableFlagTargeting, rulesFromStored } from '@/utils/flagTargeting';
import { isEditableTargeting } from '@/utils/experimentTargeting';

const REPO_ROOT = path.join(__dirname, '..', '..', '..', '..');
const SERVING_TEST = path.join(REPO_ROOT, 'backend', 'tests', 'unit', 'services', 'test_flag_split_serving.py');

const rows = (...items: Array<[string, string]>): SplitRow[] =>
  items.map(([text, weight], i) => ({ key: i + 1, text, weight }));

const THREE: FlagSplitEntry[] = [
  { value: 'red', weight: 34 },
  { value: 'green', weight: 33 },
  { value: 'blue', weight: 33 },
];

describe('the editor starts and spreads', () => {
  it('a new split is three empty values at 34/33/33', () => {
    expect(newSplitRows().map((row) => [row.text, row.weight])).toEqual([
      ['', '34'],
      ['', '33'],
      ['', '33'],
    ]);
  });

  it.each([
    [3, [34, 33, 33]],
    [4, [25, 25, 25, 25]],
    [6, [17, 17, 17, 17, 16, 16]],
    [7, [15, 15, 14, 14, 14, 14, 14]],
    [10, [10, 10, 10, 10, 10, 10, 10, 10, 10, 10]],
  ])('Spread evenly over %i gives the extra share to the first rows', (count, weights) => {
    expect(spreadEvenly(count)).toEqual(weights);
    expect(weights.reduce((a, b) => a + b, 0)).toBe(100);
  });

  it('the Total says what is missing or over', () => {
    expect(totalText(100)).toBe('Total 100%');
    expect(totalText(90)).toBe('Total 90%: 10% more needed');
    expect(totalText(105)).toBe('Total 105%: over by 5%');
  });
});

describe('weights must add up to 100 to save', () => {
  it.each([90, 99, 101])('weights adding up to %i are refused at the Total, in the API words', (total) => {
    const last = String(total - 67);
    const checked = checkSplitRows('string', rows(['red', '34'], ['green', '33'], ['blue', last]));
    expect(checked.errors).toEqual({ total: weightsTotalMessage(total), rows: {} });
    expect(checked.errors?.total).toBe(`Weights must add up to 100; they add up to ${total}.`);
    expect(checked.split).toEqual([]);
  });

  it('weights adding up to 100 are a split, in the order typed', () => {
    const checked = checkSplitRows('string', rows(['red', '34'], ['green', '33'], ['blue', '33']));
    expect(checked).toEqual({ errors: null, split: THREE });
  });

  it.each(['33.5', '-1', '101', 'x', ''])('a weight of %j is refused at its row, in the API words', (weight) => {
    const checked = checkSplitRows('string', rows(['red', '34'], ['green', weight], ['blue', '33']));
    expect(checked.errors?.rows[1]).toEqual({ weight: WEIGHT_MESSAGE });
  });

  it('a value equal to an earlier one is refused at the later row', () => {
    const checked = checkSplitRows('number', rows(['3', '34'], ['4', '33'], ['3.0', '33']));
    expect(checked.errors?.rows).toEqual({ 2: { value: VALUES_EQUAL_MESSAGE } });
  });

  it('a new split of empty strings is refused, so nothing is served until each is typed', () => {
    const checked = checkSplitRows('string', newSplitRows());
    expect(Object.keys(checked.errors?.rows ?? {})).toEqual(['1', '2']);
  });

  it('a value of the wrong type is refused at its row in the value rules words', () => {
    const checked = checkSplitRows('number', rows(['1e400', '34'], ['ten', '33'], ['3', '33']));
    expect(checked.errors?.rows[0]?.value).toBe('value: a number is outside -9007199254740991 to 9007199254740991');
    expect(checked.errors?.rows[1]?.value).toBe('Enter a number, such as 10, -3 or 2.5.');
  });

  it('two values are refused as too few', () => {
    expect(checkSplitRows('string', rows(['a', '50'], ['b', '50'])).errors?.total).toBe(AT_LEAST_NOTE);
  });
});

describe('a save sends the split only when it was changed', () => {
  const loaded = THREE;
  const same = () => rowsFromSplit('string', loaded);

  it('an untouched split is left out', () => {
    expect(splitToSend('string', same(), loaded, false)).toEqual({ kind: 'leave' });
  });

  it('a split changed and changed back is left out', () => {
    expect(splitToSend('string', same(), loaded, true)).toEqual({ kind: 'leave' });
  });

  it('a page loaded from a body without a split never sends one', () => {
    expect(splitToSend('string', newSplitRows(), undefined, true)).toEqual({ kind: 'leave' });
    expect(splitToSend('string', null, undefined, true)).toEqual({ kind: 'leave' });
  });

  it('null is sent only once the user removed a split that was loaded', () => {
    expect(splitToSend('string', null, loaded, true)).toEqual({ kind: 'send', split: null });
    expect(splitToSend('string', null, null, true)).toEqual({ kind: 'leave' });
  });

  it('a changed weight sends the whole split, in its order', () => {
    const changed = same().map((row, i) => ({ ...row, weight: ['50', '25', '25'][i] }));
    expect(splitToSend('string', changed, loaded, true)).toEqual({
      kind: 'send',
      split: [
        { value: 'red', weight: 50 },
        { value: 'green', weight: 25 },
        { value: 'blue', weight: 25 },
      ],
    });
  });

  it('a JSON value whose only change is 1 for true is a change', () => {
    const json: FlagSplitEntry[] = [
      { value: { on: true }, weight: 50 },
      { value: { on: false }, weight: 50 },
      { value: { on: null }, weight: 0 },
    ];
    const edited = rowsFromSplit('json', json);
    edited[0] = { ...edited[0], text: '{"on": 1}' };
    const sent = splitToSend('json', edited, json, true);
    expect(sent.kind).toBe('send');
  });
});

describe("the API's 422, by place", () => {
  const refusal = (detail: unknown[]) => new ApiError({ status: 422, detail, requestId: 'r-1' });

  it('the split as a whole goes to the Total, an entry field to its row, the rest stays', () => {
    const other = { loc: ['body', 'rollout_percentage'], msg: 'too big', type: 'x' };
    const found = splitRefusals(
      refusal([
        { loc: ['body', 'split'], msg: 'Weights must add up to 100; they add up to 90.', type: 'split_total' },
        { loc: ['body', 'split', 2, 'value'], msg: VALUES_EQUAL_MESSAGE, type: 'split_values_equal' },
        { loc: ['body', 'split', 1, 'weight'], msg: WEIGHT_MESSAGE, type: 'split_weight' },
        { loc: ['body', 'split', 0], msg: 'An entry of a split has a value and a weight, and nothing else.', type: 'split_entry' },
        other,
      ]),
    );
    expect(found?.errors).toEqual({
      total: 'Weights must add up to 100; they add up to 90.',
      rows: {
        0: { value: 'An entry of a split has a value and a weight, and nothing else.' },
        1: { weight: WEIGHT_MESSAGE },
        2: { value: VALUES_EQUAL_MESSAGE },
      },
    });
    expect(found?.rest?.detail).toEqual([other]);
    expect(found?.rest?.requestId).toBe('r-1');
  });

  it('an error that names no split is not one', () => {
    expect(splitRefusals(refusal([{ loc: ['body', 'on_value'], msg: 'x', type: 'x' }]))).toBeNull();
    expect(splitRefusals(new Error('x'))).toBeNull();
  });

  it.each([
    ['value must be a number, because value_type is number', true],
    ['value larger than 8192 bytes', true],
    ['value: a number is outside -9007199254740991 to 9007199254740991', true],
    [
      'Value error, value: a rule with no condition matches no user, so its value is never served; add a condition, or remove the value',
      true,
    ],
    [SERVED_VALUES_TOO_LARGE, true],
    ['Group 1, Condition 1: unknown operator', false],
    ['targeting rules: unknown key', false],
  ])('a targeting_rules refusal %j is the rule value: %s', (msg, expected) => {
    expect(isRuleValueMessage(msg)).toBe(expected);
  });

  it("the rule's own value is taken out of a targeting_rules 422, the rest stays", () => {
    const rest = { loc: ['body', 'targeting_rules'], msg: 'Group 1, Condition 1: unknown operator', type: 'x' };
    const found = ruleValueRefusal(
      refusal([{ loc: ['body', 'targeting_rules'], msg: 'Value error, value must be a string, because value_type is string', type: 'value_error' }, rest]),
    );
    expect(found?.message).toBe('value must be a string, because value_type is string');
    expect(found?.rest?.detail).toEqual([rest]);
  });
});

/** `RE_WEIGHTINGS` from the server's serving test, as `[before, after, moves]`. */
function serverReWeightings(): Array<[number[], number[], Record<string, number>]> {
  const source = fs.readFileSync(SERVING_TEST, 'utf8');
  const block = /^RE_WEIGHTINGS = \[\n([\s\S]*?)^\]/m.exec(source);
  if (!block) throw new Error(`no RE_WEIGHTINGS in ${SERVING_TEST}`);
  const line = /^\s*\(\(([\d, ]+)\), \(([\d, ]+)\), \{([^}]*)\}\),$/;
  const out = block[1]
    .split('\n')
    .filter((text) => text.trim() !== '')
    .map((text) => {
      const m = line.exec(text);
      if (!m) throw new Error(`a RE_WEIGHTINGS line this test cannot read: ${text}`);
      const numbers = (s: string) => s.split(',').map((n) => Number(n.trim())).filter((n) => !Number.isNaN(n));
      const moves = Object.fromEntries(
        m[3].split(',').map((pair) => {
          const [key, count] = pair.split(':');
          return [key.trim().replace(/"/g, ''), Number(count)];
        }),
      );
      return [numbers(m[1]), numbers(m[2]), moves] as [number[], number[], Record<string, number>];
    });
  return out;
}

describe('the impact line says exactly who moves', () => {
  // The server test's values, in its order (`VALUES`).
  const VALUES = ['red', 'green', 'blue', 'amber'];
  const split = (weights: number[]): FlagSplitEntry[] => weights.map((weight, i) => ({ value: VALUES[i], weight }));
  const table = serverReWeightings();

  it("reads the server's eight weight changes", () => {
    expect(table).toHaveLength(8);
  });

  it.each(table)('%j to %j: exactly the moves the server counted, each about as many in every 100', (before, after, moved) => {
    const moves = splitMoves(split(before), split(after));
    expect(moves).not.toBeNull();
    const named = Object.fromEntries((moves ?? []).map((m) => [`${m.from}>${m.to}`, m.slots]));
    expect(Object.keys(named).sort()).toEqual(Object.keys(moved).sort());
    // `moved` counts users of 20,000; the line says "about N in every 100".
    for (const [pair, users] of Object.entries(moved)) {
      expect(Math.abs(named[pair] - users / 200)).toBeLessThanOrEqual(1);
    }
    const line = impactText(moves) ?? '';
    for (const move of moves ?? []) {
      expect(line).toContain(`${move.slots} in every 100`);
      expect(line).toContain(`from "${move.from}" to "${move.to}"`);
    }
    expect(line.endsWith('Everyone else keeps their value.')).toBe(true);
  });

  it('one boundary moved: one move, to the raised value (test_moving_one_boundary_...)', () => {
    expect(impactText(splitMoves(split([50, 30, 20]), split([50, 20, 30])))).toBe(
      'About 10 in every 100 users the flag is on for move from "green" to "blue". Everyone else keeps their value.',
    );
  });

  it('two boundaries: both moves named, green included though its weight did not change', () => {
    expect(impactText(splitMoves(split([34, 33, 33]), split([44, 33, 23])))).toBe(
      'About 10 in every 100 users the flag is on for move from "green" to "red", and about 10 in every 100 move from "blue" to "green". Everyone else keeps their value.',
    );
  });

  it('nothing moves: no line', () => {
    expect(impactText(splitMoves(THREE, THREE))).toBeNull();
  });

  it('a change of values, not of weights, has no line', () => {
    const renamed = THREE.map((entry, i) => (i === 1 ? { ...entry, value: 'teal' } : entry));
    expect(splitMoves(THREE, renamed)).toBeNull();
    expect(splitMoves(THREE, THREE.slice(1).concat([{ value: 'red', weight: 34 }]))).toBeNull();
  });
});

describe('a long or JSON value shows in a bounded, labelled preview', () => {
  it('a value of 8192 bytes shows its first 80 characters and says so', () => {
    const long = 'x'.repeat(8190);
    const shown = preview(long);
    expect(shown.text).toHaveLength(PREVIEW_CHARS + 1);
    expect(shown.of).toBe(8192);
    expect(previewText(long)).toBe(`"${'x'.repeat(79)}… (first 80 of 8192 characters)`);
  });

  it('a short value is shown whole, as JSON', () => {
    expect(previewText('blue')).toBe('"blue"');
    expect(previewText({ a: [1, 2] })).toBe('{"a":[1,2]}');
    expect(describeSplit(THREE)).toEqual(['"red": 34%', '"green": 33%', '"blue": 33%']);
    expect(describeSplit(null)).toEqual(['None']);
  });
});

describe("the rule's own value is a key the flag pages alone edit", () => {
  const RULES = {
    logical_operator: 'AND',
    groups: [{ logical_operator: 'AND', conditions: [{ attribute: 'plan', operator: 'equals', value: 'pro' }] }],
  };

  it.each([
    ['string', 'pro-copy'],
    ['number', 3],
    ['json', { tier: 'gold' }],
  ])('a %s flag edits a rule whose value is of its type', (valueType, value) => {
    expect(isEditableFlagTargeting({ ...RULES, value }, valueType)).toBe(true);
  });

  it.each([
    ['no type given', 'pro-copy', undefined],
    ['a boolean flag', true, 'boolean'],
    ['a value of another type', 'three', 'number'],
    ['a null value', null, 'string'],
  ])('%s leaves it read-only', (_name, value, valueType) => {
    expect(isEditableFlagTargeting({ ...RULES, value }, valueType)).toBe(false);
  });

  it('a value on rules with no group leaves them read-only', () => {
    expect(isEditableFlagTargeting({ groups: [], value: 'x' }, 'string')).toBe(false);
  });

  it('the experiment page reads it as a key it does not know', () => {
    expect(isEditableTargeting({ ...RULES, value: 'pro-copy' })).toBe(false);
  });

  it('an untouched rule with a value is not sent; changing what it serves is', () => {
    const stored = { ...RULES, value: 'pro-copy' };
    const rules = rulesFromStored(stored);
    expect(flagTargetingToSend(rules, { own: true, value: 'pro-copy' }, stored, false)).toBeUndefined();
    expect(flagTargetingToSend(rules, { own: false }, stored, false)).toEqual(RULES);
    expect(flagTargetingToSend(rules, { own: true, value: 'other' }, stored, false)).toEqual({ ...RULES, value: 'other' });
    expect(flagTargetingToSend(rulesFromStored(RULES), { own: false }, RULES, false)).toBeUndefined();
  });
});

describe('the words', () => {
  // ux: the things a flag serves are values; "variant" belongs to experiments.
  const BANNED = /\b(variants?|variations?|treatments?|buckets?)\b/i;
  const SOURCES = ['utils/flagSplit.ts', 'components/feature-flags/FlagSplit.tsx'];

  /** The file without its comments: what a user can be shown, and the names around it. */
  function withoutComments(source: string): string {
    return source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/.*$/gm, '$1');
  }

  it.each(SOURCES)('%s uses none of variant, variation, treatment or bucket', (file) => {
    const text = withoutComments(fs.readFileSync(path.join(__dirname, '..', '..', file), 'utf8'));
    const hits = text.split('\n').filter((line) => BANNED.test(line));
    expect(hits).toEqual([]);
  });

  it('nor do the promote dialog and environment notes for a flag with a split', () => {
    for (const text of [promoteSummary('staging', 'production', true), notCopiedNote(true), SHARED_VALUE_SETTINGS_NOTE]) {
      expect(text).not.toMatch(BANNED);
    }
  });

  it('the guard fires on a planted word', () => {
    expect(withoutComments("export const X = 'Pick a variant';")).toMatch(BANNED);
    expect(withoutComments('// a variant in a comment')).not.toMatch(BANNED);
  });

  it('at most ten values', () => {
    expect(MAX_SPLIT_VALUES).toBe(10);
  });
});
