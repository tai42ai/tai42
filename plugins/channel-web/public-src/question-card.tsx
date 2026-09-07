/**
 * The inline widget for one `ask_user` question, per `answer_format`:
 * a text field, a Yes/No pair, one button per option, a schema-driven form, or —
 * for `external` — a link out to the question's own callback page, which is where
 * that format is answered.
 *
 * A question is LIVE only while it is neither answered nor past its deadline.
 * Answered is authoritative from the transcript (`chat.answered`), so a question
 * settled from another tab settles here too. The deadline is watched by a
 * SELF-RESCHEDULING timer that sleeps until the last minute and only then ticks
 * per second — a question hours out costs one timer, not one render a second. The
 * per-second text is for the eye alone; what is SPOKEN changes only at coarse
 * thresholds, so a minute of ticks cannot drown out the transcript's live region.
 *
 * Every settled state wears a badge, and a card that takes the controls away under
 * the visitor — by settling, or by sending the answer they just gave — keeps focus
 * on itself rather than dropping it to the document.
 */
import type { ReactElement } from 'react';
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Badge,
  Button,
  ExternalLinkButton,
  SchemaForm,
  Spinner,
  TextInput,
  defaultValueForSchema,
  validateAgainstSchema,
} from '@tai42/studio-sdk';
import type { JsonSchema, SchemaFormErrors } from '@tai42/studio-sdk';

import { MediaItems } from '@/media-card';
import type { ChatItem, FormOptionData, FormPage, FormPrefill } from '@/use-chat-stream';

/** The transcript item this card renders. */
export type QuestionItem = Extract<ChatItem, { kind: 'question' }>;

/** How long before the deadline the remaining time is spelled out. */
const COUNTDOWN_MS = 60_000;

export interface QuestionCardProps {
  readonly question: QuestionItem;
  /** Settled by a `chat.answered` frame — the durable answer state. */
  readonly answered: boolean;
  /** Sends one answer to the answer door. Rejects with the visitor-facing reason. */
  readonly onAnswer: (interactionId: string, answer: unknown) => Promise<void>;
  /** Called after an accepted answer so the page can put focus back where the
   * visitor was typing. */
  readonly onAnswered: () => void;
  /** The whole page is out of action (an ended session) — no answer can land. */
  readonly locked: boolean;
}

/** Whole seconds left, floored at zero. */
export function secondsLeft(timeoutAt: string, now: number): number {
  const deadline = Date.parse(timeoutAt);
  if (!Number.isFinite(deadline)) return 0;
  return Math.max(0, Math.ceil((deadline - now) / 1000));
}

/**
 * What a screen reader is told about the time left — COARSE, and empty outside the
 * countdown window. The visible countdown changes every second, and announcing
 * each tick would queue a minute of speech and drown out the transcript's own live
 * region; this string only changes as the remaining time crosses a threshold, and
 * an unchanged string re-renders without announcing again.
 */
export function countdownAnnouncement(remaining: number): string {
  if (remaining <= 0 || remaining >= 60) return '';
  if (remaining >= 30) return 'Less than a minute left to answer';
  if (remaining >= 10) return 'Less than 30 seconds left to answer';
  return 'Less than 10 seconds left to answer';
}

