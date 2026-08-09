/**
 * The chat page's HTTP seam onto its own public doors under `/api/channels/web`.
 *
 * There is NO credential to attach: the visitor's `tai_web_session` cookie is
 * `HttpOnly` and rides every same-origin request on its own, so these calls send
 * no auth header and read no token. Responses use the platform envelope —
 * `{"data": …}` on success, `{"error": …}` (plus a `"code"` on the refusals the
 * page acts on programmatically) on failure.
 *
 * Every non-2xx becomes a {@link ChatApiError} carrying the status, the machine
 * `code` when the door sent one, and a message already written for a VISITOR to
 * read: a shed or overflow says the assistant is busy, an unrouted identity says
 * the chat is not available. The door's own wording is never dropped — it is
 * logged verbatim so a raw diagnostic stays reachable without ever reaching the
 * transcript.
 */
import { sseOpenToken } from '@/sse';

const BASE = '/api/channels/web';

/** The door signals "your cookie resolves to no session on this web route" with
 * this code; the page answers by asking the visitor to reload (the chat URL mints
 * a fresh session for the route it belongs to). */
export const SESSION_MISSING_CODE = 'session_missing';

/** The deployment runs no transcript store, so the stream can only ever refuse. */
export const STORE_OFF_CODE = 'web_transcript_store_off';

/** A door refusal, already worded for the visitor. */
export class ChatApiError extends Error {
  readonly status: number;
  readonly code: string | null;

  constructor(message: string, status: number, code: string | null) {
    super(message);
    this.name = 'ChatApiError';
    this.status = status;
    this.code = code;
  }
}

/** True when the failure means the visitor's session is gone and only a reload
 * (which re-mints the cookie) can recover it. */
export function isSessionMissing(error: unknown): boolean {
  return error instanceof ChatApiError && error.code === SESSION_MISSING_CODE;
}

/** True when the deployment has no transcript store — a terminal refusal, so the
 * stream must stop reconnecting rather than replay it on every backoff. */
export function isStoreOff(error: unknown): boolean {
  return error instanceof ChatApiError && error.code === STORE_OFF_CODE;
}

const BUSY = "I'm getting a lot of messages right now — give me a minute and send that again.";

/**
 * The longest `text` the message door accepts. Mirrored by the composer, which
 * stops the visitor at the field rather than letting the door refuse a message
 * they have already sent. The composer's cap counts UTF-16 units where the door
 * counts code points, so it is the stricter of the two and can never let through
 * a text the door would reject.
 */
export const MAX_MESSAGE_CHARS = 8000;

/**
 * One door, in the two words a refusal needs from it: what was being attempted
 * (logged), and what to call the thing they were sending when the door would not
 * take it.
 */
interface Door {
  readonly what: string;
  readonly subject: string;
}

const MESSAGE_DOOR: Door = { what: 'sending a message', subject: 'That message' };
const QUESTION_DOOR: Door = { what: 'answering a question', subject: 'That answer' };
const ROTATE_DOOR: Door = { what: 'starting a new conversation', subject: 'That request' };
const STREAM_DOOR: Door = { what: 'opening the chat stream', subject: 'That request' };

/**
 * The visitor-facing wording for one refusal. `detail` is the door's own message,
 * used only where it is already plain and specific (a rejected body); everywhere
 * else it is replaced, because a bridge lookup failure, a queue-overflow trace, or
 * a field-level validation message is not something a visitor can act on.
 */
function friendlyMessage(status: number, detail: string | null, door: Door): string {
  if (status === 401) return 'Your chat session ended — reload the page to start a new one.';
  // The only 403 the doors this page FETCHES emit is an origin mismatch, which no
  // reload changes: the copy must not send the visitor after a fix that cannot
  // work. (The mint guard's `not_a_navigation` is the chat-page door's — it answers
  // a top-level navigation, never a fetch from this bundle, so it cannot land here.)
  if (status === 403) return 'This chat only works on the site it belongs to.';
  if (status === 404)
    return "This chat isn't available — there's no one listening on the other end.";
  if (status === 409) return 'That question was already answered.';
  if (status === 429 || status === 503) return BUSY;
  if (status === 501) return 'Chat is not switched on for this deployment.';
  // The body never reached the door's field rules — it was refused at the size cap
  // — so the same bytes are refused again every time. The copy names the one thing
  // that changes the outcome instead of promising a retry that cannot work.
  if (status === 413) return `${door.subject} is too long to send — try again with a shorter one.`;
  // A 422 is the door's own field-level validation, worded for whoever wrote the
  // request ("invalid request body: text: String should have at most 8000
  // characters"). It is logged verbatim in {@link failure}; what the visitor reads
  // names the thing that was refused and nothing of the contract behind it.
  if (status === 422) return `${door.subject} couldn't be sent.`;
  if (status === 400) return detail ?? `${door.subject} couldn't be sent.`;
  return "Something went wrong — that didn't get through. Try again.";
}

