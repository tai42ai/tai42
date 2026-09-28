import { describe, expect, it } from 'vitest';

import { applyFrame } from '@/frame-reducer';
import { fold, frame, TS } from '@/frame-reducer.test-support';
import { EMPTY_MODEL } from '@/transcript-model';

/** An inbound message frame spelled as the wire spells it: the visitor's own
 * attachments ride `media`, present only when the case is about them. */
function inboundMessage(extra: Record<string, unknown> = {}) {
  return frame('chat.message', { id: 'm1', direction: 'in', text: '', ts: TS, ...extra });
}

/** The same-origin served-media reference the server mints for a bound attachment:
 * the root-relative served route and a 43-character stored-media id. */
const SERVED_REF = '/api/interactions/media/' + 'M'.repeat(43);

describe('applyFrame: chat.message with inbound media', () => {
  it('folds an inbound image attachment onto the message, vetted like a card', () => {
    const model = fold(
      EMPTY_MODEL,
      inboundMessage({
        text: 'look at this',
        media: [{ kind: 'image', url: SERVED_REF }],
      }),
    );

    expect(model.items[0]).toMatchObject({
      kind: 'message',
      direction: 'in',
      text: 'look at this',
      media: [{ kind: 'image', url: SERVED_REF, caption: null }],
    });
  });

  it('accepts an absolute https source too, the shape an off-origin card carries', () => {
    const model = fold(
      EMPTY_MODEL,
      inboundMessage({
        media: [{ kind: 'image', url: 'https://app.example/api/interactions/media/abc' }],
      }),
    );

    expect(model.items[0]).toMatchObject({
      kind: 'message',
      media: [{ kind: 'image', url: 'https://app.example/api/interactions/media/abc' }],
    });
  });

  it('refuses a relative path that is not the served-media reference', () => {
    for (const url of [
      '/api/interactions/media/short',
      '/etc/passwd',
      'api/interactions/media/x',
    ]) {
      const model = fold(EMPTY_MODEL, inboundMessage({ media: [{ kind: 'image', url }] }));
      expect(model.items).toHaveLength(0);
    }
  });

  it('folds a caption-less document attachment carrying its download filename', () => {
    const model = fold(
      EMPTY_MODEL,
      inboundMessage({
        media: [
          {
            kind: 'document',
            url: 'https://app.example/api/interactions/media/doc',
            filename: 'report.pdf',
          },
        ],
      }),
    );

    expect(model.items[0]).toMatchObject({
      kind: 'message',
      text: '',
      media: [{ kind: 'document', filename: 'report.pdf' }],
    });
  });

  it('folds an empty media list to null, so the message renders text-only', () => {
    const model = fold(EMPTY_MODEL, inboundMessage({ text: 'no attachment', media: [] }));

    expect(model.items[0]).toMatchObject({
      kind: 'message',
      text: 'no attachment',
      media: null,
    });
  });

  it.each([
    ['a non-array media value', inboundMessage({ media: 'nope' })],
    [
      'an image carrying an http (non-https) source',
      inboundMessage({ media: [{ kind: 'image', url: 'http://app.example/x' }] }),
    ],
    [
      'an off-shape item among valid ones',
      inboundMessage({
        media: [
          { kind: 'image', url: 'https://app.example/ok' },
          { kind: 'nope', url: 'https://app.example/y' },
        ],
      }),
    ],
    [
      'a filename on a non-document kind',
      inboundMessage({
        media: [{ kind: 'image', url: 'https://app.example/x', filename: 'x.png' }],
      }),
    ],
  ])('surfaces %s as malformed rather than dropping it', (_label, bad) => {
    expect(applyFrame(EMPTY_MODEL, bad).kind).toBe('malformed');
  });
});