export function QuestionCard({
  question,
  answered,
  onAnswer,
  onAnswered,
  locked,
}: QuestionCardProps): ReactElement {
  const [now, setNow] = useState<number>(() => Date.now());
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const timeoutAt = question.timeoutAt;

  const remaining = secondsLeft(timeoutAt, now);
  const expired = !answered && remaining === 0;
  const live = !answered && !expired && !locked;

  // One timer, rescheduled after each tick: far from the deadline it sleeps until
  // the countdown window opens, inside it ticks every second, and at the deadline
  // it stops. A card that is not live shows no clock — answered, expired, or shut
  // out by an ended session — so it runs none.
  useEffect(() => {
    if (!live) return;
    const left = Date.parse(timeoutAt) - now;
    if (!Number.isFinite(left) || left <= 0) return;
    const delay = left > COUNTDOWN_MS ? left - COUNTDOWN_MS : 1000;
    const timer = setTimeout(() => setNow(Date.now()), delay);
    return () => clearTimeout(timer);
  }, [live, timeoutAt, now]);

  // Settling takes the controls away. If the visitor was inside them, focus moves
  // to the card — which now reads out the question and the badge that replaced
  // them — rather than falling to <body>. Focus is LATCHED as it moves: removing
  // the focused control fires no blur, and by the time this effect runs the
  // control is already gone from the document.
  const cardRef = useRef<HTMLDivElement | null>(null);
  const hadFocus = useRef(false);
  const wasLive = useRef(live);
  useEffect(() => {
    if (wasLive.current && !live && hadFocus.current) cardRef.current?.focus();
    wasLive.current = live;
  }, [live]);

  const submit = useCallback(
    (answer: unknown) => {
      // Sending disables the control the visitor is inside; the browser will not
      // leave focus on a disabled control, so it drops to the document and clears
      // the latch. Whether they were in the card is therefore read HERE, and a
      // refusal puts focus back on the card — where the alert now is — unless the
      // visitor has meanwhile put it somewhere of their own choosing.
      const held = hadFocus.current;
      setSending(true);
      setError(null);
      onAnswer(question.interactionId, answer).then(
        () => {
          setSending(false);
          setDraft('');
          onAnswered();
        },
        (err: unknown) => {
          setSending(false);
          setError(err instanceof Error ? err.message : String(err));
          if (held && document.activeElement === document.body) cardRef.current?.focus();
        },
      );
    },
    [onAnswer, onAnswered, question.interactionId],
  );

  return (
    <div className="tcw-row tcw-row--out tcw-row--start">
      <div
        className="tcw-question"
        ref={cardRef}
        tabIndex={-1}
        onFocus={() => {
          hadFocus.current = true;
        }}
        onBlur={() => {
          hadFocus.current = false;
        }}
      >
        <div className="tcw-question-head">
          <p className="tcw-text">{question.question}</p>
          {answered ? <Badge variant="success">Answered</Badge> : null}
          {expired ? <Badge variant="warning">Expired</Badge> : null}
          {!answered && !expired && locked ? <Badge variant="neutral">Session ended</Badge> : null}
        </div>
        {/* Display media rides between the prompt and its controls — the same
         * component a media card uses, so a question's images/links render
         * identically. Shown in every state (answered, expired, locked): the media
         * is context for the prompt, not an answer control. */}
        {question.media !== null ? <MediaItems media={question.media} /> : null}
        {live ? (
          <QuestionControls
            question={question}
            draft={draft}
            onDraft={setDraft}
            sending={sending}
            onSubmit={submit}
          />
        ) : null}
        {live && remaining <= COUNTDOWN_MS / 1000 ? (
          // Presentational: the per-second text is for the eye. The live region
          // below carries the spoken form, on its own coarser schedule.
          <p className="tcw-countdown" aria-hidden="true">
            {remaining === 1 ? '1 second left to answer' : `${remaining} seconds left to answer`}
          </p>
        ) : null}
        {live ? (
          <p className="tai-visually-hidden" role="status">
            {countdownAnnouncement(remaining)}
          </p>
        ) : null}
        {error !== null ? (
          <p className="tcw-question-error" role="alert">
            {error}
          </p>
        ) : null}
      </div>
    </div>
  );
}

interface ControlsProps {
  readonly question: QuestionItem;
  readonly draft: string;
  readonly onDraft: (value: string) => void;
  readonly sending: boolean;
  readonly onSubmit: (answer: unknown) => void;
}

/** The per-format controls. The switch has NO default arm: a new answer format
 * has to be given a widget here before it type-checks. */
