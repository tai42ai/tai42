/**
 * Fold one transcript frame into the model. Pure — the same model and frame always
 * give the same outcome — so the whole transcript behaviour is testable without a
 * stream. A malformed frame is SURFACED as an outcome, never rendered as a blank
 * bubble and never dropped in silence.
 *
 * Entries are at-least-once (a reconnect replays the whole backlog), so items are
 * de-duplicated by entry id and a redelivered entry replaces its twin in place
 * rather than appending a second bubble.
 */
import type { SseFrame } from '@/sse';
import {
  ANSWER_FORMATS,
  type AnswerFormat,
  type CardOption,
  type ChatItem,
  type LocationPoint,
  type MediaItem,
  type OptionSection,
  type StreamModel,
} from '@/transcript-model';
import {
  cardOptionsOf,
  clientMessageIdOf,
  facetOf,
  footerOf,
  formPagesOf,
  formPrefillOf,
  headerOf,
  isRecord,
  isTimestamp,
  locationOf,
  mediaOf,
  optionsOf,
  parseJson,
  sectionsOf,
} from '@/frame-parse';

/** What one frame did: advanced the model, completed the backlog, or arrived
 * malformed (which the caller surfaces — it is never swallowed). */
export type FrameOutcome =
  | { readonly kind: 'model'; readonly model: StreamModel }
  | { readonly kind: 'backlog-done' }
  | { readonly kind: 'malformed'; readonly event: string };

function malformed(event: string): FrameOutcome {
  return { kind: 'malformed', event };
}

/** Add or replace one item, keeping arrival order. A redelivered entry (the
 * backlog replay after a reconnect) patches its twin instead of appending. */
function withItem(model: StreamModel, item: ChatItem): StreamModel {
  if (!model.ids.has(item.id)) {
    return {
      items: [...model.items, item],
      ids: new Set(model.ids).add(item.id),
      answeredIds: model.answeredIds,
    };
  }
  return {
    items: model.items.map((existing) => (existing.id === item.id ? item : existing)),
    ids: model.ids,
    answeredIds: model.answeredIds,
  };
}

function applyMessageFrame(
  model: StreamModel,
  payload: Record<string, unknown>,
  id: string,
  event: string,
): FrameOutcome {
  const { direction, text, ts } = payload;
  if (direction !== 'in' && direction !== 'out') return malformed(event);
  if (typeof text !== 'string' || !isTimestamp(ts)) return malformed(event);
  const clientMessageId = clientMessageIdOf(payload.client_message_id);
  if (clientMessageId === undefined) return malformed(event);
  return {
    kind: 'model',
    model: withItem(model, { kind: 'message', id, direction, text, ts, clientMessageId }),
  };
}

/** The media card's cross-field contract, mirroring the wire's own rules so a frame
 * off them never renders: one choice surface (options XOR sections); a header/footer
 * rides an interactive card (it needs options or sections); a card with no content
 * at all is a plain notify the wire sends as `chat.message`. */
function mediaCardIsCoherent(
  media: readonly MediaItem[] | null,
  options: readonly CardOption[] | null,
  sections: readonly OptionSection[] | null,
  header: MediaItem | null,
  footer: string | null,
  location: LocationPoint | null,
): boolean {
  if (options !== null && sections !== null) return false;
  const hasChoice = options !== null || sections !== null;
  if ((header !== null || footer !== null) && !hasChoice) return false;
  if (media === null && !hasChoice && location === null) return false;
  return true;
}

function applyMediaFrame(
  model: StreamModel,
  payload: Record<string, unknown>,
  id: string,
  event: string,
): FrameOutcome {
  const { direction, text, ts } = payload;
  // The card is always the agent's turn; the wire fixes its direction to `out`.
  if (direction !== 'out') return malformed(event);
  if (typeof text !== 'string' || !isTimestamp(ts)) return malformed(event);
  const media = mediaOf(payload.media);
  if (media === undefined) return malformed(event);
  const options = cardOptionsOf(payload.options);
  if (options === undefined) return malformed(event);
  const sections = sectionsOf(payload.sections);
  if (sections === undefined) return malformed(event);
  const header = headerOf(payload.header);
  if (header === undefined) return malformed(event);
  const footer = footerOf(payload.footer);
  if (footer === undefined) return malformed(event);
  const location = locationOf(payload.location);
  if (location === undefined) return malformed(event);
  if (!mediaCardIsCoherent(media, options, sections, header, footer, location)) {
    return malformed(event);
  }
  return {
    kind: 'model',
    model: withItem(model, {
      kind: 'media',
      id,
      text,
      media,
      options,
      sections,
      header,
      footer,
      location,
      ts,
    }),
  };
}

