import { describe, expect, it } from 'vitest';

import {
  cardOptionsOf,
  clientMessageIdOf,
  facetOf,
  footerOf,
  formPagesOf,
  formPrefillOf,
  formReactionsOf,
  formUpdateOf,
  headerOf,
  isRecord,
  isTimestamp,
  locationOf,
  mediaOf,
  optionsOf,
  parseJson,
  sectionsOf,
} from '@/frame-parse';

const MEDIA_REF = `/api/interactions/media/${'M'.repeat(43)}`;

describe('isRecord', () => {
  it('accepts a plain object and rejects arrays, null, and primitives', () => {
    expect(isRecord({ a: 1 })).toBe(true);
    expect(isRecord([1, 2])).toBe(false);
    expect(isRecord(null)).toBe(false);
    expect(isRecord('x')).toBe(false);
  });
});

describe('parseJson', () => {
  it('returns null for empty input, invalid JSON, and non-object JSON', () => {
    expect(parseJson('')).toBeNull();
    expect(parseJson('not json')).toBeNull();
    expect(parseJson('[1,2]')).toBeNull();
    expect(parseJson('42')).toBeNull();
  });

  it('returns the parsed object for a JSON object', () => {
    expect(parseJson('{"a":1}')).toEqual({ a: 1 });
  });
});

describe('isTimestamp', () => {
  it('accepts a parseable date string and rejects non-strings and unparseable text', () => {
    expect(isTimestamp('2026-10-05T00:00:00Z')).toBe(true);
    expect(isTimestamp('not a date')).toBe(false);
    expect(isTimestamp(123)).toBe(false);
  });
});

describe('optionsOf', () => {
  it('maps absent to null, a string list to itself, and anything else to undefined', () => {
    expect(optionsOf(null)).toBeNull();
    expect(optionsOf(undefined)).toBeNull();
    expect(optionsOf(['a', 'b'])).toEqual(['a', 'b']);
    expect(optionsOf('a')).toBeUndefined();
    expect(optionsOf([1])).toBeUndefined();
  });
});

describe('cardOptionsOf', () => {
  it('maps absent to null and an empty or non-array list to undefined', () => {
    expect(cardOptionsOf(null)).toBeNull();
    expect(cardOptionsOf([])).toBeUndefined();
    expect(cardOptionsOf('x')).toBeUndefined();
  });

  it('parses a valid reply option, keeping an absent description/id as null', () => {
    expect(cardOptionsOf([{ kind: 'reply', text: 'Yes' }])).toEqual([
      { kind: 'reply', text: 'Yes', description: null, id: null },
    ]);
  });

  it('rejects a reply with a blank text, a non-string description, or a non-string id', () => {
    expect(cardOptionsOf([{ kind: 'reply', text: '  ' }])).toBeUndefined();
    expect(cardOptionsOf([{ kind: 'reply', text: 'Yes', description: 123 }])).toBeUndefined();
    expect(cardOptionsOf([{ kind: 'reply', text: 'Yes', id: 123 }])).toBeUndefined();
  });

  it('parses a link option and rejects a blank label or a non-http(s) url', () => {
    expect(cardOptionsOf([{ kind: 'link', label: 'Site', url: 'https://example.test/x' }])).toEqual(
      [{ kind: 'link', label: 'Site', url: 'https://example.test/x' }],
    );
    expect(
      cardOptionsOf([{ kind: 'link', label: '', url: 'https://example.test/x' }]),
    ).toBeUndefined();
    expect(
      cardOptionsOf([{ kind: 'link', label: 'Site', url: 'ftp://example.test' }]),
    ).toBeUndefined();
    expect(cardOptionsOf([{ kind: 'link', label: 'Site', url: 123 }])).toBeUndefined();
  });

  it('rejects a non-record entry', () => {
    expect(cardOptionsOf([123])).toBeUndefined();
  });
});

