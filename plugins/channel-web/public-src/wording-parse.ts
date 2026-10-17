/**
 * The raw-JSON → typed-`Wording` validator the reducer folds a send's catalogue of
 * platform-drawn texts through. The web surface draws the `form`, `choice`, `answer`
 * and `status` chapters, which are parsed down to the members it reads; the catalogue's
 * message-line (`lines`) and answer-page (`answer_page`) chapters are valid for a send
 * but draw nothing on the web surface, so they are accepted off the wire and carried no
 * further. An unknown chapter, an unknown member, a blank value, or a
 * `status.countdown.seconds_left` missing its `{seconds}` token taints the whole frame,
 * so a caller's word is never rendered blank or half-read.
 */
import { isRecord } from '@/frame-parse';
import type {
  AnswerWording,
  ChoiceWording,
  CountdownWording,
  FieldWording,
  FormWording,
  StatusWording,
  Wording,
} from '@/transcript-model';

/** One optional wording member: absent → `null` (the surface shows its own word); a
 * non-blank string → the caller's word; anything else (a blank string, a non-string,
 * an explicit null) → `undefined` (malformed). The wire omits an unset member, so a
 * present one is always the caller's own words. */
function wordingMemberOf(obj: Record<string, unknown>, key: string): string | null | undefined {
  const value = obj[key];
  if (value === undefined) return null;
  if (typeof value !== 'string' || value.trim() === '') return undefined;
  return value;
}

/** Every key of `obj` is one of `allowed` — `wording` is a closed vocabulary, so a key
 * outside the catalogue's own member names is a frame this page will not render. */
function onlyKnownKeys(obj: Record<string, unknown>, allowed: ReadonlySet<string>): boolean {
  return Object.keys(obj).every((key) => allowed.has(key));
}

/** Validate every named string member of `obj` as non-blank; `false` on any off-shape
 * value. Used for the members a surface accepts but does not draw, so a corrupt value
 * still taints the frame. */
function stringMembersValid(obj: Record<string, unknown>, keys: readonly string[]): boolean {
  return keys.every((key) => wordingMemberOf(obj, key) !== undefined);
}

const FORM_WORDING_KEYS: ReadonlySet<string> = new Set([
  'title',
  'open',
  'cancel',
  'back',
  'submit',
  'review_empty',
  'fields',
]);
const FORM_WORDING_STRING_KEYS: readonly string[] = [
  'title',
  'open',
  'cancel',
  'back',
  'submit',
  'review_empty',
];

const FIELD_WORDING_KEYS: ReadonlySet<string> = new Set(['choice_label', 'invalid']);

/** One form field's wording: a non-blank `choice_label` and/or `invalid`, and no other key. */
function fieldWordingOf(raw: unknown): FieldWording | undefined {
  if (!isRecord(raw) || !onlyKnownKeys(raw, FIELD_WORDING_KEYS)) return undefined;
  const choiceLabel = wordingMemberOf(raw, 'choice_label');
  const invalid = wordingMemberOf(raw, 'invalid');
  if (choiceLabel === undefined || invalid === undefined) return undefined;
  return { choiceLabel, invalid };
}

/** The per-field wording map: absent → `{}`; an object whose every value is a valid
 * field wording → the parsed map; `undefined` (malformed) otherwise. */
function fieldsWordingOf(raw: unknown): Readonly<Record<string, FieldWording>> | undefined {
  if (raw === undefined) return {};
  if (!isRecord(raw)) return undefined;
  const parsed: Record<string, FieldWording> = {};
  for (const [name, value] of Object.entries(raw)) {
    const field = fieldWordingOf(value);
    if (field === undefined) return undefined;
    parsed[name] = field;
  }
  return parsed;
}

/** The `form` chapter: every string member validated (non-blank), the per-field map
 * parsed; the web draws `submit`, `back` and the field placeholders, so only those are
 * carried (the inline web form draws no `title`/`open`/`cancel`/`review_empty`). */
function formWordingOf(raw: unknown): FormWording | undefined {
  if (!isRecord(raw) || !onlyKnownKeys(raw, FORM_WORDING_KEYS)) return undefined;
  if (!stringMembersValid(raw, FORM_WORDING_STRING_KEYS)) return undefined;
  const fields = fieldsWordingOf(raw.fields);
  if (fields === undefined) return undefined;
  return {
    submit: wordingMemberOf(raw, 'submit') as string | null,
    back: wordingMemberOf(raw, 'back') as string | null,
    fields,
  };
}

const CHOICE_WORDING_KEYS: ReadonlySet<string> = new Set(['label', 'reply_hint', 'group_label']);

/** The `choice` chapter: every string member validated; the web draws `group_label`. */
function choiceWordingOf(raw: unknown): ChoiceWording | undefined {
  if (!isRecord(raw) || !onlyKnownKeys(raw, CHOICE_WORDING_KEYS)) return undefined;
  if (!stringMembersValid(raw, ['label', 'reply_hint'])) return undefined;
  const groupLabel = wordingMemberOf(raw, 'group_label');
  if (groupLabel === undefined) return undefined;
  return { groupLabel };
}

const ANSWER_WORDING_KEYS: ReadonlySet<string> = new Set([
  'open',
  'confirm',
  'decline',
  'submit',
  'reply_hint',
  'rejected',
  'rejected_final',
]);
const ANSWER_DRAWN_KEYS: readonly string[] = [
  'open',
  'confirm',
  'decline',
  'submit',
  'reply_hint',
  'rejected',
];

