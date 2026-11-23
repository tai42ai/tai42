/**
 * The pure value/schema/step helpers the form renderer and its reaction hook share:
 * the schema's property order, the per-send page resolution, its image slots, the answer check
 * against the choices shown, the initial value, per-page error slicing, the single-field schema
 * the SDK renders one control from, the caller's per-field words in the SDK's `fieldWording`
 * shape, and the step-focus hook. No JSX — the presentational pieces live in
 * `schema-form-answer`.
 */
import type { FieldWords, JsonSchema, SchemaFormErrors } from '@tai42/studio-sdk';
import {
  defaultValueForSchema,
  errorPathField,
  fieldDrawingOrder,
  schemaWithSendOptions,
  validateAgainstSchema,
} from '@tai42/studio-sdk';
import type { RefObject } from 'react';
import { useEffect, useRef } from 'react';

import { isRecord } from '@/guards';
import type { FieldWording, FormOptionData, FormPage, FormPrefill } from '@/transcript-model';

/** The top-level property names of an object schema, in declared order. */
export function propertyOrder(schema: JsonSchema): readonly string[] {
  return isRecord(schema.properties) ? Object.keys(schema.properties) : [];
}

/** The pages to render: the per-send steps when set, else one page carrying every
 * top-level property in schema order (the whole form on one step). Each page's fields are
 * in drawing order, each `timeOf` time right after its date. */
export function resolvePages(
  schema: JsonSchema,
  pages: readonly FormPage[] | null,
): readonly FormPage[] {
  const properties: Readonly<Record<string, JsonSchema>> = isRecord(schema.properties)
    ? schema.properties
    : {};
  if (pages === null) {
    return [
      {
        title: '',
        fields: fieldDrawingOrder(propertyOrder(schema), properties),
        display: [],
        kind: 'input',
        buttonLabel: null,
      },
    ];
  }
  return pages.map((page) => ({ ...page, fields: fieldDrawingOrder(page.fields, properties) }));
}

/** The slot names the pages' image blocks fill — the display keys a reaction's update
 * carries as image sources. */
export function imageSlotsOf(pages: readonly FormPage[]): ReadonlySet<string> {
  const slots = new Set<string>();
  for (const page of pages)
    for (const block of page.display)
      if (block.kind === 'image' && block.slot !== null) slots.add(block.slot);
  return slots;
}

/** The answer's errors, judged against the schema with `options` as its fields' choices. */
export function answerErrors(
  schema: JsonSchema,
  options: Readonly<Record<string, readonly FormOptionData[]>>,
  answer: unknown,
  fieldWording: SdkFieldWording,
): SchemaFormErrors {
  return validateAgainstSchema(schemaWithSendOptions(schema, options), answer, { fieldWording });
}

/** The initial form value: the schema's defaults overlaid with any prefilled
 * values so a known value is shown filled in from first render. */
export function initialFormValue(schema: JsonSchema, formData: FormPrefill | null): unknown {
  const base = defaultValueForSchema(schema);
  const start = isRecord(base) ? { ...base } : {};
  return formData !== null ? { ...start, ...formData.values } : start;
}

/** The one string `format` this door renders with a native control: `date` maps to
 * `<input type="date">` (its ISO value posts unchanged). `time` and `date-time` render
 * as plain text — the native `time` / `datetime-local` controls cannot emit the value
 * shapes the answer door requires (a `date-time` needs an RFC 3339 offset). */
const NATIVELY_RENDERED_FORMATS = new Set(['date']);

/** A property normalised for this door: a string `format` the door does not render
 * natively is dropped, so the SDK renders a plain text input rather than its native
 * time / datetime-local control. Every other property passes through unchanged. */
function widgetProperty(prop: JsonSchema): JsonSchema {
  if (
    prop.type === 'string' &&
    typeof prop.format === 'string' &&
    !NATIVELY_RENDERED_FORMATS.has(prop.format)
  ) {
    const { format: _format, ...rest } = prop;
    return rest;
  }
  return prop;
}

/** A one-property object schema, so a single field renders through `SchemaForm`
 * with the same control and validation path it has in the whole form. */
export function singleFieldSchema(schema: JsonSchema, field: string): JsonSchema {
  const prop = isRecord(schema.properties) ? schema.properties[field] : undefined;
  const required = (schema.required ?? []).includes(field) ? [field] : [];
  return {
    type: 'object',
    properties: prop !== undefined ? { [field]: widgetProperty(prop) } : {},
    required,
  };
}

/** The SDK's per-field words, keyed by property name. */
export type SdkFieldWording = Readonly<Record<string, FieldWords>>;

/** The parsed per-field wording as the SDK's `fieldWording` record, keyed by property
 * name, carrying only the words the caller set. `SchemaForm` reads `choiceLabel` (an
 * unset one leaves the `enum` select its default placeholder) and a list field's
 * `addItem` / `removeItem` / `itemLabel` / `noItems`; `validateAgainstSchema` reads
 * `invalid` and `required` (an unset one leaves the SDK's own message). The yes/no
 * labels are drawn by the widget's own readback, not handed to the SDK. Each entry is an
 * own key, so a field named after a built-in object member keeps its words. */
export function toSdkFieldWording(fields: Readonly<Record<string, FieldWording>>): SdkFieldWording {
  const entries: [string, FieldWords][] = [];
  for (const [name, field] of Object.entries(fields)) {
    const words: { -readonly [K in keyof FieldWords]: FieldWords[K] } = {};
    if (field.choiceLabel !== null) words.choiceLabel = field.choiceLabel;
    if (field.invalid !== null) words.invalid = field.invalid;
    if (field.required !== null) words.required = field.required;
    if (field.addItem !== null) words.addItem = field.addItem;
    if (field.removeItem !== null) words.removeItem = field.removeItem;
    if (field.itemLabel !== null) words.itemLabel = field.itemLabel;
    if (field.noItems !== null) words.noItems = field.noItems;
    if (Object.keys(words).length > 0) entries.push([name, words]);
  }
  return Object.fromEntries(entries);
}

/** Just the errors whose field is one of `fields` — the per-page slice used to
 * gate a Next without blocking on a later page's field. */
export function errorsForFields(
  errors: SchemaFormErrors,
  fields: readonly string[],
): SchemaFormErrors {
  const set = new Set(fields);
  return Object.fromEntries(
    Object.entries(errors).filter(([path]) => set.has(errorPathField(path))),
  );
}

/** Move focus to the shown step's first control (its heading as a fallback) after a
 * step change, but only when an explicit Next/Back armed it — the initial render
 * must not steal focus into the form. */
export function useStepFocus(pageIndex: number): {
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
