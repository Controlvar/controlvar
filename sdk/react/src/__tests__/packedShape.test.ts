/**
 * @jest-environment node
 *
 * The shape of the packed package: `@controlvar/react-sdk/ssr` resolves from
 * the tarball `npm pack` makes, and what it loads is server-safe.
 *
 * `ssr` is a physical directory with its own package.json (no `exports` map,
 * so a consumer on any module resolution, including the classic "node" one,
 * finds it). The test builds the package the way the release does, packs it,
 * unpacks the tarball into a scratch node_modules and resolves the subpath
 * from there, so a `files` list that drops `ssr`, a `main` that points at a
 * missing file or an `exports` map added later fails here.
 */
import { execFileSync } from 'child_process';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';

const PKG_DIR = path.resolve(__dirname, '..', '..');

function run(cmd: string, args: string[], cwd: string): string {
  return execFileSync(cmd, args, { cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
}

describe('packed package shape', () => {
  let scratch: string;
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
    const packed = JSON.parse(run('npm', ['pack', '--json', '--pack-destination', out], build));
    const tgz = path.join(out, packed[0].filename);
    tarballFiles = packed[0].files.map((f: { path: string }) => f.path);

    installed = path.join(scratch, 'consumer', 'node_modules', '@controlvar', 'react-sdk');
    fs.mkdirSync(installed, { recursive: true });
    run('tar', ['-xzf', tgz, '-C', installed, '--strip-components=1'], scratch);
  }, 180000);

  afterAll(() => {
    if (scratch) fs.rmSync(scratch, { recursive: true, force: true });
  });

  test('the tarball carries the ssr directory and the built entry it points at', () => {
    expect(tarballFiles).toContain('ssr/package.json');
    expect(tarballFiles).toContain('dist/ssr/index.js');
    expect(tarballFiles).toContain('dist/ssr/index.d.ts');
  });

  test('the package declares no exports map', () => {
    expect(manifest.exports).toBeUndefined();
    expect(manifest.files).toEqual(expect.arrayContaining(['dist', 'ssr', 'README.md']));
  });

  test('ssr/package.json points main and types at files the tarball holds', () => {
    const ssr = JSON.parse(fs.readFileSync(path.join(installed, 'ssr', 'package.json'), 'utf8'));
    expect(fs.existsSync(path.resolve(installed, 'ssr', ssr.main))).toBe(true);
    expect(fs.existsSync(path.resolve(installed, 'ssr', ssr.types))).toBe(true);
  });

  test('@controlvar/react-sdk/ssr resolves from the installed tarball and exports ServerClient', () => {
    const consumer = path.join(scratch, 'consumer');
    const script = `
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
    `;
    fs.writeFileSync(path.join(consumer, 'probe.js'), script);
    const result = JSON.parse(run(process.execPath, ['probe.js'], consumer));
    expect(result.resolved).toContain(path.join('react-sdk', 'dist', 'ssr', 'index.js'));
    expect(result.serverClient).toBe('function');
    // Server-safe: the provider (which calls createContext at import time and
    // fails in a React Server Component) is not in the module graph.
    const names = (result.loaded as string[]).map((f) => f.split(path.sep).join('/'));
    expect(names.filter((f) => /ExperimentationProvider/.test(f))).toEqual([]);
    expect(names.filter((f) => /\/node_modules\/react\//.test(f))).toEqual([]);
  });
});
