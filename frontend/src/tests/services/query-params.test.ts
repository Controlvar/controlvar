/**
 * Guard: every query parameter the dashboard sends is one its API reads.
 *
 * For every `apiFetch(<path>, { ..., query: { ... } })` and every
 * `apiUrl(<path>, { ... })` in the dashboard sources (`frontend/src`, and
 * `modules/frontend/src` in a full run), each query key must be a query
 * parameter of the operation the call resolves to in the checked-in OpenAPI
 * snapshot:
 *
 *   full profile (default)        →  docs/api/openapi-v1.full.json
 *   EXPERIMENTLY_PROFILE=core     →  docs/api/openapi-v1.stable.json
 *                                    (the core API), core tree only
 *
 * A key the API does not declare is dropped by FastAPI without an error, so a
 * renamed or misspelt key is a filter or a page control that silently does
 * nothing (#651: the user list sent `page`, the API reads `skip`).
 *
 * Paths are resolved with the scanner `url-literals.test.ts` uses
 * (`../helpers/api-literals.ts`): string and template literals, `${CONST}`
 * prefixes and bare constants declared as `const X = '/api/v1/...'` in the
 * same file, `{param}` wildcards, trailing-slash tolerance.
 *
 * It fails, naming `file:line`, on:
 *   - a key the operation does not read;
 *   - a path it cannot resolve to a literal;
 *   - a `query` that is not an object literal (a variable, a call), or that
 *     holds a spread or a computed key, and an `apiUrl` query argument of
 *     the same kinds;
 *   - zero, or more than one, operation matching the path and method;
 *   - any `apiFetch`/`apiUrl` path argument with a `?` in its literal text
 *     (a string literal, or the static part of a template, at any depth): a
 *     hand-built query string this guard could not read; use `query:`. A `?`
 *     in code (`${on ? 'enable' : 'disable'}`, `a?.b`) is not a query string.
 *
 * Exactly one file is not scanned: `frontend/src/services/api.ts`, where
 * `apiFetch`, `apiUrl` and `buildQuery` are defined and pass a `query`
 * variable through. Nothing else is exempt.
 *
 * The number of calls read is asserted exactly per profile, and the failure
 * names the call that is new or gone, so a call the scanner stops seeing
 * cannot pass silently. The known-mismatch list is empty and asserted empty.
 *
 * ## Limits (what this does not check)
 *
 *   - Wire keys only. It reads the keys of the `query` object at the call. A
 *     form field the UI drops before it reaches the service (the audit log's
 *     old "User Email" box) is invisible to it.
 *   - No value, type or enum checks: `status_filter: 'bogus'` passes.
 *   - Beta operations may change without a snapshot update, so an unread key
 *     on a beta operation is only warned on, never failed. The beta targets
 *     today are `GET /api/v1/holdout/{holdout_id}/results` (both profiles;
 *     `services/holdouts.test.ts` pins its exact `query: {metric}`) and
 *     `GET /api/v1/auth/sso/login` (full profile); the beta target list is
 *     asserted exactly so a new one is noticed.
 *   - One call's path matches two operations and is resolved by the exact,
 *     one-entry `DISAMBIGUATE` table below; nothing else is.
 *   - An options object passed as a variable is refused, but an options-level
 *     spread (`{ method: 'POST', ...options }`) is not read. The one today,
 *     `ExperimentsService.create`, spreads a `CreateOptions`, whose type has
 *     no `query`: `tsc` refuses `create(data, { query: {...} })` (TS2353).
 *   - A query string carried in a variable (`` `/api/v1/x${qs}` ``) has no
 *     `?` in its text and is not seen, and neither is a call made through
 *     another name (an alias of `apiFetch`, or a raw `fetch`).
 */
import fs from 'fs';
import path from 'path';

import {
  HttpMethod,
  MODULE_PATHS_FIXTURE,
  OpenApiDocument,
  PROFILE,
  REPO_ROOT,
  SRC_ROOT,
  fileConstants,
  lineOf,
  matchingPaths,
  moduleSourcePrefixes,
  normalisePath,
  profileSourceFiles,
  sourceLabel,
  stripComments,
} from '../helpers/api-literals';

const SNAPSHOT = path.join(
  REPO_ROOT,
  'docs',
  'api',
  PROFILE === 'core' ? 'openapi-v1.stable.json' : 'openapi-v1.full.json',
);

/** The transport itself: it forwards a `query` variable. The only file not scanned. */
const TRANSPORT = path.join(SRC_ROOT, 'services', 'api.ts');

