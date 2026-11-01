/**
 * A flag's page and its values (#1497, UX J2).
 *
 * - A boolean flag's page is the page it was before flags had values: no
 *   type, no Values section, "Serving" / "Not serving", and a save sends what
 *   it sent before.
 * - A string, number or JSON flag's header names its type, and its status
 *   says who gets which value. Its Values section, before Targeting Rules,
 *   shows the type (which cannot change, and is never sent) and the two
 *   values, each sent only when its field was changed.
 * - An ANALYST or VIEWER reads each value as JSON in a named region.
 * - What cannot be sent as typed is stopped at the field; the API's 422 on a
 *   value is shown at the field it names, in its words.
 */
import React from 'react';
import axe from 'axe-core';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import FeatureFlagDetailPage from '@/pages/feature-flags/[id]';
import { ApiError, ApiFetchOptions, apiFetch } from '@/services/api';
import { FeatureFlag } from '@/services/featureFlags';
import { makeRouter, routedApi } from './helpers/apiMock';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

const mockRouter = makeRouter({ pathname: '/feature-flags/[id]', query: { id: 'flag-1' } });
jest.mock('next/router', () => ({ useRouter: () => mockRouter }));

// Unset by default (no session, as before #917): the page keeps every control.
const mockAuth = jest.fn();
jest.mock('@/contexts/AuthContext', () => ({ useOptionalAuth: () => mockAuth() }));

function signIn(role: string) {
  mockAuth.mockReturnValue({
    status: 'authenticated',
    user: { id: 'u1', email: 'u@example.com', username: 'u', role, is_superuser: false },
  });
}

jest.mock('next/head', () => {
  const Head = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Head.displayName = 'MockHead';
  return Head;
});

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

/** A flag as the API returns it before flags had values: no value_type, on_value or off_value. */
function flag(overrides: Partial<FeatureFlag> = {}): FeatureFlag {
  return {
    id: 'flag-1',
    key: 'checkout_label',
    name: 'Checkout label',
    description: 'The checkout button',
    status: 'inactive',
    rollout_percentage: 25,
    rules: null,
    owner_id: 'user-1',
    created_at: '2026-09-01T10:00:00Z',
    updated_at: '2026-09-05T10:00:00Z',
    ...overrides,
  };
}

const BOOLEAN = { value_type: 'boolean', on_value: true, off_value: false };
const STRING = { value_type: 'string', on_value: 'Complete purchase', off_value: 'Buy now' };

type PutHandler = (body: Record<string, unknown>) => unknown;

/**
 * Routes the page's calls against a stored flag that a PUT updates the way the
 * API does (a field left out is kept); returns every PUT body sent.
 */
function install(stored: FeatureFlag, onPut?: PutHandler) {
  let state = stored;
  const puts: Record<string, unknown>[] = [];
  mockedApiFetch.mockImplementation(
    routedApi([
      { path: '/api/v1/feature-flags/flag-1', handler: () => state },
      { path: '/api/v1/rollout-schedules', handler: () => ({ items: [], total: 0, skip: 0, limit: 50 }) },
      {
        path: '/api/v1/safety/feature-flags/flag-1/check',
        handler: () => ({ feature_flag_id: 'flag-1', is_healthy: true, metrics: [], last_checked: '2026-09-11T08:00:00Z' }),
      },
      {
        method: 'POST',
        path: /^\/api\/v1\/feature-flags\/flag-1\/(enable|disable)$/,
        handler: (p: string) => {
          state = { ...state, status: p.endsWith('/enable') ? 'active' : 'inactive' };
          return { id: 'flag-1', name: state.name, key: state.key, status: state.status, updated_at: state.updated_at, audit_log_id: null };
        },
      },
      {
        method: 'PUT',
        path: '/api/v1/feature-flags/flag-1',
        handler: (_p: string, options: ApiFetchOptions) => {
          const body = options.json as Record<string, unknown>;
          puts.push(body);
          if (onPut) return onPut(body);
          state = { ...state, ...body } as FeatureFlag;
          return state;
        },
      },
    ]) as unknown as typeof apiFetch,
  );
  return puts;
}

async function loaded() {
  return screen.findByTestId('flag-detail');
}

const onField = () => screen.getByRole('textbox', { name: 'On value' });
const offField = () => screen.getByRole('textbox', { name: 'Off value' });

