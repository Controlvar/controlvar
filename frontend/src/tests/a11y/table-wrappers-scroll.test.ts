/**
 * No table in the dashboard sits inside a card that clips it (#1532).
 *
 * A card with `overflow-hidden` around a `<table>` cuts the table off at a
 * phone width: columns past the edge (status, the On/Off control) can never be
 * reached. The wrapper must scroll (`overflow-x-auto`). The scan reads every
 * dashboard source file, core and modules, and walks up from each `<table>`:
 * the first ancestor that sets an overflow decides. `overflow-hidden` first is
 * a failure; a scroll container first is fine (a clipping card may hold an
 * inner scroller, as the users table does).
 */
import fs from 'fs';
import path from 'path';

const CORE_SRC = path.join(__dirname, '..', '..');
const MODULES_SRC = path.join(CORE_SRC, '..', '..', 'modules', 'frontend', 'src');

function sourceFiles(dir: string): string[] {
  if (!fs.existsSync(dir)) return [];
  const out: string[] = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === 'tests' || entry.name === '__tests__' || entry.name === 'node_modules') continue;
      out.push(...sourceFiles(full));
    } else if (entry.name.endsWith('.tsx') && !/\.test\.tsx$/.test(entry.name)) {
      out.push(full);
    }
  }
  return out;
}

interface Verdict {
  file: string;
  line: number;
  problem: string | null;
}

const SCROLL = /\boverflow(-x)?-(auto|scroll)\b/;
const CLIP = /\boverflow(-x)?-(hidden|clip)\b/;

/** Judge every `<table` in `source`; `problem` is null when it scrolls or is not clipped. */
export function judgeTables(file: string, source: string): Verdict[] {
  const verdicts: Verdict[] = [];
  const stack: { tag: string; className: string }[] = [];
  const tag = /<(\/?)([A-Za-z][\w.]*)((?:"[^"]*"|'[^']*'|`[^`]*`|\{(?:[^{}]|\{[^{}]*\})*\}|[^>"'`{}])*?)(\/?)>/g;
  let m: RegExpExecArray | null;
  while ((m = tag.exec(source)) !== null) {
    const [, closing, name, attrs, selfClosing] = m;
    if (closing) {
      for (let i = stack.length - 1; i >= 0; i--) {
        if (stack[i].tag === name) {
          stack.length = i;
          break;
        }
      }
      continue;
    }
    const className = /className\s*=\s*(?:"([^"]*)"|\{`([^`]*)`\}|\{"([^"]*)"\})/.exec(attrs);
    const classes = className ? (className[1] ?? className[2] ?? className[3] ?? '') : '';
    if (name === 'table') {
      let problem: string | null = null;
      for (let i = stack.length - 1; i >= 0; i--) {
        if (SCROLL.test(stack[i].className)) break;
        if (CLIP.test(stack[i].className)) {
          problem = `<${stack[i].tag}> "${stack[i].className}" clips the table`;
          break;
        }
      }
      verdicts.push({ file, line: source.slice(0, m.index).split('\n').length, problem });
    }
    if (!selfClosing) stack.push({ tag: name, className: classes });
  }
  return verdicts;
}

const files = [...sourceFiles(CORE_SRC), ...sourceFiles(MODULES_SRC)];
const verdicts = files.flatMap((f) => judgeTables(f, fs.readFileSync(f, 'utf8')));

describe('table wrappers scroll instead of clipping', () => {
  it('finds the dashboard tables (an empty scan would pass vacuously)', () => {
    expect(verdicts.length).toBeGreaterThanOrEqual(20);
    const names = verdicts.map((v) => path.relative(CORE_SRC, v.file));
    expect(names).toEqual(expect.arrayContaining([path.join('pages', 'feature-flags', 'index.tsx')]));
    expect(names).toEqual(expect.arrayContaining([path.join('pages', 'experiments', 'index.tsx')]));
  });

  it('clips no table', () => {
    const bad = verdicts
      .filter((v) => v.problem)
      .map((v) => `${path.relative(CORE_SRC, v.file)}:${v.line} ${v.problem}`);
    expect(bad).toEqual([]);
  });

  it('the judge itself refuses a clipping card and accepts a scrolling one', () => {
    const clip = judgeTables('x.tsx', '<div className="rounded overflow-hidden"><table></table></div>');
    const scroll = judgeTables('x.tsx', '<div className="rounded overflow-hidden"><div className="overflow-x-auto"><table></table></div></div>');
    expect(clip.map((v) => v.problem)).toEqual([expect.stringContaining('clips')]);
    expect(scroll.map((v) => v.problem)).toEqual([null]);
  });
});
