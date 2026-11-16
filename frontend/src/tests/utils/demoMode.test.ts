/**
 * Demo mode's parsing and copy: which `/auth/config` answers turn it on, the
 * demo apps' addresses, and the reset time written in one place only.
 */
import fs from 'fs';
import path from 'path';
import {
  LOCAL_AUTH_CONFIG,
  fetchAuthConfig,
  parseAuthConfig,
  parseDemoMode,
} from '@/services/authConfig';
import {
  DEMO_BANNER_SIGNED_IN,
  DEMO_BANNER_SIGNED_OUT,
  DEMO_LOGIN_INTRO,
  DEMO_RESET_TIME,
  DEMO_UNREACHABLE,
  DEMO_WALKTHROUGHS_URL,
  demoAppLinks,
  demoRateLimitedMessage,
} from '@/utils/demoMode';
import { SITE_ORIGIN } from '@/components/PageTitle';
import { MODULES_SRC_ROOT, SRC_ROOT, listSourceFiles, sourceLabel } from '../helpers/api-literals';
import { CONFIG_BODY, TEST_DEMO_ACCOUNT, jsonResponse, routedFetch } from '../helpers/demoMode';

const mockFetch = jest.fn();
global.fetch = mockFetch;

beforeEach(() => mockFetch.mockReset());

describe('parseDemoMode', () => {
  it.each([
    ['no demo_mode', { provider: 'local' }],
    ['demo_mode false', { provider: 'local', demo_mode: false }],
    ['demo_mode "true", a string', { provider: 'local', demo_mode: 'true' }],
    ['demo_mode 1', { provider: 'local', demo_mode: 1 }],
    ['an array', [{ demo_mode: true }]],
    ['null', null],
    ['an HTML page', '<!doctype html><title>demo_mode: true</title>'],
  ])('is off for %s', (_label, body) => {
    expect(parseDemoMode(body)).toBeNull();
  });

  it('is on with the account when the API publishes one', () => {
    expect(parseDemoMode(CONFIG_BODY.on)).toEqual({ account: TEST_DEMO_ACCOUNT });
  });

  it.each([
    ['no account', {}],
    ['an account that is not an object', { demo_account: 'viewer' }],
    ['no password', { demo_account: { email: 'v@example.test' } }],
    ['an empty password', { demo_account: { email: 'v@example.test', password: '' } }],
    ['an email without @', { demo_account: { email: 'viewer', password: 'x' } }],
    ['a password that is not a string', { demo_account: { email: 'v@example.test', password: 12345 } }],
    ['an over-long email', { demo_account: { email: `${'v'.repeat(250)}@example.test`, password: 'x' } }],
    ['an over-long password', { demo_account: { email: 'v@example.test', password: 'x'.repeat(257) } }],
  ])('is on with no account for %s', (_label, extra) => {
    expect(parseDemoMode({ provider: 'local', demo_mode: true, ...extra })).toEqual({ account: null });
  });

  it('leaves the sign-in provider as it was', () => {
    expect(parseAuthConfig(CONFIG_BODY.on)).toBe(LOCAL_AUTH_CONFIG);
  });
});

describe('fetchAuthConfig', () => {
  it('reads demo mode from the same answer as the provider', async () => {
    mockFetch.mockImplementation(routedFetch(CONFIG_BODY.on));
    await expect(fetchAuthConfig()).resolves.toEqual({
      config: LOCAL_AUTH_CONFIG,
      demo: { account: TEST_DEMO_ACCOUNT },
    });
  });

  it('is demo mode off when the API does not answer', async () => {
    mockFetch.mockRejectedValue(new TypeError('Failed to fetch'));
    await expect(fetchAuthConfig()).resolves.toEqual({ config: LOCAL_AUTH_CONFIG, demo: null });
  });

  it('is demo mode off when the API answers an error', async () => {
    mockFetch.mockResolvedValue(jsonResponse(500, CONFIG_BODY.on));
    await expect(fetchAuthConfig()).resolves.toEqual({ config: LOCAL_AUTH_CONFIG, demo: null });
  });
});

describe('demoAppLinks', () => {
  it('puts ShopLab and StreamPulse beside the dashboard, on its scheme', () => {
    expect(demoAppLinks({ protocol: 'https:', host: 'demo.example.com' }).map((l) => l.href)).toEqual([
      'https://shoplab.demo.example.com/',
      'https://streampulse.demo.example.com/',
    ]);
    expect(demoAppLinks({ protocol: 'http:', host: 'localhost:3100' }).map((l) => l.href)).toEqual([
      'http://shoplab.localhost:3100/',
      'http://streampulse.localhost:3100/',
    ]);
  });

  it('offers none without a host', () => {
    expect(demoAppLinks({ protocol: 'https:', host: '' })).toEqual([]);
  });
});

describe('the links', () => {
  it("point the walkthroughs at the public site's own page", () => {
    expect(DEMO_WALKTHROUGHS_URL).toBe(`${SITE_ORIGIN}/walkthroughs`);
    // The marketing build's page that URL serves.
    expect(fs.existsSync(path.join(SRC_ROOT, 'pages', 'walkthroughs.tsx'))).toBe(true);
  });
});

describe('the copy', () => {
  it('says how long to wait after a 429, in seconds or roughly', () => {
    expect(demoRateLimitedMessage(1)).toBe('Too many people are entering from your network. Try again in 1 second.');
    expect(demoRateLimitedMessage(30)).toBe('Too many people are entering from your network. Try again in 30 seconds.');
    expect(demoRateLimitedMessage(undefined)).toBe('Too many people are entering from your network. Try again in a minute.');
  });

  it('names the reset time in every sentence about the rebuild', () => {
    for (const sentence of [DEMO_BANNER_SIGNED_IN, DEMO_BANNER_SIGNED_OUT, DEMO_LOGIN_INTRO, DEMO_UNREACHABLE]) {
      expect(sentence).toContain(`every night at ${DEMO_RESET_TIME}`);
    }
  });

  it('writes the reset time in one file of the dashboard', () => {
    const files = [...listSourceFiles(SRC_ROOT), ...listSourceFiles(MODULES_SRC_ROOT)];
    const holding = files
      .filter((file) => fs.readFileSync(file, 'utf8').includes(DEMO_RESET_TIME))
      .map((file) => sourceLabel(file));
    expect(holding).toEqual(['utils/demoMode.ts']);
    const own = fs.readFileSync(path.join(SRC_ROOT, 'utils', 'demoMode.ts'), 'utf8');
    expect(own.split(DEMO_RESET_TIME)).toHaveLength(2);
  });
});