function type(field: HTMLElement, value: string) {
  fireEvent.change(field, { target: { value } });
}

function save() {
  fireEvent.click(screen.getByTestId('save-flag'));
}

/** The header card: name, status, key, description and the switch. */
function header() {
  return screen.getByTestId('flag-name').closest('.bg-white') as HTMLElement;
}

beforeEach(() => {
  mockedApiFetch.mockReset();
  mockAuth.mockReset();
});

describe('a boolean flag keeps the page it had', () => {
  it.each([
    ['inactive', 'Off', 'Not serving'],
    ['active', 'On', 'Serving'],
  ])('%s: the header of a boolean flag is the header of a flag from a server without values', async (status, pill, text) => {
    install(flag({ status }));
    const before = render(<FeatureFlagDetailPage />);
    await loaded();
    const plain = header().innerHTML;
    before.unmount();

    install(flag({ status, ...BOOLEAN }));
    render(<FeatureFlagDetailPage />);
    await loaded();
    expect(header().innerHTML).toBe(plain);
    expect(screen.getByTestId('flag-status')).toHaveTextContent(pill);
    expect(within(header()).getByText(text)).toBeInTheDocument();
    expect(screen.queryByTestId('flag-value-type')).toBeNull();
    expect(header()).not.toHaveTextContent(/flag$/);
  });

  it('has no Values section, no rollout value hint, and a slider described by nothing new', async () => {
    install(flag(BOOLEAN));
    render(<FeatureFlagDetailPage />);
    await loaded();
    expect(screen.queryByTestId('flag-values-section')).toBeNull();
    expect(screen.queryByRole('heading', { name: 'Values' })).toBeNull();
    expect(screen.queryByRole('textbox', { name: 'On value' })).toBeNull();
    expect(screen.queryByRole('region', { name: 'On value' })).toBeNull();
    expect(screen.queryByTestId('rollout-value-hint')).toBeNull();
    expect(screen.getByTestId('rollout-percentage')).not.toHaveAttribute('aria-describedby');
  });

  it('a save sends what it sent before: the rollout, and nothing for the values', async () => {
    const puts = install(flag(BOOLEAN));
    render(<FeatureFlagDetailPage />);
    await loaded();
    fireEvent.change(screen.getByTestId('rollout-percentage'), { target: { value: '40' } });
    save();
    await screen.findByTestId('save-success');
    expect(puts).toEqual([{ rollout_percentage: 40 }]);
  });
});

describe("a value flag's header", () => {
  it('names the type after the status, and says who gets which value; the switch is unchanged', async () => {
    install(flag(STRING));
    render(<FeatureFlagDetailPage />);
    await loaded();
    const pills = within(header()).getAllByText(/^(Off|On|String flag)$/);
    expect(pills.map((p) => p.textContent)).toEqual(['Off', 'String flag']);
    expect(within(header()).getByText('Everyone gets the off value')).toBeInTheDocument();
    expect(within(header()).queryByText('Not serving')).toBeNull();

    fireEvent.click(screen.getByRole('switch', { name: 'Turn checkout_label on' }));
    expect(await within(header()).findByText('Rules and rollout decide who gets the on value')).toBeInTheDocument();
    expect(screen.getByRole('switch', { name: 'Turn checkout_label off' })).toHaveAttribute('aria-checked', 'true');
  });

  it.each([
    ['number', 'Number flag'],
    ['json', 'JSON flag'],
  ])('%s: %s', async (valueType, words) => {
    install(flag({ value_type: valueType, on_value: valueType === 'number' ? 10 : { a: 1 }, off_value: valueType === 'number' ? 3 : [] }));
    render(<FeatureFlagDetailPage />);
    await loaded();
    expect(screen.getByTestId('flag-value-type')).toHaveTextContent(words);
  });
});