/**
 * Every call this guard reads, as `file METHOD path`. A new call, or one the
 * scanner stops seeing, fails the count test with its `file:line`.
 */
const CORE_CALLS = [
  'components/admin/api-keys/ApiKeyTable.tsx GET /api/v1/api-keys',
  'services/admin.ts GET /api/v1/admin/users',
  'services/admin.ts GET /api/v1/audit-logs/',
  'services/admin.ts GET /api/v1/notifications/delivery-log',
  'services/admin.ts POST /api/v1/safety/feature-flags/{feature_flag_id}/rollback',
  'services/experiments.ts DELETE /api/v1/experiments/{experiment_id}',
  'services/experiments.ts GET /api/v1/experiments/',
  'services/experiments.ts GET /api/v1/experiments/analysis/sample-size',
  'services/featureFlags.ts GET /api/v1/feature-flags/',
  'services/featureFlags.ts GET /api/v1/rollout-schedules/',
  'services/holdouts.ts GET /api/v1/holdout/{holdout_id}/results',
  'services/results.ts GET /api/v1/results/{experiment_id}',
  'services/results.ts GET /api/v1/results/{experiment_id}/daily',
  'services/results.ts GET /api/v1/results/{experiment_id}/sample-size',
  'services/segments.ts GET /api/v1/segments',
];
const FULL_CALLS = [...CORE_CALLS, 'modules/frontend/src/services/sso.ts GET /api/v1/auth/sso/login'].sort();
const EXPECTED_CALLS = PROFILE === 'core' ? [...CORE_CALLS].sort() : FULL_CALLS;
const EXPECTED_COUNT = PROFILE === 'core' ? 15 : 16;

/**
 * Calls the matcher reads as more than one operation, and the one each means
 * (#1455, PLAN C11, PE 8). Keyed `METHOD normalised-path`, exactly as
 * `resolveCalls` builds it. ONE entry, written out, and nothing general: a
 * rule that ranked candidates (literal segments over parameters, say) would
 * also resolve the planted `/api/v1/a/${id}` self-test below and make the
 * "operations match" check vacuous.
 *
 * `GET /api/v1/holdout/${id}/results` matches both `/holdout/check/{user_id}`
 * (`check` read as the id) and `/holdout/{holdout_id}/results`; the call
 * means the second. `resolveCalls` refuses an entry whose target is not
 * among the operations that match, and returns how often each entry was used
 * in THAT run, so the "used exactly once" test cannot be met by a self-test
 * after the real call is gone.
 */
const DISAMBIGUATE: Record<string, string> = {
  'GET /api/v1/holdout/{param}/results': '/api/v1/holdout/{holdout_id}/results',
};

/** Beta operations a call targets; unread keys there are warnings. */
const EXPECTED_BETA_TARGETS =
  PROFILE === 'core'
    ? ['GET /api/v1/holdout/{holdout_id}/results']
    : ['GET /api/v1/auth/sso/login', 'GET /api/v1/holdout/{holdout_id}/results'];

/**
 * Calls whose unread keys are accepted for now, as `file METHOD path [key]`.
 * Empty, and asserted empty: a mismatch is fixed, not listed.
 */
const KNOWN_MISMATCHES: string[] = [];

// ---------------------------------------------------------------------------
// A small bracket-aware reader (strings, templates and comments respected)
// ---------------------------------------------------------------------------

/** Index just past the string or template literal opening at `i`. */
function skipString(src: string, i: number): number {
  const quote = src[i];
  let j = i + 1;
  while (j < src.length) {
    const c = src[j];
    if (c === '\\') {
      j += 2;
      continue;
    }
    if (c === quote) return j + 1;
    if (quote === '`' && c === '$' && src[j + 1] === '{') {
      j = closeOf(src, j + 1) + 1;
      continue;
    }
    if (quote !== '`' && c === '\n') return j;
    j++;
  }
  return j;
}

const CLOSERS: Record<string, string> = { '(': ')', '[': ']', '{': '}' };

/** Index of the bracket closing the one at `open`, or -1. */
function closeOf(src: string, open: number): number {
  const stack: string[] = [CLOSERS[src[open]]];
  let i = open + 1;
  while (i < src.length) {
    const c = src[i];
    if (c === "'" || c === '"' || c === '`') {
      i = skipString(src, i);
      continue;
    }
    if (c === '/' && src[i + 1] === '/') {
      const nl = src.indexOf('\n', i);
      i = nl === -1 ? src.length : nl;
      continue;
    }
    if (c === '/' && src[i + 1] === '*') {
      const end = src.indexOf('*/', i + 2);
      i = end === -1 ? src.length : end + 2;
      continue;
    }
    if (c in CLOSERS) stack.push(CLOSERS[c]);
    else if (c === ')' || c === ']' || c === '}') {
      if (stack.pop() !== c) return -1;
      if (stack.length === 0) return i;
    }
    i++;
  }
  return -1;
}

