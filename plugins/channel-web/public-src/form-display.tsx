/**
 * The display-only parts of a form page: its ordered display blocks (headings, body
 * text, images, and data slots) and a review step's generic readback of entered values.
 * Neither is an input control — they render alongside (or, for a review page, instead of)
 * a page's fields.
 *
 * UNTRUSTED PAYLOADS: a block's `text`/`alt` renders only as React-escaped text; an image
 * `src` — the block's own or a reaction's value for its slot — is set as an attribute only
 * after the parser has vetted it (an absolute `https` URL or the same-origin served-media
 * reference — the SAME gate the chat's media cards ride on: the frame parser for the block,
 * the update parser for a reaction), with the page CSP's `img-src` as the backstop.
 */
import type { JsonSchema } from '@tai42/studio-sdk';
import { fieldDrawingOrder, isFieldVisible } from '@tai42/studio-sdk';
import type { ReactElement } from 'react';

import type { DisplayBlock, FieldWording } from '@/transcript-model';

/** A plain JS object, or not. */
function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** An arbitrary value as display text: primitives direct, objects/arrays as JSON. */
function toText(value: unknown): string {
  switch (typeof value) {
    case 'string':
      return value;
    case 'number':
    case 'boolean':
    case 'bigint':
      return String(value);
    default:
      return JSON.stringify(value);
  }
}

/** A display slot's current value as text (a computed total is a number); absent → undefined. */
function slotValueText(value: unknown): string | undefined {
  if (value === undefined || value === null) return undefined;
  return toText(value);
}

/** An image display block: the reaction's source for its slot when one arrived (`null`
 * clears it), else the block's own; with no source it degrades to its alt text. */
function renderDisplayImage(
  block: DisplayBlock,
  reactionSource: string | null | undefined,
): ReactElement | null {
  const alt = block.alt !== null && block.alt.trim() !== '' ? block.alt : undefined;
  // Both sources were vetted by a parser (an absolute https URL or the same-origin
  // served-media reference), so `src` is set as an attribute without re-checking; the page
  // CSP's `img-src` gate is the backstop. No source degrades to the alt text as body,
  // never a broken image.
  const src = reactionSource === undefined ? block.src : reactionSource;
  if (src !== null) {
    return (
      <img
        className="tcw-form-display-image"
        src={src}
        alt={alt ?? ''}
        data-testid={block.slot !== null ? `form-slot-${block.slot}` : undefined}
      />
    );
  }
  return alt !== undefined ? <p className="tcw-form-display-body">{alt}</p> : null;
}

/** One display block: an image (its slot's reaction source, else its own), or a
 * heading/body whose text is its slot's current value (when it names one) or its static
 * `text`. An empty text block renders nothing. */
function DisplayBlockView({
  block,
  slots,
  imageSources,
}: {
  readonly block: DisplayBlock;
  readonly slots: Readonly<Record<string, unknown>>;
  readonly imageSources: ReadonlyMap<string, string | null>;
}): ReactElement | null {
  if (block.kind === 'image')
    return renderDisplayImage(
      block,
      block.slot !== null ? imageSources.get(block.slot) : undefined,
    );
  const slotText = block.slot !== null ? slotValueText(slots[block.slot]) : undefined;
  const text = slotText ?? block.text ?? '';
  if (text === '') return null;
  const className = block.kind === 'heading' ? 'tcw-form-display-heading' : 'tcw-form-display-body';
  return (
    <p
      className={className}
      data-testid={block.slot !== null ? `form-slot-${block.slot}` : undefined}
    >
      {text}
    </p>
  );
}

/** A page's ordered display-only blocks (headings, body text, images, slots). */
export function DisplayBlocks({
  blocks,
  slots,
  imageSources,
}: {
  readonly blocks: readonly DisplayBlock[];
  readonly slots: Readonly<Record<string, unknown>>;
  readonly imageSources: ReadonlyMap<string, string | null>;
}): ReactElement | null {
  if (blocks.length === 0) return null;
  return (
    <div className="tcw-form-display">
      {blocks.map((block, index) => (
        <DisplayBlockView key={index} block={block} slots={slots} imageSources={imageSources} />
      ))}
    </div>
  );
}

/** One answered value rendered for the review readback; an empty value reads as "—", a
 * yes/no value as the field's own `trueLabel` / `falseLabel`, else "Yes" / "No". */
function formatReadbackValue(value: unknown, words: FieldWording | undefined): string {
  if (value === undefined || value === null || value === '') return '—';
  if (Array.isArray(value)) return value.length === 0 ? '—' : value.map(toText).join(', ');
  if (typeof value === 'boolean')
    return value ? (words?.trueLabel ?? 'Yes') : (words?.falseLabel ?? 'No');
  return toText(value);
}

/**
 * A review step's generic readback: each VISIBLE top-level field's label and the value
 * entered for it, in drawing order (each `timeOf` time right after its date). A field hidden by its `visibleWhen` predicate is absent — it is absent
 * from the answer the consumer receives.
 */
export function ReviewReadback({
  schema,
  value,
  fieldWording,
}: {
  readonly schema: JsonSchema;
  readonly value: Readonly<Record<string, unknown>>;
  readonly fieldWording: Readonly<Record<string, FieldWording>>;
}): ReactElement | null {
  const properties = isPlainObject(schema.properties) ? schema.properties : {};
  const rows = fieldDrawingOrder(Object.keys(properties), properties)
    .map((name) => [name, properties[name]] as const)
    .filter(
      (row): row is readonly [string, JsonSchema] =>
        isPlainObject(row[1]) && isFieldVisible(row[1], value),
    );
  if (rows.length === 0) return null;
  return (
    <dl className="tcw-form-review" data-testid="form-review">
      {rows.map(([name, prop]) => (
        <div key={name} className="tcw-form-review-row">
          <dt className="tcw-form-review-term">
            {isPlainObject(prop) && typeof prop.title === 'string' ? prop.title : name}
          </dt>
          <dd className="tcw-form-review-value">
            {formatReadbackValue(value[name], fieldWording[name])}
          </dd>
        </div>
      ))}
    </dl>
  );
}
