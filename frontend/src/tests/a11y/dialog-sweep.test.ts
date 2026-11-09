/**
 * No modal overlay is built by hand outside the shared Dialog (#369).
 *
 * `frontend/src/components/ui/Dialog.tsx` traps focus, gives it back, closes on
 * Escape and makes the page inert. An overlay written from scratch does none
 * of that unless its author remembers all four, so the scan reads every
 * dashboard source file, core and modules, and refuses, outside Dialog.tsx:
 *
 *   - `aria-modal` written any way (`"..."`, `'...'`, `{"..."}`, an object key);
 *   - a native `<dialog` element;
 *   - `fixed` and `inset-0` in one class string, in either order.
 *
 * The overlays still to move onto Dialog are listed in RAW_DIALOGS with their
 * exact number of banned lines and of `<Dialog` uses, so a new raw overlay in a
 * listed file fails as surely as one in a new file. A file that has moved over
 * leaves that list for MIGRATED, which pins its `<Dialog` uses; a listed file
 * that no longer exists fails, so the list only shrinks. Paths are exact:
 * `ViewSqlDialog.tsx` ends in `Dialog.tsx` and is not the component.
 *
 * Module entries are excused only when the whole `modules/` tree is absent (a
 * core build); with it present, a module path that is not found fails. The
 * scanner takes its roots as parameters, and the temp-tree cases below run it
 * with no `modules/` and with a wrong modules path: the "Jest as the core
 * profile" step runs in a full checkout, so nothing else exercises either.
 * Test files are not read.
 */
import fs from 'fs';
import os from 'os';
import path from 'path';

const CORE_SRC = path.join(__dirname, '..', '..');
const REPO_ROOT = path.join(CORE_SRC, '..', '..');
const MODULES_SRC = path.join(REPO_ROOT, 'modules', 'frontend', 'src');

/** The one file that may build an overlay. Compared whole, never as a suffix. */
export const DIALOG_FILE = 'frontend/src/components/ui/Dialog.tsx';

export interface RawEntry {
  /** Lines carrying a banned pattern. */
  banned: number;
  /** `<Dialog` uses in the same file. */
  dialogUses: number;
}

/**
 * The overlays not yet on Dialog: 2 in 2 files (the safety rollback and the
 * warehouse confirm). Each moves in a later change of #369 and leaves this
 * list when it does.
 */
export const RAW_DIALOGS: Record<string, RawEntry> = {
  'frontend/src/pages/admin/safety.tsx': { banned: 2, dialogUses: 0 },
  'modules/frontend/src/components/warehouse/common.tsx': { banned: 2, dialogUses: 0 },
};

/** Files that left RAW_DIALOGS, with their exact number of `<Dialog` uses. */
export const MIGRATED: Record<string, number> = {
  'frontend/src/components/admin/users/EditUserModal.tsx': 1,
  'frontend/src/components/admin/users/EffectivePermissionsModal.tsx': 1,
  'modules/frontend/src/components/warehouse/runs/ViewSqlDialog.tsx': 1,
  'modules/frontend/src/components/admin/roles/CreateRoleModal.tsx': 1,
  'modules/frontend/src/pages/workspaces/index.tsx': 1,
  'modules/frontend/src/pages/workspaces/[id]/index.tsx': 1,
  // Two dialogs: the invite form and the remove confirmation.
  'modules/frontend/src/pages/workspaces/[id]/members.tsx': 2,
  'frontend/src/components/admin/api-keys/CreateApiKeyModal.tsx': 1,
  'frontend/src/components/admin/users/InviteUserModal.tsx': 1,
};

/** Fewer files than this means the roots are wrong, not that the tree is clean. */
export const FLOORS = { core: 150, modules: 26 };

export interface SweepRoots {
  repoRoot: string;
  coreRoot: string;
  modulesRoot: string;
}

export interface SweepRules {
  raw: Record<string, RawEntry>;
  migrated: Record<string, number>;
  floors: { core: number; modules: number };
}

const SOURCE = /\.(tsx?|jsx?)$/;
const TEST = /\.test\.(tsx?|jsx?)$/;

/** Every non-test source file under `dir`; none when it does not exist. */
export function sourceFiles(dir: string): string[] {
  if (!fs.existsSync(dir)) return [];
  const out: string[] = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === 'tests' || entry.name === '__tests__' || entry.name === 'node_modules') continue;
      out.push(...sourceFiles(full));
    } else if (SOURCE.test(entry.name) && !TEST.test(entry.name)) {
      out.push(full);
    }
  }
  return out;
}

