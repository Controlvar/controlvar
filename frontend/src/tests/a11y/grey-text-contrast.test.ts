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

  // The note under the header (#1449) is small text: one slate colour of 500
  // or darker on its own tag. Its links are pinned as underlined in
  // experiment-detail-eligibility.test.tsx.
  it('the note under the header is slate-500 or darker', () => {
    const source = fs.readFileSync(path.join(SRC, PAGE), 'utf8');
    const tags = Array.from(source.matchAll(/<p\b[^>]*data-testid="experiment-api-settings-note"[^>]*>/g));
    expect(tags).toHaveLength(1);
    const shades = Array.from(tags[0][0].matchAll(/\btext-slate-(\d+)\b/g)).map((m) => Number(m[1]));
    expect(shades).toHaveLength(1);
    expect(shades[0]).toBeGreaterThanOrEqual(500);
  });
});

/**
 * The experiment page's schedule form (#1454). Its panel is slate-50, where
 * slate-500 is 4.55:1, too close to 4.5, so every grey in the file is
 * slate-600 (7.58:1) or darker. The nightly audit never opens the form, and
 * jsdom cannot measure contrast, so nothing else checks it.
 */
describe("the experiment page's schedule form", () => {
  const FORM = 'components/experiments/ExperimentScheduleSection.tsx';

  it('the panel is slate-50 and no text in the file is lighter than slate-600', () => {
    const source = fs.readFileSync(path.join(SRC, FORM), 'utf8');
    const forms = Array.from(source.matchAll(/<form\b[\s\S]*?data-testid="experiment-schedule-form"/g));
    expect(forms).toHaveLength(1);
    expect(forms[0][0]).toMatch(/className="[^"]*\bbg-slate-50\b/);
    const shades = Array.from(source.matchAll(/(?<!placeholder:)\btext-slate-(\d+)\b/g)).map((m) => Number(m[1]));
    // The pattern reads the file's greys: the hints, the labels, the heading.
    expect(shades.length).toBeGreaterThanOrEqual(3);
    expect(shades.filter((shade) => shade < 600)).toEqual([]);
  });
});