/** The `answer` chapter: every string member validated; the web draws them all except
 * `rejected_final` (a closed-ask notice the web card has no surface for). */
function answerWordingOf(raw: unknown): AnswerWording | undefined {
  if (!isRecord(raw) || !onlyKnownKeys(raw, ANSWER_WORDING_KEYS)) return undefined;
  if (!stringMembersValid(raw, [...ANSWER_DRAWN_KEYS, 'rejected_final'])) return undefined;
  return {
    open: wordingMemberOf(raw, 'open') as string | null,
    confirm: wordingMemberOf(raw, 'confirm') as string | null,
    decline: wordingMemberOf(raw, 'decline') as string | null,
    submit: wordingMemberOf(raw, 'submit') as string | null,
    replyHint: wordingMemberOf(raw, 'reply_hint') as string | null,
    rejected: wordingMemberOf(raw, 'rejected') as string | null,
  };
}

const COUNTDOWN_WORDING_KEYS: ReadonlySet<string> = new Set([
  'seconds_left',
  'one_second_left',
  'under_minute',
  'under_half_minute',
  'under_ten_seconds',
]);
const COUNTDOWN_PLAIN_KEYS: readonly string[] = [
  'one_second_left',
  'under_minute',
  'under_half_minute',
  'under_ten_seconds',
];

/** The `status.countdown` sub-chapter: the visible plural line must carry `{seconds}`;
 * every other member is a non-blank string. */
function countdownWordingOf(raw: unknown): CountdownWording | undefined {
  if (!isRecord(raw) || !onlyKnownKeys(raw, COUNTDOWN_WORDING_KEYS)) return undefined;
  if (!stringMembersValid(raw, COUNTDOWN_PLAIN_KEYS)) return undefined;
  const secondsLeft = wordingMemberOf(raw, 'seconds_left');
  if (secondsLeft === undefined || (secondsLeft !== null && !secondsLeft.includes('{seconds}')))
    return undefined;
  return {
    secondsLeft,
    oneSecondLeft: wordingMemberOf(raw, 'one_second_left') as string | null,
    underMinute: wordingMemberOf(raw, 'under_minute') as string | null,
    underHalfMinute: wordingMemberOf(raw, 'under_half_minute') as string | null,
    underTenSeconds: wordingMemberOf(raw, 'under_ten_seconds') as string | null,
  };
}

const STATUS_WORDING_KEYS: ReadonlySet<string> = new Set([
  'sending',
  'answered',
  'expired',
  'closed',
  'sent',
  'unavailable',
  'countdown',
]);
const STATUS_STRING_KEYS: readonly string[] = [
  'sending',
  'answered',
  'expired',
  'closed',
  'sent',
  'unavailable',
];

/** The `status` chapter: the in-flight label, the settled badges, the ask-less form's
 * sent/unavailable lines, and the live-countdown words. */
function statusWordingOf(raw: unknown): StatusWording | undefined {
  if (!isRecord(raw) || !onlyKnownKeys(raw, STATUS_WORDING_KEYS)) return undefined;
  if (!stringMembersValid(raw, STATUS_STRING_KEYS)) return undefined;
  const countdown = raw.countdown === undefined ? null : countdownWordingOf(raw.countdown);
  if (countdown === undefined) return undefined;
  return {
    sending: wordingMemberOf(raw, 'sending') as string | null,
    answered: wordingMemberOf(raw, 'answered') as string | null,
    expired: wordingMemberOf(raw, 'expired') as string | null,
    closed: wordingMemberOf(raw, 'closed') as string | null,
    sent: wordingMemberOf(raw, 'sent') as string | null,
    unavailable: wordingMemberOf(raw, 'unavailable') as string | null,
    countdown,
  };
}

const WORDING_CHAPTERS: ReadonlySet<string> = new Set([
  'form',
  'choice',
  'answer',
  'lines',
  'answer_page',
  'status',
]);

/** A chapter the web surface draws: absent → `null`; present → the parser's result
 * (which may itself be `undefined` for a malformed chapter). */
function chapterOf<T>(
  raw: unknown,
  parse: (value: unknown) => T | undefined,
): T | null | undefined {
  return raw === undefined ? null : parse(raw);
}

/** The inert chapters (`lines`, `answer_page`) draw nothing on the web surface, but a
 * present one must still be a record — a corrupt value taints the frame. */
function inertChaptersWellFormed(raw: Record<string, unknown>): boolean {
  return (
    (raw.lines === undefined || isRecord(raw.lines)) &&
    (raw.answer_page === undefined || isRecord(raw.answer_page))
  );
}

/** The send's wording catalogue: absent (`null`), or the chapters the web surface draws
 * (`form`, `choice`, `answer`, `status`). `undefined` says malformed. */
export function wordingOf(raw: unknown): Wording | null | undefined {
  if (raw === null || raw === undefined) return null;
  if (!isRecord(raw) || !onlyKnownKeys(raw, WORDING_CHAPTERS) || !inertChaptersWellFormed(raw))
    return undefined;
  const form = chapterOf(raw.form, formWordingOf);
  const choice = chapterOf(raw.choice, choiceWordingOf);
  const answer = chapterOf(raw.answer, answerWordingOf);
  const status = chapterOf(raw.status, statusWordingOf);
  if (form === undefined || choice === undefined || answer === undefined || status === undefined)
    return undefined;
  return { form, choice, answer, status };
}
