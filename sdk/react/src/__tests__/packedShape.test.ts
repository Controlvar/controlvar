/**
 * @jest-environment node
 *
 * The shape of the packed package: `@controlvar/react-sdk` and
 * `@controlvar/react-sdk/ssr` resolve from the tarball `npm pack` makes, and
 * what the `ssr` entry loads is server-safe.
 *
 * The package has an `exports` map with exactly `.`, `./ssr` and
 * `./package.json`, each mapping `types` then `default` into the CommonJS
 * `dist`, and keeps the physical `ssr/package.json` for resolvers that ignore
 * `exports` (TypeScript's node10, older tools). What this verifies, on the
 * installed tarball: a Node ES module `import` of `/ssr` and of the root; a
 * CommonJS `require` of both, and that `/ssr` never loads the provider; and
 * TypeScript's own resolver finding `/ssr`'s types under node10, node16
 * (CommonJS and ES module), nodenext (ES module) and bundler resolution. The
 * Next App Router is not built here: a `next build` on the earlier shape found
 * `/ssr` (the plan's P18), and is to be repeated on this shape before the
 * 0.1.0 publish.
 *
 * The test builds the package the way the release does, packs it, unpacks the
 * tarball into a scratch node_modules and resolves from there, so removing the
 * `exports` map or its `./ssr` entry, a `files` list that drops `ssr`, a
 * missing stub or a `main` that disagrees with `exports['.']` fails here.
 */
import { execFileSync } from 'child_process';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import * as ts from 'typescript';

const PKG_DIR = path.resolve(__dirname, '..', '..');

