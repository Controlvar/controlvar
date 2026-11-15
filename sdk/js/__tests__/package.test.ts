/**
 * What the package is: where it runs, what it needs, what it builds and exports.
 *
 * These read package.json, tsconfig.json and the sources, the files npm and tsc
 * read, so the package's description of itself holds the facts a reader acts on
 * before writing any code.
 */
import { readFileSync, readdirSync } from 'fs';
import { join } from 'path';
import * as sdk from '../src';
import type { AssignResponse, BatchResponse, ErrorReport, FlagEvaluateResponse, TrackBody } from '../src';

const SDK = join(__dirname, '..');
const pkg = JSON.parse(readFileSync(join(SDK, 'package.json'), 'utf8'));
const tsconfig = JSON.parse(readFileSync(join(SDK, 'tsconfig.json'), 'utf8'));
const sources = readdirSync(join(SDK, 'src'))
  .filter(name => name.endsWith('.ts'))
  .map(name => ({ name, text: readFileSync(join(SDK, 'src', name), 'utf8') }));

/** Every module specifier a source file imports or re-exports from. */
function specifiers(text: string): string[] {
  return [...text.matchAll(/(?:^|\n)\s*(?:import|export)\b[^;]*?\bfrom\s+['"]([^'"]+)['"]/g)].map(m => m[1]);
}

describe('the package', () => {
  test('is a 0.x beta release', () => {
    expect(pkg.version.split('.')[0]).toBe('0');
  });

  test('has no runtime dependencies', () => {
    expect(pkg.dependencies ?? {}).toEqual({});
    expect(pkg.peerDependencies ?? {}).toEqual({});
  });

  test('runs on Node 18 or later and in browsers: no source imports a module', () => {
    expect(pkg.engines).toEqual({ node: '>=18' });
    const all = sources.flatMap(({ name, text }) => specifiers(text).map(spec => `${name}: ${spec}`));
    expect(all.length).toBeGreaterThan(5); // the scan sees the relative imports
    expect(all.filter(entry => !/: \.\.?\//.test(entry))).toEqual([]);
  });

  test('builds CommonJS with type declarations into dist/', () => {
    expect(pkg.main).toBe('dist/index.js');
    expect(pkg.types).toBe('dist/index.d.ts');
    expect(pkg.type ?? 'commonjs').toBe('commonjs');
    expect(pkg.files).toEqual(expect.arrayContaining(['dist']));
    expect(pkg.scripts.build).toBe('tsc -p tsconfig.json');
    expect(tsconfig.compilerOptions).toMatchObject({ module: 'commonjs', declaration: true, outDir: './dist', rootDir: './src' });
  });

  test('npm test runs jest and npm run smoke builds and runs the contract smoke', () => {
    expect(pkg.scripts.test).toBe('jest');
    expect(pkg.jest.preset).toBe('ts-jest');
    expect(pkg.scripts.smoke).toBe('npm run build --silent && node examples/contract_smoke.mjs');
  });

  test('exports the client, the error, the hash utilities and the raw wire shapes', () => {
    expect(Object.keys(sdk).sort()).toEqual(
      ['BROWSER_LOCAL_EVALUATION_MESSAGE', 'ExperimentationClient', 'ExperimentationError', 'consistentHash', 'md5Bytes', 'md5Hex'].sort()
    );
    // Type-only exports: this file does not compile if one is missing.
    const wire: [AssignResponse, FlagEvaluateResponse, BatchResponse, TrackBody] = [
      { experiment_key: 'e', user_id: 'u', variant_id: null, variant_name: 'control', is_control: true, configuration: null },
      { key: 'f', enabled: true, config: null },
      { success_count: 0, failure_count: 0, errors: [] },
      { event_type: 'purchase', event_name: 'purchase', user_id: 'u' },
    ];
    expect(wire).toHaveLength(4);
    // reportError's argument is exported too, so callers can type the report they build.
    const report: ErrorReport = { errorType: 'crash', message: 'boom', featureFlagKey: 'f' };
    expect(report.errorType).toBe('crash');
  });

  test('nothing in the SDK calls consistentHash', () => {
    const mentions = sources.filter(({ text }) => /\bconsistentHash\b/.test(text)).map(({ name }) => name);
    expect(mentions).toEqual(expect.arrayContaining(['hash.ts', 'index.ts'])); // the scan sees it
    const callers = sources
      .filter(({ name }) => name !== 'hash.ts')
      .filter(({ text }) => /\bconsistentHash\s*\(/.test(text))
      .map(({ name }) => name);
    expect(callers).toEqual([]);
  });
});
