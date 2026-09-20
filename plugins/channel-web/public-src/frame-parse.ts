/**
 * The raw-JSON → typed-field validators the reducer folds a frame through. Each one
 * turns an untrusted wire value into a typed field, `null` for a legitimately absent
 * one, or `undefined` for an off-shape one that taints the whole frame — so a
 * malformed frame is surfaced rather than rendered as a blank or unsafe entry.
 */
import type {
  AnswerFormat,
  CardOption,
  FormOptionData,
  FormPage,
  FormPrefill,
  LocationPoint,
  MediaItem,
  MediaKind,
  OptionSection,
  QuestionFacet,
  ReplyOption,
} from '@/transcript-model';

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function parseJson(data: string): Record<string, unknown> | null {
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
export function isTimestamp(value: unknown): value is string {
  return typeof value === 'string' && Number.isFinite(Date.parse(value));
}

export function optionsOf(raw: unknown): readonly string[] | null | undefined {
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
export function cardOptionsOf(raw: unknown): readonly CardOption[] | null | undefined {
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
export function sectionsOf(raw: unknown): readonly OptionSection[] | null | undefined {
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
export function headerOf(raw: unknown): MediaItem | null | undefined {
  if (raw === null || raw === undefined) return null;
  const item = mediaItemOf(raw);
  if (item === undefined || item.kind === 'link') return undefined;
  return item;
}

/** The card's footer line: absent (`null`), or a non-blank string. `undefined`
 * says malformed. */
export function footerOf(raw: unknown): string | null | undefined {
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
export function locationOf(raw: unknown): LocationPoint | null | undefined {
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
export function mediaOf(raw: unknown): readonly MediaItem[] | null | undefined {
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
export function clientMessageIdOf(raw: unknown): string | null | undefined {
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
export function formPrefillOf(raw: unknown): FormPrefill | null | undefined {
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
export function formPagesOf(raw: unknown): readonly FormPage[] | null | undefined {
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
export function facetOf(
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
