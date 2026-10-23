import type { JsonSchema, SchemaFormErrors } from '@tai42/studio-sdk';
import { describe, expect, it } from 'vitest';

import {
  changedTopLevelKeys,
  errorsForFields,
  firstPageWithError,
  initialFormValue,
  isPlainObject,
  propertyOrder,
  resolvePages,
  singleFieldSchema,
  toSdkFieldWording,
} from '@/schema-form-helpers';
import type { FieldWording, FormPage } from '@/transcript-model';

const page = (fields: string[]): FormPage => ({
  title: '',
  fields,
  display: [],
  kind: 'input',
  buttonLabel: null,
});

describe('isPlainObject', () => {
  it('accepts a plain object and rejects arrays, null, and primitives', () => {
    expect(isPlainObject({})).toBe(true);
    expect(isPlainObject([])).toBe(false);
    expect(isPlainObject(null)).toBe(false);
    expect(isPlainObject('x')).toBe(false);
  });
});

describe('propertyOrder', () => {
  it('lists the schema properties in declared order, else an empty list', () => {
    const schema = { type: 'object', properties: { b: {}, a: {} } } as unknown as JsonSchema;
    expect(propertyOrder(schema)).toEqual(['b', 'a']);
    expect(propertyOrder({ type: 'object' } as unknown as JsonSchema)).toEqual([]);
  });
});

describe('resolvePages', () => {
  it('returns the per-send pages when set', () => {
    const pages = [page(['a'])];
    expect(resolvePages({} as unknown as JsonSchema, pages)).toEqual(pages);
  });

  const timed = {
    type: 'object',
    properties: {
      start: { type: 'string', format: 'date' },
      end: { type: 'string', format: 'date', rangeStart: 'start' },
      note: { type: 'string' },
      start_time: { type: 'string', format: 'time', timeOf: 'start' },
      end_time: { type: 'string', format: 'time', timeOf: 'end' },
    },
  } as unknown as JsonSchema;

  it('lists each time right after its date on the default page', () => {
    expect(resolvePages(timed, null)[0]?.fields).toEqual([
      'start',
      'start_time',
      'end',
      'end_time',
      'note',
    ]);
  });

  it('lists each time right after its date on every per-send page', () => {
    const pages = [page(['note']), page(['end_time', 'start_time', 'start', 'end'])];
    expect(resolvePages(timed, pages).map((resolved) => resolved.fields)).toEqual([
      ['note'],
      ['start', 'start_time', 'end', 'end_time'],
    ]);
  });

  it('falls back to one input page carrying every top-level property in order', () => {
    const schema = { type: 'object', properties: { x: {}, y: {} } } as unknown as JsonSchema;
    expect(resolvePages(schema, null)).toEqual([
      { title: '', fields: ['x', 'y'], display: [], kind: 'input', buttonLabel: null },
    ]);
  });
});

describe('initialFormValue', () => {
  it('overlays prefilled values onto the schema base', () => {
    const schema = {
      type: 'object',
      properties: { name: { type: 'string' } },
    } as unknown as JsonSchema;
    const result = initialFormValue(schema, { values: { name: 'Al' }, options: {} }) as Record<
      string,
      unknown
    >;
    expect(result.name).toBe('Al');
  });

  it('returns a plain object when there is no prefill', () => {
    const schema = {
      type: 'object',
      properties: { name: { type: 'string' } },
    } as unknown as JsonSchema;
    expect(isPlainObject(initialFormValue(schema, null))).toBe(true);
  });
});

describe('changedTopLevelKeys', () => {
  it('reports the keys whose value differs, treating a non-object as empty', () => {
    expect(changedTopLevelKeys({ a: 1, b: 2 }, { a: 1, b: 3 })).toEqual(['b']);
    expect(changedTopLevelKeys(null, { a: 1 })).toEqual(['a']);
    expect(changedTopLevelKeys({ a: 1 }, 'x')).toEqual(['a']);
  });
});

describe('singleFieldSchema', () => {
  it('builds a one-property object schema and marks the field required when it is', () => {
    const schema = {
      type: 'object',
      properties: { name: { type: 'string' } },
      required: ['name'],
    } as unknown as JsonSchema;
    expect(singleFieldSchema(schema, 'name')).toEqual({
      type: 'object',
      properties: { name: { type: 'string' } },
      required: ['name'],
    });
  });

  it('drops a non-natively-rendered string format but keeps a date format', () => {
    const schema = {
      type: 'object',
      properties: {
        when: { type: 'string', format: 'date-time' },
        day: { type: 'string', format: 'date' },
      },
    } as unknown as JsonSchema;
    expect(singleFieldSchema(schema, 'when').properties).toEqual({ when: { type: 'string' } });
    expect(singleFieldSchema(schema, 'day').properties).toEqual({
      day: { type: 'string', format: 'date' },
    });
  });

  it('yields empty properties when the field is absent', () => {
    const schema = { type: 'object', properties: {} } as unknown as JsonSchema;
    expect(singleFieldSchema(schema, 'missing').properties).toEqual({});
  });
});

describe('errorsForFields', () => {
  it('keeps only errors whose top-level field is in the set, mapping nested paths to their field', () => {
    const errors = {
      name: 'required',
      'addr.city': 'required',
      'items[0]': 'bad',
      other: 'x',
    } as unknown as SchemaFormErrors;
    expect(errorsForFields(errors, ['name', 'addr', 'items'])).toEqual({
      name: 'required',
      'addr.city': 'required',
      'items[0]': 'bad',
    });
  });
});

describe('firstPageWithError', () => {
  it('returns the index of the first page carrying an errored field (nested paths included)', () => {
    const pages = [page(['name']), page(['addr'])];
    const errors = { 'addr.city': 'required' } as unknown as SchemaFormErrors;
    expect(firstPageWithError(pages, errors)).toBe(1);
  });

  it('returns -1 when no page carries an errored field', () => {
    const pages = [page(['name'])];
    const errors = { phone: 'required' } as unknown as SchemaFormErrors;
    expect(firstPageWithError(pages, errors)).toBe(-1);
  });
});

describe('toSdkFieldWording', () => {
  const unset: FieldWording = {
    choiceLabel: null,
    invalid: null,
    trueLabel: null,
    falseLabel: null,
    required: null,
    addItem: null,
    removeItem: null,
    itemLabel: null,
    noItems: null,
  };

  it('hands the SDK every word it draws, and only the words the caller set', () => {
    const sdk = toSdkFieldWording({
      email: { ...unset, invalid: 'Not an address', required: 'Your email, please' },
      tags: {
        ...unset,
        addItem: 'Add a tag',
        removeItem: 'Drop',
        itemLabel: 'Tag {n}',
        noItems: 'No tags yet',
      },
      kind: { ...unset, choiceLabel: 'Pick a kind' },
      agree: { ...unset, trueLabel: 'Agreed', falseLabel: 'Declined' },
    });

    expect(sdk).toEqual({
      email: { invalid: 'Not an address', required: 'Your email, please' },
      tags: {
        addItem: 'Add a tag',
        removeItem: 'Drop',
        itemLabel: 'Tag {n}',
        noItems: 'No tags yet',
      },
      kind: { choiceLabel: 'Pick a kind' },
    });
  });

  it('keeps the words of a field named after a built-in object member as its own entry', () => {
    const fields = Object.fromEntries([['__proto__', { ...unset, required: 'Say something' }]]);

    const sdk = toSdkFieldWording(fields);

    expect(Object.hasOwn(sdk, '__proto__')).toBe(true);
    expect(Object.getPrototypeOf(sdk)).toBe(Object.prototype);
  });
});
