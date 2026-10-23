/**
 * A web route's own words, as the three words doors answer them. The words are keyed by
 * the route's `our_identity` and come in two groups: `page`, the chat page's refusal pages
 * (each a `heading` and a `text`), and `transport`, the sentences the chat widget draws
 * when a request is refused. Only the members a route set are present; every member left
 * out draws the built-in English.
 *
 * The catalogues below name every member with its label and what it covers, mirroring the
 * server's `WebWords` model member for member, so the panel draws one input per member.
 */
import { z } from 'zod';

/** One refusal page's words: its heading (the page title) and its sentence. */
export const refusalPageWords = z.object({
  heading: z.string().optional(),
  text: z.string().optional(),
});
export type RefusalPageWords = z.infer<typeof refusalPageWords>;

/** The refusal pages a route may word. */
export const PAGE_MEMBERS = [
  {
    name: 'not_a_navigation',
    label: 'Not opened as a page',
    description:
      'The chat URL was loaded by something other than a top-level navigation (an embed or a script).',
  },
  {
    name: 'link_invalid',
    label: 'Link not valid',
    description:
      "The chat link's query parameters break a bound (a repeated name, a bad name, too long, too many).",
  },
  {
    name: 'entry_refused',
    label: 'Entry refused',
    description: 'The route is gated and the link carries no live entry code.',
  },
  {
    name: 'page_unavailable',
    label: 'Page unavailable',
    description: 'The chat page bundle on the server cannot be served.',
  },
] as const;

export type PageMember = (typeof PAGE_MEMBERS)[number]['name'];

/** The widget's refusal sentences a route may word. */
export const TRANSPORT_MEMBERS = [
  {
    name: 'session_ended',
    label: 'Session ended',
    description: "The visitor's session is gone (HTTP 401).",
  },
  {
    name: 'wrong_site',
    label: 'Wrong site',
    description: 'The request came from another site (HTTP 403).',
  },
  {
    name: 'not_listening',
    label: 'Not listening',
    description: 'No route answers this chat (HTTP 404).',
  },
  {
    name: 'already_answered',
    label: 'Already answered',
    description: 'The question was already answered (409).',
  },
  { name: 'busy', label: 'Busy', description: 'The assistant is shedding load (HTTP 429 or 503).' },
  {
    name: 'not_switched_on',
    label: 'Not switched on',
    description: 'The deployment runs no chat store (501).',
  },
  {
    name: 'too_long',
    label: 'Too long',
    description: 'The request is over the size cap (HTTP 413). Token: {subject}.',
  },
  {
    name: 'not_sent',
    label: 'Not sent',
    description: "The door refused the request's content (HTTP 400 or 422). Token: {subject}.",
  },
  { name: 'failed', label: 'Failed', description: 'Any other failure of the door.' },
  {
    name: 'attachment_gone',
    label: 'Attachment gone',
    description: 'An attachment can no longer be sent with the message.',
  },
  {
    name: 'attachment_already_sent',
    label: 'Attachment already sent',
    description: 'An attachment was already sent with another message.',
  },
  {
    name: 'entry_refused',
    label: 'Entry refused',
    description: "A new conversation was refused by the route's entry gate.",
  },
  {
    name: 'answer_refused',
    label: 'Answer refused',
    description:
      'An answer was refused and may be given again, when the send set no sentence. Token: {reason}.',
  },
  {
    name: 'answer_retries_spent',
    label: 'Answer retries spent',
    description:
      'An answer was refused and the question can no longer be answered, when the send set no sentence. Token: {reason}.',
  },
  {
    name: 'subject_message',
    label: 'Subject: message',
    description: 'The {subject} of a chat message.',
  },
  {
    name: 'subject_answer',
    label: 'Subject: answer',
    description: "The {subject} of a question's answer.",
  },
  { name: 'subject_form', label: 'Subject: form', description: 'The {subject} of a form.' },
  {
    name: 'subject_request',
    label: 'Subject: request',
    description: 'The {subject} of any other request (a new conversation, the chat stream).',
  },
] as const;

export type TransportMember = (typeof TRANSPORT_MEMBERS)[number]['name'];

/** A web route's words as the read and replace doors answer them: only the set members. */
export const webWords = z.object({
  page: z
    .partialRecord(z.enum(PAGE_MEMBERS.map((member) => member.name)), refusalPageWords)
    .optional(),
  transport: z
    .partialRecord(z.enum(TRANSPORT_MEMBERS.map((member) => member.name)), z.string())
    .optional(),
});
export type WebWords = z.infer<typeof webWords>;

/** The words after a clear. */
export const webWordsCleared = z.object({ status: z.literal('cleared') });
