/**
 * The widget's refusal sentences: the built-in English, with the web route's own
 * sentences drawn over it.
 *
 * The chat page door writes the route's set `transport` words onto `#root` as
 * `data-words` (a JSON object, member name → sentence), and only when the route set any.
 * The attribute is read once per page. The server wrote it from a validated model, so a
 * value that does not parse here (not JSON, not an object, an unknown member, a blank or
 * non-string sentence) is a fault: it is logged as a console error and the page draws the
 * built-in sentences.
 *
 * Tokens are filled with {@link fillToken}: `{subject}` (the thing a door refused) in
 * `too_long` and `not_sent`, `{reason}` (the answer door's own reason) in `answer_refused`
 * and `answer_retries_spent`.
 */

/** The built-in refusal sentences, one per `transport` member. */
export const BUILT_IN_TRANSPORT_WORDS = {
  session_ended: 'Your chat session ended — reload the page to start a new one.',
  wrong_site: 'This chat only works on the site it belongs to.',
  not_listening: "This chat isn't available — there's no one listening on the other end.",
  already_answered: 'That question was already answered.',
  busy: "I'm getting a lot of messages right now — give me a minute and send that again.",
  not_switched_on: 'Chat is not switched on for this deployment.',
  too_long: '{subject} is too long to send — try again with a shorter one.',
  not_sent: "{subject} couldn't be sent.",
  failed: "Something went wrong — that didn't get through. Try again.",
  attachment_gone: 'That attachment is no longer available — attach it again to send it.',
  attachment_already_sent: 'That attachment was already sent with another message.',
  entry_refused: 'This chat is private. Open it from the link you were given.',
  answer_refused: 'That answer was not accepted. {reason}',
  answer_retries_spent:
    'That answer was not accepted, and that question can no longer be answered. {reason}',
  subject_message: 'That message',
  subject_answer: 'That answer',
  subject_form: 'That form',
  subject_request: 'That request',
} as const;

/** One `transport` member's name. */
export type TransportMember = keyof typeof BUILT_IN_TRANSPORT_WORDS;

/** Every `transport` member, resolved to the route's sentence or the built-in one. */
export type TransportWords = Readonly<Record<TransportMember, string>>;

/** The member names a `data-words` object may carry. */
const TRANSPORT_MEMBERS: ReadonlySet<string> = new Set(Object.keys(BUILT_IN_TRANSPORT_WORDS));

/** The route's own sentences, or a description of the first fault. */
function parseWords(raw: string): Partial<TransportWords> | string {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return 'it is not JSON';
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed))
    return 'it is not an object';
  const own: Partial<Record<TransportMember, string>> = {};
  for (const [name, value] of Object.entries(parsed)) {
    if (!TRANSPORT_MEMBERS.has(name)) return `it carries an unknown member ${JSON.stringify(name)}`;
    if (typeof value !== 'string' || value.trim() === '')
      return `its member ${JSON.stringify(name)} is not a non-blank sentence`;
    own[name as TransportMember] = value;
  }
  return own;
}

/** The words read off a page's `#root`, once per element. */
const readWords = new WeakMap<HTMLElement, TransportWords>();

function wordsOf(root: HTMLElement): TransportWords {
  const raw = root.dataset.words;
  if (raw === undefined) return BUILT_IN_TRANSPORT_WORDS;
  const own = parseWords(raw);
  if (typeof own === 'string') {
    console.error(
      `the chat page's data-words cannot be read: ${own}; the built-in words are drawn`,
    );
    return BUILT_IN_TRANSPORT_WORDS;
  }
  return { ...BUILT_IN_TRANSPORT_WORDS, ...own };
}

/** This page's refusal sentences: the route's own over the built-in English. */
export function transportWords(): TransportWords {
  const root = document.getElementById('root');
  if (root === null) return BUILT_IN_TRANSPORT_WORDS;
  let words = readWords.get(root);
  if (words === undefined) {
    words = wordsOf(root);
    readWords.set(root, words);
  }
  return words;
}

/** `text` with every `{token}` replaced by `value`: a plain split/join, never a pattern
 * replace (whose replacement string would read `$&`/`$1`). */
export function fillToken(text: string, token: string, value: string): string {
  return text.split(`{${token}}`).join(value);
}