function run(cmd: string, args: string[], cwd: string): string {
  return execFileSync(cmd, args, { cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
}

describe('packed package shape', () => {
  let scratch: string;
  let consumer: string;
  let installed: string;
  let tarballFiles: string[];
  const manifest = JSON.parse(fs.readFileSync(path.join(PKG_DIR, 'package.json'), 'utf8'));

  beforeAll(() => {
    scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'react-sdk-pack-'));
    const build = path.join(scratch, 'build');
    fs.mkdirSync(build);
    // A copy of the package as the release sees it, built fresh, so the test
    // does not depend on a `dist` left behind by an earlier command.
    for (const entry of ['package.json', 'tsconfig.json', 'README.md', 'LICENSE', 'src', 'ssr']) {
      const from = path.join(PKG_DIR, entry);
      if (fs.existsSync(from)) fs.cpSync(from, path.join(build, entry), { recursive: true });
    }
    fs.symlinkSync(path.join(PKG_DIR, 'node_modules'), path.join(build, 'node_modules'));
    run(process.execPath, [path.join(PKG_DIR, 'node_modules', 'typescript', 'bin', 'tsc')], build);

    const out = path.join(scratch, 'out');
    fs.mkdirSync(out);
    const packed = JSON.parse(
      execFileSync('npm', ['pack', '--json', '--pack-destination', out], {
        cwd: build,
        encoding: 'utf8',
        stdio: ['ignore', 'pipe', 'pipe'],
        env: { ...process.env, npm_config_update_notifier: 'false' },
      }),
    );
    const tgz = path.join(out, packed[0].filename);
    tarballFiles = packed[0].files.map((f: { path: string }) => f.path);

    consumer = path.join(scratch, 'consumer');
    installed = path.join(consumer, 'node_modules', '@controlvar', 'react-sdk');
    fs.mkdirSync(installed, { recursive: true });
    // The consumer needs react to resolve: the root entry loads it, and a
    // provider re-exported from the ssr entry must reach the module-graph
    // assertion below rather than crash the probe on a missing module.
    fs.symlinkSync(
      path.join(PKG_DIR, 'node_modules', 'react'),
      path.join(consumer, 'node_modules', 'react'),
    );
    run('tar', ['-xzf', tgz, '-C', installed, '--strip-components=1'], scratch);
  }, 180000);

  afterAll(() => {
    if (scratch) fs.rmSync(scratch, { recursive: true, force: true });
  });

  function probe(file: string, source: string): Record<string, unknown> {
    fs.writeFileSync(path.join(consumer, file), source);
    return JSON.parse(run(process.execPath, [file], consumer));
  }

  test('the tarball carries the ssr directory and the built entry it points at', () => {
    expect(tarballFiles).toContain('ssr/package.json');
    expect(tarballFiles).toContain('dist/ssr/index.js');
    expect(tarballFiles).toContain('dist/ssr/index.d.ts');
  });

  test('the package declares an exports map and still ships dist, ssr and the README', () => {
    expect(manifest.exports).toBeDefined();
    expect(manifest.files).toEqual(expect.arrayContaining(['dist', 'ssr', 'README.md']));
  });

  test('ssr/package.json points main and types at files the tarball holds', () => {
    const ssr = JSON.parse(fs.readFileSync(path.join(installed, 'ssr', 'package.json'), 'utf8'));
    expect(fs.existsSync(path.resolve(installed, 'ssr', ssr.main))).toBe(true);
    expect(fs.existsSync(path.resolve(installed, 'ssr', ssr.types))).toBe(true);
  });

  test('(a) a Node ES module import of @controlvar/react-sdk/ssr gives ServerClient', () => {
    const result = probe(
      'probe-ssr.mjs',
      `
      import { ServerClient } from '@controlvar/react-sdk/ssr';
      console.log(JSON.stringify({
        resolved: import.meta.resolve('@controlvar/react-sdk/ssr'),
        serverClient: typeof ServerClient,
      }));
    `,
    );
    expect(result.resolved).toMatch(/\/react-sdk\/dist\/ssr\/index\.js$/);
    expect(result.serverClient).toBe('function');
  });

  test('(b) a CommonJS require of @controlvar/react-sdk/ssr gives ServerClient and never loads the provider', () => {
    const result = probe(
      'probe-ssr.cjs',
      `
      const Module = require('module');
      const loaded = [];
      const orig = Module._load;
      Module._load = function (request, parent, isMain) {
        const file = Module._resolveFilename(request, parent, isMain);
        loaded.push(file);
        return orig.apply(this, arguments);
      };
      const resolved = require.resolve('@controlvar/react-sdk/ssr');
      const mod = require('@controlvar/react-sdk/ssr');
      console.log(JSON.stringify({
        resolved,
        serverClient: typeof mod.ServerClient,
        loaded,
      }));
    `,
    );
    expect(result.resolved).toContain(path.join('react-sdk', 'dist', 'ssr', 'index.js'));
    expect(result.serverClient).toBe('function');
    // Server-safe: the provider (which calls createContext at import time and
    // fails in a React Server Component) is not in the module graph.
    const names = (result.loaded as string[]).map((f) => f.split(path.sep).join('/'));
    expect(names.filter((f) => /ExperimentationProvider/.test(f))).toEqual([]);
    expect(names.filter((f) => /\/node_modules\/react\//.test(f))).toEqual([]);
  });

  test('(c) the package root imports from a Node ES module and requires from CommonJS', () => {
    const expected = { ServerClient: 'function', ExperimentationProvider: 'function', useFeatureFlag: 'function' };
    const esm = probe(
      'probe-root.mjs',
      `
      import { ServerClient, ExperimentationProvider, useFeatureFlag } from '@controlvar/react-sdk';
      console.log(JSON.stringify({
        resolved: import.meta.resolve('@controlvar/react-sdk'),
        kinds: {
          ServerClient: typeof ServerClient,
          ExperimentationProvider: typeof ExperimentationProvider,
          useFeatureFlag: typeof useFeatureFlag,
        },
      }));
    `,
    );
    expect(esm.resolved).toMatch(/\/react-sdk\/dist\/index\.js$/);
    expect(esm.kinds).toEqual(expected);
    const cjs = probe(
      'probe-root.cjs',
      `
      const mod = require('@controlvar/react-sdk');
      console.log(JSON.stringify({
        resolved: require.resolve('@controlvar/react-sdk'),
        kinds: {
          ServerClient: typeof mod.ServerClient,
          ExperimentationProvider: typeof mod.ExperimentationProvider,
          useFeatureFlag: typeof mod.useFeatureFlag,
        },
      }));
    `,
    );
    expect(cjs.resolved).toContain(path.join('react-sdk', 'dist', 'index.js'));
    expect(cjs.kinds).toEqual(expected);
  });

  test("(d) the exports map has exactly '.', './ssr' and './package.json', and main and types agree with exports['.']", () => {
    // The package.json the tarball ships, not the source copy.
    const shipped = JSON.parse(fs.readFileSync(path.join(installed, 'package.json'), 'utf8'));
    const strip = (p: unknown) => (typeof p === 'string' ? p.replace(/^\.\//, '') : p);
    expect(Object.keys(shipped.exports ?? {}).sort()).toEqual(['.', './package.json', './ssr']);
    expect(strip(shipped.main)).toBe(strip(shipped.exports?.['.']?.default));
    expect(strip(shipped.types)).toBe(strip(shipped.exports?.['.']?.types));
  });

  // (e) TypeScript's own resolver, against the installed tarball: an ES module
  // file under node16 or nodenext must find the types the runtime import loads.
  test.each([
    ['node10', ts.ModuleResolutionKind.Node10, ts.ModuleKind.CommonJS, 'index.ts', undefined],
    ['node16, CommonJS', ts.ModuleResolutionKind.Node16, ts.ModuleKind.Node16, 'index.cts', ts.ModuleKind.CommonJS],
    ['node16, ES module', ts.ModuleResolutionKind.Node16, ts.ModuleKind.Node16, 'index.mts', ts.ModuleKind.ESNext],
    ['nodenext, ES module', ts.ModuleResolutionKind.NodeNext, ts.ModuleKind.NodeNext, 'index.mts', ts.ModuleKind.ESNext],
    ['bundler', ts.ModuleResolutionKind.Bundler, ts.ModuleKind.ESNext, 'index.ts', undefined],
  ] as const)(
    '(e) TypeScript resolves @controlvar/react-sdk/ssr to dist/ssr/index.d.ts under %s',
    (_name, moduleResolution, module, containing, mode) => {
      const result = ts.resolveModuleName(
        '@controlvar/react-sdk/ssr',
        path.join(consumer, containing),
        { moduleResolution, module, strict: true },
        ts.sys,
        undefined,
        undefined,
        mode,
      );
      expect(result.resolvedModule?.resolvedFileName ?? 'UNRESOLVED').toMatch(
        /\/node_modules\/@controlvar\/react-sdk\/dist\/ssr\/index\.d\.ts$/,
      );
    },
  );
});