interface Envelope<T> {
  readonly data?: T;
  readonly error?: string;
  readonly code?: string;
}

/** The door's `error`/`code` pair, or nulls when the body is not an envelope (a
 * proxy's HTML error page, an empty body). */
async function readFailure(
  response: Response,
): Promise<{ detail: string | null; code: string | null }> {
  let text = '';
  try {
    text = await response.text();
  } catch {
    return { detail: null, code: null };
  }
  if (text === '') return { detail: null, code: null };
  let envelope: Envelope<unknown>;
  try {
    envelope = JSON.parse(text) as Envelope<unknown>;
  } catch {
    return { detail: null, code: null };
  }
  return {
    detail: typeof envelope.error === 'string' ? envelope.error : null,
    code: typeof envelope.code === 'string' ? envelope.code : null,
  };
}

async function failure(response: Response, door: Door): Promise<ChatApiError> {
  const { detail, code } = await readFailure(response);
  // The door's own wording never reaches the transcript, so it is logged here —
  // a refusal stays diagnosable without a raw trace landing in a conversation.
  console.error(`${door.what} refused: HTTP ${response.status}`, detail ?? '(no error body)');
  return new ChatApiError(friendlyMessage(response.status, detail, door), response.status, code);
}

async function readData<T>(response: Response, what: string): Promise<T> {
  const text = await response.text();
  let envelope: Envelope<T>;
  try {
    envelope = JSON.parse(text) as Envelope<T>;
  } catch {
    throw new Error(`${what} returned a body that is not JSON`);
  }
  if (envelope.data === undefined) throw new Error(`${what} returned no data`);
  return envelope.data;
}

/**
 * Send one visitor message into the conversation the session cookie addresses.
 * Resolves with the bridge's `message_id` — the SAME id the transcript frame for
 * this message carries, which is what lets the page retire its optimistic bubble
 * when the real one arrives.
 *
 * `clientMessageId` is this message's idempotency key, matching
 * `^[A-Za-z0-9_-]{8,64}$`. The door derives a visitor-scoped provider message id
 * from it, so re-sending one whose response was lost returns the ORIGINAL
 * `message_id` rather than delivering the message a second time. Every attempt at
 * the same composed message must carry the same key.
 */
export async function sendMessage(
  identity: string,
  text: string,
  clientMessageId: string,
): Promise<string> {
  const response = await fetch(`${BASE}/messages`, {
    method: 'POST',
    headers: { accept: 'application/json', 'content-type': 'application/json' },
    body: JSON.stringify({ identity, text, client_message_id: clientMessageId }),
  });
  if (!response.ok) throw await failure(response, MESSAGE_DOOR);
  const data = await readData<{ message_id?: unknown }>(response, 'sending a message');
  if (typeof data.message_id !== 'string')
    throw new Error('the message door returned no message_id');
  return data.message_id;
}

/** Answer one pending question. The door verifies the record belongs to this
 * visitor's own conversation before forwarding. */
export async function answerQuestion(interactionId: string, answer: unknown): Promise<void> {
  const response = await fetch(`${BASE}/questions/${encodeURIComponent(interactionId)}/answer`, {
    method: 'POST',
    headers: { accept: 'application/json', 'content-type': 'application/json' },
    body: JSON.stringify({ answer }),
  });
  if (!response.ok) throw await failure(response, QUESTION_DOOR);
}

/** Mint a fresh session for this web route — the visitor's "new conversation". A
 * session belongs to one route, so the door is told which; the next message opens
 * a conversation on the new address, and the old transcript is untouched.
 *
 * `entryCode` carries the page URL's `tai_entry` value when it has one: a rotation
 * on a gated route is refused without a live code, exactly as opening the page is.
 * It is omitted from the body when absent, so an ungated route's request is
 * byte-identical to before. */
export async function rotateSession(
  identity: string,
  entryCode: string | null = null,
): Promise<void> {
  const body: { identity: string; entry_code?: string } =
    entryCode === null ? { identity } : { identity, entry_code: entryCode };
  const response = await fetch(`${BASE}/session/rotate`, {
    method: 'POST',
    headers: { accept: 'application/json', 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw await failure(response, ROTATE_DOOR);
}

/**
 * Open the transcript stream. Every open gets a distinct URL (see
 * {@link sseOpenToken}) so a reconnect is never coalesced onto the dead
 * connection it is replacing. A non-2xx is raised BEFORE any body is read, so the
 * caller can tell a terminal refusal from a dropped connection.
 */
export async function openChatStream(identity: string, signal: AbortSignal): Promise<Response> {
  const url = `${BASE}/stream?identity=${encodeURIComponent(identity)}&_=${sseOpenToken()}`;
  const response = await fetch(url, { headers: { accept: 'text/event-stream' }, signal });
  if (!response.ok) throw await failure(response, STREAM_DOOR);
  return response;
}
