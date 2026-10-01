/**
 * The display-only parts of a form page: its ordered display blocks (headings, body
 * text, images, and data slots) and a review step's generic readback of entered values.
 * Neither is an input control — they render alongside (or, for a review page, instead of)
 * a page's fields.
 *
 * UNTRUSTED PAYLOADS: a block's `text`/`alt` renders only as React-escaped text; an image
 * `src` is set as an attribute only after the frame parser has vetted it (an absolute
 * `https` URL or the same-origin served-media reference — the SAME gate the chat's media
 * cards ride on), with the page CSP's `img-src` as the backstop.
 */
import type { JsonSchema } from '@tai42/studio-sdk';
import { isFieldVisible } from '@tai42/studio-sdk';
import type { ReactElement } from 'react';

import type { DisplayBlock } from '@/transcript-model';

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

/** An image display block: the frame-vetted image, degrading to its alt text with no src. */
function renderDisplayImage(block: DisplayBlock): ReactElement | null {
  const alt = block.alt !== null && block.alt.trim() !== '' ? block.alt : undefined;
  // `src` was vetted by the frame parser (an absolute https URL or the same-origin
  // served-media reference), so it is set as an attribute without re-checking; the page
  // CSP's `img-src` gate is the backstop. A block with no source degrades to its alt text
  // as body, never a broken image.
  if (block.src !== null) {
    return <img className="tcw-form-display-image" src={block.src} alt={alt ?? ''} />;
  }
  return alt !== undefined ? <p className="tcw-form-display-body">{alt}</p> : null;
}

/** One display block: an image, or a heading/body whose text is its slot's current value
 * (when it names one) or its static `text`. An empty text block renders nothing. */
function DisplayBlockView({
  block,
  slots,
}: {
  readonly block: DisplayBlock;
  readonly slots: Readonly<Record<string, unknown>>;
}): ReactElement | null {
  const slotText = block.slot !== null ? slotValueText(slots[block.slot]) : undefined;
  if (block.kind === 'image') return renderDisplayImage(block);
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
}: {
  readonly blocks: readonly DisplayBlock[];
  readonly slots: Readonly<Record<string, unknown>>;
}): ReactElement | null {
  if (blocks.length === 0) return null;
  return (
    <div className="tcw-form-display">
      {blocks.map((block, index) => (
        <DisplayBlockView key={index} block={block} slots={slots} />
      ))}
    </div>
  );
}

/** One answered value rendered for the review readback; an empty value reads as "—". */
function formatReadbackValue(value: unknown): string {
  if (value === undefined || value === null || value === '') return '—';
  if (Array.isArray(value)) return value.length === 0 ? '—' : value.map(toText).join(', ');
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  return toText(value);
}

/**
 * A review step's generic readback: each VISIBLE top-level field's label and the value
 * entered for it. A field hidden by its `visibleWhen` predicate is absent — it is absent
 * from the answer the consumer receives.
 */
export function ReviewReadback({
  schema,
  value,
}: {
  readonly schema: JsonSchema;
  readonly value: Readonly<Record<string, unknown>>;
}): ReactElement | null {
  const properties = isPlainObject(schema.properties) ? schema.properties : {};
  const rows = Object.entries(properties).filter(
    ([, prop]) => isPlainObject(prop) && isFieldVisible(prop, value),
  );
  if (rows.length === 0) return null;
  return (
    <dl className="tcw-form-review" data-testid="form-review">
      {rows.map(([name, prop]) => (
        <div key={name} className="tcw-form-review-row">
          <dt className="tcw-form-review-term">
            {isPlainObject(prop) && typeof prop.title === 'string' ? prop.title : name}
          </dt>
          <dd className="tcw-form-review-value">{formatReadbackValue(value[name])}</dd>
        </div>
      ))}
    </dl>
  );
}
