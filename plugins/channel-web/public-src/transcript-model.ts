/**
 * The transcript data model: the typed shapes one visitor's conversation folds
 * into, and the empty model a fresh subscription starts from.
 *
 * The wire carries six events. `chat.message`, `chat.question`, `chat.media` and
 * `chat.form` are transcript ENTRIES and become items in arrival order;
 * `chat.answered` is not an entry of its own — it settles the question it names,
 * so it only records an interaction id; `chat.backlog_done` marks the replayed
 * backlog as complete.
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