describe('sectionsOf', () => {
  it('maps absent to null and an empty or non-record section to undefined', () => {
    expect(sectionsOf(null)).toBeNull();
    expect(sectionsOf([])).toBeUndefined();
    expect(sectionsOf([123])).toBeUndefined();
  });

  it('rejects a section with a blank title or an empty/non-array rows list', () => {
    expect(sectionsOf([{ title: '', rows: [{ kind: 'reply', text: 'a' }] }])).toBeUndefined();
    expect(sectionsOf([{ title: 'T', rows: [] }])).toBeUndefined();
    expect(sectionsOf([{ title: 'T', rows: 'x' }])).toBeUndefined();
  });

  it('rejects a section whose row is an off-shape reply', () => {
    expect(sectionsOf([{ title: 'T', rows: [{ kind: 'reply', text: '' }] }])).toBeUndefined();
  });

  it('parses a valid section', () => {
    expect(sectionsOf([{ title: 'T', rows: [{ kind: 'reply', text: 'a' }] }])).toEqual([
      { title: 'T', rows: [{ kind: 'reply', text: 'a', description: null, id: null }] },
    ]);
  });
});

describe('headerOf', () => {
  it('maps absent to null, rejects a link media header, and parses a display media header', () => {
    expect(headerOf(null)).toBeNull();
    expect(headerOf({ kind: 'link', url: 'https://example.test/x' })).toBeUndefined();
    expect(headerOf({ kind: 'image', url: MEDIA_REF })).toEqual({
      kind: 'image',
      url: MEDIA_REF,
      caption: null,
      filename: null,
    });
  });
});

describe('footerOf', () => {
  it('maps absent to null, a non-string or blank to undefined, and a string to itself', () => {
    expect(footerOf(undefined)).toBeNull();
    expect(footerOf(123)).toBeUndefined();
    expect(footerOf('  ')).toBeUndefined();
    expect(footerOf('note')).toBe('note');
  });
});

describe('locationOf', () => {
  it('maps absent to null and a non-record to undefined', () => {
    expect(locationOf(null)).toBeNull();
    expect(locationOf(123)).toBeUndefined();
  });

  it('rejects out-of-range coordinates and a non-string name or address', () => {
    expect(locationOf({ latitude: 91, longitude: 0 })).toBeUndefined();
    expect(locationOf({ latitude: 0, longitude: 181 })).toBeUndefined();
    expect(locationOf({ latitude: 0, longitude: 0, name: 123 })).toBeUndefined();
    expect(locationOf({ latitude: 0, longitude: 0, address: 123 })).toBeUndefined();
  });

  it('parses an in-range location with an optional name/address', () => {
    expect(locationOf({ latitude: 1.5, longitude: 2.5, name: 'Spot' })).toEqual({
      latitude: 1.5,
      longitude: 2.5,
      name: 'Spot',
      address: null,
    });
  });
});

describe('mediaOf', () => {
  it('maps absent to null and a non-array to undefined', () => {
    expect(mediaOf(null)).toBeNull();
    expect(mediaOf('x')).toBeUndefined();
  });

  it('rejects a non-record item and an unknown kind', () => {
    expect(mediaOf([123])).toBeUndefined();
    expect(mediaOf([{ kind: 'spreadsheet', url: MEDIA_REF }])).toBeUndefined();
  });

  it('rejects a file-kind source that is neither https nor the served-media ref', () => {
    expect(mediaOf([{ kind: 'image', url: 'http://example.test/x.png' }])).toBeUndefined();
    expect(mediaOf([{ kind: 'image', url: '/some/other/path' }])).toBeUndefined();
  });

  it('rejects a non-string caption and a filename on a non-document kind', () => {
    expect(mediaOf([{ kind: 'image', url: MEDIA_REF, caption: 123 }])).toBeUndefined();
    expect(mediaOf([{ kind: 'image', url: MEDIA_REF, filename: 'a.png' }])).toBeUndefined();
    expect(mediaOf([{ kind: 'document', url: MEDIA_REF, filename: 123 }])).toBeUndefined();
  });

  it('rejects a url carrying userinfo', () => {
    expect(mediaOf([{ kind: 'image', url: 'https://user@example.test/x.png' }])).toBeUndefined();
  });

  it('parses a document with a filename and an https image', () => {
    expect(mediaOf([{ kind: 'document', url: MEDIA_REF, filename: 'report.pdf' }])).toEqual([
      { kind: 'document', url: MEDIA_REF, caption: null, filename: 'report.pdf' },
    ]);
    expect(mediaOf([{ kind: 'image', url: 'https://example.test/x.png' }])).toEqual([
      { kind: 'image', url: 'https://example.test/x.png', caption: null, filename: null },
    ]);
  });
});