describe("a value flag's Values section", () => {
  it('comes before Targeting Rules, shows the type as text, and offers no way to change it', async () => {
    install(flag(STRING));
    render(<FeatureFlagDetailPage />);
    await loaded();
    const values = screen.getByTestId('flag-values-section');
    const targeting = screen.getByTestId('flag-targeting-section');
    expect(values.compareDocumentPosition(targeting) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(values).getByRole('heading', { name: 'Values', level: 2 })).toBeInTheDocument();
    expect(within(values).getByTestId('flag-values-type')).toHaveTextContent('Type: String');
    expect(values).toHaveTextContent('The type cannot be changed. To serve another type, create a new flag.');
    // value_type is read-only once a flag exists: no radio, no list box, no text box for it.
    expect(screen.queryAllByRole('radio')).toEqual([]);
    expect(within(values).queryByRole('combobox')).toBeNull();
    expect(within(values).getAllByRole('textbox')).toHaveLength(2);
  });

  it.each([
    ['string', 'Complete purchase', 'Buy now', 'Complete purchase', 'Buy now'],
    ['number', 10, 2.5, '10', '2.5'],
    ['json', { color: 'blue' }, [1, 2], '{\n  "color": "blue"\n}', '[\n  1,\n  2\n]'],
  ])('%s: the fields start from the stored values', async (valueType, on, off, onText, offText) => {
    install(flag({ value_type: valueType, on_value: on, off_value: off }));
    render(<FeatureFlagDetailPage />);
    await loaded();
    expect(onField()).toHaveValue(onText);
    expect(offField()).toHaveValue(offText);
  });

  it('a save that moves only the slider sends neither value, nor the type', async () => {
    const puts = install(flag(STRING));
    render(<FeatureFlagDetailPage />);
    await loaded();
    fireEvent.change(screen.getByTestId('rollout-percentage'), { target: { value: '60' } });
    save();
    await screen.findByTestId('save-success');
    expect(puts).toEqual([{ rollout_percentage: 60 }]);
  });

  it('a changed value is sent alone, never with value_type, and the field then shows what was saved', async () => {
    const puts = install(flag({ value_type: 'json', on_value: { color: 'blue' }, off_value: { color: 'grey' } }));
    render(<FeatureFlagDetailPage />);
    await loaded();
    type(onField(), '{"color":"green"}');
    save();
    await screen.findByTestId('save-success');
    expect(puts).toEqual([{ rollout_percentage: 25, on_value: { color: 'green' } }]);
    expect(puts[0]).not.toHaveProperty('value_type');
    expect(onField()).toHaveValue('{\n  "color": "green"\n}');

    // The next save, with nothing changed, sends no value.
    save();
    await waitFor(() => expect(puts).toHaveLength(2));
    expect(puts[1]).toEqual({ rollout_percentage: 25 });
  });

  it('a value re-indented, or 10.0 for 10, is not a change', async () => {
    const puts = install(flag({ value_type: 'number', on_value: 10, off_value: 3 }));
    render(<FeatureFlagDetailPage />);
    await loaded();
    type(onField(), '10.0');
    save();
    await screen.findByTestId('save-success');
    expect(puts).toEqual([{ rollout_percentage: 25 }]);
  });

  it('a value made equal to the other one is refused at its field, and nothing is sent (EMF-5)', async () => {
    const puts = install(flag(STRING));
    render(<FeatureFlagDetailPage />);
    await loaded();
    type(onField(), 'Buy now');
    save();
    expect(await screen.findByTestId('flag-on-value-error')).toHaveTextContent(
      'on_value equals off_value; the two values must differ, or turning the flag off changes nothing',
    );
    expect(onField()).toHaveAttribute('aria-invalid', 'true');
    expect(onField()).toHaveFocus();
    expect(puts).toEqual([]);
    expect(screen.queryByTestId('save-error')).toBeNull();
  });

  it('JSON that does not parse is refused at its field, focused, and nothing is sent', async () => {
    const puts = install(flag({ value_type: 'json', on_value: { a: 1 }, off_value: { a: 2 } }));
    render(<FeatureFlagDetailPage />);
    await loaded();
    type(offField(), '{"a": }');
    save();
    const error = await screen.findByTestId('flag-off-value-error');
    expect(error.textContent).toMatch(/^Not valid JSON: \S/);
    expect(offField()).toHaveAccessibleDescription(/^Not valid JSON: /);
    expect(offField()).toHaveFocus();
    expect(puts).toEqual([]);
  });

  it("the API's 422 on a value is shown under the field it names, focused; not in the save alert", async () => {
    install(flag({ value_type: 'number', on_value: 10, off_value: 3 }), () => {
      throw new ApiError({
        status: 422,
        detail: [
          {
            loc: ['body', 'off_value'],
            msg: 'off_value: a number is outside -9007199254740991 to 9007199254740991',
            type: 'value_out_of_range',
          },
        ],
      });
    });
    render(<FeatureFlagDetailPage />);
    await loaded();
    type(offField(), '4');
    save();
    expect(await screen.findByTestId('flag-off-value-error')).toHaveTextContent(
      'off_value: a number is outside -9007199254740991 to 9007199254740991',
    );
    await waitFor(() => expect(offField()).toHaveFocus());
    expect(screen.queryByTestId('save-error')).toBeNull();
    expect(screen.queryByTestId('save-success')).toBeNull();
  });

  it('any other failure keeps the save alert', async () => {
    install(flag(STRING), () => {
      throw new ApiError({ status: 403, detail: 'Not enough permissions' });
    });
    render(<FeatureFlagDetailPage />);
    await loaded();
    type(onField(), 'Pay now');
    save();
    expect(await screen.findByTestId('save-error')).toHaveTextContent('Not enough permissions');
    expect(screen.queryByTestId('flag-on-value-error')).toBeNull();
  });

  it('shows the rollout value hint under the slider, as its description', async () => {
    install(flag(STRING));
    render(<FeatureFlagDetailPage />);
    await loaded();
    expect(screen.getByTestId('rollout-percentage')).toHaveAccessibleDescription(
      'The share of users who match no targeting rule that get the on value. Everyone else gets the off value.',
    );
  });
});

