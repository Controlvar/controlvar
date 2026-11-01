/**
 * The dashboard's side of a flag's values (#1497): what it checks before a
 * request, in the API's words where the API makes the same check
 * (backend/app/core/flag_values.py), and how it reads the API's 422.
 */
import { ApiError } from '@/services/api';
import {
  JSON_EMPTY,
  JSON_NOT_CONTAINER,
  NUMBER_INVALID,
  checkEditedValues,
  checkNewValues,
  flagValueType,
  nestedDeeperThan,
  parseValue,
  typeLabel,
  valueRefusals,
  valueToDisplay,
  valueToText,
  valuesEqual,
} from '@/utils/flagValues';

describe('parseValue', () => {
  it('a string is the text as typed, empty included', () => {
    expect(parseValue('string', 'on_value', '  Buy now ')).toEqual({ ok: true, value: '  Buy now ' });
    expect(parseValue('string', 'off_value', '')).toEqual({ ok: true, value: '' });
  });

  it.each([
    ['10', 10],
    ['-3', -3],
    ['2.5', 2.5],
    [' 7 ', 7],
    ['1e3', 1000],
    ['0', 0],
    ['9007199254740991', 9007199254740991],
    ['-9007199254740991', -9007199254740991],
  ])('a number: %j reads %p', (text, value) => {
    expect(parseValue('number', 'on_value', text)).toEqual({ ok: true, value });
  });

  it.each(['', 'ten', '+1', '.5', '5.', '01', '0x10', 'Infinity', 'NaN', '1,000', '10 %'])(
    'a number: %j is refused with the example',
    (text) => {
      expect(parseValue('number', 'on_value', text)).toEqual({ ok: false, message: NUMBER_INVALID });
    },
  );

  it.each([
    ['1e400', 'on_value'],
    ['9007199254740992', 'off_value'],
    ['-12345678901234567890', 'on_value'],
  ] as const)("a number: %j cannot be sent as typed, so it gets the API's range refusal at %s", (text, field) => {
    expect(parseValue('number', field, text)).toEqual({
      ok: false,
      message: `${field}: a number is outside -9007199254740991 to 9007199254740991`,
    });
  });

  it('JSON: an object or an array, parsed', () => {
    expect(parseValue('json', 'on_value', '{"color": "blue"}')).toEqual({ ok: true, value: { color: 'blue' } });
    expect(parseValue('json', 'on_value', ' [1, 2] ')).toEqual({ ok: true, value: [1, 2] });
  });

  it('JSON: empty, unparsable, a scalar', () => {
    expect(parseValue('json', 'on_value', '  ')).toEqual({ ok: false, message: JSON_EMPTY });
    const bad = parseValue('json', 'on_value', '{"color": blue}');
    expect(bad.ok).toBe(false);
    expect(!bad.ok && bad.message).toMatch(/^Not valid JSON: \S/);
    for (const scalar of ['"blue"', '10', 'true', 'null']) {
      expect(parseValue('json', 'on_value', scalar)).toEqual({ ok: false, message: JSON_NOT_CONTAINER });
    }
  });

  it("JSON: deeper than 10 levels gets the API's depth refusal; 10 levels is accepted", () => {
    const ten = '['.repeat(10) + ']'.repeat(10);
    const eleven = '['.repeat(11) + ']'.repeat(11);
    expect(parseValue('json', 'on_value', ten).ok).toBe(true);
    expect(parseValue('json', 'off_value', eleven)).toEqual({
      ok: false,
      message: 'off_value nested more than 10 levels deep',
    });
  });

  it('JSON: a number inside that JSON.stringify would send as null, or round, is refused', () => {
    expect(parseValue('json', 'on_value', '{"limit": 1e400}')).toEqual({
      ok: false,
      message: 'on_value: a number is outside -9007199254740991 to 9007199254740991',
    });
    expect(parseValue('json', 'on_value', '[9007199254740993]').ok).toBe(false);
    expect(parseValue('json', 'on_value', '[9007199254740991]').ok).toBe(true);
  });
});

describe('valuesEqual mirrors jsonb equality (the API values_equal, PF-5)', () => {
  it.each([
    [3, 3.0, true],
    [{ a: 1 }, { a: 1.0 }, true],
    [{ a: 1, b: [1, { c: 'x' }] }, { b: [1, { c: 'x' }], a: 1 }, true],
    ['Buy now', 'Buy now', true],
    ['', '', true],
    [[1, 2], [2, 1], false],
    [{ a: true }, { a: 1 }, false],
    [[true], [1], false],
    ['3', 3, false],
    [{ a: 1 }, { a: 1, b: 2 }, false],
    [{ a: 1 }, [1], false],
    [{ a: null }, { a: null }, true],
    [{ a: null }, { a: false }, false],
    ['Buy now', 'Buy Now', false],
  ])('%j and %j: %p', (a, b, equal) => {
    expect(valuesEqual(a, b)).toBe(equal);
    expect(valuesEqual(b, a)).toBe(equal);
  });
});

describe('nestedDeeperThan', () => {
  it('counts the value itself as level 1, and a scalar as none', () => {
    expect(nestedDeeperThan('x', 0)).toBe(false);
    expect(nestedDeeperThan({}, 1)).toBe(false);
    expect(nestedDeeperThan({ a: [] }, 1)).toBe(true);
    expect(nestedDeeperThan({ a: [] }, 2)).toBe(false);
  });
});

