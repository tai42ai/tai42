/**
 * The paged, prefill-aware form the ask path and the ask-less form card share: the
 * schema's defaults overlaid with `formData.values`, `formData.options` replacing a
 * property's choices, and `pages` rendered as steps — each step drawing its ordered
 * display blocks and, for a review step, a generic readback of entered values. The
 * terminal button's copy is the caller's — a question ANSWERS/Submits, an ask-less card
 * SENDS — so the one implementation serves both without new copy of its own.
 *
 * The value/step state and the change / advance / submit handlers (including the reaction
 * round-trip of a reacting form) live in `useReactingForm`; this module is the
 * presentation: the per-field controls and the step surface they render on.
 */
import type { JsonSchema, SchemaFormErrors } from '@tai42/studio-sdk';
import { Badge, Button, isFieldVisible, SchemaForm, Spinner } from '@tai42/studio-sdk';
import type { ReactElement, RefObject } from 'react';

import { DisplayBlocks, ReviewReadback } from '@/form-display';
import { isPlainObject, singleFieldSchema, toSdkFieldWording } from '@/schema-form-helpers';
import type { FieldWording, FormOptionData, FormPage } from '@/transcript-model';
import type { ReactingFormInput, ReactionError } from '@/use-reacting-form';
import { useReactingForm } from '@/use-reacting-form';
import { fillToken } from '@/words';

/** A LOUD inline notice for a structurally-malformed schema: a visible `alert`
 * rather than an empty or silently-dropped control. */
export function MalformedNotice({ message }: { readonly message: string }): ReactElement {
  return (
    <div className="tcw-question-malformed" role="alert">
      <Badge variant="danger">Malformed</Badge>
      <span>{message}</span>
    </div>
  );
}

/** The text shown for one option: its label (falling back to the value) and, when it
 * carries a second line, that line folded on after an em dash — a native `<option>` has
 * no slot for a second line, so the content rides the option text rather than being lost. */
function optionText(option: FormOptionData): string {
  const label = option.label ?? option.value;
  const description =
    typeof option.description === 'string' && option.description.trim() !== ''
      ? option.description
      : null;
  return description === null ? label : `${label} — ${description}`;
}

/** A per-send option field: a native `<select>` whose options show their label
 * (falling back to the value) and post their value. Native so it is keyboard
 * reachable and themed by the widget's tokens; a leading placeholder keeps it
 * controlled and unselected until the visitor (or a prefill) picks a value. A field
 * `hint` (the schema property's second line) draws under the label, matching the SDK's
 * own field hint; an option's second line folds onto its option text. */
