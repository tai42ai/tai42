/**
 * The inline widget for one agent-sent ask-less form card: markdown prompt text,
 * any images/links, and the schema-driven form whose submission enters the
 * conversation as a regular guest message.
 *
 * Unlike a question there is no deadline and no answered state: the card follows
 * the option-chips precedent. Settle is LOCAL — a "Sent" badge for this page
 * session only, with the form still fillable (every submission is its own guest
 * message), so a backlog replay after a reload renders the card fillable again.
 * The submit button is disabled only while a send is in flight (the double-click
 * guard). A `locked` page (an ended session) hides the controls behind a badge,
 * exactly as the question widget does. A 404 from the door means the form's token
 * no longer resolves — the card shows an inline "no longer available" line and
 * withdraws the controls, since the same token can only 404 again.
 *
 * SAFETY: the prompt renders through the SDK's `Markdown` (React elements only,
 * no raw HTML), and every media URL was proven absolute and scheme-appropriate by
 * the stream reducer — the same guarantees the media card rides on.
 */
import type { ReactElement } from 'react';
import { useState } from 'react';
import {
  Badge,
  Button,
  Markdown,
  SchemaForm,
  Spinner,
  defaultValueForSchema,
  validateAgainstSchema,
} from '@tai42/studio-sdk';
import type { SchemaFormErrors } from '@tai42/studio-sdk';

import { isFormGone } from '@/api';
import { MediaItems } from '@/media-card';
import type { ChatItem } from '@/use-chat-stream';

/** The transcript item this card renders. */
export type FormCardItem = Extract<ChatItem, { kind: 'form' }>;

export interface FormCardProps {
  readonly item: FormCardItem;
  /** Submits one values object through the card's token door. Rejects with the
   * visitor-facing reason. */
  readonly onSubmitForm: (token: string, values: Record<string, unknown>) => Promise<void>;
  /** The whole page is out of action (an ended session) — no submission can land. */
  readonly locked: boolean;
}

/** The one values shape the form door accepts. A schema whose root builds
 * anything else has no submittable form — surfaced loudly, never sent. */
function isValuesObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function FormCard({ item, onSubmitForm, locked }: FormCardProps): ReactElement {
  const [value, setValue] = useState<unknown>(() => defaultValueForSchema(item.schema));
  const [errors, setErrors] = useState<SchemaFormErrors>({});
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState(false);
  const [gone, setGone] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Validate against the schema before sending — a courtesy to the visitor, not a
  // gate the server relies on: the door never validates values against the schema
  // (guest-shaped data), so this only saves a round trip for obvious misses.
  const submit = (): void => {
    const found = validateAgainstSchema(item.schema, value);
    setErrors(found);
    if (Object.keys(found).length > 0) return;
    if (!isValuesObject(value)) {
      setError('This form is malformed: it does not build an object to send.');
      return;
    }
    setSending(true);
    setError(null);
    onSubmitForm(item.token, value).then(
      () => {
        setSending(false);
        setSent(true);
      },
      (err: unknown) => {
        setSending(false);
        if (isFormGone(err)) {
          setGone(true);
          return;
        }
        setError(err instanceof Error ? err.message : String(err));
      },
    );
  };

  return (
    <div className="tcw-row tcw-row--out tcw-row--start">
      <div className="tcw-form">
        <div className="tcw-question-head">
          {item.text !== '' ? <Markdown markdown={item.text} className="tcw-prose" /> : null}
          {sent && !gone ? <Badge variant="success">Sent</Badge> : null}
          {locked ? <Badge variant="neutral">Session ended</Badge> : null}
        </div>
        {item.media !== null ? <MediaItems media={item.media} /> : null}
        {gone ? (
          <p className="tcw-form-gone" role="status">
            This form is no longer available.
          </p>
        ) : null}
        {!gone && !locked ? (
          <div className="tcw-question-actions tcw-question-form">
            <SchemaForm
              schema={item.schema}
              value={value}
              onChange={setValue}
              errors={errors}
              idPrefix={item.id}
            />
            <Button type="button" variant="primary" disabled={sending} onClick={submit}>
              {sending ? <Spinner label="Sending the form" /> : 'Send'}
            </Button>
          </div>
        ) : null}
        {error !== null && !gone ? (
          <p className="tcw-question-error" role="alert">
            {error}
          </p>
        ) : null}
      </div>
    </div>
  );
}