function applyFormFrame(
  model: StreamModel,
  payload: Record<string, unknown>,
  id: string,
  event: string,
): FrameOutcome {
  const { text, schema, token, ts } = payload;
  if (typeof text !== 'string' || !isTimestamp(ts)) return malformed(event);
  // Strict: the schema must be an object and the token a non-blank string —
  // without either the card has no widget to render or no door to submit to,
  // and a silently dropped control is exactly what "malformed" exists to stop.
  if (!isRecord(schema)) return malformed(event);
  if (typeof token !== 'string' || token.trim() === '') return malformed(event);
  const media = mediaOf(payload.media);
  if (media === undefined) return malformed(event);
  const location = locationOf(payload.location);
  if (location === undefined) return malformed(event);
  // The per-send enrichment the card opens filled in from — parsed exactly as a
  // `form` question's, so a malformed `data`/`pages` taints the frame rather than
  // rendering a blank or half-built form.
  const formData = formPrefillOf(payload.data);
  if (formData === undefined) return malformed(event);
  const pages = formPagesOf(payload.pages);
  if (pages === undefined) return malformed(event);
  return {
    kind: 'model',
    model: withItem(model, {
      kind: 'form',
      id,
      text,
      schema,
      token,
      media,
      location,
      formData,
      pages,
      ts,
    }),
  };
}

function applyQuestionFrame(
  model: StreamModel,
  payload: Record<string, unknown>,
  id: string,
  event: string,
): FrameOutcome {
  const { interaction_id, question, answer_format, callback_url, schema, timeout_at, ts } = payload;
  const options = optionsOf(payload.options);
  if (typeof interaction_id !== 'string') return malformed(event);
  if (typeof question !== 'string') return malformed(event);
  if (typeof answer_format !== 'string' || !ANSWER_FORMATS.has(answer_format)) {
    return malformed(event);
  }
  const facet = facetOf(
    callback_url,
    schema,
    payload.data,
    payload.pages,
    answer_format as AnswerFormat,
  );
  if (facet === undefined) return malformed(event);
  if (!isTimestamp(timeout_at) || !isTimestamp(ts)) return malformed(event);
  if (options === undefined) return malformed(event);
  // The same vetted-media parse the media card uses — one off-shape item taints
  // the whole frame, so an image `src`/link `href` on the question is as trusted
  // as one on a card.
  const media = mediaOf(payload.media);
  if (media === undefined) return malformed(event);
  return {
    kind: 'model',
    model: withItem(model, {
      kind: 'question',
      id,
      interactionId: interaction_id,
      question,
      options,
      media,
      timeoutAt: timeout_at,
      ts,
      ...facet,
    }),
  };
}

function applyAnsweredFrame(
  model: StreamModel,
  payload: Record<string, unknown>,
  event: string,
): FrameOutcome {
  const interactionId = payload.interaction_id;
  if (typeof interactionId !== 'string') return malformed(event);
  // Recorded by interaction id rather than applied to the question item, so an
  // answered frame that arrives BEFORE its question (or without one) still
  // settles the widget when the question shows up.
  return {
    kind: 'model',
    model: {
      items: model.items,
      ids: model.ids,
      answeredIds: new Set(model.answeredIds).add(interactionId),
    },
  };
}

/**
 * Fold one frame into the model. Pure — the same model and frame always give the
 * same outcome — so the whole transcript behaviour is testable without a stream.
 */
export function applyFrame(model: StreamModel, frame: SseFrame): FrameOutcome {
  if (frame.event === 'chat.backlog_done') return { kind: 'backlog-done' };

  const payload = parseJson(frame.data);
  if (payload === null) return malformed(frame.event);
  const id = payload.id;
  if (typeof id !== 'string') return malformed(frame.event);

  switch (frame.event) {
    case 'chat.message':
      return applyMessageFrame(model, payload, id, frame.event);
    case 'chat.media':
      return applyMediaFrame(model, payload, id, frame.event);
    case 'chat.form':
      return applyFormFrame(model, payload, id, frame.event);
    case 'chat.question':
      return applyQuestionFrame(model, payload, id, frame.event);
    case 'chat.answered':
      return applyAnsweredFrame(model, payload, frame.event);
    default:
      return malformed(frame.event);
  }
}