function QuestionControls(props: ControlsProps): ReactElement {
  const { question } = props;
  switch (question.answerFormat) {
    case 'text':
      return <TextAnswer {...props} />;
    case 'confirm':
      return <ConfirmAnswer {...props} />;
    case 'select':
      return <SelectAnswer {...props} options={question.options ?? []} />;
    case 'form':
      // The answer schema reaches the page for this format alone, and the type
      // carries that: the stream admits a `form` question only when it carries an
      // object schema. A schema that is nonetheless not a usable object at render
      // is a LOUD notice, never a dropped control. The per-send `formData` (prefill +
      // choices) and `pages` (steps) ride the same form variant.
      return (
        <FormAnswer
          {...props}
          schema={question.schema}
          formData={question.formData}
          pages={question.pages}
          idPrefix={question.interactionId}
        />
      );
    case 'external':
      // The callback ticket reaches the page for this format alone, and the type
      // carries that: the stream admits an `external` question only when it
      // carries one. An unusable URL is neutralized into plain text by
      // `ExternalLinkButton`.
      return (
        <div className="tcw-question-actions">
          <ExternalLinkButton url={question.callbackUrl}>Open to answer</ExternalLinkButton>
        </div>
      );
  }
}

function TextAnswer({ question, draft, onDraft, sending, onSubmit }: ControlsProps): ReactElement {
  const blank = draft.trim() === '';
  return (
    <form
      className="tcw-question-actions"
      onSubmit={(event) => {
        event.preventDefault();
        if (!blank && !sending) onSubmit(draft.trim());
      }}
    >
      <TextInput
        value={draft}
        onChange={(event) => onDraft(event.target.value)}
        aria-label={question.question}
        placeholder="Type your answer"
        disabled={sending}
      />
      <Button type="submit" variant="primary" disabled={blank || sending}>
        {sending ? <Spinner label="Sending your answer" /> : 'Answer'}
      </Button>
    </form>
  );
}

function ConfirmAnswer({ sending, onSubmit }: ControlsProps): ReactElement {
  return (
    <div className="tcw-question-actions">
      <Button type="button" variant="primary" disabled={sending} onClick={() => onSubmit(true)}>
        Yes
      </Button>
      <Button type="button" variant="secondary" disabled={sending} onClick={() => onSubmit(false)}>
        No
      </Button>
      {sending ? <Spinner label="Sending your answer" /> : null}
    </div>
  );
}

function SelectAnswer({
  options,
  sending,
  onSubmit,
}: ControlsProps & { readonly options: readonly string[] }): ReactElement {
  return (
    <div className="tcw-question-actions">
      {options.map((option) => (
        <Button
          key={option}
          type="button"
          variant="secondary"
          disabled={sending}
          onClick={() => onSubmit(option)}
        >
          {option}
        </Button>
      ))}
      {sending ? <Spinner label="Sending your answer" /> : null}
    </div>
  );
}

/** A structurally-usable schema object, or not — the stream already rejects a
 * non-object schema, so this is the defensive last line: a schema that slips through
 * as a non-object renders the loud notice below rather than a dropped control. */