/**
 * The string literals of `source` with the index of their opening quote.
 * Comments are skipped. A `'` right after a letter or digit is an apostrophe
 * in JSX text, not a quote, and a `'` or `"` string ends at the line's end.
 */
export function stringLiterals(source: string): { text: string; index: number }[] {
  const out: { text: string; index: number }[] = [];
  const n = source.length;
  let i = 0;
  while (i < n) {
    const c = source[i];
    if (c === '/' && source[i + 1] === '/') {
      const end = source.indexOf('\n', i);
      i = end < 0 ? n : end;
      continue;
    }
    if (c === '/' && source[i + 1] === '*') {
      const end = source.indexOf('*/', i + 2);
      i = end < 0 ? n : end + 2;
      continue;
    }
    if (c === '"' || c === '`' || (c === "'" && !/\w/.test(source[i - 1] ?? ''))) {
      const start = i;
      i += 1;
      while (i < n && source[i] !== c) {
        if (source[i] === '\\') i += 1;
        else if (c !== '`' && source[i] === '\n') break;
        i += 1;
      }
      out.push({ text: source.slice(start + 1, i), index: start });
      i += 1;
      continue;
    }
    i += 1;
  }
  return out;
}

const FIXED = /^(?:[\w-]+:)*!?fixed$/;
const INSET_0 = /^(?:[\w-]+:)*!?inset-0$/;

/** True when one string holds both `fixed` and `inset-0` as classes. */
export function isOverlayClassString(text: string): boolean {
  const tokens = text.split(/[\s'"`{}()$?,+]+/);
  return tokens.some((t) => FIXED.test(t)) && tokens.some((t) => INSET_0.test(t));
}

/** The 1-based lines of `source` that carry a banned pattern, ascending. */
export function bannedLines(source: string): number[] {
  const lines = new Set<number>();
  source.split('\n').forEach((text, i) => {
    if (text.includes('aria-modal') || /<dialog(?![\w-])/.test(text)) lines.add(i + 1);
  });
  let line = 1;
  let at = 0;
  for (const literal of stringLiterals(source)) {
    if (!isOverlayClassString(literal.text)) continue;
    for (; at < literal.index; at += 1) if (source[at] === '\n') line += 1;
    lines.add(line);
  }
  return Array.from(lines).sort((a, b) => a - b);
}

/** How many `<Dialog` elements `source` opens. */
export function dialogUses(source: string): number {
  return (source.match(/<Dialog[\s>/]/g) ?? []).length;
}

export function isExcluded(rel: string): boolean {
  return rel === DIALOG_FILE;
}

export interface SweepResult {
  problems: string[];
  scanned: { core: number; modules: number };
  /** Repository-relative paths read, Dialog.tsx excluded. */
  files: string[];
}

export function sweep(roots: SweepRoots, rules: SweepRules): SweepResult {
  const problems: string[] = [];
  const modulesTreePresent = fs.existsSync(path.join(roots.repoRoot, 'modules'));
  const coreFiles = sourceFiles(roots.coreRoot);
  const moduleFiles = sourceFiles(roots.modulesRoot);
  if (coreFiles.length < rules.floors.core) {
    problems.push(
      `scanned ${coreFiles.length} core files under ${roots.coreRoot}, fewer than ${rules.floors.core}: the root is wrong`,
    );
  }
  if (modulesTreePresent && moduleFiles.length < rules.floors.modules) {
    problems.push(
      `scanned ${moduleFiles.length} module files under ${roots.modulesRoot}, fewer than ${rules.floors.modules}, with modules/ present: the root is wrong`,
    );
  }

  const seen = new Set<string>();
  const files: string[] = [];
  for (const file of [...coreFiles, ...moduleFiles]) {
    const rel = path.relative(roots.repoRoot, file).split(path.sep).join('/');
    if (isExcluded(rel)) continue;
    files.push(rel);
    const source = fs.readFileSync(file, 'utf8');
    const lines = bannedLines(source);
    const uses = dialogUses(source);
    const raw = rules.raw[rel];
    if (raw) {
      seen.add(rel);
      if (lines.length !== raw.banned) {
        problems.push(
          `${rel}: ${lines.length} raw dialog lines (${lines.join(', ') || 'none'}), RAW_DIALOGS says ${raw.banned}`,
        );
      }
      if (uses !== raw.dialogUses) {
        problems.push(`${rel}: ${uses} <Dialog> uses, RAW_DIALOGS says ${raw.dialogUses}`);
      }
      continue;
    }
    if (rel in rules.migrated) {
      seen.add(rel);
      for (const line of lines) problems.push(`${rel}:${line} raw dialog; use <Dialog>`);
      if (uses !== rules.migrated[rel]) {
        problems.push(`${rel}: ${uses} <Dialog> uses, MIGRATED says ${rules.migrated[rel]}`);
      }
      continue;
    }
    for (const line of lines) problems.push(`${rel}:${line} raw dialog; use <Dialog>`);
  }

  for (const rel of [...Object.keys(rules.raw), ...Object.keys(rules.migrated)]) {
    if (seen.has(rel)) continue;
    if (rel.startsWith('modules/') && !modulesTreePresent) continue;
    problems.push(`${rel}: listed, but not found (a file leaves the list when it moves; the list only shrinks)`);
  }
  return { problems, scanned: { core: coreFiles.length, modules: moduleFiles.length }, files };
}

const REAL_ROOTS: SweepRoots = { repoRoot: REPO_ROOT, coreRoot: CORE_SRC, modulesRoot: MODULES_SRC };
const RULES: SweepRules = { raw: RAW_DIALOGS, migrated: MIGRATED, floors: FLOORS };

/** A scratch repository: `files` maps repository paths to their contents. */
function scratchTree(files: Record<string, string>): string {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'dialog-sweep-'));
  for (const [rel, text] of Object.entries(files)) {
    const full = path.join(root, rel);
    fs.mkdirSync(path.dirname(full), { recursive: true });
    fs.writeFileSync(full, text);
  }
  return root;
}