describe('an ANALYST or VIEWER reads the values (#917)', () => {
  it.each(['ANALYST', 'VIEWER'])('%s: each value as JSON in a named region, no text box', async (role) => {
    signIn(role);
    install(flag({ value_type: 'string', on_value: 'Buy now', off_value: '' }));
    render(<FeatureFlagDetailPage />);
    await loaded();
    const on = screen.getByRole('region', { name: 'On value' });
    const off = screen.getByRole('region', { name: 'Off value' });
    expect(on).toHaveTextContent('"Buy now"');
    expect(off.textContent).toBe('""');
    expect(on).toHaveAttribute('tabindex', '0');
    expect(screen.queryByRole('textbox', { name: 'On value' })).toBeNull();
    expect(screen.queryByRole('textbox', { name: 'Off value' })).toBeNull();
    expect(screen.getByTestId('flag-values-type')).toHaveTextContent('Type: String');
  });

  it('a JSON value is pretty-printed', async () => {
    signIn('VIEWER');
    install(flag({ value_type: 'json', on_value: { color: 'blue' }, off_value: [1] }));
    render(<FeatureFlagDetailPage />);
    await loaded();
    expect(screen.getByRole('region', { name: 'On value' }).textContent).toBe('{\n  "color": "blue"\n}');
  });
});

describe('a type this dashboard does not know', () => {
  it('is shown as the API wrote it, and its values read-only', async () => {
    install(flag({ value_type: 'colour', on_value: '#00f', off_value: '#888' }));
    render(<FeatureFlagDetailPage />);
    await loaded();
    expect(screen.getByTestId('flag-value-type')).toHaveTextContent('colour flag');
    expect(screen.getByRole('region', { name: 'On value' })).toHaveTextContent('"#00f"');
    expect(screen.queryByRole('textbox', { name: 'On value' })).toBeNull();
  });
});

describe("a value flag's page is accessible (axe-core in jsdom; colour contrast is not computable here)", () => {
  const axeOptions: axe.RunOptions = { rules: { 'color-contrast': { enabled: false } } };

  async function violations(node: Element) {
    const result = await axe.run(node, axeOptions);
    return result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(', ')}`);
  }

  it('editable (DEVELOPER)', async () => {
    signIn('DEVELOPER');
    install(flag({ value_type: 'json', on_value: { a: 1 }, off_value: { a: 2 } }));
    const { container } = render(<FeatureFlagDetailPage />);
    await loaded();
    expect(await violations(container)).toEqual([]);
  });

  it('read-only (VIEWER)', async () => {
    signIn('VIEWER');
    install(flag(STRING));
    const { container } = render(<FeatureFlagDetailPage />);
    await loaded();
    expect(await violations(container)).toEqual([]);
  });

  it('with a refusal shown', async () => {
    install(flag(STRING));
    const { container } = render(<FeatureFlagDetailPage />);
    await loaded();
    type(offField(), 'Complete purchase');
    save();
    await screen.findByTestId('flag-off-value-error');
    expect(await violations(container)).toEqual([]);
  });
});
