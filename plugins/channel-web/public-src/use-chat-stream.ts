/**
 * The transcript stream: the SSE feed of one visitor's conversation, folded into
 * the ordered items the page renders.
 *
 * The wire carries six events. `chat.message`, `chat.question`, `chat.media` and
 * `chat.form` are transcript ENTRIES and become items in arrival order;
 * `chat.answered` is not an entry of its own — it settles the question it names,
 * so it only records an interaction id; `chat.backlog_done` marks the replayed
 * backlog as complete.
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

/** One per-send choice for a form field: `value` is submitted as the answer,
 * `label` is shown in its place (`null` shows the value itself). A per-send option
 * list REPLACES a property's schema choices for this one delivery. */
export interface FormOptionData {
  readonly value: string;
  readonly label: string | null;
}

/** A form question's per-send enrichment: `values` prefills top-level properties
 * (shown filled in) and `options` supplies the per-send choice list for a property,
 * keyed by property name. Both keyed by the schema's top-level property names. */
export interface FormPrefill {
  readonly values: Readonly<Record<string, unknown>>;
  readonly options: Readonly<Record<string, readonly FormOptionData[]>>;
}

/** One step of a stepped form: `title` heads the step and `fields` names the
 * top-level properties shown on it, in order. Across a form's pages every property
 * appears exactly once; absent pages means one page. */
export interface FormPage {
  readonly title: string;
  readonly fields: readonly string[];
}

/** The format-dependent extras a question row carries, keyed by the format that
 * decides whether the wire carries each: the interactions callback ticket for
 * `external` (the one widget that opens it), the JSON answer schema for `form` (the
 * one widget that builds an answer from it) plus that form's optional per-send
 * `formData` and `pages`, and none of these for the scalar formats. Extras and format
 * travel as ONE type, so a widget that reads an extra has the format that guarantees
 * it. */
export type QuestionFacet =
  | {
      readonly answerFormat: 'external';
      readonly callbackUrl: string;
      readonly schema: null;
      readonly formData: null;
      readonly pages: null;
    }
  | {
      readonly answerFormat: 'form';
      readonly callbackUrl: null;
      readonly schema: JsonSchema;
      /** Per-send prefill + choices, or `null` when the ask carried none. */
      readonly formData: FormPrefill | null;
      /** The form's steps, or `null` for one page. */
      readonly pages: readonly FormPage[] | null;
    }
  | {
      readonly answerFormat: 'text' | 'confirm' | 'select';
      readonly callbackUrl: null;
      readonly schema: null;
      readonly formData: null;
      readonly pages: null;
    };

/** The media kinds the page renders: an inline `image`, a `document` download
 * card, native `video`/`audio` players, and a safe outbound `link` anchor. */
export type MediaKind = 'image' | 'link' | 'document' | 'video' | 'audio';

/** One attachment on a card or a question. The url is already proven absolute by
 * the reducer — `https:` for a file kind (image/document/video/audio), `http(s):`
 * for a link — so a renderer sets it as an attribute without re-checking.
 * `filename` is the document's suggested download name and is `null` on every
 * other kind (the server sends it on a document only). */
export interface MediaItem {
  readonly kind: MediaKind;
  readonly url: string;
  readonly caption: string | null;
  readonly filename: string | null;
}

/** A tappable reply option: a tap SUBMITS `text` as the visitor's next message.
 * `description` is an optional secondary line; `id` is the author-set stable id
 * that rides the submission (`params.reply_id`) when set. */
export interface ReplyOption {
  readonly kind: 'reply';
  readonly text: string;
  readonly description: string | null;
  readonly id: string | null;
}

/** A tappable link action: a tap OPENS `url` (a new tab), submitting nothing.
 * The url is proven absolute `http(s):` by the reducer. Distinct from a reply. */
export interface LinkOption {
  readonly kind: 'link';
  readonly label: string;
  readonly url: string;
}

/** One tappable option on a card: a reply chip or a link action. */
export type CardOption = ReplyOption | LinkOption;

