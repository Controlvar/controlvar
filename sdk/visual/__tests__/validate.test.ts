/**
 * The script checks page changes as the server does: on every vector in
 * tests/sdk-contract/visual-changes-vectors.json (generated from the
 * server's validator) it gives the server's answer, its lists and limits are
 * the file's, and its page rules match as the server's do.
 */
import { readFileSync } from 'fs';
import { join } from 'path';
import {
  ALLOWED_ATTRIBUTES,
  ALLOWED_STYLE_FUNCTIONS,
  ALLOWED_STYLE_PROPERTIES,
  FORMAT_VERSION,
  LIMITS,
  MARGIN_PROPERTIES,
  OPS,
} from '../src/rules';
import { ruleMatches } from '../src/pages';
import { normalizeHost, normalizePath, splitUrl } from '../src/url';
import { storedSize, validateChanges } from '../src/validate';

const VECTORS = JSON.parse(
  readFileSync(join(__dirname, '..', '..', '..', 'tests', 'sdk-contract', 'visual-changes-vectors.json'), 'utf8'),
);
const LIMIT_CASES = JSON.parse(readFileSync(join(__dirname, 'limit-cases.json'), 'utf8'));

interface ChangeVector {
  id: string;
  accepted: boolean;
  document: unknown;
}

interface RuleVector {
  id: string;
  host: string;
  path: string;
  rule: { match: 'exact' | 'prefix'; url: string };
  matches: boolean;
}

describe('the lists and limits', () => {
  test('equal the server-generated vectors file', () => {
    expect(FORMAT_VERSION).toBe(VECTORS.format_version);
    expect([...OPS]).toEqual(VECTORS.ops);
    expect([...ALLOWED_ATTRIBUTES]).toEqual(VECTORS.allowed_attributes);
    expect([...ALLOWED_STYLE_PROPERTIES]).toEqual(VECTORS.allowed_style_properties);
    expect([...MARGIN_PROPERTIES]).toEqual(VECTORS.margin_properties);
    expect([...ALLOWED_STYLE_FUNCTIONS]).toEqual(VECTORS.allowed_style_functions);
    expect({ ...LIMITS }).toEqual(VECTORS.limits);
  });
});

describe('every change vector', () => {
  const vectors: ChangeVector[] = VECTORS.changes;

  test('is there, exactly as many as the file counts', () => {
    expect(vectors).toHaveLength(124);
    expect(vectors).toHaveLength(VECTORS.counts.changes);
    expect(vectors.filter((vector) => vector.accepted)).toHaveLength(VECTORS.counts.accepted);
  });

  test('gets the server answer from the script', () => {
    const disagreements = vectors
      .filter((vector) => validateChanges(JSON.parse(JSON.stringify(vector.document))).ok !== vector.accepted)
      .map((vector) => `${vector.id}: server ${vector.accepted ? 'accepts' : 'refuses'}, script ${vector.accepted ? 'refuses' : 'accepts'}`);
    expect(disagreements).toEqual([]);
  });

  test('refused for an unknown operation says so, and for anything else says invalid', () => {
    const reasons = vectors
      .filter((vector) => !vector.accepted)
      .map((vector) => {
        const verdict = validateChanges(vector.document);
        return verdict.ok ? 'accepted' : verdict.reason;
      });
    expect(reasons.filter((reason) => reason === 'accepted')).toEqual([]);
    expect(validateChanges({ version: 1, changes: [{ op: 'set_html', selector: 'a', html: '<b>' }] })).toEqual({
      ok: false,
      reason: 'unsupported_operation',
    });
    expect(validateChanges({ version: 1, changes: [{ selector: 'a' }] })).toEqual({ ok: false, reason: 'invalid' });
  });
});

describe('every page rule vector', () => {
  const vectors: RuleVector[] = VECTORS.page_rules;

  test('is there, the hash-routed one included', () => {
    expect(vectors).toHaveLength(20);
    expect(vectors).toHaveLength(VECTORS.counts.page_rules);
    expect(vectors.find((vector) => vector.id === 'hash-route')).toMatchObject({ path: '/#/cart', matches: false });
  });

  test('gets the server answer from the script', () => {
    const disagreements = vectors
      .filter((vector) => {
        const parts = splitUrl(vector.rule.url);
        const sameHost = normalizeHost(vector.host) === normalizeHost(parts.netloc);
        const matches = sameHost && ruleMatches(vector.path, { match: vector.rule.match, path: normalizePath(parts.path) });
        return matches !== vector.matches;
      })
      .map((vector) => vector.id);
    expect(disagreements).toEqual([]);
  });
});

/** The recipes limit-cases.json describes; the server's test builds the same documents. */
function utf8Bytes(text: string): number {
  return storedSize(text) - 2;
}

function build(testCase: { build: string; unit?: string; count: number }): unknown {
  if (testCase.build === 'text') {
    return { version: 1, changes: [{ op: 'set_text', selector: '#t', text: (testCase.unit as string).repeat(testCase.count) }] };
  }
  if (testCase.build === 'changes') {
    return { version: 1, changes: Array.from({ length: testCase.count }, (_, index) => ({ op: 'hide', selector: `#c${index}` })) };
  }
  const unit = testCase.unit as string;
  const width = utf8Bytes(unit);
  const per = Math.floor(2000 / width);
  const changes: { op: string; selector: string; text: string }[] = [];
  for (;;) {
    const next = [...changes, { op: 'set_text', selector: '#t', text: unit.repeat(per) }];
    if (storedSize({ version: 1, changes: next }) > testCase.count - 2100) break;
    changes.push(next[next.length - 1]);
  }
  const last = { op: 'set_text', selector: '#t', text: '' };
  changes.push(last);
  const rest = testCase.count - storedSize({ version: 1, changes });
  last.text = unit.repeat(Math.floor(rest / width)) + 'a'.repeat(rest % width);
  return { version: 1, changes };
}

interface LimitCase {
  id: string;
  build: string;
  unit?: string;
  count: number;
  accepted: boolean;
}

describe('the limits, at their boundary, in characters of more than one byte', () => {
  const cases: [string, LimitCase][] = LIMIT_CASES.cases.map((testCase: LimitCase) => [testCase.id, testCase]);
  test.each(cases)('%s', (_id, testCase) => {
    const document = build(testCase);
    if (testCase.build === 'bytes') expect(storedSize(document)).toBe(testCase.count);
    expect(validateChanges(document).ok).toBe(testCase.accepted);
  });

  test('there are exactly ten cases', () => {
    expect(LIMIT_CASES.cases).toHaveLength(10);
  });
});
