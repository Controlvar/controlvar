/**
 * The grey secondary text on the pages the nightly accessibility audit scans
 * (login, experiments, feature flags, the admin shell) meets WCAG AA.
 *
 * Tailwind `slate-400` (#94a3b8) is 2.56:1 on white and 2.45:1 on `slate-50`,
 * under the 4.5:1 that small text needs; `slate-500` (#64748b) is 4.76:1 and
 * 4.55:1. The rendered check is `tests/e2e/accessibility.spec.ts`, which only
 * runs nightly; this is the pull-request-time pin on the same elements.
 *
 * `placeholder:text-slate-400` is exempt: a placeholder is not scanned by the
 * contrast rule and is not the only label of its field.
 */
import fs from 'fs';
import path from 'path';

const SRC = path.join(__dirname, '..', '..');

const FILES = [
  'pages/login.tsx',
  'pages/experiments/index.tsx',
  'pages/feature-flags/index.tsx',
  'components/admin/AdminSidebar.tsx',
  'components/admin/AdminLayout.tsx',
];

describe('grey text contrast', () => {
  it.each(FILES)('%s uses no slate-400 text', (file) => {
    const source = fs.readFileSync(path.join(SRC, file), 'utf8');
    const hits = source.match(/(?<!placeholder:)\btext-slate-400\b/g);
    expect(hits).toBeNull();
  });
});

/**
 * The flag page's rollout schedule section (#1456). The nightly audit does
 * not open a flag's page and jsdom cannot measure contrast, so this is the
 * only check on it: no text lighter than slate-500 on white, and slate-600
 * inside the form's bg-slate-50 panel, where slate-500 is 4.55:1.
 */
describe("the flag page's rollout schedule section", () => {
  const FILE = 'components/feature-flags/RolloutScheduleManager.tsx';

  it('uses no slate-400 (or lighter) text', () => {
    const source = fs.readFileSync(path.join(SRC, FILE), 'utf8');
    expect(source.match(/(?<!placeholder:)\btext-slate-[1-4]00\b/g)).toBeNull();
  });

  it('uses no slate-500 text inside the form', () => {
    const source = fs.readFileSync(path.join(SRC, FILE), 'utf8');
    // The stage fields, and the <form> itself. Each slice must be found and
    // long, or a renamed marker would make this pass on nothing.
    const fields = source.slice(source.indexOf('function StageFields'), source.indexOf('function OrderNotes'));
    const form = source.slice(source.indexOf('<form'), source.indexOf('</form>'));
    expect(fields.length).toBeGreaterThan(1000);
    expect(form.length).toBeGreaterThan(1000);
    expect((fields + form).match(/\btext-slate-500\b/g)).toBeNull();
  });
});

/**
 * The experiment page's header terms (#1449). The page has other slate-400
 * text, so it cannot join FILES; this pins its header `<dl>`, the page's only
 * one, by its `dt` tags. The nightly audit never opens this page, and jsdom
 * cannot measure contrast, so nothing else checks it.
 */
describe("the experiment page's header terms", () => {
  const PAGE = 'pages/experiments/[id].tsx';

  it('no dt is slate-400 or lighter', () => {
    const source = fs.readFileSync(path.join(SRC, PAGE), 'utf8');
    const tags = Array.from(source.matchAll(/<dt\b[^>]*>/g));
    // Every `<dt` is a whole tag the pattern read, and there are as many as
    // the header has rows; a pattern that stops matching fails here.
    expect(tags).toHaveLength((source.match(/<dt\b/g) ?? []).length);
    expect(tags.length).toBeGreaterThanOrEqual(10);
    const light = tags
      .filter((tag) => /\btext-slate-[1-4]00\b/.test(tag[0]))
      .map((tag) => `${PAGE}:${source.slice(0, tag.index).split('\n').length}: ${tag[0]}`);
    expect(light).toEqual([]);
  });
});
