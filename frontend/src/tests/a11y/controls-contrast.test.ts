/**
 * Three colour and link rules on the dashboard's controls, pinned as class
 * names because jsdom paints nothing. The rendered check is the browser run
 * (axe over the experiment page, and the flag page at 320 px in the feature
 * flag journey).
 *
 *   - Focus ring: Tailwind `blue-400` (#60a5fa) is 2.54:1 on white, under the
 *     3:1 a focus indicator needs (WCAG 1.4.11); `blue-600` (#2563eb) is 5.17:1.
 *   - Start button: white on `green-600` (#16a34a) is 3.29:1, under the 4.5:1
 *     that small text needs; `green-700` (#15803d) is 5.02:1.
 *   - A link inside a sentence is underlined at rest. Blue `#2563eb` against
 *     the sentence's `#64748b` is 1.08:1, so colour does not set it apart
 *     (WCAG 1.4.1); an underline that shows only on hover does not either.
 */
import fs from 'fs';
import path from 'path';

const SRC = path.join(__dirname, '..', '..');

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name !== 'tests') walk(full, out);
    } else if (/\.(ts|tsx)$/.test(entry.name)) {
      out.push(full);
    }
  }
  return out;
}

const FILES = walk(SRC);

function linesMatching(files: string[], pattern: RegExp): string[] {
  const hits: string[] = [];
  for (const file of files) {
    fs.readFileSync(file, 'utf8')
      .split('\n')
      .forEach((line, i) => {
        if (pattern.test(line)) hits.push(`${path.relative(SRC, file)}:${i + 1}: ${line.trim()}`);
      });
  }
  return hits;
}

/** Every whole whitespace-separated token in the file, so a class is matched exactly, not as a prefix. */
function classTokens(text: string): string[] {
  return text.split(/[\s'"`]+/);
}

describe('controls: contrast and link rules', () => {
  it('has files to scan', () => {
    expect(FILES.length).toBeGreaterThan(100);
  });

  it('no input rings its focus in blue-400', () => {
    expect(linesMatching(FILES, /focus:ring-blue-400/)).toEqual([]);
  });

  it('the targeting inputs, the segment picker, the safety form and the new flag form ring in blue-600', () => {
    for (const file of [
      'components/targeting/ConditionRow.tsx',
      'components/targeting/SegmentPicker.tsx',
      'components/admin/safety/SafetySettingsForm.tsx',
      'pages/feature-flags/new.tsx',
    ]) {
      const text = fs.readFileSync(path.join(SRC, file), 'utf8');
      expect(classTokens(text)).toContain('focus:ring-blue-600');
    }
  });

  it('no button puts white text on green-600 or lighter', () => {
    expect(linesMatching(FILES, /bg-green-(?:[1-6]00|[1-6]0)\b[^'"`]*text-white/)).toEqual([]);
    expect(linesMatching(FILES, /text-white[^'"`]*bg-green-(?:[1-6]00|[1-6]0)\b/)).toEqual([]);
  });

  it('the experiment page links inside a sentence are underlined at rest', () => {
    const page = fs.readFileSync(path.join(SRC, 'pages/experiments/[id].tsx'), 'utf8');
    const adminLink = /className="([^"]*)"\s+data-testid="sdk-hint-admin-api-keys"/.exec(page);
    expect(adminLink).not.toBeNull();
    expect(adminLink![1].split(/\s+/)).toContain('underline');

    const hint = path.join(SRC, 'components/experiments/ApiKeyRouteHint.tsx');
    expect(linesMatching([hint], /text-blue-600 hover:underline/)).toEqual([]);
    const links = fs.readFileSync(hint, 'utf8').match(/<(?:Link|a)\b[^>]*className="[^"]*"/g) ?? [];
    expect(links).toHaveLength(2);
    for (const link of links) {
      expect(/className="([^"]*)"/.exec(link)![1].split(/\s+/)).toContain('underline');
    }
  });
});