const scratch: string[] = [];
afterAll(() => {
  for (const dir of scratch) fs.rmSync(dir, { recursive: true, force: true });
});

function scratchRoots(files: Record<string, string>): SweepRoots {
  const root = scratchTree(files);
  scratch.push(root);
  return {
    repoRoot: root,
    coreRoot: path.join(root, 'frontend', 'src'),
    modulesRoot: path.join(root, 'modules', 'frontend', 'src'),
  };
}

const NO_FLOORS: SweepRules['floors'] = { core: 0, modules: 0 };

describe('dialog sweep: the dashboard', () => {
  const result = sweep(REAL_ROOTS, RULES);

  it('reads the dashboard, core and modules (an empty scan would pass vacuously)', () => {
    expect(result.scanned.core).toBeGreaterThanOrEqual(FLOORS.core);
    if (fs.existsSync(path.join(REPO_ROOT, 'modules'))) {
      expect(result.scanned.modules).toBeGreaterThanOrEqual(FLOORS.modules);
    }
    expect(result.files).toEqual(expect.arrayContaining(['frontend/src/pages/admin/users.tsx']));
    expect(result.files).not.toContain(DIALOG_FILE);
  });

  it('builds no overlay by hand outside Dialog.tsx and RAW_DIALOGS, and every count matches', () => {
    expect(result.problems).toEqual([]);
  });

  it('lists only overlays still to move: no entry already uses Dialog', () => {
    for (const [rel, entry] of Object.entries(RAW_DIALOGS)) {
      expect(`${rel} ${entry.dialogUses}`).toBe(`${rel} 0`);
      expect(rel in MIGRATED).toBe(false);
    }
  });
});