/** One titled section of a sectioned option list: a header and its non-empty
 * reply rows (a section holds reply rows only — a link is a button, never a row). */
export interface OptionSection {
  readonly title: string;
  readonly rows: readonly ReplyOption[];
}

/** A shared geographic point rendered as a map-pin element. The coordinates are
 * finite and in WGS84 range; `name`/`address` are optional labels. */
export interface LocationPoint {
  readonly latitude: number;
  readonly longitude: number;
  readonly name: string | null;
  readonly address: string | null;
}

/** Everything a question row carries apart from its ticket. */
interface QuestionBase {
  readonly kind: 'question';
  readonly id: string;
  readonly interactionId: string;
  readonly question: string;
  readonly options: readonly string[] | null;
  /** Display media shown WITH the question — the same shape a media card carries.
   * `null` when the question carries none. Display-only: never part of the answer. */
  readonly media: readonly MediaItem[] | null;
  readonly timeoutAt: string;
  readonly ts: string;
}

/** One rendered row of the transcript. */
export type ChatItem =
  | {
      /** An agent-sent ask-less form card: a prompt, a fillable schema and the
       * server-minted token its submission door is named by. Always the agent's
       * turn. Unlike a question it has no deadline, no answered state and no
       * options — it stays fillable for as long as it replays. */
      readonly kind: 'form';
      readonly id: string;
      readonly text: string;
      readonly schema: JsonSchema;
      readonly token: string;
      readonly media: readonly MediaItem[] | null;
      /** A shared location the form rides alongside its fields — a map-pin
       * element. `null` when the form carries none. */
      readonly location: LocationPoint | null;
      readonly ts: string;
    }
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
      /** An agent-sent rich card: markdown text with any media (image/document/
       * video/audio/link), a flat option list (reply chips + link actions) OR a
       * sectioned reply list, an optional media header and muted footer, and an
       * optional location map-pin. Always the agent's turn, so it carries no
       * direction. `options` and `sections` are mutually exclusive. */
      readonly kind: 'media';
      readonly id: string;
      readonly text: string;
      readonly media: readonly MediaItem[] | null;
      readonly options: readonly CardOption[] | null;
      readonly sections: readonly OptionSection[] | null;
      readonly header: MediaItem | null;
      readonly footer: string | null;
      readonly location: LocationPoint | null;
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

/** An optional non-blank secondary label (a reply option's `description`): a
 * string when present, `null` when absent, `undefined` (malformed) otherwise. */
function secondaryLabelOf(raw: unknown): string | null | undefined {
  if (raw === undefined) return null;
  if (typeof raw !== 'string' || raw.trim() === '') return undefined;
  return raw;
}

/** One reply option: a `reply` kind, a non-blank `text`, and an optional non-blank
 * `description` and `id`. `undefined` on anything off-shape. */
function replyOptionOf(raw: unknown): ReplyOption | undefined {
  if (!isRecord(raw) || raw.kind !== 'reply') return undefined;
  const { text } = raw;
  if (typeof text !== 'string' || text.trim() === '') return undefined;
  const description = secondaryLabelOf(raw.description);
  if (description === undefined) return undefined;
  const id = secondaryLabelOf(raw.id);
  if (id === undefined) return undefined;
  return { kind: 'reply', text, description, id };
}

/** One card option: a reply chip (a tap submits its text) or a link action (a tap
 * opens its absolute `http(s):` url). `undefined` on anything off-shape. */
function cardOptionOf(raw: unknown): CardOption | undefined {
  if (!isRecord(raw)) return undefined;
  if (raw.kind === 'link') {
    const { label, url } = raw;
    if (typeof label !== 'string' || label.trim() === '') return undefined;
    if (!isAbsoluteUrl(url, ['http:', 'https:'])) return undefined;
    return { kind: 'link', label, url };
  }
  return replyOptionOf(raw);
}

/** The flat option list on a card: absent (`null`), or a NON-EMPTY list whose
 * every entry is a valid reply/link option. An empty list is a control row with no
 * controls, and one off-shape entry taints the whole list: `undefined` says
 * malformed. */