describe('clientMessageIdOf', () => {
  it('maps a string to itself, absent to null, and anything else to undefined', () => {
    expect(clientMessageIdOf('k-1')).toBe('k-1');
    expect(clientMessageIdOf(undefined)).toBeNull();
    expect(clientMessageIdOf(123)).toBeUndefined();
  });
});

describe('formPrefillOf', () => {
  it('maps absent to null and a non-record or non-record values to undefined', () => {
    expect(formPrefillOf(null)).toBeNull();
    expect(formPrefillOf(123)).toBeUndefined();
    expect(formPrefillOf({ values: 'x' })).toBeUndefined();
  });

  it('rejects an off-shape options map and parses a valid prefill', () => {
    expect(formPrefillOf({ values: {}, options: { f: [] } })).toBeUndefined();
    expect(formPrefillOf({ values: { a: 1 }, options: { f: [{ value: 'v' }] } })).toEqual({
      values: { a: 1 },
      options: { f: [{ value: 'v', label: null }] },
    });
  });
});

describe('formUpdateOf', () => {
  it('rejects a non-record and non-record values/display', () => {
    expect(formUpdateOf(123)).toBeUndefined();
    expect(formUpdateOf({ values: 'x' })).toBeUndefined();
    expect(formUpdateOf({ display: 123 })).toBeUndefined();
  });

  it('rejects an off-shape options or errors map', () => {
    expect(formUpdateOf({ options: 123 })).toBeUndefined();
    expect(formUpdateOf({ options: { f: [{ value: '' }] } })).toBeUndefined();
    expect(formUpdateOf({ options: { f: [123] } })).toBeUndefined();
    expect(formUpdateOf({ errors: 123 })).toBeUndefined();
    expect(formUpdateOf({ errors: { f: 123 } })).toBeUndefined();
  });

  it('rejects a form option with a non-string label', () => {
    expect(formUpdateOf({ options: { f: [{ value: 'v', label: 123 }] } })).toBeUndefined();
  });

  it('fills every absent part with an empty default', () => {
    expect(formUpdateOf({})).toEqual({ values: {}, options: {}, errors: {}, display: {} });
  });

  it('parses a populated update', () => {
    expect(
      formUpdateOf({ values: { a: 1 }, options: { f: [{ value: 'v' }] }, errors: { a: 'bad' } }),
    ).toEqual({
      values: { a: 1 },
      options: { f: [{ value: 'v', label: null }] },
      errors: { a: 'bad' },
      display: {},
    });
  });
});

describe('formReactionsOf', () => {
  it('maps absent to null and a non-record to undefined', () => {
    expect(formReactionsOf(null)).toBeNull();
    expect(formReactionsOf(123)).toBeUndefined();
  });

  it('rejects an off-shape field_changed, page_advanced, choices, or submitted', () => {
    expect(formReactionsOf({ field_changed: 123 })).toBeUndefined();
    expect(formReactionsOf({ page_advanced: 123 })).toBeUndefined();
    expect(formReactionsOf({ choices: 123 })).toBeUndefined();
    expect(formReactionsOf({ field_changed: [''] })).toBeUndefined();
    expect(formReactionsOf({ submitted: 'yes' })).toBeUndefined();
  });

  it('parses triggers and keeps an explicit submitted flag', () => {
    expect(formReactionsOf({ field_changed: ['a'], submitted: true })).toEqual({
      fieldChanged: ['a'],
      pageAdvanced: [],
      choices: [],
      submitted: true,
    });
  });
});

