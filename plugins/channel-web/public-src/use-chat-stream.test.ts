import { describe, expect, it } from 'vitest';

import { EMPTY_MODEL, applyFrame, type StreamModel } from '@/use-chat-stream';

const TS = '2026-08-07T10:00:00+00:00';
const DEADLINE = '2026-08-07T10:05:00+00:00';
const CALLBACK = 'https://app.example/api/interactions/callback/t1';

function frame(event: string, data: unknown) {
  return { event, data: JSON.stringify(data) };
}

function fold(model: StreamModel, ...frames: { event: string; data: string }[]): StreamModel {
  let current = model;
  for (const one of frames) {
    const outcome = applyFrame(current, one);
    if (outcome.kind === 'model') current = outcome.model;
  }
  return current;
}

/** A `chat.question` frame spelled as the wire spells it: no `callback_url` key,
 * because the ticket is carried for `external` alone. `extra` adds or replaces
 * whatever a case is about. */
function questionFrame(format: string, extra: Record<string, unknown> = {}) {
  return frame('chat.question', {
    id: 'q1',
    interaction_id: 'int-1',
    question: 'Deploy?',
    answer_format: format,
    options: format === 'select' ? ['Now', 'Tonight'] : null,
    timeout_at: DEADLINE,
    ts: TS,
    ...extra,
  });
}

/** A `chat.media` frame spelled as the wire spells it: `direction` fixed to `out`,
 * `media` and `options` each present only when the case is about them. */
function mediaFrame(extra: Record<string, unknown> = {}) {
  return frame('chat.media', {
    id: 'md1',
    direction: 'out',
    text: 'Here you go',
    ts: TS,
    ...extra,
  });
}

const message = frame('chat.message', { id: 'm1', direction: 'out', text: 'hi', ts: TS });
const question = questionFrame('confirm');

