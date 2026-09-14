import { describe, expect, it } from 'vitest';

import { applyFrame } from '@/frame-reducer';
import { EMPTY_MODEL } from '@/transcript-model';
import { CALLBACK, DEADLINE, frame, fold, questionFrame } from '@/frame-reducer.test-support';

const question = questionFrame('confirm');

describe('applyFrame: chat.question', () => {
  it('folds a question entry with its widget fields', () => {
    const model = fold(EMPTY_MODEL, question);

    expect(model.items[0]).toMatchObject({
      kind: 'question',
      interactionId: 'int-1',
      answerFormat: 'confirm',
      options: null,
      timeoutAt: DEADLINE,
    });
  });

  it.each(['text', 'confirm', 'select'])(
    'folds a %s question, which the wire sends with no callback ticket at all',
    (format) => {
      const model = fold(EMPTY_MODEL, questionFrame(format));

      expect(model.items[0]).toMatchObject({
        kind: 'question',
        answerFormat: format,
        callbackUrl: null,
      });
    },
  );

  it('carries the callback ticket on the one format whose widget opens it', () => {
    const model = fold(EMPTY_MODEL, questionFrame('external', { callback_url: CALLBACK }));

    expect(model.items[0]).toMatchObject({ answerFormat: 'external', callbackUrl: CALLBACK });
  });

  it('carries the answer schema on the one format whose widget builds from it', () => {
    const schema = { type: 'object', properties: { name: { type: 'string' } } };
    const model = fold(EMPTY_MODEL, questionFrame('form', { schema }));

    expect(model.items[0]).toMatchObject({ answerFormat: 'form', schema, callbackUrl: null });
  });

  it('carries the per-send data and pages on a form question', () => {
    const schema = {
      type: 'object',
      properties: { colour: { type: 'string' }, note: { type: 'string' } },
    };
    const data = { values: { note: 'hi' }, options: { colour: [{ value: 'r', label: 'Red' }] } };
    const pages = [
      { title: 'Pick', fields: ['colour'] },
      { title: 'Say', fields: ['note'] },
    ];
    const model = fold(EMPTY_MODEL, questionFrame('form', { schema, data, pages }));

    expect(model.items[0]).toMatchObject({
      answerFormat: 'form',
      formData: { values: { note: 'hi' }, options: { colour: [{ value: 'r', label: 'Red' }] } },
      pages,
    });
  });

  it('defaults a form question with no per-send data or pages to null', () => {
    const model = fold(
      EMPTY_MODEL,
      questionFrame('form', { schema: { type: 'object', properties: {} } }),
    );

    expect(model.items[0]).toMatchObject({ answerFormat: 'form', formData: null, pages: null });
  });

  it('carries display media on a question, in order, with its captions', () => {
    const model = fold(
      EMPTY_MODEL,
      questionFrame('text', {
        media: [
          { kind: 'image', url: 'https://example.com/a.png', caption: 'A shot' },
          { kind: 'link', url: 'https://docs.example/p', caption: 'The doc' },
        ],
      }),
    );

    expect(model.items[0]).toMatchObject({
      kind: 'question',
      answerFormat: 'text',
      media: [
        { kind: 'image', url: 'https://example.com/a.png', caption: 'A shot' },
        { kind: 'link', url: 'https://docs.example/p', caption: 'The doc' },
      ],
    });
  });

  it('accepts a question image served from the interactions media door (absolute https)', () => {
    // The fixed ask-media pipeline substitutes an inline data: image for an ABSOLUTE
    // served-media URL (https://host/api/interactions/media/{id}) before the frame is
    // sent, so that is the real shape a question image now arrives in. It is a plain
    // absolute https URL with no userinfo, so the parser accepts it and carries it
    // through to the card unchanged.
    const served = 'https://app.example/api/interactions/media/med-abc123';
    const model = fold(
      EMPTY_MODEL,
      questionFrame('text', { media: [{ kind: 'image', url: served, caption: 'A shot' }] }),
    );

    expect(model.items[0]).toMatchObject({
      kind: 'question',
      answerFormat: 'text',
      media: [{ kind: 'image', url: served, caption: 'A shot' }],
    });
  });

  it('leaves a question with no media key carrying null media', () => {
    const model = fold(EMPTY_MODEL, questionFrame('confirm'));

    expect(model.items[0]).toMatchObject({ kind: 'question', media: null });
  });

  it('records an answered frame against the interaction, not as a row of its own', () => {
    const model = fold(
      EMPTY_MODEL,
      question,
      frame('chat.answered', { id: 'a1', interaction_id: 'int-1' }),
    );

    expect(model.items).toHaveLength(1);
    expect(model.answeredIds.has('int-1')).toBe(true);
  });

  it('settles a question whose answered frame arrived first', () => {
    const model = fold(
      EMPTY_MODEL,
      frame('chat.answered', { id: 'a1', interaction_id: 'int-1' }),
      question,
    );

    expect(model.answeredIds.has('int-1')).toBe(true);
  });

  it.each([
    ['an unknown answer format', questionFrame('wizard')],
    ['non-string options', questionFrame('select', { options: [1, 2] })],
    ['an external question with no callback ticket', questionFrame('external')],
    [
      'an external question whose callback ticket is null',
      questionFrame('external', { callback_url: null }),
    ],
    [
      'a non-string callback ticket on an external question',
      questionFrame('external', { callback_url: 7 }),
    ],
    ['a non-string callback ticket on a text question', questionFrame('text', { callback_url: 7 })],
    [
      'a null callback ticket on a confirm question',
      questionFrame('confirm', { callback_url: null }),
    ],
    [
      // The ticket is a bearer credential for the interaction: a format that
      // answers through this channel's own door has no business being handed one,
      // and a frame that carries one anyway is not the contract's frame.
      'a callback ticket on a text question, whose ticket never leaves the server',
      questionFrame('text', { callback_url: CALLBACK }),
    ],
    ['a callback ticket on a select question', questionFrame('select', { callback_url: CALLBACK })],
    ['a form question with no schema at all', questionFrame('form')],
    ['a form question whose schema is not an object', questionFrame('form', { schema: [1, 2] })],
    ['a form question whose schema is null', questionFrame('form', { schema: null })],
    [
      // The schema reaches the page for the form widget alone; a scalar format that
      // answers through this channel's own door has no business carrying one.
      'a schema on a text question, whose format carries none',
      questionFrame('text', { schema: { type: 'object' } }),
    ],
    [
      'a callback ticket on a form question, whose ticket never leaves the server',
      questionFrame('form', { schema: { type: 'object' }, callback_url: CALLBACK }),
    ],
    [
      'per-send form data on a text question, whose format carries none',
      questionFrame('text', { data: { values: {}, options: {} } }),
    ],
    [
      'form pages on a select question, whose format carries none',
      questionFrame('select', { pages: [{ title: 'x', fields: ['a'] }] }),
    ],
    [
      'a form whose per-send option list is empty',
      questionFrame('form', {
        schema: { type: 'object' },
        data: { values: {}, options: { a: [] } },
      }),
    ],
    [
      'a form whose per-send option value is blank',
      questionFrame('form', {
        schema: { type: 'object' },
        data: { values: {}, options: { a: [{ value: ' ' }] } },
      }),
    ],
    [
      'a form page with no fields',
      questionFrame('form', { schema: { type: 'object' }, pages: [{ title: 'x', fields: [] }] }),
    ],
    ['an answered frame with no interaction id', frame('chat.answered', { id: 'a' })],
    [
      // A question's media is vetted exactly as a card's — one off-shape item (an
      // http image src the inbox CSP would refuse) taints the whole question frame.
      'a question whose display media carries a non-https image',
      questionFrame('text', { media: [{ kind: 'image', url: 'http://example.com/a.png' }] }),
    ],
    ['a question whose display media is not an array', questionFrame('text', { media: 'a.png' })],
  ])('surfaces %s as malformed rather than dropping it', (_label, bad) => {
    expect(applyFrame(EMPTY_MODEL, bad).kind).toBe('malformed');
  });
});
