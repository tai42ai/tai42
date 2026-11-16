import { describe, expect, it } from 'vitest';

import { applyFrame } from '@/frame-reducer';
import { frame } from '@/frame-reducer.test-support';
import { EMPTY_MODEL } from '@/transcript-model';

describe('applyFrame: chat.working', () => {
  it('reads an active frame as the working state, never a transcript item', () => {
    const outcome = applyFrame(
      EMPTY_MODEL,
      frame('chat.working', { id: '1-0', active: true, ttl: 10.0 }),
    );

    expect(outcome).toEqual({
      kind: 'working',
      working: { id: '1-0', active: true, ttlSeconds: 10 },
    });
  });

  it('reads a stop frame as an inactive working state', () => {
    const outcome = applyFrame(
      EMPTY_MODEL,
      frame('chat.working', { id: '2-0', active: false, ttl: 10.0 }),
    );

    expect(outcome).toEqual({
      kind: 'working',
      working: { id: '2-0', active: false, ttlSeconds: 10 },
    });
  });

  it.each([
    ['a missing id', frame('chat.working', { active: true, ttl: 10 })],
    ['a non-boolean active', frame('chat.working', { id: '1-0', active: '1', ttl: 10 })],
    ['a missing ttl', frame('chat.working', { id: '1-0', active: true })],
    ['a string ttl', frame('chat.working', { id: '1-0', active: true, ttl: '10' })],
    ['a zero ttl', frame('chat.working', { id: '1-0', active: true, ttl: 0 })],
    ['a negative ttl', frame('chat.working', { id: '1-0', active: true, ttl: -1 })],
    ['unparsable data', { event: 'chat.working', data: 'not json' }],
  ])('surfaces %s as malformed rather than dropping it', (_label, bad) => {
    expect(applyFrame(EMPTY_MODEL, bad).kind).toBe('malformed');
  });
});
