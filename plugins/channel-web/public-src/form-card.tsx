/**
 * The inline widget for one agent-sent ask-less form card: markdown prompt text,
 * any images/links, and the schema-driven form whose submission enters the
 * conversation as a regular participant message.
 *
 * The form is the SAME paged, prefill-aware component the ask path renders
 * (`SchemaFormAnswer`): the card opens with any per-send `data.values` filled in,
 * `data.options` replacing a property's choices, and `pages` shown as steps — a
 * reply-part form thus opens already filled in, exactly as a `form` question does.
 * The terminal button reads "Send": a submission is a participant message, not an answer.
 *
 * Unlike a question there is no deadline and no answered state: the card follows
 * the option-chips precedent. Settle is LOCAL — a "Sent" badge for this page
 * session only, with the form still fillable (every submission is its own participant
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
import { Badge, Markdown } from '@tai42/studio-sdk';
import type { ReactElement } from 'react';
import { useState } from 'react';

import { isFormGone } from '@/api';
import { LocationPin, MediaItems } from '@/media-card';
import { SchemaFormAnswer } from '@/schema-form-answer';
import type { ChatItem, FieldWording, FormWording, StatusWording } from '@/transcript-model';

/** The transcript item this card renders. */
export type FormCardItem = Extract<ChatItem, { kind: 'form' }>;

/** The platform's own words, drawn when the send set no wording for the card. */
const SENT_BADGE = 'Sent';
const CLOSED_BADGE = 'Session ended';
const UNAVAILABLE_LINE = 'This form is no longer available.';
const SUBMIT_LABEL = 'Send';
const SENDING_LABEL = 'Sending the form';
const BACK_LABEL = 'Back';
const FIELD_PLACEHOLDER = 'Choose an option';

const EMPTY_FIELDS: Readonly<Record<string, FieldWording>> = {};

/** The card's form-chrome words resolved to the caller's or the platform's own. */
function resolveFormWords(form: FormWording | null | undefined): {
  readonly submit: string;
  readonly back: string;
  readonly fields: Readonly<Record<string, FieldWording>>;
  readonly updateFailed: string | null;
  readonly stepProgress: string | null;
} {
  return {
    submit: form?.submit ?? SUBMIT_LABEL,
    back: form?.back ?? BACK_LABEL,
    fields: form?.fields ?? EMPTY_FIELDS,
    updateFailed: form?.updateFailed ?? null,
    stepProgress: form?.stepProgress ?? null,
  };
}

/** The card's status words resolved to the caller's or the platform's own. */
function resolveFormStatus(status: StatusWording | null | undefined): {
  readonly sent: string;
  readonly closed: string;
  readonly unavailable: string;
  readonly sending: string;
} {
  return {
    sent: status?.sent ?? SENT_BADGE,
    closed: status?.closed ?? CLOSED_BADGE,
    unavailable: status?.unavailable ?? UNAVAILABLE_LINE,
    sending: status?.sending ?? SENDING_LABEL,
  };
}

export interface FormCardProps {
  readonly item: FormCardItem;
  /** Submits one values object through the card's token door. Rejects with the
   * visitor-facing reason. */
  readonly onSubmitForm: (token: string, values: Record<string, unknown>) => Promise<void>;
  /** The whole page is out of action (an ended session) — no submission can land. */
  readonly locked: boolean;
}

/** The one values shape the form door accepts. `SchemaFormAnswer` always builds an
 * object from an object schema, so this is the defensive last line, never a path a
 * well-formed frame reaches. */
function isValuesObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function FormCard({ item, onSubmitForm, locked }: FormCardProps): ReactElement {
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState(false);
  const [gone, setGone] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // The submit the shared form calls with the validated values: it schema-validates
  // upstream and only reaches here on a clean object, so this just carries the send
  // through the card's token door and settles the local state.
  const submit = (values: unknown): void => {
    if (!isValuesObject(values)) {
      setError('This form is malformed: it does not build an object to send.');
      return;
    }
    setSending(true);
    setError(null);
    onSubmitForm(item.token, values).then(
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

  const form = resolveFormWords(item.wording?.form);
  const status = resolveFormStatus(item.wording?.status);
  return (
    <div className="tcw-row tcw-row--out tcw-row--start">
      <div className="tcw-form">
        <div className="tcw-question-head">
          {item.text !== '' ? <Markdown markdown={item.text} className="tcw-prose" /> : null}
          {sent && !gone ? <Badge variant="success">{status.sent}</Badge> : null}
          {locked ? <Badge variant="neutral">{status.closed}</Badge> : null}
        </div>
        {item.media !== null ? <MediaItems media={item.media} /> : null}
        {item.location !== null ? <LocationPin location={item.location} /> : null}
        {gone ? (
          <p className="tcw-form-gone" role="status">
            {status.unavailable}
          </p>
        ) : null}
        {!gone && !locked ? (
          <SchemaFormAnswer
            schema={item.schema}
            formData={item.formData}
            pages={item.pages}
            reactions={null}
            interactionId={null}
            sending={sending}
            onSubmit={submit}
            idPrefix={item.id}
            submitLabel={form.submit}
            steppedSubmitLabel={SUBMIT_LABEL}
            sendingLabel={status.sending}
            backLabel={form.back}
            fieldWording={form.fields}
            fieldPlaceholder={FIELD_PLACEHOLDER}
            updateFailedLine={form.updateFailed}
            stepProgressLine={form.stepProgress}
          />
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
