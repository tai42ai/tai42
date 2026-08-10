/**
 * The transcript stream: the SSE feed of one visitor's conversation, folded into
 * the ordered items the page renders.
 *
 * The wire carries four events. `chat.message` and `chat.question` are transcript
 * ENTRIES and become items in arrival order; `chat.answered` is not an entry of
 * its own — it settles the question it names, so it only records an interaction
 * id; `chat.backlog_done` marks the replayed backlog as complete.
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

import { isSessionMissing, isStoreOff, openChatStream } from '@/api';
import { readSseFrames, type SseFrame } from '@/sse';

/** The answer shapes a channel-delivered question can take (the contract's
 * channel-deliverable set — `form` is never delivered to a channel). */
export type AnswerFormat = 'text' | 'confirm' | 'select' | 'external';

const ANSWER_FORMATS: ReadonlySet<string> = new Set<AnswerFormat>([
  'text',
  'confirm',
  'select',
  'external',
]);

/** The interactions callback door — the answer sink an `external` question sends
 * the visitor to. It is an interaction bearer ticket, so it reaches the page for
 * that one format and is `null` for the other three, which answer by interaction
 * id through this channel's own door. Ticket and format travel as ONE type, so a
 * widget that reads the ticket has the format that guarantees it. */
export type QuestionTicket =
  | { readonly answerFormat: 'external'; readonly callbackUrl: string }
  | { readonly answerFormat: Exclude<AnswerFormat, 'external'>; readonly callbackUrl: null };

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
  | (QuestionBase & QuestionTicket);

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

/** The sender's own idempotency key, echoed onto their message. The wire carries
 * the key as a string or omits it entirely — the key is the sender's, so the
 * server never invents one. Any other spelling (a non-string, an explicit null) is
 * a frame this page will not render: `undefined` says malformed. */
function clientMessageIdOf(raw: unknown): string | null | undefined {
  if (typeof raw === 'string') return raw;
  return raw === undefined ? null : undefined;
}

/** The callback ticket, by the format that decides whether the wire carries one:
 * present and a string for `external` — the only widget that opens it — and
 * ABSENT for the other three, whose ticket never leaves the server. Any other
 * spelling (a missing external ticket, a non-string, an explicit null, a ticket on
 * a format that answers here) is a frame this page will not render: `undefined`
 * says malformed. */
function ticketOf(raw: unknown, format: AnswerFormat): QuestionTicket | undefined {
  if (format === 'external') {
    return typeof raw === 'string' ? { answerFormat: format, callbackUrl: raw } : undefined;
  }
  return raw === undefined ? { answerFormat: format, callbackUrl: null } : undefined;
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

  if (frame.event === 'chat.question') {
    const { interaction_id, question, answer_format, callback_url, timeout_at, ts } = payload;
    const options = optionsOf(payload.options);
    if (typeof interaction_id !== 'string') return { kind: 'malformed', event: frame.event };
    if (typeof question !== 'string') return { kind: 'malformed', event: frame.event };
    if (typeof answer_format !== 'string' || !ANSWER_FORMATS.has(answer_format)) {
      return { kind: 'malformed', event: frame.event };
    }
    const ticket = ticketOf(callback_url, answer_format as AnswerFormat);
    if (ticket === undefined) return { kind: 'malformed', event: frame.event };
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
        ...ticket,
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
