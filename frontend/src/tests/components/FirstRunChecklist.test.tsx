/**
 * The first-run card on an empty /experiments page says only what a reader can
 * do from there (#1142).
 *
 * The card is shown only while the workspace has no experiment, and creating
 * one moves the reader to the experiment's page, so:
 *
 * - the API key comes first, while the card is still on the screen;
 * - the sample cannot know the experiment's key: it shows a placeholder, and
 *   the step says to put in the key the experiment's page shows;
 * - the step says to press Start, because only an active experiment assigns
 *   users (`POST /api/v1/tracking/assign` answers 404 for any other).
 *
 * That the experiment's page shows the key and a Start button for a draft is
 * held by experiment-detail.test.tsx ("shows the header, variants, metrics and
 * owner", "draft → Start calls POST /start ..."), and that creating an
 * experiment opens its page by experiment-new.test.tsx. The `first-run` docs
 * journey walks the card end to end on a stack with no seed data.
 */
import React from 'react';
import { render, screen, within } from '@testing-library/react';
import {
  EXPERIMENT_KEY_PLACEHOLDER,
  FirstRunChecklist,
  assignCurlSample,
} from '@/components/experiments/FirstRunChecklist';

const mockAuth = jest.fn().mockReturnValue(null);
jest.mock('@/contexts/AuthContext', () => ({ useOptionalAuth: () => mockAuth() }));

beforeEach(() => {
  process.env.NEXT_PUBLIC_API_URL = 'http://api.test';
  mockAuth.mockReturnValue(null);
});

afterAll(() => {
  delete process.env.NEXT_PUBLIC_API_URL;
});

function steps(): HTMLElement[] {
  render(<FirstRunChecklist />);
  const card = screen.getByTestId('first-run-checklist');
  return [1, 2, 3].map((n) => within(card).getByTestId(`checklist-step-${n}`));
}

describe('FirstRunChecklist', () => {
  it('says what the three steps lead to: a first assignment', () => {
    render(<FirstRunChecklist />);
    expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent(
      'Assign your first user in three steps',
    );
  });

  it('asks for the API key before the experiment, whose creation leaves this page', () => {
    const headings = steps().map((step) => within(step).getByRole('heading', { level: 3 }).textContent);
    expect(headings).toEqual([
      'Create an API key',
      'Create an experiment',
      'Start it, then send an assignment',
    ]);
  });

  it("links step 2 to the new-experiment form and says it opens the experiment's page", () => {
    const step = steps()[1];
    expect(within(step).getByRole('link', { name: '+ New Experiment' })).toHaveAttribute(
      'href',
      '/experiments/new',
    );
    expect(step).toHaveTextContent("Creating it opens the experiment's page, which shows its key.");
  });

  it('links a superuser to Admin → API Keys, the page that opens for a superuser only', () => {
    mockAuth.mockReturnValue({ user: { is_superuser: true, role: 'ADMIN' } });
    const step = steps()[0];
    expect(within(step).getByRole('link', { name: 'Open Admin → API Keys' })).toHaveAttribute(
      'href',
      '/admin/api-keys',
    );
  });

  it('says what a key looks like and that it is shown once', () => {
    // generate_api_key (backend/app/models/api_key.py) makes eptk_<hex>; the
    // first-run journey keeps the one key on the screen that starts eptk_.
    expect(steps()[0]).toHaveTextContent('A key starts with eptk_ and is shown once — copy it when it appears.');
  });

  it('says to press Start before sending an assignment', () => {
    const step = steps()[2];
    expect(within(step).getByTestId('checklist-start')).toHaveTextContent(
      "On the experiment's page, press Start: only an active experiment assigns users.",
    );
  });

  it("shows a placeholder for the experiment's key, never a key that looks real", () => {
    const step = steps()[2];
    const curl = within(step).getByTestId('checklist-curl').textContent ?? '';
    expect(curl).toContain(`"experiment_key": "${EXPERIMENT_KEY_PLACEHOLDER}"`);
    expect(curl).toContain('-H "X-API-Key: $API_KEY"');
    expect(EXPERIMENT_KEY_PLACEHOLDER).toBe('YOUR_EXPERIMENT_KEY');
    // The step names both values the reader replaces, as the sample spells them.
    expect(within(step).getByTestId('checklist-start')).toHaveTextContent(
      `with the experiment's key in place of ${EXPERIMENT_KEY_PLACEHOLDER} and your API key in API_KEY.`,
    );
    expect(assignCurlSample('http://api.test')).toBe(curl);
  });

  it('underlines the SDK link at rest: colour alone is 1.09:1 against the text (#1533)', () => {
    const link = within(steps()[2]).getByRole('link', { name: 'SDK' }) as HTMLElement;
    expect(link).toHaveAttribute('href', '/docs');
    expect(link.className.split(/\s+/)).toContain('underline');
  });
});
