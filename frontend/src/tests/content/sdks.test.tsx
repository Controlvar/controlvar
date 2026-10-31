/**
 * The SDK list on the public pages is held to `sdk/`.
 *
 * The homepage once gave an SDK count that `sdk/` had outgrown and left one
 * SDK out of its list. `content/sdks.ts` is now the one place that names each
 * SDK directory, and this test checks it against the repository in both
 * directions: the map against `sdk/`, and the pages against the map.
 *
 * It reads `sdk/` from the working tree, not from git, so it passes in a
 * `git archive` copy and in `scripts/core_build.sh`'s copy, neither of which
 * has a `.git`. A missing or empty `sdk/` is an error, never a skip: a test
 * that passes because it found nothing to check is the defect it exists for.
 */
import fs from 'fs';
import path from 'path';
import React from 'react';
import { render } from '@testing-library/react';
import { AuthProvider } from '@/contexts/AuthContext';
import { SDK_GUIDES, SDKS } from '@/content/sdks';
import HomePage from '@/pages/index';
import DocsIndex from '@/pages/docs/index';
import { isMarketingSite } from '@/utils/site-mode';
import { claimText } from '../pages/helpers/claimText';

// The platform build unless a test says otherwise; the count rule renders both.
jest.mock('@/utils/site-mode', () => ({
  ...jest.requireActual('@/utils/site-mode'),
  isMarketingSite: jest.fn(),
}));
const mode = isMarketingSite as jest.Mock;

jest.mock('next/head', () => { const H=({children}:{children:React.ReactNode})=><>{children}</>; H.displayName='H'; return H; });
jest.mock('next/router', () => ({ useRouter: () => ({ pathname:'/', asPath:'/', query:{}, push:jest.fn(), replace:jest.fn(), isReady:true }) }));

const REPO_ROOT = path.resolve(__dirname, '..', '..', '..', '..');
const SDK_DIR = path.join(REPO_ROOT, 'sdk');

function sdkDirectories(): string[] {
  if (!fs.existsSync(SDK_DIR)) {
    throw new Error(`${SDK_DIR} does not exist; the SDK list cannot be checked`);
  }
  const dirs = fs
    .readdirSync(SDK_DIR, { withFileTypes: true })
    .filter((e) => e.isDirectory())
    .map((e) => e.name)
    .sort();
  if (dirs.length === 0) {
    throw new Error(`${SDK_DIR} has no directories; the SDK list cannot be checked`);
  }
  return dirs;
}

/**
 * Asserts every display name appears in `text` as a word of its own.
 *
 * Longest names first, and each is removed once found, so "Java" is not
 * satisfied by "JavaScript", nor "React" by "React Native". The letter
 * look-arounds stop "Go" matching inside another word.
 */
function missingNames(text: string): string[] {
  const names = Array.from(new Set(Object.values(SDKS))).sort((a, b) => b.length - a.length);
  const missing: string[] = [];
  let rest = text;
  for (const name of names) {
    const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const word = new RegExp(`(?<![A-Za-z])${escaped}(?![A-Za-z])`, 'g');
    if (!word.test(rest)) missing.push(name);
    rest = rest.replace(word, ' ');
  }
  return missing;
}

/**
 * The rendered text with a space between text nodes. `textContent` runs
 * adjacent elements together ("integrationJava SDK"), which would hide a
 * name from the word match above.
 */
function renderedText(container: HTMLElement): string {
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  const parts: string[] = [];
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    parts.push(node.textContent ?? '');
  }
  return parts.join(' ');
}

/**
 * A number of SDKs, in digits: "14 SDKs", "14 native SDKs", "SDKs in 14
 * languages". The spelled-out count is index.test.tsx's `retired` rule; this
 * is the digit one, which the mockup's "14 SDKs" passed (#1459, QA 1g).
 * Not a count: "iOS 14+" (no space after the digits), and the "0" of
 * "Apache-2.0 and the SDKs" (a version, and two words before "SDKs").
 */
const SDK_COUNT = /(?<![\w.-])(\d+)\s+(?:[A-Za-z+-]+\s+)?(?:SDKs?|languages|client librar(?:y|ies))\b/gi;

/** Each digit count of SDKs in `text`, as a number. */
function sdkCounts(text: string): number[] {
  const counts: number[] = [];
  const re = new RegExp(SDK_COUNT.source, SDK_COUNT.flags);
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) counts.push(Number(m[1]));
  return counts;
}

beforeEach(() => {
  localStorage.clear();
  mode.mockReset().mockReturnValue(false);
  global.fetch = jest.fn(() => Promise.reject(new Error('no API'))) as unknown as typeof fetch;
});

describe('content/sdks.ts', () => {
  it('names exactly the directories under sdk/', () => {
    expect(Object.keys(SDKS).sort()).toEqual(sdkDirectories());
  });

  it('every SDK is named on the homepage', () => {
    const { container } = render(<AuthProvider><HomePage /></AuthProvider>);
    expect(missingNames(renderedText(container))).toEqual([]);
  });

  it('every SDK is named on the docs hub', () => {
    const { container } = render(<DocsIndex />);
    expect(missingNames(renderedText(container))).toEqual([]);
  });

  it("links each SDK's guide through a map with exactly its keys", () => {
    expect(Object.keys(SDK_GUIDES).sort()).toEqual(Object.keys(SDKS).sort());
  });
});

/**
 * The pages state no SDK count, or the one `SDKS` gives. A typed number
 * drifted from `sdk/` before; with this rule a count that is not the size of
 * `SDKS` fails at once, and one that is fails the day an SDK is added.
 */
describe('no SDK count but the one SDKS gives', () => {
  const size = Object.keys(SDKS).length;

  it('finds the count in a sentence that states one, so the rule can fire', () => {
    expect(sdkCounts('There are 14 SDKs, plus 2 OpenFeature providers, in 12 languages.')).toEqual([14, 12]);
    expect(sdkCounts('For iOS 14+ and macOS 11+.')).toEqual([]);
    expect(sdkCounts('The whole platform is Apache-2.0 and the SDKs are MIT.')).toEqual([]);
  });

  it.each([
    ['platform', false],
    ['marketing', true],
  ])('on the homepage (%s build)', (_build, marketing) => {
    mode.mockReturnValue(marketing);
    const { container } = render(<AuthProvider><HomePage /></AuthProvider>);
    expect(sdkCounts(claimText(container)).filter((n) => n !== size)).toEqual([]);
  });

  it('on the docs hub', () => {
    const { container } = render(<DocsIndex />);
    expect(sdkCounts(claimText(container)).filter((n) => n !== size)).toEqual([]);
  });
});