describe('dialog sweep: the patterns', () => {
  it.each([
    ['aria-modal="true"', '<div role="dialog" aria-modal="true">'],
    ["aria-modal='true'", "<div role='dialog' aria-modal='true'>"],
    ['aria-modal={"true"}', '<div role="dialog" aria-modal={"true"}>'],
    ['aria-modal={true}', '<div role="dialog" aria-modal={true}>'],
    ['an object key', "const props = { 'aria-modal': true };"],
    ['a native dialog', '<dialog open>'],
    ['a native dialog with its attributes on the next lines', '<dialog\n  open\n>'],
  ])('refuses %s', (_name, line) => {
    expect(bannedLines(`const a = 1;\n${line}\n`)).toEqual([2]);
  });

  it.each([
    ['fixed first', '<div className="fixed inset-0 z-50">'],
    ['inset-0 first', '<div className="inset-0 z-50 fixed">'],
    ['single quotes', "<div className='z-50 fixed inset-0'>"],
    ['a template literal', '<div className={`fixed ${tone} inset-0`}>'],
    ['a variant prefix', '<div className="md:fixed md:inset-0">'],
    ['a string outside JSX', "const overlay = 'bg-black/40 inset-0 fixed';"],
  ])('refuses fixed and inset-0 in one class string: %s', (_name, line) => {
    expect(bannedLines(`const a = 1;\n${line}\n`)).toEqual([2]);
  });

  it('dates a class string that spans lines to its first line', () => {
    expect(bannedLines('const a = 1;\nconst c = `fixed\n  inset-0`;\n')).toEqual([2]);
  });

  it.each([
    ['the component', '<Dialog open={open} onClose={close} title="x">'],
    ['absolute inset-0', '<div className="absolute inset-0">'],
    ['fixed without inset-0', '<div className="fixed top-0 right-0">'],
    ['inset-x-0', '<div className="fixed inset-x-0 bottom-0">'],
    ['two separate strings', '<div className="fixed" data-x="inset-0">'],
    ['a comment', '// a "fixed" bar; inset-0 would cover the page'],
    ['an apostrophe in JSX text', "<p>Don't use fixed inset-0 here: it's an overlay.</p>"],
  ])('allows %s', (_name, line) => {
    expect(bannedLines(`const a = 1;\n${line}\n`)).toEqual([]);
  });

  it('counts each <Dialog element, not <DialogHeader or the word', () => {
    expect(dialogUses('<Dialog open>\n<Dialog\n  open>\n<DialogHeader/>\nconst Dialog = 1;')).toBe(2);
  });

  it('excludes Dialog.tsx by its whole path: ViewSqlDialog.tsx is not it', () => {
    expect(isExcluded(DIALOG_FILE)).toBe(true);
    expect(isExcluded('modules/frontend/src/components/warehouse/runs/ViewSqlDialog.tsx')).toBe(false);
    expect(isExcluded('frontend/src/components/admin/Dialog.tsx')).toBe(false);
  });
});

describe('dialog sweep: the scanner on scratch trees', () => {
  const RAW_OVERLAY = [
    'export function X() {',
    '  return <div className="fixed inset-0" role="dialog" aria-modal="true" />;',
    '}',
  ].join('\n');

  it('a new raw aria-modal is refused with its line', () => {
    const roots = scratchRoots({ 'frontend/src/components/New.tsx': 'export const a = 1;\nexport const p = { "aria-modal": true };\n' });
    expect(sweep(roots, { raw: {}, migrated: {}, floors: NO_FLOORS }).problems).toEqual([
      'frontend/src/components/New.tsx:2 raw dialog; use <Dialog>',
    ]);
  });

  it('a new role-less fixed inset-0 is refused, in either class order', () => {
    const roots = scratchRoots({
      'frontend/src/components/A.tsx': 'export const A = () => <div className="fixed inset-0 bg-black/40" />;\n',
      'frontend/src/components/B.tsx': 'export const B = () => <div className="bg-black/40 inset-0 fixed" />;\n',
    });
    expect(sweep(roots, { raw: {}, migrated: {}, floors: NO_FLOORS }).problems.sort()).toEqual([
      'frontend/src/components/A.tsx:1 raw dialog; use <Dialog>',
      'frontend/src/components/B.tsx:1 raw dialog; use <Dialog>',
    ]);
  });

  it('a listed file whose count moved is refused, and so is a listed file that is gone', () => {
    const roots = scratchRoots({ 'frontend/src/components/Old.tsx': RAW_OVERLAY });
    const raw = {
      'frontend/src/components/Old.tsx': { banned: 2, dialogUses: 0 },
      'frontend/src/components/Gone.tsx': { banned: 2, dialogUses: 0 },
    };
    expect(sweep(roots, { raw, migrated: {}, floors: NO_FLOORS }).problems).toEqual([
      'frontend/src/components/Old.tsx: 1 raw dialog lines (2), RAW_DIALOGS says 2',
      'frontend/src/components/Gone.tsx: listed, but not found (a file leaves the list when it moves; the list only shrinks)',
    ]);
  });

  it('an allow-list entry for a file that already moved is refused', () => {
    const roots = scratchRoots({
      'frontend/src/components/Moved.tsx': 'export const M = () => <Dialog open title="x" onClose={f} />;\n',
    });
    const raw = { 'frontend/src/components/Moved.tsx': { banned: 2, dialogUses: 0 } };
    expect(sweep(roots, { raw, migrated: {}, floors: NO_FLOORS }).problems).toEqual([
      'frontend/src/components/Moved.tsx: 0 raw dialog lines (none), RAW_DIALOGS says 2',
      'frontend/src/components/Moved.tsx: 1 <Dialog> uses, RAW_DIALOGS says 0',
    ]);
  });

  it('a migrated file that goes back to a raw overlay is refused', () => {
    const roots = scratchRoots({ 'frontend/src/components/Moved.tsx': RAW_OVERLAY });
    const migrated = { 'frontend/src/components/Moved.tsx': 1 };
    expect(sweep(roots, { raw: {}, migrated, floors: NO_FLOORS }).problems).toEqual([
      'frontend/src/components/Moved.tsx:2 raw dialog; use <Dialog>',
      'frontend/src/components/Moved.tsx: 0 <Dialog> uses, MIGRATED says 1',
    ]);
  });

  it('a file whose name ends in Dialog.tsx is read like any other', () => {
    const roots = scratchRoots({ 'modules/frontend/src/components/runs/ViewSqlDialog.tsx': RAW_OVERLAY });
    expect(sweep(roots, { raw: {}, migrated: {}, floors: NO_FLOORS }).problems).toEqual([
      'modules/frontend/src/components/runs/ViewSqlDialog.tsx:2 raw dialog; use <Dialog>',
    ]);
  });

  it('roots pointed at the wrong directory fail on the floors and on every listed path', () => {
    const wrong: SweepRoots = {
      repoRoot: REPO_ROOT,
      coreRoot: path.join(REPO_ROOT, 'frontend', 'source'),
      modulesRoot: path.join(REPO_ROOT, 'modules', 'frontend', 'source'),
    };
    const { problems } = sweep(wrong, RULES);
    expect(problems).toEqual(
      expect.arrayContaining([
        expect.stringMatching(/^scanned 0 core files under .*fewer than 150: the root is wrong$/),
        expect.stringMatching(/^scanned 0 module files under .*with modules\/ present: the root is wrong$/),
        expect.stringMatching(/^frontend\/src\/pages\/admin\/safety\.tsx: listed, but not found/),
        expect.stringMatching(/^frontend\/src\/components\/admin\/users\/EditUserModal\.tsx: listed, but not found/),
      ]),
    );
  });
});

