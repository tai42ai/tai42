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
    ['an answered frame with no interaction id', frame('chat.answered', { id: 'a' })],
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
