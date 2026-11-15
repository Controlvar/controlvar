/**
 * The docs hub (`/docs`) makes no claim the repository does not back (T86).
 *
 * Each entry is a claim the hub used to make: an SDK count `sdk/` had
 * outgrown, a latency nobody measured, local hashing the SDKs do not do (the
 * server decides), a one-step deploy that takes several, a search box that was
 * a folder listing, the AWS name as a hyphenated adjective, and Jira,
 * Salesforce and GitHub syncs that nothing in the platform makes: it calls
 * none of them, and an inbound webhook changes nothing.
 *
 * The fragments are concatenated, not joined with whitespace, so each entry is
 * exactly the regular expression it reads as once put together; they are split
 * only so the old wording never appears in this file. A bare `\b14\b` is
 * deliberately absent: it matches the hub's correct "iOS 14+".
 *
 * Matched against `claimText()` (tests/pages/helpers/claimText.ts): the
 * container's `textContent`, so a claim split across nested elements is still
 * seen, plus every `alt`, `aria-label` and `title`, and the `<Head>`
 * description, which the marketing build copies into the link preview. No
 * grep of the marketing build's HTML sweeps those (#1459, PE 11).
 */
import React from 'react';
import { render, within } from '@testing-library/react';
import DocsIndex from '@/pages/docs/index';
import { BOOKS_URL, ROADMAP_URL } from '@/content/site-links';
import { docsUrl } from '@/services/docs';
import { claimText, metaDescriptions } from './helpers/claimText';

jest.mock('next/head', () => { const H=({children}:{children:React.ReactNode})=><>{children}</>; H.displayName='H'; return H; });
jest.mock('next/router', () => ({ useRouter: () => ({ pathname:'/docs', asPath:'/docs', query:{}, push:jest.fn(), replace:jest.fn(), isReady:true }) }));

const retired: Array<[string[], string]> = [
  [['AW', 'S-\\w'], 'the AWS name is not a hyphenated adjective'],
  [['\\b14\\s+', '(languages|SDK)'], 'the SDK count drifted from sdk/'],
  [['Sub-milli', 'second'], 'no latency has been measured'],
  [['consistent\\s+', '(MD5\\s+)?hash'], 'the SDKs do not bucket locally'],
  [['One-', 'command'], 'the CDK deploy takes several steps'],
  [['Search\\s+the', '\\s+documentation'], 'the link opens a listing, not a search'],
  [['Sync\\s+experiment', '\\s+status'], 'nothing in the platform calls Jira'],
  [['auto-', 'create'], 'nothing in the platform creates a Jira issue'],
  [['webhook\\s+', 'sync'], 'a Salesforce delivery is answered and changes nothing'],
  [['PR\\s+', 'linking'], 'a pull request is not linked to an experiment'],
  // Retired when the hub was registered claim by claim (#1142): each said
  // something its page does not.
  [['SSR support', '\\s+via\\s+Server', 'Client'], 'the React page describes no ServerClient entry point'],
  [['change flag', '\\s+vendors'], 'the OpenFeature page shows no vendor switch'],
  [['Hex', '\\s+package'], 'the Elixir SDK is not on Hex yet'],
  [['PSR-', '4'], 'the PHP page says nothing of PSR-4'],
  [['LRU', '\\s+cache'], 'the .NET page describes no LRU cache'],
  [['DynamoDB', '\\s+and\\s+Lambda'], 'no Lambda function serves requests or processes events'],
  [['send each variant', '\\s+to a different URL'], 'Controlvar sends no visitor anywhere: your server or edge redirects them'],
  [['record of', '\\s+feature-flag status'], 'the audit log records more than flag status changes'],
  [['JWT', '\\s+tokens'], 'the authentication page names bearer tokens'],
  [['Complete', '\\s+endpoint'], 'the endpoint page does not list every endpoint'],
  [['20\\+', '\\s+operators'], 'nothing counts the operators'],
  [['statistical', '\\s+rigour'], 'a description says what a page covers'],
  [['existing data', '\\s+stack'], 'nothing in the platform calls Jira, Salesforce or GitHub'],
  [['methods used', '\\s+by the platform'], 'post-stratification is not available through the API'],
  [['Google', '\\s+Workspace'], 'the SSO page names the google provider type'],
];