function cardOptionsOf(raw: unknown): readonly CardOption[] | null | undefined {
  if (raw === null || raw === undefined) return null;
  if (!Array.isArray(raw) || raw.length === 0) return undefined;
  const options: CardOption[] = [];
  for (const one of raw) {
    const option = cardOptionOf(one);
    if (option === undefined) return undefined;
    options.push(option);
  }
  return options;
}

/** One section of a sectioned list: a non-blank `title` and a NON-EMPTY list of
 * reply rows. `undefined` on anything off-shape. */
function sectionOf(raw: unknown): OptionSection | undefined {
  if (!isRecord(raw)) return undefined;
  const { title, rows } = raw;
  if (typeof title !== 'string' || title.trim() === '') return undefined;
  if (!Array.isArray(rows) || rows.length === 0) return undefined;
  const parsed: ReplyOption[] = [];
  for (const one of rows) {
    const row = replyOptionOf(one);
    if (row === undefined) return undefined;
    parsed.push(row);
  }
  return { title, rows: parsed };
}

/** The sectioned option list on a card: absent (`null`), or a NON-EMPTY list whose
 * every section validates. `undefined` says malformed. */
function sectionsOf(raw: unknown): readonly OptionSection[] | null | undefined {
  if (raw === null || raw === undefined) return null;
  if (!Array.isArray(raw) || raw.length === 0) return undefined;
  const sections: OptionSection[] = [];
  for (const one of raw) {
    const section = sectionOf(one);
    if (section === undefined) return undefined;
    sections.push(section);
  }
  return sections;
}

/** The card's media header: absent (`null`), or a single DISPLAY-media item — a
 * `link` header is off-contract (an anchor is content, not a header). `undefined`
 * says malformed. */
function headerOf(raw: unknown): MediaItem | null | undefined {
  if (raw === null || raw === undefined) return null;
  const item = mediaItemOf(raw);
  if (item === undefined || item.kind === 'link') return undefined;
  return item;
}

/** The card's footer line: absent (`null`), or a non-blank string. `undefined`
 * says malformed. */
function footerOf(raw: unknown): string | null | undefined {
  if (raw === undefined) return null;
  if (typeof raw !== 'string' || raw.trim() === '') return undefined;
  return raw;
}

/** A coordinate constrained to `[min, max]`, finite. */
function coordinateOf(raw: unknown, min: number, max: number): number | undefined {
  if (typeof raw !== 'number' || !Number.isFinite(raw) || raw < min || raw > max) return undefined;
  return raw;
}

/** A shared location: absent (`null`), or finite in-range coordinates plus an
 * optional non-blank name/address. `undefined` says malformed. */
