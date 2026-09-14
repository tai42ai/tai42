import { applyFrame } from '@/frame-reducer';
import type { StreamModel } from '@/transcript-model';

export const TS = '2026-08-07T10:00:00+00:00';
export const DEADLINE = '2026-08-07T10:05:00+00:00';
export const CALLBACK = 'https://app.example/api/interactions/callback/t1';

export function frame(event: string, data: unknown) {
  return { event, data: JSON.stringify(data) };
}

export function fold(
  model: StreamModel,
  ...frames: { event: string; data: string }[]
): StreamModel {
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
export function questionFrame(format: string, extra: Record<string, unknown> = {}) {
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
export function mediaFrame(extra: Record<string, unknown> = {}) {
  return frame('chat.media', {
    id: 'md1',
    direction: 'out',
    text: 'Here you go',
    ts: TS,
    ...extra,
  });
}

/** A `chat.form` frame spelled as the wire spells it: `media` present only when
 * the case is about it. */
export function formFrame(extra: Record<string, unknown> = {}) {
  return frame('chat.form', {
    id: 'f1',
    text: 'Fill this in',
    schema: { type: 'object', properties: { note: { type: 'string' } } },
    token: 'tok-1',
    ts: TS,
    ...extra,
  });
}
