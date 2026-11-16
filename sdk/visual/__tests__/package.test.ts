/**
 * What the package is and what its build writes: the classic script a site
 * loads with <script src>, and the ESM entry. The build runs once, here, from
 * package.json's own build script.
 */
import { execFileSync } from 'child_process';
import { existsSync, readFileSync } from 'fs';
import { join } from 'path';
import { pathToFileURL } from 'url';
import { runInNewContext } from 'vm';

const ROOT = join(__dirname, '..');
const pkg = JSON.parse(readFileSync(join(ROOT, 'package.json'), 'utf8'));
const CLASSIC = join(ROOT, 'dist', 'controlvar-visual.js');
const ESM = join(ROOT, 'dist', 'index.mjs');

beforeAll(() => {
  execFileSync('npm', ['run', '--silent', 'build'], { cwd: ROOT, stdio: 'pipe' });
});

/** Run the classic script with `window` as its global object; return that object. */
function runClassic(window: Record<string, unknown>): Record<string, unknown> {
  window.window = window;
  runInNewContext(readFileSync(CLASSIC, 'utf8'), window, { filename: CLASSIC });
  return window;
}

describe('the package', () => {
  test('is @controlvar/visual, MIT, a 0.x version', () => {
    expect(pkg.name).toBe('@controlvar/visual');
    expect(pkg.license).toBe('MIT');
    expect(pkg.version).toMatch(/^0\.\d+\.\d+$/);
  });

  test('has no runtime dependencies', () => {
    expect(pkg.dependencies).toEqual({});
    expect(pkg.peerDependencies ?? {}).toEqual({});
    expect(pkg.optionalDependencies ?? {}).toEqual({});
  });

  test('names its repository directory', () => {
    expect(pkg.repository).toEqual({
      type: 'git',
      url: 'git+https://github.com/Controlvar/controlvar.git',
      directory: 'sdk/visual',
    });
  });

  test('ships dist/ and the README, and points its entries at the two built files', () => {
    expect(pkg.files).toEqual(['dist', 'README.md']);
    expect(pkg.exports['.'].import).toBe('./dist/index.mjs');
    expect(pkg.exports['./classic']).toBe('./dist/controlvar-visual.js');
  });
});

describe('the build', () => {
  test('writes the classic script and the ESM entry', () => {
    expect(existsSync(CLASSIC)).toBe(true);
    expect(existsSync(ESM)).toBe(true);
  });

  test('the classic script is not a module: no import or export statement', () => {
    const text = readFileSync(CLASSIC, 'utf8');
    expect(text).not.toMatch(/^\s*(import|export)\b/m);
  });

  test("the classic script publishes window.controlvar.visual with package.json's version", () => {
    const window = runClassic({});
    expect(window.controlvar).toEqual({ visual: { version: pkg.version } });
  });

  test('the classic script keeps what the page already has under window.controlvar', () => {
    const other = { mine: true };
    const window = runClassic({ controlvar: { other } });
    expect(window.controlvar).toEqual({ other, visual: { version: pkg.version } });
  });

  test("the ESM entry exports package.json's version", () => {
    const script = `import(${JSON.stringify(pathToFileURL(ESM).href)}).then((m) => process.stdout.write(JSON.stringify(m)))`;
    const out = execFileSync(process.execPath, ['-e', script], { encoding: 'utf8' });
    expect(JSON.parse(out)).toEqual({ version: pkg.version });
  });

  test('the bundle carries no part of package.json but the version', () => {
    for (const file of [CLASSIC, ESM]) {
      const text = readFileSync(file, 'utf8');
      expect(text).not.toContain('devDependencies');
      expect(text).not.toContain(pkg.description);
    }
  });
});