function locationOf(raw: unknown): LocationPoint | null | undefined {
  if (raw === null || raw === undefined) return null;
  if (!isRecord(raw)) return undefined;
  const latitude = coordinateOf(raw.latitude, -90, 90);
  const longitude = coordinateOf(raw.longitude, -180, 180);
  if (latitude === undefined || longitude === undefined) return undefined;
  const name = secondaryLabelOf(raw.name);
  if (name === undefined) return undefined;
  const address = secondaryLabelOf(raw.address);
  if (address === undefined) return undefined;
  return { latitude, longitude, name, address };
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

/** A document's suggested download filename: the wire carries a non-blank string
 * or omits it (a blank name is no name — the server never sends one). Any other
 * spelling is malformed (`undefined`); an absent one is `null`. */
function filenameOf(raw: unknown): string | null | undefined {
  if (raw === undefined) return null;
  if (typeof raw !== 'string' || raw.trim() === '') return undefined;
  return raw;
}

/** The file media kinds — a fetchable body constrained to an absolute `https:`
 * source (an `image`, `document`, `video`, or `audio`), as opposed to a `link`
 * anchor which the human clicks through (`http(s):`). */
const FILE_MEDIA_KINDS: ReadonlySet<string> = new Set<MediaKind>([
  'image',
  'document',
  'video',
  'audio',
]);

/** One media attachment, validated: a known kind, a scheme-appropriate absolute
 * URL, an optional string caption, and a `filename` that rides a `document` ONLY
 * (present on any other kind is off-contract and malformed). `undefined` on
 * anything off-shape. */
function mediaItemOf(raw: unknown): MediaItem | undefined {
  if (!isRecord(raw)) return undefined;
  const { kind, url } = raw;
  if (typeof kind !== 'string' || (!FILE_MEDIA_KINDS.has(kind) && kind !== 'link'))
    return undefined;
  const protocols = kind === 'link' ? ['http:', 'https:'] : ['https:'];
  if (!isAbsoluteUrl(url, protocols)) return undefined;
  const caption = captionOf(raw.caption);
  if (caption === undefined) return undefined;
  const filename = filenameOf(raw.filename);
  if (filename === undefined) return undefined;
  // A filename names a download the medium offers, which only a document has.
  if (filename !== null && kind !== 'document') return undefined;
  return { kind: kind as MediaKind, url, caption, filename };
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

/** One per-send form option: a non-blank string `value` and an optional non-blank
 * `label` (absent → `null`). `undefined` on anything off-shape. */
function formOptionOf(raw: unknown): FormOptionData | undefined {
  if (!isRecord(raw)) return undefined;
  const { value } = raw;
  if (typeof value !== 'string' || value.trim() === '') return undefined;
  const label = secondaryLabelOf(raw.label);
  if (label === undefined) return undefined;
  return { value, label };
}

/** A form question's per-send enrichment: absent (`null`), or a `{values, options}`
 * record whose `values` is an object and whose `options` maps each property to a
 * NON-EMPTY list of valid options. One off-shape entry taints the whole frame:
 * `undefined` says malformed. */
function formPrefillOf(raw: unknown): FormPrefill | null | undefined {
  if (raw === null || raw === undefined) return null;
  if (!isRecord(raw)) return undefined;
  const { values, options } = raw;
  if (!isRecord(values) || !isRecord(options)) return undefined;
  const parsed: Record<string, readonly FormOptionData[]> = {};
  for (const [name, list] of Object.entries(options)) {
    if (!Array.isArray(list) || list.length === 0) return undefined;
    const choices: FormOptionData[] = [];
    for (const one of list) {
      const option = formOptionOf(one);
      if (option === undefined) return undefined;
      choices.push(option);
    }
    parsed[name] = choices;
  }
  return { values, options: parsed };
}

/** One form page: a non-blank `title` and a NON-EMPTY list of non-blank field names.
 * `undefined` on anything off-shape. */
function formPageOf(raw: unknown): FormPage | undefined {
  if (!isRecord(raw)) return undefined;
  const { title, fields } = raw;
  if (typeof title !== 'string' || title.trim() === '') return undefined;
  if (!Array.isArray(fields) || fields.length === 0) return undefined;
  const names: string[] = [];
  for (const field of fields) {
    if (typeof field !== 'string' || field.trim() === '') return undefined;
    names.push(field);
  }
  return { title, fields: names };
}

/** A form question's step layout: absent (`null`), or a NON-EMPTY list whose every
 * page validates. `undefined` says malformed. */
function formPagesOf(raw: unknown): readonly FormPage[] | null | undefined {
  if (raw === null || raw === undefined) return null;
  if (!Array.isArray(raw) || raw.length === 0) return undefined;
  const pages: FormPage[] = [];
  for (const one of raw) {
    const page = formPageOf(one);
    if (page === undefined) return undefined;
    pages.push(page);
  }
  return pages;
}

/** The format-dependent extras, validated together by the format that decides
 * whether the wire carries each: `external` carries a string callback ticket and no
 * schema; `form` carries an object schema, no ticket, and optional per-send
 * `data`/`pages`; the scalar formats carry none. Any other spelling — a
 * missing/non-string ticket, a missing/non-object schema, malformed `data`/`pages`,
 * or any of these extras on a format that carries none — is a frame this page will
 * not render: `undefined` says malformed. */
function facetOf(
  callbackRaw: unknown,
  schemaRaw: unknown,
  dataRaw: unknown,
  pagesRaw: unknown,
  format: AnswerFormat,
): QuestionFacet | undefined {
  if (format === 'external') {
    if (typeof callbackRaw !== 'string' || schemaRaw !== undefined) return undefined;
    if (dataRaw !== undefined || pagesRaw !== undefined) return undefined;
    return {
      answerFormat: format,
      callbackUrl: callbackRaw,
      schema: null,
      formData: null,
      pages: null,
    };
  }
  if (format === 'form') {
    if (callbackRaw !== undefined || !isRecord(schemaRaw)) return undefined;
    const formData = formPrefillOf(dataRaw);
    if (formData === undefined) return undefined;
    const pages = formPagesOf(pagesRaw);
    if (pages === undefined) return undefined;
    return { answerFormat: format, callbackUrl: null, schema: schemaRaw, formData, pages };
  }
  if (callbackRaw !== undefined || schemaRaw !== undefined) return undefined;
  if (dataRaw !== undefined || pagesRaw !== undefined) return undefined;
  return { answerFormat: format, callbackUrl: null, schema: null, formData: null, pages: null };
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
    const options = cardOptionsOf(payload.options);
    if (options === undefined) return { kind: 'malformed', event: frame.event };
    const sections = sectionsOf(payload.sections);
    if (sections === undefined) return { kind: 'malformed', event: frame.event };
    const header = headerOf(payload.header);
    if (header === undefined) return { kind: 'malformed', event: frame.event };
    const footer = footerOf(payload.footer);
    if (footer === undefined) return { kind: 'malformed', event: frame.event };
    const location = locationOf(payload.location);
    if (location === undefined) return { kind: 'malformed', event: frame.event };
    // Composition, mirroring the contract so a frame off its own rules never
    // renders: one choice surface (options XOR sections); a header/footer rides an
    // interactive card (it needs options or sections); a card with no content at all
    // is a plain notify the wire sends as `chat.message`.
    if (options !== null && sections !== null) return { kind: 'malformed', event: frame.event };
    const hasChoice = options !== null || sections !== null;
    if ((header !== null || footer !== null) && !hasChoice)
      return { kind: 'malformed', event: frame.event };
    if (media === null && !hasChoice && location === null)
      return { kind: 'malformed', event: frame.event };
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

  if (frame.event === 'chat.form') {
    const { text, schema, token, ts } = payload;
    if (typeof text !== 'string' || !isTimestamp(ts)) {
      return { kind: 'malformed', event: frame.event };
    }
    // Strict: the schema must be an object and the token a non-blank string —
    // without either the card has no widget to render or no door to submit to,
    // and a silently dropped control is exactly what "malformed" exists to stop.
    if (!isRecord(schema)) return { kind: 'malformed', event: frame.event };
    if (typeof token !== 'string' || token.trim() === '') {
      return { kind: 'malformed', event: frame.event };
    }
    const media = mediaOf(payload.media);
    if (media === undefined) return { kind: 'malformed', event: frame.event };
    const location = locationOf(payload.location);
    if (location === undefined) return { kind: 'malformed', event: frame.event };
    return {
      kind: 'model',
      model: withItem(model, { kind: 'form', id, text, schema, token, media, location, ts }),
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
    const facet = facetOf(
      callback_url,
      schema,
      payload.data,
      payload.pages,
      answer_format as AnswerFormat,
    );
    if (facet === undefined) return { kind: 'malformed', event: frame.event };
    if (!isTimestamp(timeout_at) || !isTimestamp(ts)) {
      return { kind: 'malformed', event: frame.event };
    }
    if (options === undefined) return { kind: 'malformed', event: frame.event };
    // The same vetted-media parse the media card uses — one off-shape item taints
    // the whole frame, so an image `src`/link `href` on the question is as trusted
    // as one on a card.
    const media = mediaOf(payload.media);
    if (media === undefined) return { kind: 'malformed', event: frame.event };
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
