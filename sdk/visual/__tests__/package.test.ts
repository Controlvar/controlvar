/**
 * What the package is and what its build writes: the classic script a site
 * loads with <script src>, and the ESM entry. The build runs once, here, from
 * package.json's own build script.
 */
import { execFileSync } from 'child_process';
import { existsSync, readFileSync } from 'fs';
import { join } from 'path';
import { pathToFileURL } from 'url';
import { CLASSIC, runClassic } from './classic';

const ROOT = join(__dirname, '..');
const pkg = JSON.parse(readFileSync(join(ROOT, 'package.json'), 'utf8'));
const ESM = join(ROOT, 'dist', 'index.mjs');
const TAG = { 'data-api-url': 'https://cv.example', 'data-api-key': 'test-website-key' };

beforeAll(() => {
  execFileSync('npm', ['run', '--silent', 'build'], { cwd: ROOT, stdio: 'pipe' });
});

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
    const page = runClassic('https://shop.example/', TAG);
    expect(JSON.parse(JSON.stringify((page.window as unknown as { controlvar: unknown }).controlvar))).toEqual({
      visual: { version: pkg.version },
    });
    expect(page.console).toEqual([]);
    page.close();
  });

  test('the classic script keeps what the page already has under window.controlvar', () => {
    const page = runClassic('https://shop.example/', TAG, (window) => {
      (window as unknown as { controlvar: unknown }).controlvar = { other: { mine: true } };
    });
    expect(JSON.parse(JSON.stringify((page.window as unknown as { controlvar: unknown }).controlvar))).toEqual({
      other: { mine: true },
      visual: { version: pkg.version },
    });
    page.close();
  });

  test('a tag without data-api-url or data-api-key gets one console error and sends nothing', () => {
    const tags: Record<string, string>[] = [{}, { 'data-api-key': 'k' }, { 'data-api-url': 'https://cv.example' }];
    for (const attributes of tags) {
      const page = runClassic('https://shop.example/', attributes);
      expect(page.console).toEqual([
        'error: Controlvar: the script tag needs data-api-url (an http or https URL) and data-api-key; the script does nothing without them.',
      ]);
      expect(page.fetches).toEqual([]);
      page.close();
    }
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
