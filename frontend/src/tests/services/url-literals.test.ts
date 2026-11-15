/**
 * Guard: every `/api/v1/...` URL the dashboard calls must exist on the backend.
 *
 * Scans the dashboard sources (services, components, pages, hooks, contexts —
 * everything but tests and mocks) for string and template literals starting
 * with `/api/v1/`, expands `${CONST}` prefixes declared as
 * `const X = '/api/v1/...'` in the same file, normalises `${expr}` / `{name}`
 * segments to `{param}` and checks each against the paths (and, when the call
 * names one, the HTTP method) in `src/tests/fixtures/openapi.json`.
 *
 * Regenerate the fixture after changing backend routes:
 *   npm run openapi:dump   (= python -m backend.scripts.dump_openapi from the repo root)
 *
 * ## Profiles
 *
 * The dump is taken from a full-profile build and carries 58 paths that only
 * the modules serve. A core backend serves the rest, so the rule "every URL
 * literal exists in the dump" needs a profile, not a single document. Rather
 * than keeping two dumps in sync, one dump is kept and
 * `openapi.module-paths.json` names the modules' subset:
 *
 *   EXPERIMENTLY_PROFILE=core  →  those 58 paths are removed from the
 *                                 document, and only the core tree
 *                                 (`frontend/src`, minus anything the manifest
 *                                 still lists there) is scanned.
 *   anything else (default)    →  the whole document, and both trees:
 *                                 `frontend/src` and `modules/frontend/src`.
 *
 * The modules' sources live under `modules/frontend/src`, so a core scan
 * never sees them at all; which paths count as module paths is still read
 * from `modules-manifest.txt`, the same file `scripts/core_build.sh` deletes,
 * so this test and the build cannot disagree about where the boundary is.
 */
import fs from 'fs';
import path from 'path';

import {
  FIXTURE,
  MODULE_PATHS_FIXTURE,
  MODULES_SRC_ROOT,
  OpenApiDocument,
  PROFILE,
  SRC_ROOT,
  coreDocument,
  extractUrlLiterals,
  findMismatches,
  isModuleSource,
  matchingPaths,
  moduleSourcePrefixes,
  profileSourceFiles,
} from '../helpers/api-literals';