interface Piece {
  text: string;
  /** Offset of `text` in the source. */
  at: number;
}

/** Split `src[from, to)` on top-level commas; each piece trimmed, empty ones dropped. */
function splitTopLevel(src: string, from: number, to: number): Piece[] {
  const out: Piece[] = [];
  let start = from;
  const push = (end: number) => {
    // A piece starts at its code, past any whitespace and leading `//` comment lines.
    let at = start;
    for (;;) {
      while (at < end && /\s/.test(src[at])) at++;
      if (src.startsWith('//', at)) {
        const nl = src.indexOf('\n', at);
        at = nl === -1 || nl > end ? end : nl;
        continue;
      }
      break;
    }
    const raw = src.slice(at, end);
    if (codeOf(raw)) out.push({ text: raw.trim(), at });
  };
  let i = from;
  while (i < to) {
    const c = src[i];
    if (c === "'" || c === '"' || c === '`') {
      i = skipString(src, i);
      continue;
    }
    if (c === '/' && src[i + 1] === '/') {
      const nl = src.indexOf('\n', i);
      i = nl === -1 ? to : nl;
      continue;
    }
    if (c in CLOSERS) {
      const close = closeOf(src, i);
      if (close === -1) return out;
      i = close + 1;
      continue;
    }
    if (c === ',') {
      push(i);
      start = i + 1;
    }
    i++;
  }
  push(to);
  return out;
}

/** Strip a trailing `// comment` the bracket reader leaves on a piece. */
function codeOf(text: string): string {
  return text.replace(/\/\/[^\n]*$/gm, '').trim();
}

type Property =
  | { kind: 'named'; key: string; value: string; at: number }
  | { kind: 'shorthand'; key: string; at: number }
  | { kind: 'spread'; at: number }
  | { kind: 'computed'; at: number }
  | { kind: 'other'; at: number };

/** The top-level properties of the object literal `text` (which starts with `{`). */
function properties(src: string, open: number): Property[] {
  const close = closeOf(src, open);
  if (close === -1) return [{ kind: 'other', at: open }];
  return splitTopLevel(src, open + 1, close).map(({ text, at }): Property => {
    const code = codeOf(text);
    if (code.startsWith('...')) return { kind: 'spread', at };
    if (code.startsWith('[')) return { kind: 'computed', at };
    const named = code.match(/^(?:([A-Za-z_$][\w$]*)|'([^']*)'|"([^"]*)")\s*:\s*([\s\S]*)$/);
    if (named) return { kind: 'named', key: named[1] ?? named[2] ?? named[3], value: named[4], at };
    if (/^[A-Za-z_$][\w$]*$/.test(code)) return { kind: 'shorthand', key: code, at };
    return { kind: 'other', at };
  });
}

// ---------------------------------------------------------------------------
// Call extraction
// ---------------------------------------------------------------------------

export interface QueryCall {
  file: string;
  line: number;
  fn: 'apiFetch' | 'apiUrl';
  /** The path as written, constants expanded; null when it cannot be resolved. */
  raw: string | null;
  method: HttpMethod;
  /** Query keys; null when the call carries no query. */
  keys: string[] | null;
}

export interface Extraction {
  calls: QueryCall[];
  problems: string[];
}

/** Index after a `<...>` type argument list starting at `i` (angle brackets nest; `=>` is not a closer). */
function skipTypeArgs(src: string, i: number): number {
  let depth = 0;
  let j = i;
  while (j < src.length) {
    const c = src[j];
    if (c === '<') depth++;
    else if (c === '>' && src[j - 1] !== '=') {
      depth--;
      if (depth === 0) return j + 1;
    } else if (c === "'" || c === '"' || c === '`') {
      j = skipString(src, j);
      continue;
    } else if (c in CLOSERS) {
      const close = closeOf(src, j);
      if (close === -1) return j;
      j = close + 1;
      continue;
    }
    j++;
  }
  return j;
}

/**
 * The literal text in a path argument: the contents of its string literals and
 * the static parts of its templates, at any depth. A `?` there is a hand-built
 * query string; a `?` in code (`${on ? 'enable' : 'disable'}`, `a?.b`) is not.
 */
