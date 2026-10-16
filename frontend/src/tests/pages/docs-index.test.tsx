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
 * Matched against `container.textContent`, so a claim split across nested
 * elements is still seen. The `<Head>` description is not in it; the
 * marketing build grep covers that.
 */
import React from 'react';
import { render } from '@testing-library/react';
import DocsIndex from '@/pages/docs/index';

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
  [['send each variant', '\\s+to a different URL'], 'the edge router does not split traffic yet'],
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
  it.each(retired)('no longer says %s (%s)', (parts) => {
    const pattern = new RegExp((parts as string[]).join(''), 'i');
    const { container } = render(<DocsIndex />);
    expect(container.textContent ?? '').not.toMatch(pattern);
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

  it('says custom roles are not applied yet (#891)', () => {
    const { getAllByRole } = render(<DocsIndex />);
    const rbac = getAllByRole('link').filter((a) => /^RBAC/.test(a.textContent ?? ''));
    expect(rbac).toHaveLength(1);
    expect(rbac[0].textContent ?? '').toMatch(/custom roles \(full profile\) are stored but not applied yet/);
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