function OptionSelect({
  id,
  label,
  placeholder,
  hint,
  options,
  value,
  error,
  onChange,
}: {
  readonly id: string;
  readonly label: string;
  readonly placeholder: string;
  readonly hint: string | undefined;
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
      {hint !== undefined ? <p className="tai-field-hint">{hint}</p> : null}
      <select
        id={id}
        className="tcw-select"
        value={selected}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="" disabled>
          {placeholder}
        </option>
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {optionText(option)}
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

/** One field of the current step: nothing when a `visibleWhen` predicate hides it, a
 * per-send option select when a choice list is supplied, a loud notice for a field the
 * schema does not declare, else the schema's own single-field control (which the SDK
 * renders as multi-select / radio / native date / conditional as the property declares). */
function FormField({
  field,
  schema,
  options,
  value,
  errors,
  idPrefix,
  fieldWording,
  fieldPlaceholder,
  onChange,
}: {
  readonly field: string;
  readonly schema: JsonSchema;
  readonly options: Readonly<Record<string, readonly FormOptionData[]>>;
  readonly value: unknown;
  readonly errors: SchemaFormErrors;
  readonly idPrefix: string;
  readonly fieldWording: Readonly<Record<string, FieldWording>>;
  readonly fieldPlaceholder: string;
  readonly onChange: (value: unknown) => void;
}): ReactElement | null {
  const prop = isPlainObject(schema.properties) ? schema.properties[field] : undefined;
  // A field hidden by its `visibleWhen` predicate (evaluated against the whole value) is
  // not shown; the answer facet drops its value from the answer the consumer receives.
  if (prop !== undefined && !isFieldVisible(prop, isPlainObject(value) ? value : {})) return null;
  const choices = options[field];
  if (choices !== undefined) {
    const label = typeof prop?.title === 'string' ? prop.title : field;
    const hint =
      typeof prop?.description === 'string' && prop.description.trim() !== ''
        ? prop.description
        : undefined;
    return (
      <OptionSelect
        id={`${idPrefix}-${field}`}
        label={label}
        placeholder={fieldWording[field]?.choiceLabel ?? fieldPlaceholder}
        hint={hint}
        options={choices}
        value={isPlainObject(value) ? value[field] : undefined}
        error={errors[field]}
        onChange={(next) =>
          onChange(isPlainObject(value) ? { ...value, [field]: next } : { [field]: next })
        }
      />
    );
  }
  if (prop === undefined) {
    return <MalformedNotice message={`This form is malformed: it has no field "${field}".`} />;
  }
  return (
    <SchemaForm
      schema={singleFieldSchema(schema, field)}
      value={value}
      onChange={onChange}
      errors={errors}
      idPrefix={`${idPrefix}-${field}`}
      fieldWording={toSdkFieldWording(fieldWording)}
    />
  );
}

/** The step controls: Back once off the first page, Next until the last, and the
 * terminal Submit/Answer button on the last (or only) page. The current page's
 * `buttonLabel` overrides the primary button's text when the sender set one; absent, the
 * primary button keeps its generic word ('Next' off the last step, the terminal
 * Submit/Answer copy on it). */
function FormNav({
  stepped,
  pageIndex,
  isLast,
  sending,
  sendingLabel,
  submitLabel,
  steppedSubmitLabel,
  backLabel,
  buttonLabel,
  onBack,
  onNext,
  onSubmit,
}: {
  readonly stepped: boolean;
  readonly pageIndex: number;
  readonly isLast: boolean;
  readonly sending: boolean;
  readonly sendingLabel: string;
  readonly submitLabel: string;
  readonly steppedSubmitLabel: string;
  readonly backLabel: string;
  readonly buttonLabel: string | null;
  readonly onBack: () => void;
  readonly onNext: () => void;
  readonly onSubmit: () => void;
}): ReactElement {
  const terminalLabel = stepped ? steppedSubmitLabel : submitLabel;
  return (
    <div className="tcw-form-nav">
      {stepped && pageIndex > 0 ? (
        <Button type="button" variant="secondary" disabled={sending} onClick={onBack}>
          {backLabel}
        </Button>
      ) : null}
      {stepped && !isLast ? (
        <Button type="button" variant="primary" disabled={sending} onClick={onNext}>
          {buttonLabel ?? 'Next'}
        </Button>
      ) : (
        <Button type="button" variant="primary" disabled={sending} onClick={onSubmit}>
          {sending ? <Spinner label={sendingLabel} /> : (buttonLabel ?? terminalLabel)}
        </Button>
      )}
    </div>
  );
}

/** The rendered form surface for the current step: the step marker, a loud reaction-error
 * banner, the page's display blocks, its body (input fields or a review readback), and the
 * Back / Next / Submit navigation. The stateful `useReactingForm` owns the value/handlers. */
/** A stepped form's progress words: the caller's line with `{step}` and `{steps}` filled,
 * else the widget's own. */
function stepProgress(line: string | null, step: number, steps: number): string {
  if (line === null) return `Step ${step} of ${steps}`;
  return fillToken(fillToken(line, 'step', String(step)), 'steps', String(steps));
}

/** The line drawn when a mid-form reaction fails: the caller's line with the react
 * door's own reason filled into `{reason}` (nothing when the door gave none, never the
 * widget's English), else the widget's own line with the failure's message. */
function updateFailed(line: string | null, error: ReactionError): string {
  if (line === null) return `The form could not update: ${error.message}`;
  return fillToken(line, 'reason', error.reason ?? '').trim();
}

function FormSurface({
  schema,
  page,
  pageIndex,
  pageCount,
  stepped,
  isLast,
  options,
  value,
  errors,
  slots,
  idPrefix,
  reactionError,
  busy,
  submitLabel,
  steppedSubmitLabel,
  sendingLabel,
  backLabel,
  fieldWording,
  fieldPlaceholder,
  updateFailedLine,
  stepProgressLine,
  pageRef,
  headingRef,
  onChange,
  onBack,
  onNext,
  onSubmit,
}: {
  readonly schema: JsonSchema;
  readonly page: FormPage;
  readonly pageIndex: number;
  readonly pageCount: number;
  readonly stepped: boolean;
  readonly isLast: boolean;
  readonly options: Readonly<Record<string, readonly FormOptionData[]>>;
  readonly value: unknown;
  readonly errors: SchemaFormErrors;
  readonly slots: Readonly<Record<string, unknown>>;
  readonly idPrefix: string;
  readonly reactionError: ReactionError | null;
  readonly busy: boolean;
  readonly submitLabel: string;
  readonly steppedSubmitLabel: string;
  readonly sendingLabel: string;
  readonly backLabel: string;
  readonly fieldWording: Readonly<Record<string, FieldWording>>;
  readonly fieldPlaceholder: string;
  readonly updateFailedLine: string | null;
  readonly stepProgressLine: string | null;
  readonly pageRef: RefObject<HTMLDivElement | null>;
  readonly headingRef: RefObject<HTMLParagraphElement | null>;
  readonly onChange: (value: unknown) => void;
  readonly onBack: () => void;
  readonly onNext: () => void;
  readonly onSubmit: () => void;
}): ReactElement {
  const objectValue = isPlainObject(value) ? value : {};
  return (
    <div className="tcw-question-actions tcw-question-form">
      {stepped ? (
        <p className="tcw-form-progress" role="status" ref={headingRef} tabIndex={-1}>
          {`${stepProgress(stepProgressLine, pageIndex + 1, pageCount)} · ${page.title}`}
        </p>
      ) : null}
      {reactionError !== null ? (
        <p className="tcw-form-reaction-error" role="alert" data-testid="form-reaction-error">
          {updateFailed(updateFailedLine, reactionError)}
        </p>
      ) : null}
      <div className="tcw-form-page" ref={pageRef}>
        <DisplayBlocks blocks={page.display} slots={slots} />
        {page.kind === 'review' ? (
          <ReviewReadback schema={schema} value={objectValue} fieldWording={fieldWording} />
        ) : (
          page.fields.map((field) => (
            <FormField
              key={field}
              field={field}
              schema={schema}
              options={options}
              value={value}
              errors={errors}
              idPrefix={idPrefix}
              fieldWording={fieldWording}
              fieldPlaceholder={fieldPlaceholder}
              onChange={onChange}
            />
          ))
        )}
      </div>
      <FormNav
        stepped={stepped}
        pageIndex={pageIndex}
        isLast={isLast}
        sending={busy}
        sendingLabel={sendingLabel}
        submitLabel={submitLabel}
        steppedSubmitLabel={steppedSubmitLabel}
        backLabel={backLabel}
        buttonLabel={page.buttonLabel}
        onBack={onBack}
        onNext={onNext}
        onSubmit={onSubmit}
      />
    </div>
  );
}

export function SchemaFormAnswer(
  props: ReactingFormInput & {
    readonly idPrefix: string;
    /** The terminal button on a single-page form. */
    readonly submitLabel: string;
    /** The terminal button on the last step of a paged form. */
    readonly steppedSubmitLabel: string;
    /** The spinner label shown while a submission is in flight. */
    readonly sendingLabel: string;
    /** The button that returns to the previous step of a stepped form. */
    readonly backLabel: string;
    /** The placeholder of a per-send option field when the caller set none. */
    readonly fieldPlaceholder: string;
    /** The caller's could-not-update line (`{reason}`), or `null` for the widget's own. */
    readonly updateFailedLine: string | null;
    /** The caller's step-progress line (`{step}`, `{steps}`), or `null` for the widget's own. */
    readonly stepProgressLine: string | null;
  },
): ReactElement {
  const {
    schema,
    idPrefix,
    submitLabel,
    steppedSubmitLabel,
    sendingLabel,
    backLabel,
    fieldWording,
    fieldPlaceholder,
    updateFailedLine,
    stepProgressLine,
  } = props;
  const form = useReactingForm(props);
  return (
    <FormSurface
      schema={schema}
      page={form.page}
      pageIndex={form.pageIndex}
      pageCount={form.pageCount}
      stepped={form.stepped}
      isLast={form.isLast}
      options={form.options}
      value={form.value}
      errors={form.errors}
      slots={form.slots}
      idPrefix={idPrefix}
      reactionError={form.reactionError}
      busy={form.busy}
      submitLabel={submitLabel}
      steppedSubmitLabel={steppedSubmitLabel}
      sendingLabel={sendingLabel}
      backLabel={backLabel}
      fieldWording={fieldWording}
      fieldPlaceholder={fieldPlaceholder}
      updateFailedLine={updateFailedLine}
      stepProgressLine={stepProgressLine}
      pageRef={form.pageRef}
      headingRef={form.headingRef}
      onChange={form.onChange}
      onBack={form.onBack}
      onNext={form.onNext}
      onSubmit={form.onSubmit}
    />
  );
}
