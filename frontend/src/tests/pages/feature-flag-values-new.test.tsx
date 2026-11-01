/**
 * New Feature Flag, its Values section (#1497, UX J1): the type is chosen
 * from four radios, Boolean by default; Boolean sends exactly the body a flag
 * had before values; String, Number and JSON add an On value and an Off value.
 * What cannot be sent as typed is stopped before the request, at the field;
 * the API's 422 on a value is shown at the field it names, in its words.
 */
import React from 'react';
import axe from 'axe-core';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import NewFeatureFlagPage from '@/pages/feature-flags/new';
import { ApiError, ApiFetchOptions, apiFetch } from '@/services/api';
import { makeRouter, routedApi } from './helpers/apiMock';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

const mockRouter = makeRouter({ pathname: '/feature-flags/new' });
jest.mock('next/router', () => ({ useRouter: () => mockRouter }));

const mockAuth = jest.fn();
jest.mock('@/contexts/AuthContext', () => ({ useOptionalAuth: () => mockAuth() }));

jest.mock('next/head', () => {
  const Head = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Head.displayName = 'MockHead';
  return Head;
});

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

/** Routes POST /feature-flags/; returns every body sent. */
function install(onPost?: (body: Record<string, unknown>) => unknown) {
  const posts: Record<string, unknown>[] = [];
  mockedApiFetch.mockImplementation(
    routedApi([
      {
        method: 'POST',
        path: '/api/v1/feature-flags',
        handler: (_p: string, options: ApiFetchOptions) => {
          const body = options.json as Record<string, unknown>;
          posts.push(body);
          if (onPost) return onPost(body);
          return { id: 'flag-9', ...body, created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-01T00:00:00Z' };
        },
      },
    ]) as unknown as typeof apiFetch,
  );
  return posts;
}

const refusal = (field: string, msg: string, type: string) =>
  new ApiError({ status: 422, detail: [{ loc: ['body', field], msg, type }] });

function fillName(name = 'Checkout button label') {
  fireEvent.change(screen.getByTestId('flag-name-input'), { target: { value: name } });
}

function chooseType(name: 'Boolean' | 'String' | 'Number' | 'JSON') {
  fireEvent.click(screen.getByRole('radio', { name }));
}

const onField = () => screen.getByRole('textbox', { name: 'On value' });
const offField = () => screen.getByRole('textbox', { name: 'Off value' });

function type(field: HTMLElement, value: string) {
  fireEvent.change(field, { target: { value } });
}

function submit() {
  fireEvent.click(screen.getByTestId('submit-flag'));
}

beforeEach(() => {
  mockedApiFetch.mockReset();
  mockAuth.mockReset();
  mockRouter.push.mockClear();
});

describe('the type choice', () => {
  it('is a group of four radios named Type, Boolean selected, each with its hint as its description', () => {
    install();
    render(<NewFeatureFlagPage />);
    const group = screen.getByRole('group', { name: 'Type' });
    const radios = within(group).getAllByRole('radio');
    expect(radios.map((r) => (r as HTMLInputElement).labels?.[0]?.textContent)).toEqual([
      'Boolean',
      'String',
      'Number',
      'JSON',
    ]);
    expect(screen.getByRole('radio', { name: 'Boolean' })).toBeChecked();
    expect(screen.getByRole('radio', { name: 'Boolean' })).toHaveAccessibleDescription(
      'On or off. Your code reads whether the flag is on.',
    );
    expect(screen.getByRole('radio', { name: 'String' })).toHaveAccessibleDescription('Text, such as a button label.');
    expect(screen.getByRole('radio', { name: 'Number' })).toHaveAccessibleDescription(
      'A number, such as a limit or a timeout.',
    );
    expect(screen.getByRole('radio', { name: 'JSON' })).toHaveAccessibleDescription(
      'An object or an array, such as a configuration.',
    );
    expect(group).toHaveAccessibleDescription('The type cannot be changed after the flag is created.');
    expect(screen.getByRole('heading', { name: 'Values', level: 2 })).toBeInTheDocument();
  });

  it('Boolean shows no value fields and no rollout value hint', () => {
    install();
    render(<NewFeatureFlagPage />);
    expect(screen.queryByRole('textbox', { name: 'On value' })).toBeNull();
    expect(screen.queryByRole('textbox', { name: 'Off value' })).toBeNull();
    expect(screen.queryByTestId('rollout-value-hint')).toBeNull();
    expect(screen.getByTestId('rollout-percentage')).not.toHaveAttribute('aria-describedby');
  });
});

describe('a boolean flag', () => {
  it('sends exactly the body a flag had before values: no value_type, on_value or off_value', async () => {
    const posts = install();
    render(<NewFeatureFlagPage />);
    fillName('Dark mode');
    submit();
    await waitFor(() => expect(mockRouter.push).toHaveBeenCalledWith('/feature-flags/flag-9'));
    // As sent: JSON drops the undefined description.
    const sent = JSON.parse(JSON.stringify(posts[0]));
    expect(Object.keys(sent).sort()).toEqual(['is_active', 'key', 'name', 'rollout_percentage']);
  });

  it('sends nothing for the values after the type went to String and back, text typed', async () => {
    const posts = install();
    render(<NewFeatureFlagPage />);
    fillName('Dark mode');
    chooseType('String');
    type(onField(), 'Complete purchase');
    chooseType('Boolean');
    expect(screen.queryByRole('textbox', { name: 'On value' })).toBeNull();
    submit();
    await waitFor(() => expect(mockRouter.push).toHaveBeenCalled());
    expect(posts[0]).not.toHaveProperty('value_type');
    expect(posts[0]).not.toHaveProperty('on_value');
    expect(posts[0]).not.toHaveProperty('off_value');
  });
});

describe('a string, number or JSON flag', () => {
  it('String: two text fields with their hints, and the two strings sent with value_type string', async () => {
    const posts = install();
    render(<NewFeatureFlagPage />);
    fillName();
    chooseType('String');
    expect(onField()).toHaveAccessibleDescription(
      'What users the flag is on for get: users who match a targeting rule, and the rollout percentage of everyone else. Leave empty to serve an empty string.',
    );
    expect(offField()).toHaveAccessibleDescription(
      'What everyone else gets, and what everyone gets while the flag is off. Leave empty to serve an empty string.',
    );
    expect(screen.getByTestId('flag-values-public-note')).toHaveTextContent(
      'Every application that evaluates this flag can read both values. Do not put secrets in them.',
    );
    expect(screen.getByTestId('rollout-value-hint')).toHaveTextContent(
      'The share of users who match no targeting rule that get the on value. Everyone else gets the off value.',
    );
    expect(screen.getByTestId('rollout-percentage')).toHaveAccessibleDescription(
      'The share of users who match no targeting rule that get the on value. Everyone else gets the off value.',
    );
    // A string may be empty, so it is not required.
    expect(onField()).not.toHaveAttribute('aria-required');
    type(onField(), 'Complete purchase');
    type(offField(), 'Buy now');
    submit();
    await waitFor(() => expect(mockRouter.push).toHaveBeenCalledWith('/feature-flags/flag-9'));
    expect(posts[0]).toMatchObject({
      key: 'checkout_button_label',
      is_active: false,
      value_type: 'string',
      on_value: 'Complete purchase',
      off_value: 'Buy now',
    });
  });

  it('String: an empty string is a value', async () => {
    const posts = install();
    render(<NewFeatureFlagPage />);
    fillName();
    chooseType('String');
    type(onField(), 'New');
    submit();
    await waitFor(() => expect(mockRouter.push).toHaveBeenCalled());
    expect(posts[0]).toMatchObject({ value_type: 'string', on_value: 'New', off_value: '' });
  });

  it('Number: plain text fields (no type=number, no inputmode), required, sent as numbers', async () => {
    const posts = install();
    render(<NewFeatureFlagPage />);
    fillName('Checkout retries');
    chooseType('Number');
    expect(onField()).toHaveAttribute('type', 'text');
    expect(onField()).not.toHaveAttribute('inputmode');
    expect(onField()).toHaveAttribute('aria-required', 'true');
    type(onField(), '10');
    type(offField(), '-2.5');
    submit();
    await waitFor(() => expect(mockRouter.push).toHaveBeenCalled());
    expect(posts[0]).toMatchObject({ value_type: 'number', on_value: 10, off_value: -2.5 });
  });

  it('JSON: monospace text areas that leave spelling alone, sent parsed', async () => {
    const posts = install();
    render(<NewFeatureFlagPage />);
    fillName('Checkout theme');
    chooseType('JSON');
    expect(onField().tagName).toBe('TEXTAREA');
    expect(onField()).toHaveAttribute('spellcheck', 'false');
    expect(onField()).toHaveAttribute('aria-required', 'true');
    type(onField(), '{"color": "blue", "sizes": [1, 2]}');
    type(offField(), '[]');
    submit();
    await waitFor(() => expect(mockRouter.push).toHaveBeenCalled());
    expect(posts[0]).toMatchObject({
      value_type: 'json',
      on_value: { color: 'blue', sizes: [1, 2] },
      off_value: [],
    });
  });

  it('keeps the typed text across String, Number and JSON, and checks it against the type chosen', async () => {
    const posts = install();
    render(<NewFeatureFlagPage />);
    fillName();
    chooseType('String');
    type(onField(), '10');
    type(offField(), '3');
    chooseType('JSON');
    expect(onField()).toHaveValue('10');
    chooseType('Boolean');
    chooseType('Number');
    expect(onField()).toHaveValue('10');
    expect(offField()).toHaveValue('3');
    submit();
    await waitFor(() => expect(mockRouter.push).toHaveBeenCalled());
    expect(posts[0]).toMatchObject({ value_type: 'number', on_value: 10, off_value: 3 });
  });
});

describe('what is stopped before the request', () => {
  it('JSON that does not parse: "Not valid JSON:" under the field, linked and focused, nothing sent', async () => {
    const posts = install();
    render(<NewFeatureFlagPage />);
    fillName();
    chooseType('JSON');
    type(onField(), '{"color": blue}');
    type(offField(), '{}');
    submit();
    const error = await screen.findByTestId('flag-on-value-error');
    expect(error.textContent).toMatch(/^Not valid JSON: \S/);
    expect(onField()).toHaveAttribute('aria-invalid', 'true');
    expect(onField()).toHaveAccessibleDescription(/^Not valid JSON: /);
    expect(onField()).toHaveFocus();
    expect(offField()).not.toHaveAttribute('aria-invalid');
    expect(posts).toEqual([]);
    expect(mockRouter.push).not.toHaveBeenCalled();
  });

  it('JSON: empty, and a single value, each in its own words; focus on the first', async () => {
    const posts = install();
    render(<NewFeatureFlagPage />);
    fillName();
    chooseType('JSON');
    type(offField(), '"blue"');
    submit();
    expect(await screen.findByTestId('flag-on-value-error')).toHaveTextContent(
      'Enter a JSON object or array, such as {"color": "blue"}.',
    );
    expect(screen.getByTestId('flag-off-value-error')).toHaveTextContent(
      'A JSON value must be an object or an array. For a single text or number, choose String or Number.',
    );
    expect(onField()).toHaveFocus();
    expect(posts).toEqual([]);
  });

  it('Number: text that is not a number gets the example; focus moves to it', async () => {
    const posts = install();
    render(<NewFeatureFlagPage />);
    fillName();
    chooseType('Number');
    type(onField(), '10');
    type(offField(), 'ten');
    submit();
    expect(await screen.findByTestId('flag-off-value-error')).toHaveTextContent('Enter a number, such as 10, -3 or 2.5.');
    expect(offField()).toHaveAccessibleDescription(/^Enter a number, such as 10, -3 or 2\.5\./);
    expect(offField()).toHaveFocus();
    expect(screen.queryByTestId('flag-on-value-error')).toBeNull();
    expect(posts).toEqual([]);
  });

  it("equal values are refused at Off value in the API's words (3 and 3.0 are one number)", async () => {
    const posts = install();
    render(<NewFeatureFlagPage />);
    fillName();
    chooseType('Number');
    type(onField(), '3');
    type(offField(), '3.0');
    submit();
    expect(await screen.findByTestId('flag-off-value-error')).toHaveTextContent(
      'off_value equals on_value; the two values must differ, or turning the flag off changes nothing',
    );
    expect(offField()).toHaveFocus();
    expect(posts).toEqual([]);
  });

  it('editing a refused field clears its refusal', async () => {
    install();
    render(<NewFeatureFlagPage />);
    fillName();
    chooseType('Number');
    type(onField(), 'x');
    type(offField(), '1');
    submit();
    await screen.findByTestId('flag-on-value-error');
    type(onField(), '2');
    expect(screen.queryByTestId('flag-on-value-error')).toBeNull();
    expect(onField()).not.toHaveAttribute('aria-invalid');
  });
});

describe("the API's 422 on a value", () => {
  it('is shown under the field it names, verbatim, and focused; not in the general alert', async () => {
    install(() => {
      throw refusal('on_value', 'on_value larger than 8192 bytes', 'value_too_large');
    });
    render(<NewFeatureFlagPage />);
    fillName();
    chooseType('String');
    type(onField(), 'x'.repeat(9000));
    type(offField(), 'Buy now');
    submit();
    const error = await screen.findByTestId('flag-on-value-error');
    expect(error).toHaveTextContent('on_value larger than 8192 bytes');
    expect(onField()).toHaveAttribute('aria-invalid', 'true');
    expect(onField()).toHaveAccessibleDescription(/^on_value larger than 8192 bytes /);
    await waitFor(() => expect(onField()).toHaveFocus());
    expect(screen.queryByTestId('create-error')).toBeNull();
    expect(mockRouter.push).not.toHaveBeenCalled();
  });

  it("values_equal from the API is shown at Off value, the field it names", async () => {
    const posts = install(() => {
      throw refusal(
        'off_value',
        'off_value equals on_value; the two values must differ, or turning the flag off changes nothing',
        'values_equal',
      );
    });
    render(<NewFeatureFlagPage />);
    fillName();
    chooseType('JSON');
    type(onField(), '{"a": 1}');
    type(offField(), '{"a": 2}');
    submit();
    expect(await screen.findByTestId('flag-off-value-error')).toHaveTextContent(
      'off_value equals on_value; the two values must differ, or turning the flag off changes nothing',
    );
    await waitFor(() => expect(offField()).toHaveFocus());
    expect(posts).toHaveLength(1);
    expect(screen.queryByTestId('create-error')).toBeNull();
  });

  it('any other refusal keeps the general alert', async () => {
    install(() => {
      throw new ApiError({ status: 409, detail: 'A feature flag with this key already exists' });
    });
    render(<NewFeatureFlagPage />);
    fillName();
    chooseType('String');
    type(onField(), 'a');
    type(offField(), 'b');
    submit();
    expect(await screen.findByTestId('create-error')).toHaveTextContent('A feature flag with this key already exists');
    expect(screen.queryByTestId('flag-on-value-error')).toBeNull();
    expect(screen.queryByTestId('flag-off-value-error')).toBeNull();
  });
});

describe('the Values section is accessible (axe-core in jsdom; colour contrast is not computable here)', () => {
  const axeOptions: axe.RunOptions = { rules: { 'color-contrast': { enabled: false } } };

  async function violations(node: Element) {
    const result = await axe.run(node, axeOptions);
    return result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(', ')}`);
  }

  it.each(['Boolean', 'String', 'Number', 'JSON'] as const)('with %s chosen', async (name) => {
    install();
    const { container } = render(<NewFeatureFlagPage />);
    chooseType(name);
    expect(await violations(container)).toEqual([]);
  });

  it('with a refusal made here shown', async () => {
    install();
    const { container } = render(<NewFeatureFlagPage />);
    fillName();
    chooseType('JSON');
    type(onField(), '{');
    submit();
    await screen.findByTestId('flag-on-value-error');
    expect(await violations(container)).toEqual([]);
  });

  it("with the API's 422 shown", async () => {
    install(() => {
      throw refusal('off_value', 'off_value must be a number, because value_type is number', 'value_type_mismatch');
    });
    const { container } = render(<NewFeatureFlagPage />);
    fillName();
    chooseType('Number');
    type(onField(), '1');
    type(offField(), '2');
    submit();
    await screen.findByTestId('flag-off-value-error');
    expect(await violations(container)).toEqual([]);
  });
});