describe('/docs hub', () => {
  it('sweeps the meta description too, so the sweep below reads it', () => {
    const { container } = render(<DocsIndex />);
    const descriptions = metaDescriptions(container);
    expect(descriptions).toHaveLength(1);
    expect(descriptions[0].length).toBeGreaterThan(50);
    expect(claimText(container)).toContain(descriptions[0]);
  });

  it.each(retired)('no longer says %s (%s)', (parts) => {
    const pattern = new RegExp((parts as string[]).join(''), 'i');
    const { container } = render(<DocsIndex />);
    expect(claimText(container)).not.toMatch(pattern);
  });
});

/**
 * A hub entry for a method that is not usable today has to say so: the
 * post-stratification route answers 501 (#577), so its entry must contain the
 * word "not". CUPED left this list when it began reading each user's own
 * history (#217); its entry now describes what it does. Located by the link
 * label; the description is in the same anchor.
 */
describe('/docs hub entries for methods that do not work yet', () => {
  it('the CUPED entry describes the method, not a defect (#217)', () => {
    const { getAllByRole } = render(<DocsIndex />);
    const links = getAllByRole('link').filter((a) => /^CUPED/.test(a.textContent ?? ''));
    expect(links).toHaveLength(1);
    expect(links[0].textContent ?? '').toMatch(/pre-experiment events/i);
    expect(links[0].textContent ?? '').not.toMatch(/\bnot\b|\bbeta\b/i);
  });

  it.each([
    ['Post-Stratification', /^Post-Stratification/],
  ])('the %s entry says it is not available', (_name, label) => {
    const { getAllByRole } = render(<DocsIndex />);
    const links = getAllByRole('link').filter((a) => label.test(a.textContent ?? ''));
    expect(links).toHaveLength(1);
    expect(links[0].textContent ?? '').toMatch(/\bnot\b/i);
  });

  it('links the table of what the dashboard does and what is API only (#442)', () => {
    const { getAllByRole } = render(<DocsIndex />);
    const table = getAllByRole('link').filter((a) => /^Dashboard and API/.test(a.textContent ?? ''));
    expect(table).toHaveLength(1);
    expect(table[0].getAttribute('href')).toMatch(/guides\/dashboard-and-api/);
  });

  it('links the Python SDK to its own page, not the SDK integration guide', () => {
    const { getAllByRole } = render(<DocsIndex />);
    const python = getAllByRole('link').filter((a) => /^Python SDK/.test(a.textContent ?? ''));
    expect(python).toHaveLength(1);
    expect(python[0].getAttribute('href')).toMatch(/\/sdk\/python\.md$/);
  });

  it('says custom roles and direct grants add to the built-in roles (#891)', () => {
    const { getAllByRole } = render(<DocsIndex />);
    const rbac = getAllByRole('link').filter((a) => /^RBAC/.test(a.textContent ?? ''));
    expect(rbac).toHaveLength(1);
    expect(rbac[0].textContent ?? '').toMatch(
      /in the full profile, custom roles and direct grants add to them/,
    );
  });

  it('lists the Kubernetes (Helm) self-hosting guide', () => {
    const { getAllByRole } = render(<DocsIndex />);
    const helm = getAllByRole('link').filter((a) => /^Kubernetes \(Helm\)/.test(a.textContent ?? ''));
    expect(helm).toHaveLength(1);
    expect(helm[0].getAttribute('href')).toMatch(/self-hosting\/kubernetes/);
  });

  it('leads Self-Hosting -> Docker Compose to the self-hosting stack, not the development guide (#1148)', () => {
    const { getAllByRole } = render(<DocsIndex />);
    const compose = getAllByRole('link').filter((a) => /^Docker Compose/.test(a.textContent ?? ''));
    expect(compose).toHaveLength(1);
    expect(compose[0].getAttribute('href')).toBe(
      'https://github.com/Controlvar/controlvar/blob/main/deploy/compose/README.md',
    );
    expect(compose[0].getAttribute('href')).not.toMatch(/docker-guide/);
    expect(compose[0].textContent ?? '').toMatch(/deploy\/compose\/compose\.yml/);
  });
});

