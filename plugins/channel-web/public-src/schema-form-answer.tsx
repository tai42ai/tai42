/**
 * The paged, prefill-aware form the ask path and the ask-less form card share: the
 * schema's defaults overlaid with `formData.values`, `formData.options` replacing a
 * property's choices, and `pages` rendered as steps. The terminal button's copy is
 * the caller's — a question ANSWERS/Submits, an ask-less card SENDS — so the one
 * implementation serves both without new copy of its own.
 */
import type { JsonSchema, SchemaFormErrors } from '@tai42/studio-sdk';
import {
  Badge,
  Button,
  defaultValueForSchema,
  SchemaForm,
  Spinner,
  validateAgainstSchema,
} from '@tai42/studio-sdk';
import type { ReactElement, RefObject } from 'react';
import { useEffect, useRef, useState } from 'react';

import type { FormOptionData, FormPage, FormPrefill } from '@/transcript-model';

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
  return pages ?? [{ title: '', fields: propertyOrder(schema) }];
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

/** One field of the current step: a per-send option select when a choice list is
 * supplied, a loud notice for a field the schema does not declare, else the schema's
 * own single-field control. */
function FormField({
  field,
  schema,
  options,
  value,
  errors,
  idPrefix,
  onChange,
}: {
  readonly field: string;
  readonly schema: JsonSchema;
  readonly options: Readonly<Record<string, readonly FormOptionData[]>>;
  readonly value: unknown;
  readonly errors: SchemaFormErrors;
  readonly idPrefix: string;
  readonly onChange: (value: unknown) => void;
}): ReactElement {
  const choices = options[field];
  if (choices !== undefined) {
    const prop = isPlainObject(schema.properties) ? schema.properties[field] : undefined;
    const label = isPlainObject(prop) && typeof prop.title === 'string' ? prop.title : field;
    return (
      <OptionSelect
        id={`${idPrefix}-${field}`}
        label={label}
        options={choices}
        value={isPlainObject(value) ? value[field] : undefined}
        error={errors[field]}
        onChange={(next) =>
          onChange(isPlainObject(value) ? { ...value, [field]: next } : { [field]: next })
        }
      />
    );
  }
  if (!isPlainObject(schema.properties) || schema.properties[field] === undefined) {
    return <MalformedNotice message={`This form is malformed: it has no field "${field}".`} />;
  }
  return (
    <SchemaForm
      schema={singleFieldSchema(schema, field)}
      value={value}
      onChange={onChange}
      errors={errors}
      idPrefix={`${idPrefix}-${field}`}
    />
  );
}

/** The step controls: Back once off the first page, Next until the last, and the
 * terminal Submit/Answer button on the last (or only) page. */
function FormNav({
  stepped,
  pageIndex,
  isLast,
  sending,
  sendingLabel,
  submitLabel,
  steppedSubmitLabel,
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
  readonly onBack: () => void;
  readonly onNext: () => void;
  readonly onSubmit: () => void;
}): ReactElement {
  return (
    <div className="tcw-form-nav">
      {stepped && pageIndex > 0 ? (
        <Button type="button" variant="secondary" disabled={sending} onClick={onBack}>
          Back
        </Button>
      ) : null}
      {stepped && !isLast ? (
        <Button type="button" variant="primary" disabled={sending} onClick={onNext}>
          Next
        </Button>
      ) : (
        <Button type="button" variant="primary" disabled={sending} onClick={onSubmit}>
          {sending ? <Spinner label={sendingLabel} /> : stepped ? steppedSubmitLabel : submitLabel}
        </Button>
      )}
    </div>
  );
}

/** Move focus to the shown step's first control (its heading as a fallback) after a
 * step change, but only when an explicit Next/Back armed it — the initial render
 * must not steal focus into the form. */
function useStepFocus(pageIndex: number): {
  pageRef: RefObject<HTMLDivElement | null>;
  headingRef: RefObject<HTMLParagraphElement | null>;
  armStepMove: () => void;
} {
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
  return {
    pageRef,
    headingRef,
    armStepMove: () => {
      moveFocusOnStep.current = true;
    },
  };
}

export function SchemaFormAnswer({
  schema,
  formData,
  pages,
  sending,
  onSubmit,
  idPrefix,
  submitLabel,
  steppedSubmitLabel,
  sendingLabel,
}: {
  readonly schema: JsonSchema;
  readonly formData: FormPrefill | null;
  readonly pages: readonly FormPage[] | null;
  readonly sending: boolean;
  readonly onSubmit: (answer: unknown) => void;
  readonly idPrefix: string;
  /** The terminal button on a single-page form. */
  readonly submitLabel: string;
  /** The terminal button on the last step of a paged form. */
  readonly steppedSubmitLabel: string;
  /** The spinner label shown while a submission is in flight. */
  readonly sendingLabel: string;
}): ReactElement {
  const resolvedPages = resolvePages(schema, pages);
  const stepped = resolvedPages.length > 1;
  const [value, setValue] = useState<unknown>(() => initialFormValue(schema, formData));
  const [errors, setErrors] = useState<SchemaFormErrors>({});
  const [pageIndex, setPageIndex] = useState(0);
  const options = formData?.options ?? {};

  const { pageRef, headingRef, armStepMove } = useStepFocus(pageIndex);
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
      armStepMove();
      setPageIndex((index) => index + 1);
    }
  };

  const back = (): void => {
    setErrors({});
    armStepMove();
    setPageIndex((index) => Math.max(index - 1, 0));
  };

  return (
    <div className="tcw-question-actions tcw-question-form">
      {stepped ? (
        <p className="tcw-form-progress" role="status" ref={headingRef} tabIndex={-1}>
          {`Step ${pageIndex + 1} of ${resolvedPages.length} · ${page.title}`}
        </p>
      ) : null}
      <div className="tcw-form-page" ref={pageRef}>
        {page.fields.map((field) => (
          <FormField
            key={field}
            field={field}
            schema={schema}
            options={options}
            value={value}
            errors={errors}
            idPrefix={idPrefix}
            onChange={setValue}
          />
        ))}
      </div>
      <FormNav
        stepped={stepped}
        pageIndex={pageIndex}
        isLast={isLast}
        sending={sending}
        sendingLabel={sendingLabel}
        submitLabel={submitLabel}
        steppedSubmitLabel={steppedSubmitLabel}
        onBack={back}
        onNext={next}
        onSubmit={submit}
      />
    </div>
  );
}
