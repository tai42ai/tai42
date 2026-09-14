import { describe, expect, it } from 'vitest';

import { applyFrame } from '@/frame-reducer';
import { EMPTY_MODEL } from '@/transcript-model';
import { TS, fold, formFrame } from '@/frame-reducer.test-support';

describe('applyFrame: chat.form', () => {
  it('folds a form entry with its schema and submission token', () => {
    const model = fold(EMPTY_MODEL, formFrame());

    expect(model.items).toEqual([
      {
        kind: 'form',
        id: 'f1',
        text: 'Fill this in',
        schema: { type: 'object', properties: { note: { type: 'string' } } },
        token: 'tok-1',
        media: null,
        location: null,
        formData: null,
        pages: null,
        ts: TS,
      },
    ]);
  });

  it('folds a form entry carrying per-send prefill, options and step pages', () => {
    const model = fold(
      EMPTY_MODEL,
      formFrame({
        schema: {
          type: 'object',
          properties: { note: { type: 'string' }, colour: { type: 'string' } },
        },
        data: {
          values: { note: 'draft' },
          options: { colour: [{ value: 'r', label: 'Red' }] },
        },
        pages: [
          { title: 'Basics', fields: ['note'] },
          { title: 'Extras', fields: ['colour'] },
        ],
      }),
    );

    expect(model.items[0]).toMatchObject({
      kind: 'form',
      formData: {
        values: { note: 'draft' },
        options: { colour: [{ value: 'r', label: 'Red' }] },
      },
      pages: [
        { title: 'Basics', fields: ['note'] },
        { title: 'Extras', fields: ['colour'] },
      ],
    });
  });

  it('folds a form entry carrying media through the same vetted parse a card uses', () => {
    const model = fold(
      EMPTY_MODEL,
      formFrame({ media: [{ kind: 'image', url: 'https://example.com/a.png' }] }),
    );

    expect(model.items[0]).toMatchObject({
      kind: 'form',
      media: [{ kind: 'image', url: 'https://example.com/a.png', caption: null, filename: null }],
    });
  });

  it('folds a location on a form card', () => {
    const model = fold(
      EMPTY_MODEL,
      formFrame({ location: { latitude: 51.5, longitude: -0.12, name: 'London' } }),
    );

    expect(model.items[0]).toMatchObject({
      kind: 'form',
      location: { latitude: 51.5, longitude: -0.12, name: 'London', address: null },
    });
  });

  it.each([
    ['a form missing its token', formFrame({ token: undefined })],
    ['a form with a blank token', formFrame({ token: '   ' })],
    ['a form with a non-string token', formFrame({ token: 42 })],
    ['a form missing its schema', formFrame({ schema: undefined })],
    ['a form whose schema is not an object', formFrame({ schema: ['not', 'an', 'object'] })],
    ['a form missing its text', formFrame({ text: undefined })],
    ['a form with an unparsable timestamp', formFrame({ ts: 'soon' })],
    [
      'a form with an off-scheme image',
      formFrame({ media: [{ kind: 'image', url: 'http://example.com/a.png' }] }),
    ],
    ['a form whose data is not an object', formFrame({ data: 42 })],
    ['a form whose data.values is not an object', formFrame({ data: { values: 1, options: {} } })],
    [
      'a form whose per-send option list is empty',
      formFrame({ data: { values: {}, options: { colour: [] } } }),
    ],
    ['a form whose pages is not a list', formFrame({ pages: 'first' })],
    ['a form with an empty pages list', formFrame({ pages: [] })],
    ['a form page missing its title', formFrame({ pages: [{ fields: ['note'] }] })],
  ])('surfaces %s as malformed rather than dropping the widget', (_label, bad) => {
    expect(applyFrame(EMPTY_MODEL, bad).kind).toBe('malformed');
  });
});
