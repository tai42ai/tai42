/**
 * The pure value/schema/step helpers the form renderer and its reaction hook share:
 * object guards, the per-send page resolution and initial value, per-page error slicing,
 * the single-field schema the SDK renders one control from, and the step-focus hook. No
 * JSX — the presentational pieces live in `schema-form-answer`.
 */
import type { JsonSchema, SchemaFormErrors } from '@tai42/studio-sdk';
import { defaultValueForSchema } from '@tai42/studio-sdk';
import type { RefObject } from 'react';
import { useEffect, useRef } from 'react';

import type { FormPage, FormPrefill } from '@/transcript-model';

/** A plain JS object (a form's values bag), or not. */
export function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** The top-level property names of an object schema, in declared order. */
export function propertyOrder(schema: JsonSchema): readonly string[] {
  return isPlainObject(schema.properties) ? Object.keys(schema.properties) : [];
}

/** The pages to render: the per-send steps when set, else one page carrying every
 * top-level property in schema order (the whole form on one step). */
export function resolvePages(
  schema: JsonSchema,
  pages: readonly FormPage[] | null,
): readonly FormPage[] {
  return pages ?? [{ title: '', fields: propertyOrder(schema), display: [], kind: 'input' }];
}

/** The initial form value: the schema's defaults overlaid with any prefilled
 * values so a known value is shown filled in from first render. */
export function initialFormValue(schema: JsonSchema, formData: FormPrefill | null): unknown {
  const base = defaultValueForSchema(schema);
  const start = isPlainObject(base) ? { ...base } : {};
  return formData !== null ? { ...start, ...formData.values } : start;
}

/** The top-level keys whose value differs between the previous and next form value — the
 * fields a change touched, so a declared one can trigger its reaction. */
export function changedTopLevelKeys(previous: unknown, next: unknown): string[] {
  const before = isPlainObject(previous) ? previous : {};
  const after = isPlainObject(next) ? next : {};
  const keys = new Set([...Object.keys(before), ...Object.keys(after)]);
  return [...keys].filter((key) => before[key] !== after[key]);
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
  const prop = isPlainObject(schema.properties) ? schema.properties[field] : undefined;
  const required = (schema.required ?? []).includes(field) ? [field] : [];
  return {
    type: 'object',
    properties: prop !== undefined ? { [field]: widgetProperty(prop) } : {},
    required,
  };
}

/** The top-level field an error path belongs to (the segment before the first `.`
 * or `[`), so a nested error still maps to its page. */
function fieldOfPath(path: string): string {
  const cut = [path.indexOf('.'), path.indexOf('[')].filter((i) => i !== -1);
  return cut.length > 0 ? path.slice(0, Math.min(...cut)) : path;
}

/** Just the errors whose field is one of `fields` — the per-page slice used to
 * gate a Next without blocking on a later page's field. */
export function errorsForFields(
  errors: SchemaFormErrors,
  fields: readonly string[],
): SchemaFormErrors {
  const set = new Set(fields);
  return Object.fromEntries(Object.entries(errors).filter(([path]) => set.has(fieldOfPath(path))));
}

/** The first page carrying an errored field, or `-1` — where the visitor is sent
 * when a submit fails on a field that is not on the current step. */
export function firstPageWithError(pages: readonly FormPage[], errors: SchemaFormErrors): number {
  const errored = new Set(Object.keys(errors).map(fieldOfPath));
  return pages.findIndex((page) => page.fields.some((field) => errored.has(field)));
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
