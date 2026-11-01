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
  // #1455: the nightly audit does not visit these, so this is their only pin.
  'pages/experiments/exclusion-groups/index.tsx',
  'pages/experiments/exclusion-groups/[id].tsx',
  'components/experiments/ExclusionGroupForm.tsx',
  'pages/experiments/holdouts.tsx',
  'components/experiments/HoldoutForm.tsx',
  'components/experiments/HoldoutResultsPanel.tsx',
  // The experiment page and the new-experiment form's basic fields and type
  // note, whole. The nightly audit checks neither page's contrast (it opens the
  // form for its label rules only), so this is their only pin.
  'pages/experiments/[id].tsx',
  'components/experiments/new/BasicInfoFields.tsx',
  'components/experiments/new/ExperimentTypeField.tsx',
];

/**
 * The exclusion-group and global-holdout pages (#1455) sit on the slate-50 page background,
 * where slate-500 is 4.55:1, too close to 4.5. So they use no text lighter
 * than slate-600 at all (UX: slate-600 inside bg-slate-50).
 */
const SLATE_50_FILES = [
  'pages/experiments/exclusion-groups/index.tsx',
  'pages/experiments/exclusion-groups/[id].tsx',
  'components/experiments/ExclusionGroupForm.tsx',
  'pages/experiments/holdouts.tsx',
  'components/experiments/HoldoutForm.tsx',
  'components/experiments/HoldoutResultsPanel.tsx',
];

describe('slate-50 pages use slate-600 or darker text', () => {
  it.each(SLATE_50_FILES)('%s', (file) => {
    const source = fs.readFileSync(path.join(SRC, file), 'utf8');
    const light = Array.from(source.matchAll(/(?<!placeholder:)\btext-slate-([1-5])00\b/g)).map((m) => m[0]);
    expect(light).toEqual([]);
    // The pattern reads this file's shades: a file with none would pass vacuously.
    expect(source).toMatch(/\btext-slate-[6-9]00\b/);
  });
});

describe('grey text contrast', () => {
  it.each(FILES)('%s uses no slate-400 text', (file) => {
    const source = fs.readFileSync(path.join(SRC, file), 'utf8');
    const hits = source.match(/(?<!placeholder:)\btext-slate-400\b/g);
    expect(hits).toBeNull();
  });
});

/**
 * The experiment page's "Edit variants and metrics" (#1453) and what it
 * renders. The nightly audit never opens the form, and jsdom cannot measure
 * contrast, so this is the only check on its grey text. The form's own panel
 * is white, so guided setup's slate-500 hints keep their 4.76:1 there.
 */
describe("the experiment page's variants and metrics form", () => {
  const DESIGN_FILES = [
    'components/experiments/ExperimentDesignForm.tsx',
    'components/experiments/ExperimentManageSection.tsx',
    'components/experiments/new/VariantsEditor.tsx',
    'components/experiments/new/MetricsEditor.tsx',
  ];

  it.each(DESIGN_FILES)('%s uses no slate-400 text', (file) => {
    const source = fs.readFileSync(path.join(SRC, file), 'utf8');
    expect(source.match(/(?<!placeholder:)\btext-slate-400\b/g)).toBeNull();
  });

  it('the form panel is white, not slate-50', () => {
    const source = fs.readFileSync(path.join(SRC, DESIGN_FILES[0]), 'utf8');
    // From `<form` to its test id: the tag's own attributes (an `=>` inside
    // one would end a `[^>]*` match early).
    const forms = Array.from(source.matchAll(/<form\b[\s\S]*?data-testid="design-form"/g));
    expect(forms).toHaveLength(1);
    expect(forms[0][0]).toMatch(/className="[^"]*\bbg-white\b/);
    expect(forms[0][0]).not.toMatch(/\bbg-slate-50\b/);
  });

  // The refusal is small text on `red-50` (#fef2f2): `red-700` (#b91c1c) is
  // 5.91:1 there, `red-600` (#dc2626) 4.41:1, under the 4.5:1 it needs. So one
  // text colour, red and 700 or darker, on that one background.
  it('the refusal is red-700 or darker on red-50', () => {
    const source = fs.readFileSync(path.join(SRC, DESIGN_FILES[0]), 'utf8');
    const tags = Array.from(source.matchAll(/<div\b[^>]*data-testid="design-error"[^>]*>/g));
    expect(tags).toHaveLength(1);
    const texts = Array.from(tags[0][0].matchAll(/\btext-([a-z]+)-(\d+)\b/g)).map((m) => ({
      colour: m[1],
      shade: Number(m[2]),
    }));
    expect(texts).toHaveLength(1);
    expect(texts[0].colour).toBe('red');
    expect(texts[0].shade).toBeGreaterThanOrEqual(700);
    const backgrounds = Array.from(tags[0][0].matchAll(/\bbg-([a-z]+-\d+|white)\b/g)).map((m) => m[1]);
    expect(backgrounds).toEqual(['red-50']);
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
 * A flag's Type and value fields (#1497), on the new-flag form and a value
 * flag's page. The nightly audit (tests/e2e/accessibility.spec.ts) scans the
 * form and a value flag's header and Values section, but only nightly, and
 * jsdom cannot measure contrast, so this is their pull-request-time pin: no
 * grey lighter than slate-600 (7.58:1 on white; the read-only values sit on
 * slate-50), and each refusal red-700 (6.47:1 on white) or darker.
 */
describe("a flag's Type and value fields", () => {
  const FILE = 'components/feature-flags/FlagValueFields.tsx';

  it('use no grey lighter than slate-600', () => {
    const source = fs.readFileSync(path.join(SRC, FILE), 'utf8');
    const shades = Array.from(source.matchAll(/(?<!placeholder:)\btext-slate-(\d+)\b/g)).map((m) => Number(m[1]));
    // The pattern reads the file's greys: the labels, the hints, the values.
    expect(shades.length).toBeGreaterThanOrEqual(5);
    expect(shades.filter((shade) => shade < 600)).toEqual([]);
  });

  it('show each refusal in red-700 or darker', () => {
    const source = fs.readFileSync(path.join(SRC, FILE), 'utf8');
    const reds = Array.from(source.matchAll(/\btext-red-(\d+)\b/g)).map((m) => Number(m[1]));
    expect(reds.length).toBeGreaterThanOrEqual(1);
    expect(reds.filter((shade) => shade < 700)).toEqual([]);
  });
});

/**
 * The experiment page's header terms (#1449). The whole page is in FILES, which
 * refuses slate-400; this is stricter on its header `<dl>`, the page's only
 * one: no `dt` is slate-400 or lighter. The nightly audit never opens this
 * page, and jsdom cannot measure contrast, so nothing else checks it.
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

  // "Dates saved." is small text on white, outside the panel: green-700
  // (#15803d, 5.0:1) or darker; green-600 is 3.3:1.
  it('"Dates saved." is green-700 or darker', () => {
    const source = fs.readFileSync(path.join(SRC, 'components/experiments/ExperimentManageSection.tsx'), 'utf8');
    const tags = Array.from(source.matchAll(/<p\b[^>]*data-testid="schedule-saved"[^>]*>/g));
    expect(tags).toHaveLength(1);
    const shades = Array.from(tags[0][0].matchAll(/\btext-([a-z]+)-(\d+)\b/g)).map((m) => [m[1], Number(m[2])]);
    expect(shades).toHaveLength(1);
    expect(shades[0][0]).toBe('green');
    expect(shades[0][1]).toBeGreaterThanOrEqual(700);
  });
});