describe('applyFrame', () => {
  it('folds a message entry in arrival order', () => {
    const model = fold(EMPTY_MODEL, message);

    expect(model.items).toEqual([
      { kind: 'message', id: 'm1', direction: 'out', text: 'hi', ts: TS, clientMessageId: null },
    ]);
  });

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

  it('folds a media card with an image and its caption', () => {
    const model = fold(
      EMPTY_MODEL,
      mediaFrame({
        media: [{ kind: 'image', url: 'https://example.com/a.png', caption: 'Item A' }],
      }),
    );

    expect(model.items[0]).toEqual({
      kind: 'media',
      id: 'md1',
      text: 'Here you go',
      media: [
        { kind: 'image', url: 'https://example.com/a.png', caption: 'Item A', filename: null },
      ],
      options: null,
      sections: null,
      header: null,
      footer: null,
      location: null,
      ts: TS,
    });
  });

  it('folds a media card that is text and tappable options only', () => {
    const model = fold(
      EMPTY_MODEL,
      mediaFrame({
        options: [
          { kind: 'reply', text: 'Item A' },
          { kind: 'reply', text: 'Item B' },
        ],
      }),
    );

    expect(model.items[0]).toMatchObject({
      kind: 'media',
      media: null,
      options: [
        { kind: 'reply', text: 'Item A', description: null, id: null },
        { kind: 'reply', text: 'Item B', description: null, id: null },
      ],
    });
  });

  it('folds reply and link options together, preserving id and description', () => {
    const model = fold(
      EMPTY_MODEL,
      mediaFrame({
        options: [
          { kind: 'reply', text: 'Book', description: 'the fast lane', id: 'opt-1' },
          { kind: 'link', label: 'Read more', url: 'https://example.com/more' },
        ],
      }),
    );

    expect(model.items[0]).toMatchObject({
      kind: 'media',
      options: [
        { kind: 'reply', text: 'Book', description: 'the fast lane', id: 'opt-1' },
        { kind: 'link', label: 'Read more', url: 'https://example.com/more' },
      ],
    });
  });

  it('folds a media card carrying an image and options together', () => {
    const model = fold(
      EMPTY_MODEL,
      mediaFrame({
        media: [{ kind: 'image', url: 'https://example.com/a.png' }],
        options: [{ kind: 'reply', text: 'See all' }],
      }),
    );

    expect(model.items[0]).toMatchObject({
      kind: 'media',
      media: [{ kind: 'image', url: 'https://example.com/a.png', caption: null, filename: null }],
      options: [{ kind: 'reply', text: 'See all', description: null, id: null }],
    });
  });

  it('folds a sectioned reply list', () => {
    const model = fold(
      EMPTY_MODEL,
      mediaFrame({
        sections: [
          { title: 'Today', rows: [{ kind: 'reply', text: '09:00', id: 't-9' }] },
          { title: 'Tomorrow', rows: [{ kind: 'reply', text: '11:00' }] },
        ],
      }),
    );

    expect(model.items[0]).toMatchObject({
      kind: 'media',
      options: null,
      sections: [
        { title: 'Today', rows: [{ kind: 'reply', text: '09:00', description: null, id: 't-9' }] },
        {
          title: 'Tomorrow',
          rows: [{ kind: 'reply', text: '11:00', description: null, id: null }],
        },
      ],
    });
  });

  it('folds a header, footer and location on an interactive card', () => {
    const model = fold(
      EMPTY_MODEL,
      mediaFrame({
        header: { kind: 'image', url: 'https://example.com/banner.png', caption: 'Banner' },
        footer: 'Powered by TAI',
        location: { latitude: 51.5, longitude: -0.12, name: 'London', address: 'Trafalgar Square' },
        options: [{ kind: 'reply', text: 'Item A' }],
      }),
    );

    expect(model.items[0]).toMatchObject({
      kind: 'media',
      header: {
        kind: 'image',
        url: 'https://example.com/banner.png',
        caption: 'Banner',
        filename: null,
      },
      footer: 'Powered by TAI',
      location: { latitude: 51.5, longitude: -0.12, name: 'London', address: 'Trafalgar Square' },
    });
  });

  it('folds a document with its filename and native video/audio media', () => {
    const model = fold(
      EMPTY_MODEL,
      mediaFrame({
        media: [
          {
            kind: 'document',
            url: 'https://example.com/r.pdf',
            caption: 'Q3',
            filename: 'report.pdf',
          },
          { kind: 'video', url: 'https://example.com/clip.mp4' },
          { kind: 'audio', url: 'https://example.com/note.mp3' },
        ],
      }),
    );

    expect(model.items[0]).toMatchObject({
      kind: 'media',
      media: [
        {
          kind: 'document',
          url: 'https://example.com/r.pdf',
          caption: 'Q3',
          filename: 'report.pdf',
        },
        { kind: 'video', url: 'https://example.com/clip.mp4', caption: null, filename: null },
        { kind: 'audio', url: 'https://example.com/note.mp3', caption: null, filename: null },
      ],
    });
  });

  it('folds a content-only location card (blank text, no media/options)', () => {
    const model = fold(
      EMPTY_MODEL,
      mediaFrame({ text: '', location: { latitude: 1, longitude: 2 } }),
    );

    expect(model.items[0]).toMatchObject({
      kind: 'media',
      text: '',
      media: null,
      options: null,
      location: { latitude: 1, longitude: 2, name: null, address: null },
    });
  });

  it('carries a link attachment as a validated http(s) url', () => {
    const model = fold(
      EMPTY_MODEL,
      mediaFrame({ media: [{ kind: 'link', url: 'http://example.com/a', caption: 'Open A' }] }),
    );

    expect(model.items[0]).toMatchObject({
      media: [{ kind: 'link', url: 'http://example.com/a', caption: 'Open A', filename: null }],
    });
  });

  it('de-duplicates a redelivered media card on backlog replay', () => {
    const model = fold(
      EMPTY_MODEL,
      mediaFrame({ options: [{ kind: 'reply', text: 'Item A' }] }),
      mediaFrame({ options: [{ kind: 'reply', text: 'Item A' }] }),
    );

    expect(model.items).toHaveLength(1);
    expect(model.items[0]).toMatchObject({
      kind: 'media',
      options: [{ kind: 'reply', text: 'Item A', description: null, id: null }],
    });
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

  it("carries the sender's own idempotency key back off the frame", () => {
    // The key is what identifies a message as one this page sent when the door's
    // answer — and with it the message id — never arrived.
    const model = fold(
      EMPTY_MODEL,
      frame('chat.message', {
        id: 'm1',
        direction: 'in',
        text: 'hi',
        ts: TS,
        client_message_id: 'c0ffee-cafe_1234',
      }),
    );

    expect(model.items[0]).toMatchObject({ clientMessageId: 'c0ffee-cafe_1234' });
  });

  it('de-duplicates a redelivered entry instead of showing it twice', () => {
    const edited = frame('chat.message', { id: 'm1', direction: 'out', text: 'edited', ts: TS });
    const model = fold(EMPTY_MODEL, message, edited);

    expect(model.items).toHaveLength(1);
    expect(model.items[0]).toMatchObject({ text: 'edited' });
  });

  it('reports the backlog marker without touching the model', () => {
    expect(applyFrame(EMPTY_MODEL, { event: 'chat.backlog_done', data: '{}' })).toEqual({
      kind: 'backlog-done',
    });
  });

  it.each([
    ['unparsable data', { event: 'chat.message', data: 'not json' }],
    ['a non-object payload', { event: 'chat.message', data: '[]' }],
    ['a missing id', frame('chat.message', { direction: 'out', text: 'x', ts: TS })],
    [
      'an unknown direction',
      frame('chat.message', { id: 'm', direction: 'sideways', text: 'x', ts: TS }),
    ],
    ['a non-string text', frame('chat.message', { id: 'm', direction: 'out', text: 7, ts: TS })],
    [
      'an unparsable timestamp',
      frame('chat.message', { id: 'm', direction: 'out', text: 'x', ts: 'soon' }),
    ],
    [
      'a non-string idempotency key',
      frame('chat.message', { id: 'm', direction: 'out', text: 'x', ts: TS, client_message_id: 7 }),
    ],
    [
      'an explicitly null idempotency key, which the wire omits instead',
      frame('chat.message', {
        id: 'm',
        direction: 'out',
        text: 'x',
        ts: TS,
        client_message_id: null,
      }),
    ],
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
    [
      // The new known set (document/video/audio) is admitted; a kind outside it
      // still rejects rather than rendering as a blank attachment.
      'a media card with an unknown attachment kind',
      mediaFrame({ media: [{ kind: 'sticker', url: 'https://example.com/a.webp' }] }),
    ],
    [
      // A video/audio/document still obeys the https file discipline — an http
      // source the CSP would refuse taints the frame.
      'a media video whose url is not https',
      mediaFrame({ media: [{ kind: 'video', url: 'http://example.com/a.mp4' }] }),
    ],
    [
      // filename rides a document only; on any other kind it is off-contract.
      'a filename on a non-document media item',
      mediaFrame({
        media: [{ kind: 'image', url: 'https://example.com/a.png', filename: 'a.png' }],
      }),
    ],
    [
      'a media image whose url carries a user@ authority',
      mediaFrame({ media: [{ kind: 'image', url: 'https://user:pass@example.com/a.png' }] }),
    ],
    [
      'a media link whose url carries a user@ authority',
      mediaFrame({ media: [{ kind: 'link', url: 'https://user:pass@example.com/x' }] }),
    ],
    [
      'a media image whose url is not https',
      mediaFrame({ media: [{ kind: 'image', url: 'http://example.com/a.png' }] }),
    ],
    [
      'a media image whose url is relative rather than absolute',
      mediaFrame({ media: [{ kind: 'image', url: '/a.png' }] }),
    ],
    [
      'a media link whose url is neither http nor https',
      mediaFrame({ media: [{ kind: 'link', url: 'ftp://example.com/a' }] }),
    ],
    [
      'a media attachment whose caption is not a string',
      mediaFrame({ media: [{ kind: 'image', url: 'https://example.com/a.png', caption: 7 }] }),
    ],
    [
      'a media attachment whose caption is explicitly null',
      mediaFrame({ media: [{ kind: 'image', url: 'https://example.com/a.png', caption: null }] }),
    ],
    ['a reply option with blank text', mediaFrame({ options: [{ kind: 'reply', text: '  ' }] })],
    ['a bare-string option (the old flat shape)', mediaFrame({ options: ['Item A'] })],
    ['a media card with an empty options array', mediaFrame({ options: [] })],
    ['an option with no kind', mediaFrame({ options: [{ text: 'Item A' }] })],
    [
      'a reply option whose text is not a string',
      mediaFrame({ options: [{ kind: 'reply', text: 1 }] }),
    ],
    [
      'a link option whose url is not http(s)',
      mediaFrame({ options: [{ kind: 'link', label: 'x', url: 'ftp://example.com/a' }] }),
    ],
    [
      'a link option with a blank label',
      mediaFrame({ options: [{ kind: 'link', label: ' ', url: 'https://ex/a' }] }),
    ],
    ['a media card whose options is a non-array object', mediaFrame({ options: { a: 1 } })],
    [
      'options and sections together (two choice surfaces)',
      mediaFrame({
        options: [{ kind: 'reply', text: 'a' }],
        sections: [{ title: 'S', rows: [{ kind: 'reply', text: 'b' }] }],
      }),
    ],
    ['a section with empty rows', mediaFrame({ sections: [{ title: 'S', rows: [] }] })],
    [
      'a section with a blank title',
      mediaFrame({ sections: [{ title: ' ', rows: [{ kind: 'reply', text: 'b' }] }] }),
    ],
    [
      'a section row that is a link (rows are replies only)',
      mediaFrame({
        sections: [{ title: 'S', rows: [{ kind: 'link', label: 'x', url: 'https://ex/a' }] }],
      }),
    ],
    [
      'a header with no options or sections',
      mediaFrame({ header: { kind: 'image', url: 'https://example.com/a.png' } }),
    ],
    [
      'a link header (a header is display media, never a link)',
      mediaFrame({
        header: { kind: 'link', url: 'https://ex/a' },
        options: [{ kind: 'reply', text: 'a' }],
      }),
    ],
    ['a footer with no options or sections', mediaFrame({ footer: 'trailing' })],
    [
      'a location with an out-of-range latitude',
      mediaFrame({ location: { latitude: 91, longitude: 0 } }),
    ],
    [
      'a location with a non-number coordinate',
      mediaFrame({ location: { latitude: '51', longitude: 0 } }),
    ],
    [
      'a media card missing its text',
      frame('chat.media', {
        id: 'md1',
        direction: 'out',
        ts: TS,
        options: [{ kind: 'reply', text: 'Item A' }],
      }),
    ],
    [
      'a media card whose direction is not out',
      mediaFrame({ direction: 'in', options: [{ kind: 'reply', text: 'Item A' }] }),
    ],
    ['a media card with no content at all', mediaFrame({})],
    [
      'a media card whose media is not an array',
      mediaFrame({ media: { kind: 'image', url: 'https://example.com/a.png' } }),
    ],
    ['an unknown event', frame('chat.something', { id: 'x' })],
  ])('surfaces %s as malformed rather than dropping it', (_label, bad) => {
    expect(applyFrame(EMPTY_MODEL, bad).kind).toBe('malformed');
  });

  it('leaves the previous model untouched — the fold is pure', () => {
    const first = fold(EMPTY_MODEL, message);
    fold(first, question);

    expect(first.items).toHaveLength(1);
    expect(EMPTY_MODEL.items).toHaveLength(0);
  });
});

/** A `chat.form` frame spelled as the wire spells it: `media` present only when
 * the case is about it. */
function formFrame(extra: Record<string, unknown> = {}) {
  return frame('chat.form', {
    id: 'f1',
    text: 'Fill this in',
    schema: { type: 'object', properties: { note: { type: 'string' } } },
    token: 'tok-1',
    ts: TS,
    ...extra,
  });
}

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
        ts: TS,
      },
    ]);
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
  ])('surfaces %s as malformed rather than dropping the widget', (_label, bad) => {
    expect(applyFrame(EMPTY_MODEL, bad).kind).toBe('malformed');
  });
});