describe('a new flag', () => {
  it('sends both values when both read and differ', () => {
    expect(checkNewValues('number', { on_value: '10', off_value: '3' })).toEqual({
      errors: {},
      values: { on_value: 10, off_value: 3 },
    });
  });

  it("refuses equal values at off_value, in the API's words", () => {
    expect(checkNewValues('number', { on_value: '3', off_value: '3.0' })).toEqual({
      errors: { off_value: 'off_value equals on_value; the two values must differ, or turning the flag off changes nothing' },
      values: {},
    });
    expect(checkNewValues('string', { on_value: '', off_value: '' }).errors.off_value).toMatch(/^off_value equals on_value/);
  });

  it('reports each field that does not read, and sends nothing', () => {
    expect(checkNewValues('json', { on_value: '10', off_value: '' })).toEqual({
      errors: { on_value: JSON_NOT_CONTAINER, off_value: JSON_EMPTY },
      values: {},
    });
  });
});

describe('an edited flag (EMF-5: the pair as it will be stored)', () => {
  const stored = { on_value: 'a', off_value: 'b' };
  const initial = { on_value: 'a', off_value: 'b' };

  it('an unchanged field is not sent, and not even read', () => {
    expect(checkEditedValues('string', initial, initial, stored)).toEqual({ errors: {}, values: {} });
    // A stored number this page could not send as typed is left alone while its text is untouched.
    expect(
      checkEditedValues('number', { on_value: '1e400', off_value: '3' }, { on_value: '1e400', off_value: '3' }, { on_value: Number.POSITIVE_INFINITY, off_value: 3 }),
    ).toEqual({ errors: {}, values: {} });
  });

  it('a changed field equal to the stored value (re-indented, 10.0 for 10) is not sent', () => {
    expect(
      checkEditedValues('json', { on_value: '{"a":1}', off_value: '[1]' }, { on_value: '{\n  "a": 1\n}', off_value: '[\n  1\n]' }, { on_value: { a: 1 }, off_value: [1] }),
    ).toEqual({ errors: {}, values: {} });
    expect(
      checkEditedValues('number', { on_value: '10.0', off_value: '3' }, { on_value: '10', off_value: '3' }, { on_value: 10, off_value: 3 }),
    ).toEqual({ errors: {}, values: {} });
  });

  it('sends only the field that changed', () => {
    expect(checkEditedValues('string', { on_value: 'c', off_value: 'b' }, initial, stored)).toEqual({
      errors: {},
      values: { on_value: 'c' },
    });
  });

  it('on_value sent equal to the stored off_value is refused at on_value', () => {
    expect(checkEditedValues('string', { on_value: 'b', off_value: 'b' }, initial, stored)).toEqual({
      errors: { on_value: 'on_value equals off_value; the two values must differ, or turning the flag off changes nothing' },
      values: {},
    });
  });

  it('both sent and equal is refused at off_value', () => {
    expect(checkEditedValues('string', { on_value: 'x', off_value: 'x' }, initial, stored).errors).toEqual({
      off_value: 'off_value equals on_value; the two values must differ, or turning the flag off changes nothing',
    });
  });
});

describe('valueRefusals', () => {
  it("takes the on_value and off_value items, in the API's words, and keeps the rest", () => {
    const err = new ApiError({
      status: 422,
      detail: [
        { loc: ['body', 'on_value'], msg: 'on_value must be a number, because value_type is number', type: 'value_type_mismatch' },
        { loc: ['body', 'key'], msg: 'Value error, Key must be lowercase', type: 'value_error' },
      ],
    });
    const refused = valueRefusals(err);
    expect(refused?.fields).toEqual({ on_value: 'on_value must be a number, because value_type is number' });
    expect(refused?.rest?.status).toBe(422);
    expect(refused?.rest?.message).toBe('key: Value error, Key must be lowercase');
  });

  it('is null for an error with no value refusal, and for anything but a 422', () => {
    expect(valueRefusals(new ApiError({ status: 422, detail: [{ loc: ['body', 'key'], msg: 'bad', type: 'x' }] }))).toBeNull();
    expect(valueRefusals(new ApiError({ status: 409, detail: 'A feature flag with this key already exists' }))).toBeNull();
    expect(valueRefusals(new Error('boom'))).toBeNull();
  });

  it('has no rest when every item is a value refusal', () => {
    const err = new ApiError({
      status: 422,
      detail: [{ loc: ['body', 'off_value'], msg: 'off_value larger than 8192 bytes', type: 'value_too_large' }],
    });
    expect(valueRefusals(err)).toEqual({ fields: { off_value: 'off_value larger than 8192 bytes' }, rest: null });
  });
});

describe('reading a flag', () => {
  it('a flag with no type (a server without values) is boolean', () => {
    expect(flagValueType({})).toBe('boolean');
    expect(flagValueType({ value_type: 'json' })).toBe('json');
  });

  it('names the types, and a type this dashboard does not know as the API wrote it', () => {
    expect(['boolean', 'string', 'number', 'json', 'colour'].map(typeLabel)).toEqual([
      'Boolean',
      'String',
      'Number',
      'JSON',
      'colour',
    ]);
  });

  it('fills a field: a string as is, a number as written, JSON with 2 spaces', () => {
    expect(valueToText('string', 'Buy now')).toBe('Buy now');
    expect(valueToText('number', 2.5)).toBe('2.5');
    expect(valueToText('json', { color: 'blue' })).toBe('{\n  "color": "blue"\n}');
  });

  it('shows a value read-only as JSON, so an empty string reads ""', () => {
    expect(valueToDisplay('Buy now')).toBe('"Buy now"');
    expect(valueToDisplay('')).toBe('""');
    expect(valueToDisplay(10)).toBe('10');
  });
});