export function literalText(code: string): string {
  let out = '';
  let i = 0;
  while (i < code.length) {
    const c = code[i];
    if (c === "'" || c === '"') {
      const end = skipString(code, i);
      out += code.slice(i + 1, end - 1);
      i = end;
    } else if (c === '`') {
      let j = i + 1;
      while (j < code.length && code[j] !== '`') {
        if (code[j] === '\\') {
          out += code.slice(j, j + 2);
          j += 2;
        } else if (code[j] === '$' && code[j + 1] === '{') {
          const close = closeOf(code, j + 1);
          if (close === -1) return out + code.slice(j);
          out += literalText(code.slice(j + 2, close));
          j = close + 1;
        } else {
          out += code[j];
          j++;
        }
      }
      i = j + 1;
    } else {
      i++;
    }
  }
  return out;
}

/** The literal a path argument stands for, or null. */
function resolvePath(text: string, constants: Map<string, string>): string | null {
  const code = codeOf(text);
  const quoted = code.match(/^(['"])(\/api\/v1\/[^'"\n]*)\1$/);
  if (quoted) return quoted[2];
  const template = code.match(/^`([^`]*)`$/);
  if (template) {
    const body = template[1];
    if (body.startsWith('/api/v1/')) return body;
    const prefix = body.match(/^\$\{([A-Za-z_$][\w$]*)\}/);
    if (prefix && constants.has(prefix[1])) return constants.get(prefix[1]) + body.slice(prefix[0].length);
    return null;
  }
  if (/^[A-Za-z_$][\w$]*$/.test(code)) return constants.get(code) ?? null;
  return null;
}

/** The keys of a query object literal at `open`, or a problem. */
function queryKeys(src: string, open: number, where: (at: number) => string): { keys: string[] } | { problem: string } {
  const keys: string[] = [];
  for (const prop of properties(src, open)) {
    if (prop.kind === 'named' || prop.kind === 'shorthand') keys.push(prop.key);
    else
      return {
        problem: `${where(prop.at)}  cannot read query keys: ${
          prop.kind === 'spread' ? 'a spread' : prop.kind === 'computed' ? 'a computed key' : 'an unreadable entry'
        } in the query object (write every key out)`,
      };
  }
  return { keys };
}

/** Every `apiFetch` / `apiUrl` call in one file that this guard reads, plus what it could not read. */
export function extractQueryCalls(file: string, rawSource: string): Extraction {
  const src = stripComments(rawSource);
  const label = sourceLabel(file);
  const constants = fileConstants(src);
  const calls: QueryCall[] = [];
  const problems: string[] = [];
  const where = (at: number) => `${label}:${lineOf(src, at)}`;

  const callRe = /\b(apiFetch|apiUrl)\b/g;
  let m: RegExpExecArray | null;
  while ((m = callRe.exec(src)) !== null) {
    const fn = m[1] as QueryCall['fn'];
    let i = m.index + fn.length;
    while (/\s/.test(src[i] ?? '')) i++;
    if (src[i] === '<') {
      i = skipTypeArgs(src, i);
      while (/\s/.test(src[i] ?? '')) i++;
    }
    // A mention that is not a call (an import, a type position) is not read.
    if (src[i] !== '(') continue;
    const close = closeOf(src, i);
    if (close === -1) {
      problems.push(`${where(m.index)}  cannot read the ${fn}(...) call`);
      continue;
    }
    const args = splitTopLevel(src, i + 1, close);
    callRe.lastIndex = i + 1;
    if (args.length === 0) {
      problems.push(`${where(m.index)}  ${fn}() with no path`);
      continue;
    }
    const pathArg = args[0];
    if (literalText(codeOf(pathArg.text)).includes('?')) {
      problems.push(
        `${where(pathArg.at)}  ${fn} path ${codeOf(pathArg.text)} contains "?": a hand-built query string; pass the keys as query: { ... }`,
      );
      continue;
    }
    // apiFetch(path) with no options carries no query.
    if (fn === 'apiFetch' && args.length < 2) continue;
    const raw = resolvePath(pathArg.text, constants);

    // apiUrl(path, query): the query is the second argument and the call is a GET navigation.
    // apiFetch(path, options): the query is `options.query`; the method is `options.method`.
    let method: HttpMethod = 'get';
    let queryAt: number | null = null;
    let queryProblem: string | null = null;
    const second = args[1];
    if (fn === 'apiUrl') {
      if (second) {
        if (codeOf(second.text).startsWith('{')) queryAt = second.at;
        else queryProblem = `${where(second.at)}  cannot read query keys: apiUrl query ${codeOf(second.text)} is not an object literal`;
      }
    } else if (second) {
      if (!codeOf(second.text).startsWith('{')) {
        problems.push(
          `${where(second.at)}  cannot read apiFetch options ${codeOf(second.text)}: not an object literal (write the options, and any query, at the call)`,
        );
        continue;
      }
      for (const prop of properties(src, second.at)) {
        if (prop.kind === 'shorthand' && prop.key === 'query') {
          queryProblem = `${where(prop.at)}  cannot read query keys: query is a variable, not an object literal`;
        } else if (prop.kind === 'named' && prop.key === 'method') {
          const mm = codeOf(prop.value).match(/^['"](GET|POST|PUT|PATCH|DELETE)['"]$/);
          if (mm) method = mm[1].toLowerCase() as HttpMethod;
          else problems.push(`${where(prop.at)}  cannot read the method ${codeOf(prop.value)}`);
        } else if (prop.kind === 'named' && prop.key === 'query') {
          let valueAt = src.indexOf(':', prop.at) + 1;
          while (/\s/.test(src[valueAt] ?? '')) valueAt++;
          if (src[valueAt] === '{') queryAt = valueAt;
          else queryProblem = `${where(prop.at)}  cannot read query keys: query ${codeOf(prop.value)} is not an object literal`;
        } else if (prop.kind === 'computed') {
          queryProblem = `${where(prop.at)}  cannot read apiFetch options: a computed key`;
        }
      }
      // An apiFetch with no query is not this guard's business.
      if (queryAt === null && queryProblem === null) continue;
    }

    if (raw === null) {
      problems.push(`${where(pathArg.at)}  cannot resolve the ${fn} path ${codeOf(pathArg.text)} to a literal`);
      continue;
    }
    if (queryProblem) {
      problems.push(queryProblem);
      continue;
    }
    let keys: string[] | null = null;
    if (queryAt !== null) {
      const got = queryKeys(src, queryAt, where);
      if ('problem' in got) {
        problems.push(got.problem);
        continue;
      }
      keys = got.keys;
    }
    calls.push({ file: label, line: lineOf(src, m.index), fn, raw, method, keys });
  }
  return { calls, problems };
}

// ---------------------------------------------------------------------------
// Matching against the snapshot
// ---------------------------------------------------------------------------

interface Operation {
  parameters?: Array<{ in?: string; name?: string; $ref?: string }>;
  'x-stability'?: string;
}

export interface Resolved {
  call: QueryCall;
  /** `METHOD /openapi/path`. */
  target: string;
  beta: boolean;
  unread: string[];
}

export function resolveCalls(
  doc: OpenApiDocument,
  calls: QueryCall[],
  disambiguate: Record<string, string> = {},
): { resolved: Resolved[]; problems: string[]; used: Record<string, number> } {
  const resolved: Resolved[] = [];
  const problems: string[] = [];
  // Per run: each entry starts at 0 here, never in a module-level counter.
  const used: Record<string, number> = {};
  for (const key of Object.keys(disambiguate)) used[key] = 0;
  for (const call of calls) {
    const where = `${call.file}:${call.line}`;
    const normalised = normalisePath(call.raw as string);
    let candidates = matchingPaths(doc, normalised).filter((p) => call.method in doc.paths[p]);
    const key = `${call.method.toUpperCase()} ${normalised}`;
    if (candidates.length > 1 && key in disambiguate) {
      const target = disambiguate[key];
      if (!candidates.includes(target)) {
        problems.push(
          `${where}  ${key}  →  DISAMBIGUATE names ${target}, which is not one of the ${candidates.length} operations that match: ${candidates.join(', ')}`,
        );
        continue;
      }
      used[key] += 1;
      candidates = [target];
    }
    if (candidates.length !== 1) {
      problems.push(
        `${where}  ${call.method.toUpperCase()} ${call.raw}  →  ${candidates.length} operations match` +
          (candidates.length ? `: ${candidates.join(', ')}` : ''),
      );
      continue;
    }
    const p = candidates[0];
    const item = doc.paths[p] as Record<string, unknown>;
    const op = item[call.method] as Operation;
    const params = [
      ...((item.parameters as Operation['parameters']) ?? []),
      ...(op.parameters ?? []),
    ];
    if (params.some((x) => x.$ref)) {
      problems.push(`${where}  ${p} has a $ref parameter this guard does not resolve`);
      continue;
    }
    const read = new Set(params.filter((x) => x.in === 'query').map((x) => x.name as string));
    const unread = (call.keys ?? []).filter((k) => !read.has(k)).sort();
    resolved.push({ call, target: `${call.method.toUpperCase()} ${p}`, beta: op['x-stability'] === 'beta', unread });
  }
  return { resolved, problems, used };
}

function readsOf(doc: OpenApiDocument, target: string): string[] {
  const [method, p] = target.split(' ');
  const item = doc.paths[p] as Record<string, Operation>;
  return (item[method.toLowerCase()].parameters ?? [])
    .filter((x) => x.in === 'query')
    .map((x) => x.name as string)
    .sort();
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe(`every query parameter the dashboard sends is one its API reads (${PROFILE})`, () => {
  const doc: OpenApiDocument = JSON.parse(fs.readFileSync(SNAPSHOT, 'utf8'));
  const files = profileSourceFiles(PROFILE, moduleSourcePrefixes()).filter((f) => f !== TRANSPORT);
  const extractions = files.map((f) => extractQueryCalls(f, fs.readFileSync(f, 'utf8')));
  const calls = extractions.flatMap((e) => e.calls);
  const readProblems = extractions.flatMap((e) => e.problems);
  const { resolved, problems: matchProblems, used } = resolveCalls(doc, calls, DISAMBIGUATE);
  const signature = (r: Resolved) => `${r.call.file} ${r.target}`;

  // PE 8 (c): the table's keys, written out, so an entry added for a call
  // that does not exist, or one quietly removed, fails here by name.
  it('the DISAMBIGUATE table holds exactly one entry, for the holdout results call', () => {
    expect(Object.keys(DISAMBIGUATE)).toEqual(['GET /api/v1/holdout/{param}/results']);
    expect(DISAMBIGUATE['GET /api/v1/holdout/{param}/results']).toBe('/api/v1/holdout/{holdout_id}/results');
  });

  // PE 8 (b): counted in THIS run of resolveCalls over the dashboard's calls.
  it('every DISAMBIGUATE entry is used by exactly one call', () => {
    expect(used).toEqual(Object.fromEntries(Object.keys(DISAMBIGUATE).map((key) => [key, 1])));
  });

  it('reads the snapshot for this profile', () => {
    expect(Object.keys(doc.paths).length).toBeGreaterThan(100);
    expect(doc.paths['/api/v1/admin/users']).toBeDefined();
    const modulePaths: string[] = JSON.parse(fs.readFileSync(MODULE_PATHS_FIXTURE, 'utf8')).paths;
    const present = modulePaths.filter((p) => p in doc.paths);
    if (PROFILE === 'core') expect(present).toEqual([]);
    else expect(present).toHaveLength(modulePaths.length);
  });

  it('excludes exactly one file, the transport', () => {
    expect(fs.existsSync(TRANSPORT)).toBe(true);
    const all = profileSourceFiles(PROFILE, moduleSourcePrefixes());
    expect(all.filter((f) => !files.includes(f)).map((f) => path.relative(REPO_ROOT, f))).toEqual([
      'frontend/src/services/api.ts',
    ]);
  });

  it('can read every apiFetch / apiUrl call that carries a query, and every path', () => {
    expect([...readProblems, ...matchProblems]).toEqual([]);
  });

  it(`reads exactly ${EXPECTED_COUNT} calls, the ones it is known to read`, () => {
    const found = resolved.map(signature).sort();
    // Compared as multisets, so a second call to an operation already listed
    // is reported as new, with its file:line.
    const remaining = new Map<string, number>();
    for (const s of EXPECTED_CALLS) remaining.set(s, (remaining.get(s) ?? 0) + 1);
    const newCalls: string[] = [];
    for (const r of resolved) {
      const left = remaining.get(signature(r)) ?? 0;
      if (left > 0) remaining.set(signature(r), left - 1);
      else newCalls.push(`${r.call.file}:${r.call.line} ${r.target}`);
    }
    const gone = EXPECTED_CALLS.filter((s) => {
      const left = remaining.get(s) ?? 0;
      if (left === 0) return false;
      remaining.set(s, left - 1);
      return true;
    });
    // A new call is added to CORE_CALLS / FULL_CALLS above; a call that is gone
    // means the scanner stopped seeing it, or it was removed on purpose.
    expect({ newCalls, gone }).toEqual({ newCalls: [], gone: [] });
    expect(found).toEqual(EXPECTED_CALLS);
    expect(resolved).toHaveLength(EXPECTED_COUNT);
  });

  it('the known-mismatch list is empty', () => {
    expect(KNOWN_MISMATCHES).toEqual([]);
  });

  it('every query key is a query parameter of the operation it calls', () => {
    const unread: string[] = [];
    for (const r of resolved) {
      if (r.beta || r.unread.length === 0) continue;
      const entry = `${r.call.file}:${r.call.line} ${r.target} unread [${r.unread.join(', ')}]; API reads [${readsOf(doc, r.target).join(', ')}]`;
      if (!KNOWN_MISMATCHES.includes(`${r.call.file} ${r.target} [${r.unread.join(', ')}]`)) unread.push(entry);
    }
    expect(unread).toEqual([]);
  });

  it('only warns on beta operations, and knows which ones it targets', () => {
    const beta = resolved.filter((r) => r.beta);
    for (const r of beta) {
      if (r.unread.length) {
        console.warn(
          `${r.call.file}:${r.call.line} ${r.target} (beta) sends [${r.unread.join(', ')}], which the snapshot does not list`,
        );
      }
    }
    expect(Array.from(new Set(beta.map((r) => r.target))).sort()).toEqual(EXPECTED_BETA_TARGETS);
  });

  describe('reader', () => {
    const file = path.join(SRC_ROOT, 'services', 'sample.ts');
    const read = (body: string) => extractQueryCalls(file, body);

    it('reads keys, shorthand keys, quoted keys, constants and methods', () => {
      const { calls, problems } = read(`
        const BASE = '/api/v1/things';
        apiFetch<Record<string, Thing[]>>(BASE, { query: { search, limit: 5, 'sort-by': x } });
        apiFetch<T>(\`\${BASE}/\${id}\`, {
          method: 'DELETE', // trailing comment, with an apostrophe: don't
          query: { key: id },
        });
        apiFetch(\`\${BASE}/\${id}\`, { method: 'PUT', json: data });
        apiUrl('/api/v1/go', { a: 1 });
      `);
      expect(problems).toEqual([]);
      expect(calls.map((c) => [c.fn, c.method, c.raw, c.keys, c.line])).toEqual([
        ['apiFetch', 'get', '/api/v1/things', ['search', 'limit', 'sort-by'], 3],
        ['apiFetch', 'delete', '/api/v1/things/${id}', ['key'], 4],
        ['apiUrl', 'get', '/api/v1/go', ['a'], 9],
      ]);
    });

    it('refuses what it cannot read, naming file:line', () => {
      const cases: Array<[string, string]> = [
        ['apiFetch(BASE, { query: someVar });', 'services/sample.ts:1  cannot read query keys: query someVar is not an object literal'],
        ['apiFetch(BASE, { query });', 'services/sample.ts:1  cannot read query keys: query is a variable'],
        ['apiFetch(BASE, { query: { ...rest } });', 'services/sample.ts:1  cannot read query keys: a spread'],
        ['apiFetch(BASE, { query: { [k]: 1 } });', 'services/sample.ts:1  cannot read query keys: a computed key'],
        ['apiFetch(BASE, opts);', 'services/sample.ts:1  cannot read apiFetch options opts'],
        ['apiFetch(UNKNOWN, { query: { a: 1 } });', 'services/sample.ts:1  cannot resolve the apiFetch path UNKNOWN'],
        ['apiUrl(somePath, q);', 'services/sample.ts:1  cannot resolve the apiUrl path somePath'],
        ["apiUrl('/api/v1/x', q);", 'services/sample.ts:1  cannot read query keys: apiUrl query q'],
        ['apiFetch(`/api/v1/api-keys?bogus=1`);', 'services/sample.ts:1  apiFetch path `/api/v1/api-keys?bogus=1` contains "?"'],
        ["apiFetch(`${BASE}${q ? `?search=${q}` : ''}`);", 'contains "?"'],
        ["apiUrl('/api/v1/x?a=1');", 'apiUrl path \'/api/v1/x?a=1\' contains "?"'],
      ];
      for (const [body, expected] of cases) {
        const { calls, problems } = read(`const BASE = '/api/v1/things'; ${body}`);
        expect(calls).toEqual([]);
        expect(problems).toHaveLength(1);
        expect(problems[0]).toContain(expected);
      }
    });

    it('ignores an apiFetch with no query, a mention that is not a call, and comments', () => {
      const { calls, problems } = read(`
        import { apiFetch, apiUrl } from '@/services/api';
        // apiFetch(UNKNOWN, { query: { a: 1 } });
        /* apiUrl(nowhere, q) */
        apiFetch(somePath, { method: 'POST' });
        apiFetch(somePath);
        apiFetch(\`\${BASE}/\${id}/\${on ? 'enable' : 'disable'}\`, { method: 'POST' });
        apiFetch(\`/api/v1/x/\${a?.b}\`);
      `);
      expect(calls).toEqual([]);
      expect(problems).toEqual([]);
    });

    it('fails zero or several matching operations, and names unread keys', () => {
      const mini: OpenApiDocument = {
        paths: {
          '/api/v1/a/{x}': { get: { parameters: [{ in: 'query', name: 'skip' }] } },
          '/api/v1/a/b': { get: {} },
          '/api/v1/c': { get: { parameters: [{ in: 'query', name: 'limit' }, { in: 'path', name: 'page' }] } },
        },
      };
      const call = (raw: string, keys: string[], method: HttpMethod = 'get'): QueryCall => ({
        file: 'f.ts',
        line: 7,
        fn: 'apiFetch',
        raw,
        method,
        keys,
      });
      const { resolved: got, problems } = resolveCalls(mini, [
        call('/api/v1/a/${id}', ['skip']),
        call('/api/v1/nowhere', []),
        call('/api/v1/c', [], 'post'),
        call('/api/v1/c/', ['page', 'limit']),
      ]);
      expect(problems).toEqual([
        'f.ts:7  GET /api/v1/a/${id}  →  2 operations match: /api/v1/a/{x}, /api/v1/a/b',
        'f.ts:7  GET /api/v1/nowhere  →  0 operations match',
        'f.ts:7  POST /api/v1/c  →  0 operations match',
      ]);
      expect(got.map((r) => [r.target, r.unread])).toEqual([['GET /api/v1/c', ['page']]]);
    });

    // PE 8 (a) and (b) on a document of its own: a table entry picks one of
    // the operations the matcher found, never one it did not, and counts per
    // run. The planted `/api/v1/a/${id}` above stays ambiguous without it.
    describe('the DISAMBIGUATE table', () => {
      const mini: OpenApiDocument = {
        paths: {
          '/api/v1/h/check/{user_id}': { get: {} },
          '/api/v1/h/{h_id}/results': { get: { parameters: [{ in: 'query', name: 'metric' }] } },
          '/api/v1/h/{h_id}': { put: {} },
        },
      };
      const call = (raw: string): QueryCall => ({ file: 'f.ts', line: 3, fn: 'apiFetch', raw, method: 'get', keys: ['metric'] });

      it('without an entry the call is ambiguous', () => {
        const { resolved: got, problems, used } = resolveCalls(mini, [call('/api/v1/h/${id}/results')]);
        expect(got).toEqual([]);
        expect(problems).toEqual([
          'f.ts:3  GET /api/v1/h/${id}/results  →  2 operations match: /api/v1/h/check/{user_id}, /api/v1/h/{h_id}/results',
        ]);
        expect(used).toEqual({});
      });

      it('an entry resolves it to the operation it names, and counts the use in this run only', () => {
        const table = { 'GET /api/v1/h/{param}/results': '/api/v1/h/{h_id}/results' };
        const first = resolveCalls(mini, [call('/api/v1/h/${id}/results')], table);
        expect(first.problems).toEqual([]);
        expect(first.resolved.map((r) => [r.target, r.unread])).toEqual([['GET /api/v1/h/{h_id}/results', []]]);
        expect(first.used).toEqual({ 'GET /api/v1/h/{param}/results': 1 });
        const second = resolveCalls(mini, [], table);
        expect(second.used).toEqual({ 'GET /api/v1/h/{param}/results': 0 });
      });

      it('an entry naming an operation the matcher did not find is refused', () => {
        const table = { 'GET /api/v1/h/{param}/results': '/api/v1/h/{h_id}' };
        const { resolved: got, problems } = resolveCalls(mini, [call('/api/v1/h/${id}/results')], table);
        expect(got).toEqual([]);
        expect(problems).toEqual([
          'f.ts:3  GET /api/v1/h/{param}/results  →  DISAMBIGUATE names /api/v1/h/{h_id}, which is not one of the 2 operations that match: /api/v1/h/check/{user_id}, /api/v1/h/{h_id}/results',
        ]);
      });

      it('an entry never touches a call that is not ambiguous, nor the planted /a/${id} one', () => {
        const table = { 'GET /api/v1/h/{param}/results': '/api/v1/h/{h_id}/results' };
        const planted: OpenApiDocument = {
          paths: { '/api/v1/a/{x}': { get: {} }, '/api/v1/a/b': { get: {} } },
        };
        const { problems, used } = resolveCalls(planted, [call('/api/v1/a/${id}')], table);
        expect(problems).toEqual(['f.ts:3  GET /api/v1/a/${id}  →  2 operations match: /api/v1/a/{x}, /api/v1/a/b']);
        expect(used).toEqual({ 'GET /api/v1/h/{param}/results': 0 });
      });
    });
  });
});