// The scanner and matcher live in `../helpers/api-literals.ts`, shared with
// `query-params.test.ts`; this file holds only the tests.

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe(`frontend /api/v1 URL literals match the backend OpenAPI spec (${PROFILE})`, () => {
  const fullDoc: OpenApiDocument = JSON.parse(fs.readFileSync(FIXTURE, 'utf8'));
  const modulePaths: string[] = JSON.parse(fs.readFileSync(MODULE_PATHS_FIXTURE, 'utf8')).paths;
  const modulePrefixes = moduleSourcePrefixes();

  const doc = PROFILE === 'core' ? coreDocument(fullDoc, modulePaths) : fullDoc;
  const files = profileSourceFiles(PROFILE, modulePrefixes);
  const literals = files.flatMap((f) => extractUrlLiterals(f, fs.readFileSync(f, 'utf8')));

  it('loads a populated OpenAPI fixture (run `npm run openapi:dump` to refresh it)', () => {
    expect(Object.keys(doc.paths).length).toBeGreaterThan(100);
    expect(doc.paths['/api/v1/feature-flags/']).toBeDefined();
    expect(doc.paths['/api/v1/modules']).toBeDefined();
  });

  describe('per-profile fixture', () => {
    it('every path listed as a module path is really in the dump', () => {
      const unknown = modulePaths.filter((p) => !(p in fullDoc.paths));
      expect(unknown).toEqual([]);
    });

    it('the module subset is the 58 paths the modules registration mounts', () => {
      expect(modulePaths).toHaveLength(58);
      expect(modulePaths).toContain('/api/v1/tracking/split-url/routes');
      expect(modulePaths).toContain('/api/v1/tracking/split-url/assign');
      expect(modulePaths).toContain('/api/v1/warehouse/analysis/runs/{run_id}');
      expect(modulePaths).toContain('/api/v1/rbac/roles');
      expect(modulePaths).toContain('/api/v1/workspaces/');
      // Routes whose URL is declared on a core router in every profile (501
      // without the module body) are core paths: a core source may name them,
      // so they must not be stripped from the core document.
      expect(modulePaths).not.toContain('/api/v1/compliance/audit-events');
      expect(modulePaths).not.toContain('/api/v1/compliance/export');
      expect(modulePaths).not.toContain('/api/v1/compliance/reports/{standard}');
      expect(modulePaths).not.toContain('/api/v1/experiments/{experiment_id}/split-url/preview');
      // The profile endpoint itself is core: a core build answers it.
      expect(modulePaths).not.toContain('/api/v1/modules');
    });

    it('the core document is the dump minus exactly those paths', () => {
      const core = coreDocument(fullDoc, modulePaths);
      expect(Object.keys(core.paths)).toHaveLength(Object.keys(fullDoc.paths).length - 58);
      expect(core.paths['/api/v1/rbac/roles']).toBeUndefined();
      expect(core.paths['/api/v1/experiments/']).toBeDefined();
      expect(core.paths['/api/v1/modules']).toBeDefined();
    });

    it('a core document rejects a module URL literal, a full one accepts it', () => {
      const literal = {
        file: 'services/admin.ts',
        line: 125,
        raw: '/api/v1/rbac/roles',
        path: '/api/v1/rbac/roles',
        method: null,
      };
      expect(findMismatches(fullDoc, [literal])).toEqual([]);
      const problems = findMismatches(coreDocument(fullDoc, modulePaths), [literal]);
      expect(problems).toHaveLength(1);
      expect(problems[0]).toContain('no backend route');
    });

    it('reads the modules\' dashboard paths out of modules-manifest.txt', () => {
      // The whole modules tree is one manifest entry; the per-module entries
      // inside it (the workspaces and rbac groups) collapse into that prefix.
      expect(modulePrefixes).toEqual(['modules/frontend']);
      expect(isModuleSource('modules/frontend/src/rbac.ts', modulePrefixes)).toBe(true);
      expect(
        isModuleSource('modules/frontend/src/components/admin/roles/RoleTable.tsx', modulePrefixes),
      ).toBe(true);
      expect(isModuleSource('frontend/src/services/admin.ts', modulePrefixes)).toBe(false);
      // The thin re-export pages are core: with modules/frontend gone they
      // resolve to the stub tree and render the "module not installed"
      // notice, which is the designed core experience for those URLs, not a
      // 404.
      expect(isModuleSource('frontend/src/pages/workspaces/index.tsx', modulePrefixes)).toBe(false);
      expect(isModuleSource('frontend/src/pages/admin/roles.tsx', modulePrefixes)).toBe(false);
      // The stub tree is what a core build ships; it must never be listed.
      expect(isModuleSource('frontend/src/modules-stub/rbac.ts', modulePrefixes)).toBe(false);
      // A prefix must not match a sibling that merely starts with the same text.
      expect(isModuleSource('modules/frontend-other/x.ts', modulePrefixes)).toBe(false);
    });

    it('still honours a manifest entry that has not left frontend/ yet', () => {
      const tmp = path.join(SRC_ROOT, 'tests', 'fixtures', 'manifest.tmp-url-literals.txt');
      fs.writeFileSync(
        tmp,
        [
          '# comment',
          'backend/app/x.py',
          'frontend/src/services/legacy.ts   # inline comment',
          'modules/frontend/src/services/workspaces.ts',
          'frontend/src/components/legacy/',
          '',
        ].join('\n'),
      );
      try {
        const prefixes = moduleSourcePrefixes(tmp);
        expect(prefixes).toEqual([
          'frontend/src/components/legacy',
          'frontend/src/services/legacy.ts',
          'modules/frontend',
        ]);
        expect(isModuleSource('frontend/src/services/legacy.ts', prefixes)).toBe(true);
        expect(isModuleSource('frontend/src/services/legacy.tsx', prefixes)).toBe(false);
        expect(isModuleSource('frontend/src/components/legacy/Table.tsx', prefixes)).toBe(true);
      } finally {
        fs.unlinkSync(tmp);
      }
      expect(moduleSourcePrefixes(path.join(SRC_ROOT, 'no-such-manifest.txt'))).toEqual([
        'modules/frontend',
      ]);
    });

    it('never lists the stub tree, which is what a core build ships', () => {
      expect(isModuleSource('frontend/src/modules-stub', modulePrefixes)).toBe(false);
    });
  });

  it('finds the URL literals the dashboard uses', () => {
    expect(files.length).toBeGreaterThan(20);
    expect(literals.length).toBeGreaterThan(40);
    const paths = new Set(literals.map((l) => l.path));
    expect(paths).toContain('/api/v1/experiments/{param}');
    expect(paths).toContain('/api/v1/feature-flags/{param}/{param}');
    expect(paths).toContain('/api/v1/auth/login');
  });

  it(`scans the modules tree only in a full run (${PROFILE})`, () => {
    const moduleFiles = files.filter((f) => f.startsWith(MODULES_SRC_ROOT));
    const paths = new Set(literals.map((l) => l.path));
    if (PROFILE === 'core') {
      // A core scan is the core tree alone, and nothing in it names a module
      // route: that is the whole point of the seam.
      expect(moduleFiles).toEqual([]);
      const moduleLiterals = Array.from(paths).filter(
        (p) => p.startsWith('/api/v1/rbac/') || p.startsWith('/api/v1/workspaces'),
      );
      expect(moduleLiterals).toEqual([]);
    } else if (fs.existsSync(MODULES_SRC_ROOT)) {
      // A full scan checks the modules' sources too, so a URL typo in
      // modules/frontend/src is caught by the same rule as one in frontend/src.
      expect(moduleFiles.length).toBeGreaterThanOrEqual(12);
      expect(paths).toContain('/api/v1/rbac/roles');
      expect(paths).toContain('/api/v1/workspaces/');
      expect(paths).toContain('/api/v1/workspaces/{param}/members/{param}');
      const moduleTreeLiterals = literals.filter((l) => l.file.startsWith('modules/frontend/src/'));
      expect(moduleTreeLiterals.length).toBeGreaterThanOrEqual(20);
    }
  });

  it('every literal resolves to a backend route with the method it uses', () => {
    const problems = findMismatches(doc, literals);
    expect(problems).toEqual([]);
  });

  describe('extractor', () => {
    const sample = `
      // GET '/api/v1/should-be-ignored'
      /* '/api/v1/also-ignored' */
      const BASE = '/api/v1/things';
      apiFetch<T>(BASE, { query: { a: 1 } });
      apiFetch<T>(BASE, { method: 'POST', json: data });
      apiFetch<T>(\`\${BASE}/\${id}/archive\`, { method: 'POST' });
      apiFetch<T>(\`/api/v1/other/\${encodeURIComponent(key)}?user_id=\${u}\`);
      apiFetch<T>("/api/v1/quoted/{name}", { method: 'DELETE' });
    `;

    it('expands constants, normalises params and detects methods', () => {
      const got = extractUrlLiterals(path.join(SRC_ROOT, 'x.ts'), sample).map((l) => [l.path, l.method]);
      expect(got).toEqual(
        expect.arrayContaining([
          ['/api/v1/things', null],
          ['/api/v1/things', null],
          ['/api/v1/things', 'post'],
          ['/api/v1/things/{param}/archive', 'post'],
          ['/api/v1/other/{param}', null],
          ['/api/v1/quoted/{param}', 'delete'],
        ]),
      );
      expect(got.some(([p]) => String(p).includes('ignored'))).toBe(false);
    });

    it('matches with trailing-slash tolerance and {param} wildcards', () => {
      const mini: OpenApiDocument = {
        paths: {
          '/api/v1/things/': { get: {}, post: {} },
          '/api/v1/things/{thing_id}': { get: {}, put: {} },
          '/api/v1/things/{thing_id}/archive': { post: {} },
        },
      };
      expect(matchingPaths(mini, '/api/v1/things')).toEqual(['/api/v1/things/']);
      expect(matchingPaths(mini, '/api/v1/things/{param}')).toEqual(['/api/v1/things/{thing_id}']);
      expect(matchingPaths(mini, '/api/v1/things/abc/archive')).toEqual(['/api/v1/things/{thing_id}/archive']);
      expect(matchingPaths(mini, '/api/v1/nothing')).toEqual([]);

      const problems = findMismatches(mini, [
        { file: 'a.ts', line: 1, raw: '/api/v1/things/{param}', path: '/api/v1/things/{param}', method: 'delete' },
        { file: 'a.ts', line: 2, raw: '/api/v1/missing', path: '/api/v1/missing', method: null },
        { file: 'a.ts', line: 3, raw: '/api/v1/things', path: '/api/v1/things', method: 'post' },
      ]);
      expect(problems).toHaveLength(2);
      expect(problems[0]).toContain('DELETE');
      expect(problems[1]).toContain('no backend route');
    });
  });
});
