/**
 * The transcript data model: the typed shapes one visitor's conversation folds
 * into, and the empty model a fresh subscription starts from.
 *
 * The wire carries seven events. `chat.message`, `chat.question`, `chat.media`,
 * `chat.form` and `chat.unavailable` are transcript ENTRIES and become items in
 * arrival order (`chat.unavailable` is the placeholder for a stored entry the server
 * could not render); `chat.answered` is not an entry of its own — it settles the
 * question it names, so it only records an interaction id; `chat.backlog_done` marks
 * the replayed backlog as complete.
 */
import type { JsonSchema } from '@tai42/studio-sdk';

/** The permissive schema type the form widget renders, re-exported so a question
 * consumer types against one import. */
export type { JsonSchema };

/** The answer shapes a channel-delivered question can take. `form` IS delivered to
 * this channel — the page advertises `supports_form_delivery` and renders a
 * schema-driven form widget for it. */
export type AnswerFormat = 'text' | 'confirm' | 'select' | 'form' | 'external';

export const ANSWER_FORMATS: ReadonlySet<string> = new Set<AnswerFormat>([
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

/** The kind of an ordered display-only block a form page may carry alongside its
 * input fields: a `heading` or `body` text block, or an `image`. */
export type DisplayBlockKind = 'heading' | 'body' | 'image';

/** One ordered, display-only block on a form page (never an input): `text` carries a
 * heading/body's words; `src`/`alt` carry an image's source and alt text; an optional
 * `slot` names a data key whose current value the block shows in place of static `text`
 * (filled from the per-send data and/or a reaction's `display` update — a computed total
 * among them). Each optional field is `null` when the wire omitted it. */
export interface DisplayBlock {
  readonly kind: DisplayBlockKind;
  readonly text: string | null;
  readonly src: string | null;
  readonly alt: string | null;
  readonly slot: string | null;
}

/** One step of a stepped form: `title` heads the step and `fields` names the
 * top-level properties shown on it, in order. Across a form's pages every property
 * appears exactly once; absent pages means one page. `display` is the step's ordered
 * display-only blocks (empty when it carries none); `kind` marks a terminal `review`
 * step — a generic readback of entered values and the confirm footer, carrying no new
 * input fields — or an ordinary `input` step. */
export interface FormPage {
  readonly title: string;
  readonly fields: readonly string[];
  readonly display: readonly DisplayBlock[];
  readonly kind: 'input' | 'review';
}

/** When a form reacts while it is open: the plain, renderer-readable description of
 * WHEN the form rounds-trips to its reaction door. `fieldChanged` names the fields whose
 * change fires a reaction; `pageAdvanced` names the pages (by title) whose advance fires
 * one; `submitted` is true when the submission is checked by a reaction before it is
 * accepted; `choices` names the fields whose choice list a reaction may replace while the
 * form is open. `null` on a static (non-reacting) form. */
export interface FormReactions {
  readonly fieldChanged: readonly string[];
  readonly pageAdvanced: readonly string[];
  readonly submitted: boolean;
  readonly choices: readonly string[];
}

/** The form update a reaction door returns: `values` sets field values; `options`
 * replaces the per-send choice list of option-bearing fields; `errors` shows per-field
 * messages (keyed by field name); `display` fills display slots (keyed by slot name) — a
 * computed total among them. Each is empty when the reaction supplied none. */
export interface FormUpdate {
  readonly values: Readonly<Record<string, unknown>>;
  readonly options: Readonly<Record<string, readonly FormOptionData[]>>;
  readonly errors: Readonly<Record<string, string>>;
  readonly display: Readonly<Record<string, unknown>>;
}

/** Which of the three declared events a reaction round-trip reports. */
export type ReactionEventKind = 'field_changed' | 'page_advanced' | 'submitted';

/** The event a reaction round-trip reports to the door: `kind` is which event fired,
 * `field` names the changed field for a `field_changed`, `page` names the advanced-from
 * page (by title) for a `page_advanced`; a `submitted` carries neither. */
export interface ReactionEvent {
  readonly kind: ReactionEventKind;
  readonly field?: string;
  readonly page?: string;
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
      /** The form's reaction triggers when it reacts while open, or `null` for a
       * static form. The widget reads these to know which on-change / page-advance /
       * submit events to round-trip to the reaction door. */
      readonly reactions: FormReactions | null;
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
      /** Per-send prefill + choices, or `null` when the card carried none — the
       * same enrichment a `form` question rides, so an ask-less form opens filled in. */
      readonly formData: FormPrefill | null;
      /** The form's steps, or `null` for one page. */
      readonly pages: readonly FormPage[] | null;
      readonly ts: string;
    }
  | {
      readonly kind: 'message';
      readonly id: string;
      /** `in` is the visitor's own message, `out` the agent's. */
      readonly direction: 'in' | 'out';
      readonly text: string;
      readonly ts: string;
      /** The attachments the visitor sent with this message — the same shape a media
       * card carries, so the visitor's own upload renders through the media
       * components. Rides an inbound message only; `null` when the message carries
       * none. */
      readonly media: readonly MediaItem[] | null;
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
  | (QuestionBase & QuestionFacet)
  | {
      /** A stored entry the server could not render, replayed as a dedicated
       * placeholder so the visitor sees a persistent marker where the message was
       * rather than a silent gap. Ordered by its own `ts` like every other entry. */
      readonly kind: 'unavailable';
      readonly id: string;
      readonly ts: string;
    };

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