describe('formPagesOf', () => {
  it('maps absent to null and an empty or non-array list to undefined', () => {
    expect(formPagesOf(null)).toBeNull();
    expect(formPagesOf([])).toBeUndefined();
    expect(formPagesOf([123])).toBeUndefined();
  });

  it('rejects a page with a blank title, a non-array or blank field, or an input page with no fields', () => {
    expect(formPagesOf([{ title: '', fields: ['a'] }])).toBeUndefined();
    expect(formPagesOf([{ title: 'T', fields: 123 }])).toBeUndefined();
    expect(formPagesOf([{ title: 'T', fields: [123] }])).toBeUndefined();
    expect(formPagesOf([{ title: 'T', fields: [] }])).toBeUndefined();
  });

  it('rejects an unknown kind and an off-shape display block', () => {
    expect(formPagesOf([{ title: 'T', fields: ['a'], kind: 'wizard' }])).toBeUndefined();
    expect(formPagesOf([{ title: 'T', fields: ['a'], display: [123] }])).toBeUndefined();
    expect(
      formPagesOf([{ title: 'T', fields: ['a'], display: [{ kind: 'photo' }] }]),
    ).toBeUndefined();
  });

  it('rejects a display block with a non-string text/src/alt/slot or an off-scheme image src', () => {
    expect(
      formPagesOf([{ title: 'T', fields: ['a'], display: [{ kind: 'body', text: 123 }] }]),
    ).toBeUndefined();
    expect(
      formPagesOf([{ title: 'T', fields: ['a'], display: [{ kind: 'image', src: 123 }] }]),
    ).toBeUndefined();
    expect(
      formPagesOf([
        {
          title: 'T',
          fields: ['a'],
          display: [{ kind: 'image', src: 'http://example.test/x.png' }],
        },
      ]),
    ).toBeUndefined();
    expect(
      formPagesOf([{ title: 'T', fields: ['a'], display: [{ kind: 'body', alt: 123 }] }]),
    ).toBeUndefined();
    expect(
      formPagesOf([{ title: 'T', fields: ['a'], display: [{ kind: 'body', slot: 123 }] }]),
    ).toBeUndefined();
  });

  it('allows an empty review page and parses a populated input page', () => {
    expect(formPagesOf([{ title: 'Review', fields: [], kind: 'review' }])).toEqual([
      { title: 'Review', fields: [], display: [], kind: 'review' },
    ]);
    expect(
      formPagesOf([{ title: 'Step', fields: ['a'], display: [{ kind: 'heading', text: 'Hi' }] }]),
    ).toEqual([
      {
        title: 'Step',
        fields: ['a'],
        display: [{ kind: 'heading', text: 'Hi', src: null, alt: null, slot: null }],
        kind: 'input',
      },
    ]);
  });
});

describe('facetOf', () => {
  it('parses an external facet and rejects a missing ticket, a stray schema, or stray extras', () => {
    expect(facetOf('ticket-1', undefined, undefined, undefined, undefined, 'external')).toEqual({
      answerFormat: 'external',
      callbackUrl: 'ticket-1',
      schema: null,
      formData: null,
      pages: null,
    });
    expect(
      facetOf(undefined, undefined, undefined, undefined, undefined, 'external'),
    ).toBeUndefined();
    expect(
      facetOf('ticket-1', { a: 1 }, undefined, undefined, undefined, 'external'),
    ).toBeUndefined();
    expect(
      facetOf('ticket-1', undefined, { v: 1 }, undefined, undefined, 'external'),
    ).toBeUndefined();
  });

  it('parses a form facet and rejects a missing schema, a stray callback, or malformed extras', () => {
    expect(facetOf(undefined, { type: 'object' }, undefined, undefined, undefined, 'form')).toEqual(
      {
        answerFormat: 'form',
        callbackUrl: null,
        schema: { type: 'object' },
        formData: null,
        pages: null,
        reactions: null,
      },
    );
    expect(
      facetOf('t', { type: 'object' }, undefined, undefined, undefined, 'form'),
    ).toBeUndefined();
    expect(facetOf(undefined, undefined, undefined, undefined, undefined, 'form')).toBeUndefined();
    expect(
      facetOf(undefined, { type: 'object' }, 123, undefined, undefined, 'form'),
    ).toBeUndefined();
    expect(
      facetOf(undefined, { type: 'object' }, undefined, [], undefined, 'form'),
    ).toBeUndefined();
    expect(
      facetOf(undefined, { type: 'object' }, undefined, undefined, 123, 'form'),
    ).toBeUndefined();
  });

  it('parses a scalar facet and rejects any stray callback, schema, or extras on it', () => {
    expect(facetOf(undefined, undefined, undefined, undefined, undefined, 'text')).toEqual({
      answerFormat: 'text',
      callbackUrl: null,
      schema: null,
      formData: null,
      pages: null,
    });
    expect(facetOf('t', undefined, undefined, undefined, undefined, 'text')).toBeUndefined();
    expect(facetOf(undefined, { a: 1 }, undefined, undefined, undefined, 'text')).toBeUndefined();
    expect(facetOf(undefined, undefined, { v: 1 }, undefined, undefined, 'text')).toBeUndefined();
  });
});
