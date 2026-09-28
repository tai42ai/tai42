import { describe, expect, it } from 'vitest';

import { applyFrame } from '@/frame-reducer';
import { fold, frame, TS } from '@/frame-reducer.test-support';
import { EMPTY_MODEL } from '@/transcript-model';

const unavailable = frame('chat.unavailable', { id: 'u1', ts: TS });

describe('applyFrame: chat.unavailable', () => {
  it('folds a placeholder entry in arrival order', () => {
    const model = fold(EMPTY_MODEL, unavailable);

    expect(model.items).toEqual([{ kind: 'unavailable', id: 'u1', ts: TS }]);
  });

  it('de-duplicates a redelivered placeholder instead of showing it twice', () => {
    const model = fold(EMPTY_MODEL, unavailable, unavailable);

    expect(model.items).toHaveLength(1);
  });

  it('orders the placeholder among the surrounding entries by id, one per stream entry', () => {
    const before = frame('chat.message', { id: 'm0', direction: 'out', text: 'hi', ts: TS });
    const after = frame('chat.message', { id: 'm2', direction: 'in', text: 'bye', ts: TS });
    const second = frame('chat.unavailable', { id: 'u2', ts: TS });
    const model = fold(EMPTY_MODEL, before, unavailable, second, after);

    expect(model.items.map((item) => item.kind)).toEqual([
      'message',
      'unavailable',
      'unavailable',
      'message',
    ]);
  });

  it.each([
    ['a missing id', frame('chat.unavailable', { ts: TS })],
    ['a missing timestamp', frame('chat.unavailable', { id: 'u1' })],
    ['an unparsable timestamp', frame('chat.unavailable', { id: 'u1', ts: 'soon' })],
    ['unparsable data', { event: 'chat.unavailable', data: 'not json' }],
  ])('surfaces %s as malformed rather than dropping it', (_label, bad) => {
    expect(applyFrame(EMPTY_MODEL, bad).kind).toBe('malformed');
  });
});
