/**
 * The transcript stream: the SSE feed of one visitor's conversation, folded into
 * the ordered items the page renders.
 *
 * The wire carries five events. `chat.message`, `chat.question` and `chat.media`
 * are transcript ENTRIES and become items in arrival order; `chat.answered` is not
 * an entry of its own — it settles the question it names, so it only records an
 * interaction id; `chat.backlog_done` marks the replayed backlog as complete.
 *
 * Entries are at-least-once (a reconnect replays the whole backlog), so items are
 * de-duplicated by entry id and a redelivered entry replaces its twin in place
 * rather than appending a second bubble.
 *
 * A malformed frame is SURFACED as an error, never rendered as a blank bubble and
 * never dropped in silence; one good frame afterwards clears it, because a single
 * bad entry on a healthy stream must not stick.
 */
import { useEffect, useRef, useState } from 'react';
import type { JsonSchema } from '@tai42/studio-sdk';

import { isSessionMissing, isStoreOff, openChatStream } from '@/api';
import { readSseFrames, type SseFrame } from '@/sse';

/** The permissive schema type the form widget renders, re-exported so a question
 * consumer types against one import. */
export type { JsonSchema };

/** The answer shapes a channel-delivered question can take. `form` IS delivered to
 * this channel — the page advertises `supports_form_delivery` and renders a
 * schema-driven form widget for it. */
export type AnswerFormat = 'text' | 'confirm' | 'select' | 'form' | 'external';

const ANSWER_FORMATS: ReadonlySet<string> = new Set<AnswerFormat>([
  'text',
  'confirm',
  'select',
  'form',
  'external',
]);

/** The format-dependent extras a question row carries, keyed by the format that
 * decides whether the wire carries each: the interactions callback ticket for
 * `external` (the one widget that opens it), the JSON answer schema for `form` (the
 * one widget that builds an answer from it), and neither for the scalar formats.
 * Extras and format travel as ONE type, so a widget that reads an extra has the
 * format that guarantees it. */
export type QuestionFacet =
  | { readonly answerFormat: 'external'; readonly callbackUrl: string; readonly schema: null }
  | { readonly answerFormat: 'form'; readonly callbackUrl: null; readonly schema: JsonSchema }
  | {
      readonly answerFormat: 'text' | 'confirm' | 'select';
      readonly callbackUrl: null;
      readonly schema: null;
    };

/** One attachment on a media card: an inline image or a safe outbound link. The
 * url is already proven absolute — `https:` for an image, `http(s):` for a link —
 * by the reducer, so a renderer sets it as an attribute without re-checking. */
export interface MediaItem {
  readonly kind: 'image' | 'link';
  readonly url: string;
  readonly caption: string | null;
}

/** Everything a question row carries apart from its ticket. */
interface QuestionBase {
  readonly kind: 'question';
  readonly id: string;
  readonly interactionId: string;
  readonly question: string;
  readonly options: readonly string[] | null;
  readonly timeoutAt: string;
  readonly ts: string;
}

/** One rendered row of the transcript. */
export type ChatItem =
  | {
      readonly kind: 'message';
      readonly id: string;
      /** `in` is the visitor's own message, `out` the agent's. */
      readonly direction: 'in' | 'out';
      readonly text: string;
      readonly ts: string;
      /** The idempotency key the sender put on this message, echoed back onto their
       * own frame. It is what identifies a message as one THIS page sent even when
       * the door's answer never arrived, so the optimistic bubble can be retired
       * without the id that answer would have carried. `null` on anything sent
       * without a key — the agent's own messages included. */
      readonly clientMessageId: string | null;
    }
  | {
      /** An agent-sent card: markdown text with any images/links and any tappable
       * option chips. Always the agent's turn, so it carries no direction. */
      readonly kind: 'media';
      readonly id: string;
      readonly text: string;
      readonly media: readonly MediaItem[] | null;
      readonly options: readonly string[] | null;
      readonly ts: string;
    }
  | (QuestionBase & QuestionFacet);

/** The folded stream: the ordered items, a fast id index for the dedupe, and the
 * interaction ids a `chat.answered` frame has settled. */
export interface StreamModel {
  readonly items: readonly ChatItem[];
  readonly ids: ReadonlySet<string>;
  readonly answeredIds: ReadonlySet<string>;
}

export const EMPTY_MODEL: StreamModel = {
  items: [],
  ids: new Set(),
  answeredIds: new Set(),
};

/** What one frame did: advanced the model, completed the backlog, or arrived
 * malformed (which the caller surfaces — it is never swallowed). */
