import { describe, expect, it } from 'vitest';

import { applyFrame } from '@/frame-reducer';
import { fold, frame, questionFrame, TS } from '@/frame-reducer.test-support';
import { EMPTY_MODEL } from '@/transcript-model';

const message = frame('chat.message', { id: 'm1', direction: 'out', text: 'hi', ts: TS });

describe('applyFrame: chat.message', () => {
  it('folds a message entry in arrival order', () => {
    const model = fold(EMPTY_MODEL, message);

    expect(model.items).toEqual([
      { kind: 'message', id: 'm1', direction: 'out', text: 'hi', ts: TS, clientMessageId: null },
    ]);
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

  it('leaves the previous model untouched — the fold is pure', () => {
    const first = fold(EMPTY_MODEL, message);
    fold(first, questionFrame('confirm'));

    expect(first.items).toHaveLength(1);
    expect(EMPTY_MODEL.items).toHaveLength(0);
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
    ['an unknown event', frame('chat.something', { id: 'x' })],
  ])('surfaces %s as malformed rather than dropping it', (_label, bad) => {
    expect(applyFrame(EMPTY_MODEL, bad).kind).toBe('malformed');
  });
});