describe('dialog sweep: with no modules/ (a core tree) and with a wrong modules path', () => {
  // A copy of the real core tree with no modules/ beside it, as
  // scripts/core_build.sh makes one, and no .git.
  let core: SweepRoots;
  beforeAll(() => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'dialog-sweep-core-'));
    scratch.push(root);
    fs.cpSync(CORE_SRC, path.join(root, 'frontend', 'src'), { recursive: true });
    core = {
      repoRoot: root,
      coreRoot: path.join(root, 'frontend', 'src'),
      modulesRoot: path.join(root, 'modules', 'frontend', 'src'),
    };
  });

  it('the core tree passes with the real lists: module entries are excused when modules/ is absent', () => {
    const result = sweep(core, RULES);
    expect(result.problems).toEqual([]);
    expect(result.scanned.core).toBeGreaterThanOrEqual(FLOORS.core);
    expect(result.scanned.modules).toBe(0);
  });

  it('a core overlay is still refused in the core tree', () => {
    const file = path.join(core.coreRoot, 'components', 'Planted.tsx');
    fs.writeFileSync(file, 'export const P = () => <div className="fixed inset-0" />;\n');
    try {
      expect(sweep(core, RULES).problems).toEqual(['frontend/src/components/Planted.tsx:1 raw dialog; use <Dialog>']);
    } finally {
      fs.rmSync(file);
    }
  });

  it('with modules/ present, a wrong modules path is refused: the module entries are not found', () => {
    const wrong: SweepRoots = { ...REAL_ROOTS, modulesRoot: path.join(REPO_ROOT, 'modules', 'frontend', 'srcs') };
    const problems = fs.existsSync(path.join(REPO_ROOT, 'modules'))
      ? sweep(wrong, RULES).problems
      : (() => {
          // A core checkout: give the copy a modules/ directory, so the
          // wrong path is the only thing between it and the real tree.
          fs.mkdirSync(path.join(core.repoRoot, 'modules', 'frontend', 'src'), { recursive: true });
          return sweep({ ...core, modulesRoot: path.join(core.repoRoot, 'modules', 'frontend', 'srcs') }, RULES)
            .problems;
        })();
    const moduleEntries = Object.keys(RAW_DIALOGS).filter((rel) => rel.startsWith('modules/'));
    expect(moduleEntries.length).toBeGreaterThan(0);
    for (const rel of moduleEntries) {
      expect(problems).toContain(
        `${rel}: listed, but not found (a file leaves the list when it moves; the list only shrinks)`,
      );
    }
    expect(problems).toEqual(expect.arrayContaining([expect.stringMatching(/^scanned 0 module files/)]));
  });
});