export type FrameOutcome =
  | { readonly kind: 'model'; readonly model: StreamModel }
  | { readonly kind: 'backlog-done' }
  | { readonly kind: 'malformed'; readonly event: string };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function parseJson(data: string): Record<string, unknown> | null {
  if (!data) return null;
  try {
    const parsed: unknown = JSON.parse(data);
    return isRecord(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

/** A wire timestamp the transcript can order and label by. A value that does not
 * parse makes its whole frame malformed — grouping and countdowns read these, and
 * a silently unparsable one would render as an "Invalid Date" divider. */
function isTimestamp(value: unknown): value is string {
  return typeof value === 'string' && Number.isFinite(Date.parse(value));
}

function optionsOf(raw: unknown): readonly string[] | null | undefined {
  if (raw === null || raw === undefined) return null;
  if (!Array.isArray(raw) || !raw.every((option) => typeof option === 'string')) return undefined;
  return raw;
}

/** The tappable chips on a media card: absent, or a NON-EMPTY list of non-blank
 * labels. A blank chip has no text to feed back, and an empty list is a control
 * row with no controls — both are frames this page will not render: `undefined`
 * says malformed. */
function chipOptionsOf(raw: unknown): readonly string[] | null | undefined {
  if (raw === null || raw === undefined) return null;
  if (!Array.isArray(raw) || raw.length === 0) return undefined;
  if (!raw.every((option) => typeof option === 'string' && option.trim() !== '')) return undefined;
  return raw;
}

/** An absolute URL whose scheme is one of `protocols` (each spelled WITH its
 * trailing colon, as `URL.protocol` returns it). A relative or wrong-scheme value
 * is refused — an image source is constrained to `https:`, a link to `http(s):` —
 * and a URL carrying a `user@` authority (userinfo) is refused too, so nothing but
 * a vetted absolute URL ever reaches an `src` or `href`. */
function isAbsoluteUrl(value: unknown, protocols: readonly string[]): value is string {
  if (typeof value !== 'string') return false;
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return false;
  }
  if (parsed.username !== '' || parsed.password !== '') return false;
  return protocols.includes(parsed.protocol);
}

/** A card attachment's caption: the wire carries a string or omits it. Any other
 * spelling (a non-string, an explicit null) is a frame this page will not render:
 * `undefined` says malformed, `null` says the caption is absent. */
function captionOf(raw: unknown): string | null | undefined {
  if (raw === undefined) return null;
  return typeof raw === 'string' ? raw : undefined;
}

/** One media attachment, validated: a known kind, a scheme-appropriate absolute
 * URL, and an optional string caption. `undefined` on anything off-shape. */
function mediaItemOf(raw: unknown): MediaItem | undefined {
  if (!isRecord(raw)) return undefined;
  const { kind, url } = raw;
  if (kind !== 'image' && kind !== 'link') return undefined;
  const protocols = kind === 'image' ? ['https:'] : ['http:', 'https:'];
  if (!isAbsoluteUrl(url, protocols)) return undefined;
  const caption = captionOf(raw.caption);
  if (caption === undefined) return undefined;
  return { kind, url, caption };
}

/** The card's attachments: absent, or a list whose every item validates. One
 * off-shape item taints the whole list — `undefined` says malformed. */
function mediaOf(raw: unknown): readonly MediaItem[] | null | undefined {
  if (raw === null || raw === undefined) return null;
  if (!Array.isArray(raw)) return undefined;
  const items: MediaItem[] = [];
  for (const one of raw) {
    const item = mediaItemOf(one);
    if (item === undefined) return undefined;
    items.push(item);
  }
  return items;
}

/** The sender's own idempotency key, echoed onto their message. The wire carries
 * the key as a string or omits it entirely — the key is the sender's, so the
 * server never invents one. Any other spelling (a non-string, an explicit null) is
 * a frame this page will not render: `undefined` says malformed. */
function clientMessageIdOf(raw: unknown): string | null | undefined {
  if (typeof raw === 'string') return raw;
  return raw === undefined ? null : undefined;
}

/** The format-dependent extras, validated together by the format that decides
 * whether the wire carries each: `external` carries a string callback ticket and no
 * schema; `form` carries an object schema and no ticket; the scalar formats carry
 * neither. Any other spelling — a missing/non-string ticket, a missing/non-object
 * schema, or either extra on a format that carries none — is a frame this page will
 * not render: `undefined` says malformed. */
function facetOf(
  callbackRaw: unknown,
  schemaRaw: unknown,
  format: AnswerFormat,
): QuestionFacet | undefined {
  if (format === 'external') {
    if (typeof callbackRaw !== 'string' || schemaRaw !== undefined) return undefined;
    return { answerFormat: format, callbackUrl: callbackRaw, schema: null };
  }
  if (format === 'form') {
    if (callbackRaw !== undefined || !isRecord(schemaRaw)) return undefined;
    return { answerFormat: format, callbackUrl: null, schema: schemaRaw };
  }
  if (callbackRaw !== undefined || schemaRaw !== undefined) return undefined;
  return { answerFormat: format, callbackUrl: null, schema: null };
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

/**
 * Fold one frame into the model. Pure — the same model and frame always give the
 * same outcome — so the whole transcript behaviour is testable without a stream.
 */
export function applyFrame(model: StreamModel, frame: SseFrame): FrameOutcome {
  if (frame.event === 'chat.backlog_done') return { kind: 'backlog-done' };

  const payload = parseJson(frame.data);
  if (payload === null) return { kind: 'malformed', event: frame.event };
  const id = payload.id;
  if (typeof id !== 'string') return { kind: 'malformed', event: frame.event };

  if (frame.event === 'chat.message') {
    const { direction, text, ts } = payload;
    if (direction !== 'in' && direction !== 'out') return { kind: 'malformed', event: frame.event };
    if (typeof text !== 'string' || !isTimestamp(ts)) {
      return { kind: 'malformed', event: frame.event };
    }
    const clientMessageId = clientMessageIdOf(payload.client_message_id);
    if (clientMessageId === undefined) return { kind: 'malformed', event: frame.event };
    return {
      kind: 'model',
      model: withItem(model, { kind: 'message', id, direction, text, ts, clientMessageId }),
    };
  }

  if (frame.event === 'chat.media') {
    const { direction, text, ts } = payload;
    // The card is always the agent's turn; the wire fixes its direction to `out`.
    if (direction !== 'out') return { kind: 'malformed', event: frame.event };
    if (typeof text !== 'string' || !isTimestamp(ts)) {
      return { kind: 'malformed', event: frame.event };
    }
    const media = mediaOf(payload.media);
    if (media === undefined) return { kind: 'malformed', event: frame.event };
    const options = chipOptionsOf(payload.options);
    if (options === undefined) return { kind: 'malformed', event: frame.event };
    // A card with neither attachments nor chips is a plain notify, which the wire
    // sends as `chat.message` — here it is a frame off its own contract.
    if (media === null && options === null) return { kind: 'malformed', event: frame.event };
    return {
      kind: 'model',
      model: withItem(model, { kind: 'media', id, text, media, options, ts }),
    };
  }

  if (frame.event === 'chat.question') {
    const { interaction_id, question, answer_format, callback_url, schema, timeout_at, ts } =
      payload;
    const options = optionsOf(payload.options);
    if (typeof interaction_id !== 'string') return { kind: 'malformed', event: frame.event };
    if (typeof question !== 'string') return { kind: 'malformed', event: frame.event };
    if (typeof answer_format !== 'string' || !ANSWER_FORMATS.has(answer_format)) {
      return { kind: 'malformed', event: frame.event };
    }
    const facet = facetOf(callback_url, schema, answer_format as AnswerFormat);
    if (facet === undefined) return { kind: 'malformed', event: frame.event };
    if (!isTimestamp(timeout_at) || !isTimestamp(ts)) {
      return { kind: 'malformed', event: frame.event };
    }
    if (options === undefined) return { kind: 'malformed', event: frame.event };
    return {
      kind: 'model',
      model: withItem(model, {
        kind: 'question',
        id,
        interactionId: interaction_id,
        question,
        options,
        timeoutAt: timeout_at,
        ts,
        ...facet,
      }),
    };
  }

  if (frame.event === 'chat.answered') {
    const interactionId = payload.interaction_id;
    if (typeof interactionId !== 'string') return { kind: 'malformed', event: frame.event };
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

  return { kind: 'malformed', event: frame.event };
}

/** The live state of one visitor's transcript. */
export interface ChatStreamState {
  readonly items: readonly ChatItem[];
  readonly answeredIds: ReadonlySet<string>;
  readonly connected: boolean;
  /** The replayed backlog has arrived at least once — until then the page shows
   * its loading state rather than an empty conversation. */
  readonly backlogLoaded: boolean;
  readonly error: Error | null;
  /** Terminal: the deployment runs no transcript store, so reconnecting would
   * replay the same 501 forever. */
  readonly disabled: boolean;
  /** Terminal: the session cookie resolves to nothing; only a reload recovers. */
  readonly sessionExpired: boolean;
}

const INITIAL_STATE: ChatStreamState = {
  items: EMPTY_MODEL.items,
  answeredIds: EMPTY_MODEL.answeredIds,
  connected: false,
  backlogLoaded: false,
  error: null,
  disabled: false,
  sessionExpired: false,
};

// Reconnect backoff: capped exponential with FULL jitter. The delay for attempt n
// is a random value in [0, min(CAP, BASE * 2**n)); the attempt counter resets only
// when a connection is proven healthy (`chat.backlog_done`), not when it merely
// opens — so a server that accepts the request then drops the body still backs off
// instead of tight-looping. The jitter spreads many visitors apart rather than
// letting them all retry in lockstep.
const RECONNECT_BASE_MS = 1500;
const RECONNECT_CAP_MS = 30000;

/**
 * The wait before reconnect attempt `attempt` (0-based), given one `random` draw
 * in [0, 1): full jitter over the capped exponential ceiling. Pure, so the
 * schedule the loop follows is observable without a live stream.
 */
export function reconnectDelayMs(attempt: number, random: number): number {
  return random * Math.min(RECONNECT_CAP_MS, RECONNECT_BASE_MS * 2 ** attempt);
}

/**
 * Subscribe to the visitor's transcript for as long as the page is mounted.
 *
 * `epoch` restarts the subscription from an EMPTY model: rotating the session
 * gives the visitor a new address, so the old conversation's items must not
 * survive into the new one.
 */
export function useChatStream(identity: string, epoch: number): ChatStreamState {
  const [state, setState] = useState<ChatStreamState>(INITIAL_STATE);
  const modelRef = useRef<StreamModel>(EMPTY_MODEL);

  useEffect(() => {
    const controller = new AbortController();
    const aborted = (): boolean => controller.signal.aborted;
    modelRef.current = EMPTY_MODEL;
    setState(INITIAL_STATE);

    // Reset ONLY on a proven-healthy connection (`chat.backlog_done`), never on a
    // bare open — see the backoff note above.
    let reconnectAttempt = 0;

    const handle = (frame: SseFrame): void => {
      const outcome = applyFrame(modelRef.current, frame);
      if (outcome.kind === 'malformed') {
        setState((prev) => ({
          ...prev,
          error: new Error(`malformed transcript frame: ${outcome.event}`),
        }));
        return;
      }
      if (outcome.kind === 'backlog-done') {
        reconnectAttempt = 0;
        setState((prev) => ({ ...prev, backlogLoaded: true, error: null }));
        return;
      }
      modelRef.current = outcome.model;
      setState((prev) => ({
        ...prev,
        items: outcome.model.items,
        answeredIds: outcome.model.answeredIds,
        error: null,
      }));
    };

    const run = async (): Promise<void> => {
      while (!aborted()) {
        try {
          const response = await openChatStream(identity, controller.signal);
          if (aborted()) return;
          setState((prev) => ({ ...prev, connected: true, error: null }));
          for await (const frame of readSseFrames(response, controller.signal)) {
            if (aborted()) return;
            handle(frame);
          }
        } catch (err) {
          if (aborted()) return;
          const error = err instanceof Error ? err : new Error(String(err));
          if (isSessionMissing(error)) {
            // Terminal: reconnecting replays the same refusal on every backoff.
            // Only re-opening the chat URL mints a session, so the page asks the
            // visitor to reload instead of looping.
            setState((prev) => ({ ...prev, connected: false, sessionExpired: true, error }));
            return;
          }
          if (isStoreOff(error)) {
            setState((prev) => ({ ...prev, connected: false, disabled: true, error }));
            return;
          }
          setState((prev) => ({ ...prev, connected: false, error }));
        }
        if (aborted()) return;
        setState((prev) => ({ ...prev, connected: false }));
        const delay = reconnectDelayMs(reconnectAttempt, Math.random());
        reconnectAttempt += 1;
        await new Promise<void>((resolve) => {
          const onAbort = (): void => {
            clearTimeout(timer);
            resolve();
          };
          const timer = setTimeout(() => {
            // Normal wake: drop the abort listener so it does not accumulate
            // across reconnects ({ once: true } only self-removes on firing).
            controller.signal.removeEventListener('abort', onAbort);
            resolve();
          }, delay);
          controller.signal.addEventListener('abort', onAbort, { once: true });
        });
      }
    };

    void run();
    return () => {
      controller.abort();
    };
  }, [identity, epoch]);

  return state;
}
