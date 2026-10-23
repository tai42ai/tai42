import { describe, expect, it } from 'vitest';

import { applyFrame } from '@/frame-reducer';
import { fold, formFrame, mediaFrame, questionFrame } from '@/frame-reducer.test-support';
import { EMPTY_MODEL } from '@/transcript-model';

describe('applyFrame carries a send wording onto the entry', () => {
  it('folds the parsed wording onto a question', () => {
    const model = fold(
      EMPTY_MODEL,
      questionFrame('confirm', { wording: { answer: { confirm: 'Approve', decline: 'Reject' } } }),
    );
    expect(model.items[0]).toMatchObject({
      kind: 'question',
      wording: {
        answer: {
          open: null,
          confirm: 'Approve',
          decline: 'Reject',
          submit: null,
          replyHint: null,
          rejected: null,
          rejectedFinal: null,
        },
      },
    });
  });

  it('folds the parsed wording onto a media card', () => {
    const model = fold(
      EMPTY_MODEL,
      mediaFrame({
        options: [{ kind: 'reply', text: 'Yes' }],
        wording: { choice: { group_label: 'Replies' } },
      }),
    );
    expect(model.items[0]).toMatchObject({
      kind: 'media',
      wording: { choice: { groupLabel: 'Replies' } },
    });
  });

  it('folds the parsed wording onto a form card', () => {
    const model = fold(EMPTY_MODEL, formFrame({ wording: { status: { sent: 'Filed' } } }));
    expect(model.items[0]).toMatchObject({ kind: 'form', wording: { status: { sent: 'Filed' } } });
  });

  it('leaves wording null when the frame carries none', () => {
    const model = fold(EMPTY_MODEL, questionFrame('text'));
    expect(model.items[0]).toMatchObject({ kind: 'question', wording: null });
  });

  it('marks a question frame malformed when its wording is off-shape', () => {
    const outcome = applyFrame(EMPTY_MODEL, questionFrame('confirm', { wording: { bogus: {} } }));
    expect(outcome).toEqual({ kind: 'malformed', event: 'chat.question' });
  });

  it('marks a media frame malformed when its wording is off-shape', () => {
    const outcome = applyFrame(
      EMPTY_MODEL,
      mediaFrame({
        options: [{ kind: 'reply', text: 'Yes' }],
        wording: { choice: { group_label: '' } },
      }),
    );
    expect(outcome).toEqual({ kind: 'malformed', event: 'chat.media' });
  });

  it('marks a form frame malformed when its wording is off-shape', () => {
    const outcome = applyFrame(
      EMPTY_MODEL,
      formFrame({ wording: { status: { countdown: { seconds_left: 'no token' } } } }),
    );
    expect(outcome).toEqual({ kind: 'malformed', event: 'chat.form' });
  });
});