/**
 * The hub's layout since the site design of 2026-11-11 (/docs items 1-4). The
 * stretched three-column grid made each row as tall as its tallest card and
 * left 4,000 px of empty card at 1440 px; "Label — sentence" started each
 * sentence at a different x; the books were not linked at all; the icons were
 * emoji and letters. jsdom lays nothing out, so this pins the structure and
 * the classes that do it; the heights were measured in Chromium.
 */
describe('/docs hub layout', () => {
  it('has one h1, then "Start here" and "Every topic" as h2s, and each group an h3', () => {
    const { container } = render(<DocsIndex />);
    const headings = Array.from(container.querySelectorAll('h1, h2, h3')).map((h) => `${h.tagName} ${h.textContent}`);
    expect(headings.slice(0, 4)).toEqual(['H1 Controlvar Documentation', 'H2 Start here', 'H2 Every topic', 'H3 Getting Started']);
    expect(headings.filter((h) => h.startsWith('H1'))).toHaveLength(1);
    expect(headings.filter((h) => h.startsWith('H3')).length).toBeGreaterThanOrEqual(10);
  });

  it('starts with the quick start, the concepts, the Controlvar books (BOOKS_URL), what Controlvar does and the roadmap (D380)', () => {
    const { container } = render(<DocsIndex />);
    const start = container.querySelector('section[aria-labelledby="start-here-heading"]') as HTMLElement;
    expect(within(start).getAllByRole('link').map((a) => [a.textContent, a.getAttribute('href')])).toEqual([
      ['Quick start', docsUrl('getting-started/quick-start')],
      ['Core concepts', docsUrl('getting-started/concepts')],
      ['The Controlvar books', BOOKS_URL],
      ['What Controlvar does', docsUrl('capabilities')],
      ['Roadmap', ROADMAP_URL],
    ]);
    expect(ROADMAP_URL).toBe('https://github.com/Controlvar/controlvar/blob/main/ROADMAP.md');
    // Each entry is its title alone (the text above; a description would be a sentence with no registered
    // claim) and a line icon.
    for (const a of within(start).getAllByRole('link')) expect(a.querySelector('svg[aria-hidden="true"] path')).not.toBeNull();
  });

  it('offers one button to the whole documentation, and no search-styled box', () => {
    const { container, getAllByRole } = render(<DocsIndex />);
    const browse = getAllByRole('link').filter((a) => a.getAttribute('href') === docsUrl(''));
    expect(browse).toHaveLength(1);
    expect(browse[0]).toHaveAccessibleName('Browse the documentation');
    expect(claimText(container)).not.toMatch(/search/i);
  });

  it('flows the groups in CSS columns, each kept whole, instead of a stretched grid', () => {
    const { container } = render(<DocsIndex />);
    const topics = container.querySelector('section[aria-labelledby="topics-heading"]') as HTMLElement;
    const flow = topics.querySelector('h2 + div') as HTMLElement;
    expect(flow.className.split(/\s+/)).toEqual(expect.arrayContaining(['md:columns-2', 'lg:columns-3']));
    expect(flow.className).not.toMatch(/\bgrid\b/);
    const groups = Array.from(flow.children);
    expect(groups.length).toBeGreaterThanOrEqual(10);
    for (const group of groups) expect(group.className.split(/\s+/)).toContain('break-inside-avoid');
  });

  it('puts each link title on a line of its own, with its description under it', () => {
    const { container } = render(<DocsIndex />);
    const links = Array.from(container.querySelectorAll('section[aria-labelledby="topics-heading"] li > a'));
    expect(links.length).toBeGreaterThan(60);
    for (const a of links) {
      const parts = Array.from(a.children);
      expect(parts.map((p) => p.className.split(/\s+/).includes('block'))).toEqual([true, true]);
      expect(a.textContent).not.toMatch(/—/);
    }
  });

  it('draws line icons, never emoji', () => {
    const { container } = render(<DocsIndex />);
    // A character outside the Basic Multilingual Plane (🚀, 📦, 🔒 …) or in Miscellaneous Symbols and Dingbats (⚗, 🏗's ⚙ kin).
    expect(container.textContent ?? '').not.toMatch(/[\uD800-\uDBFF][\uDC00-\uDFFF]|[☀-➿]/);
    const groups = Array.from(container.querySelectorAll('section[aria-labelledby="topics-heading"] h3'));
    for (const h3 of groups) expect(h3.parentElement?.querySelector('svg[aria-hidden="true"] path')).not.toBeNull();
  });
});