function isSchemaObject(value: unknown): value is JsonSchema {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function FormAnswer({
  sending,
  onSubmit,
  schema,
  formData,
  pages,
  idPrefix,
}: ControlsProps & {
  readonly schema: unknown;
  readonly formData: FormPrefill | null;
  readonly pages: readonly FormPage[] | null;
  readonly idPrefix: string;
}): ReactElement {
  if (!isSchemaObject(schema)) {
    return <MalformedNotice message="This form is malformed: its schema must be an object." />;
  }
  return (
    <SchemaFormAnswer
      schema={schema}
      formData={formData}
      pages={pages}
      sending={sending}
      onSubmit={onSubmit}
      idPrefix={idPrefix}
    />
  );
}

/** A plain JS object (a form's values bag), or not. */
function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** The top-level property names of an object schema, in declared order. */
function propertyOrder(schema: JsonSchema): readonly string[] {
  return isPlainObject(schema.properties) ? Object.keys(schema.properties) : [];
}

/** The pages to render: the per-send steps when set, else one page carrying every
 * top-level property in schema order (the whole form on one step). */
function resolvePages(schema: JsonSchema, pages: readonly FormPage[] | null): readonly FormPage[] {
  return pages !== null ? pages : [{ title: '', fields: propertyOrder(schema) }];
}

/** The initial form value: the schema's defaults overlaid with any prefilled
 * values so a known value is shown filled in from first render. */
function initialFormValue(schema: JsonSchema, formData: FormPrefill | null): unknown {
  const base = defaultValueForSchema(schema);
  const start = isPlainObject(base) ? { ...base } : {};
  return formData !== null ? { ...start, ...formData.values } : start;
}

/** A one-property object schema, so a single field renders through `SchemaForm`
 * with the same control and validation path it has in the whole form. */
function singleFieldSchema(schema: JsonSchema, field: string): JsonSchema {
  const prop = isPlainObject(schema.properties) ? schema.properties[field] : undefined;
  const required = (schema.required ?? []).includes(field) ? [field] : [];
  return { type: 'object', properties: prop !== undefined ? { [field]: prop } : {}, required };
}

/** The top-level field an error path belongs to (the segment before the first `.`
 * or `[`), so a nested error still maps to its page. */
function fieldOfPath(path: string): string {
  const cut = [path.indexOf('.'), path.indexOf('[')].filter((i) => i !== -1);
  return cut.length > 0 ? path.slice(0, Math.min(...cut)) : path;
}

/** Just the errors whose field is one of `fields` — the per-page slice used to
 * gate a Next without blocking on a later page's field. */
function errorsForFields(errors: SchemaFormErrors, fields: readonly string[]): SchemaFormErrors {
  const set = new Set(fields);
  return Object.fromEntries(Object.entries(errors).filter(([path]) => set.has(fieldOfPath(path))));
}

/** The first page carrying an errored field, or `-1` — where the visitor is sent
 * when a submit fails on a field that is not on the current step. */
function firstPageWithError(pages: readonly FormPage[], errors: SchemaFormErrors): number {
  const errored = new Set(Object.keys(errors).map(fieldOfPath));
  return pages.findIndex((page) => page.fields.some((field) => errored.has(field)));
}

function SchemaFormAnswer({
  schema,
  formData,
  pages,
  sending,
  onSubmit,
  idPrefix,
}: {
  readonly schema: JsonSchema;
  readonly formData: FormPrefill | null;
  readonly pages: readonly FormPage[] | null;
  readonly sending: boolean;
  readonly onSubmit: (answer: unknown) => void;
  readonly idPrefix: string;
}): ReactElement {
  const resolvedPages = resolvePages(schema, pages);
  const stepped = resolvedPages.length > 1;
  const [value, setValue] = useState<unknown>(() => initialFormValue(schema, formData));
  const [errors, setErrors] = useState<SchemaFormErrors>({});
  const [pageIndex, setPageIndex] = useState(0);
  const options = formData?.options ?? {};

  // Moving between steps swaps the visible controls under the visitor. Focus follows
  // to the new step's first control (its heading as a fallback when the step has none),
  // so a keyboard or screen-reader user lands on the step they were sent to rather than
  // being left on the now-hidden control's place. Armed only by an explicit Next/Back —
  // the initial render must not steal focus into the form.
  const pageRef = useRef<HTMLDivElement | null>(null);
  const headingRef = useRef<HTMLParagraphElement | null>(null);
  const moveFocusOnStep = useRef(false);
  useEffect(() => {
    if (!moveFocusOnStep.current) return;
    moveFocusOnStep.current = false;
    const first = pageRef.current?.querySelector<HTMLElement>(
      'input, select, textarea, button, [href], [tabindex]:not([tabindex="-1"])',
    );
    if (first !== null && first !== undefined) first.focus();
    else headingRef.current?.focus();
  }, [pageIndex]);
  // `resolvePages` always yields at least one page; the fallback only satisfies the
  // index type and never renders.
  const page = resolvedPages[Math.min(pageIndex, resolvedPages.length - 1)] ?? {
    title: '',
    fields: [],
  };
  const isLast = pageIndex >= resolvedPages.length - 1;

  // The whole schema is validated on submit and the per-path errors fed back to
  // the controls; an invalid form is not sent — the answer is one-shot, so a bad
  // object cannot be recalled once the callback door records it. When a step form's
  // submit fails on a field the visitor cannot see, they are moved to its page.
  const submit = (): void => {
    const found = validateAgainstSchema(schema, value);
    setErrors(found);
    if (Object.keys(found).length === 0) {
      onSubmit(value);
      return;
    }
    if (stepped) {
      const target = firstPageWithError(resolvedPages, found);
      if (target !== -1) setPageIndex(target);
    }
  };

  // Advancing validates only THIS step's fields, so a later page's still-empty
  // required field never blocks moving forward.
  const next = (): void => {
    const found = errorsForFields(validateAgainstSchema(schema, value), page.fields);
    setErrors(found);
    if (Object.keys(found).length === 0) {
      moveFocusOnStep.current = true;
      setPageIndex((index) => index + 1);
    }
  };

  const back = (): void => {
    setErrors({});
    moveFocusOnStep.current = true;
    setPageIndex((index) => Math.max(index - 1, 0));
  };

  const renderField = (field: string): ReactElement => {
    const choices = options[field];
    if (choices !== undefined) {
      const prop = isPlainObject(schema.properties) ? schema.properties[field] : undefined;
      const label = isPlainObject(prop) && typeof prop.title === 'string' ? prop.title : field;
      return (
        <OptionSelect
          key={field}
          id={`${idPrefix}-${field}`}
          label={label}
          options={choices}
          value={isPlainObject(value) ? value[field] : undefined}
          error={errors[field]}
          onChange={(next) =>
            setValue(isPlainObject(value) ? { ...value, [field]: next } : { [field]: next })
          }
        />
      );
    }
    if (!isPlainObject(schema.properties) || schema.properties[field] === undefined) {
      return (
        <MalformedNotice
          key={field}
          message={`This form is malformed: it has no field "${field}".`}
        />
      );
    }
    return (
      <SchemaForm
        key={field}
        schema={singleFieldSchema(schema, field)}
        value={value}
        onChange={setValue}
        errors={errors}
        idPrefix={`${idPrefix}-${field}`}
      />
    );
  };

  return (
    <div className="tcw-question-actions tcw-question-form">
      {stepped ? (
        <p className="tcw-form-progress" role="status" ref={headingRef} tabIndex={-1}>
          {`Step ${pageIndex + 1} of ${resolvedPages.length} · ${page.title}`}
        </p>
      ) : null}
      <div className="tcw-form-page" ref={pageRef}>
        {page.fields.map(renderField)}
      </div>
      <div className="tcw-form-nav">
        {stepped && pageIndex > 0 ? (
          <Button type="button" variant="secondary" disabled={sending} onClick={back}>
            Back
          </Button>
        ) : null}
        {stepped && !isLast ? (
          <Button type="button" variant="primary" disabled={sending} onClick={next}>
            Next
          </Button>
        ) : (
          <Button type="button" variant="primary" disabled={sending} onClick={submit}>
            {sending ? <Spinner label="Sending your answer" /> : stepped ? 'Submit' : 'Answer'}
          </Button>
        )}
      </div>
    </div>
  );
}

/** A per-send option field: a native `<select>` whose options show their label
 * (falling back to the value) and post their value. Native so it is keyboard
 * reachable and themed by the widget's tokens; a leading placeholder keeps it
 * controlled and unselected until the visitor (or a prefill) picks a value. */
function OptionSelect({
  id,
  label,
  options,
  value,
  error,
  onChange,
}: {
  readonly id: string;
  readonly label: string;
  readonly options: readonly FormOptionData[];
  readonly value: unknown;
  readonly error: string | undefined;
  readonly onChange: (value: string) => void;
}): ReactElement {
  const selected = typeof value === 'string' ? value : '';
  return (
    <div className="tcw-form-field">
      <label className="tcw-form-field-label" htmlFor={id}>
        {label}
      </label>
      <select
        id={id}
        className="tcw-select"
        value={selected}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="" disabled>
          Choose an option
        </option>
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label ?? option.value}
          </option>
        ))}
      </select>
      {error !== undefined ? (
        <span className="tcw-form-field-error" role="alert">
          {error}
        </span>
      ) : null}
    </div>
  );
}

/** A LOUD inline notice for a structurally-malformed schema: a visible `alert`
 * rather than an empty or silently-dropped control. */
function MalformedNotice({ message }: { readonly message: string }): ReactElement {
  return (
    <div className="tcw-question-malformed" role="alert">
      <Badge variant="danger">Malformed</Badge>
      <span>{message}</span>
    </div>
  );
}
