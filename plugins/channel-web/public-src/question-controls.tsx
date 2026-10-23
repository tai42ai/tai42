/**
 * The per-format answer controls for one `ask` question: a text field, a
 * Yes/No pair, one button per option, a schema-driven form, or — for `external` —
 * a link out to the question's own callback page. The dispatch switch has NO default
 * arm: a new answer format has to be given a widget here before it type-checks.
 */
import type { JsonSchema } from '@tai42/studio-sdk';
import { Button, ExternalLinkButton, Spinner, TextInput } from '@tai42/studio-sdk';
import type { ReactElement } from 'react';

import { MalformedNotice, SchemaFormAnswer } from '@/schema-form-answer';
import type {
  ChatItem,
  FormPage,
  FormPrefill,
  FormReactions,
  FormWording,
} from '@/transcript-model';

/** The transcript item this card renders. */
export type QuestionItem = Extract<ChatItem, { kind: 'question' }>;

/** The platform's own words, drawn when the send set no wording for a control. */
const OPEN_LABEL = 'Open to answer';
const REPLY_PLACEHOLDER = 'Type your answer';
const TEXT_SUBMIT_LABEL = 'Answer';
const CONFIRM_LABEL = 'Yes';
const DECLINE_LABEL = 'No';
const SENDING_LABEL = 'Sending your answer';
/** The accessible name of a flat group of option buttons — the platform's word for the
 * same choose-from-a-list decision Studio's select field and the web form field make. */
export const SELECT_GROUP_LABEL = 'Choose an option';
/** The terminal button of a one-page form answered as an ask. */
const FORM_SUBMIT_LABEL = 'Answer';
/** The button that returns to a stepped form's previous step. */
const FORM_BACK_LABEL = 'Back';
/** The placeholder of a form field's per-send option picker. */
export const FIELD_PLACEHOLDER = 'Choose an option';

interface ControlsProps {
  readonly question: QuestionItem;
  readonly draft: string;
  readonly onDraft: (value: string) => void;
  readonly sending: boolean;
  readonly onSubmit: (answer: unknown) => void;
}

/** The per-format controls. The switch has NO default arm: a new answer format
 * has to be given a widget here before it type-checks. */
export function QuestionControls(props: ControlsProps): ReactElement {
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
          reactions={question.reactions}
          interactionId={question.interactionId}
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
          <ExternalLinkButton url={question.callbackUrl}>
            {question.wording?.answer?.open ?? OPEN_LABEL}
          </ExternalLinkButton>
        </div>
      );
  }
}

function TextAnswer({ question, draft, onDraft, sending, onSubmit }: ControlsProps): ReactElement {
  const blank = draft.trim() === '';
  const answer = question.wording?.answer;
  const sendingLabel = question.wording?.status?.sending ?? SENDING_LABEL;
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
        placeholder={answer?.replyHint ?? REPLY_PLACEHOLDER}
        disabled={sending}
      />
      <Button type="submit" variant="primary" disabled={blank || sending}>
        {sending ? <Spinner label={sendingLabel} /> : (answer?.submit ?? TEXT_SUBMIT_LABEL)}
      </Button>
    </form>
  );
}

function ConfirmAnswer({ question, sending, onSubmit }: ControlsProps): ReactElement {
  const answer = question.wording?.answer;
  const sendingLabel = question.wording?.status?.sending ?? SENDING_LABEL;
  return (
    <div className="tcw-question-actions">
      <Button type="button" variant="primary" disabled={sending} onClick={() => onSubmit(true)}>
        {answer?.confirm ?? CONFIRM_LABEL}
      </Button>
      <Button type="button" variant="secondary" disabled={sending} onClick={() => onSubmit(false)}>
        {answer?.decline ?? DECLINE_LABEL}
      </Button>
      {sending ? <Spinner label={sendingLabel} /> : null}
    </div>
  );
}

function SelectAnswer({
  question,
  options,
  sending,
  onSubmit,
}: ControlsProps & { readonly options: readonly string[] }): ReactElement {
  const sendingLabel = question.wording?.status?.sending ?? SENDING_LABEL;
  return (
    <div
      className="tcw-question-actions"
      role="group"
      aria-label={question.wording?.choice?.groupLabel ?? SELECT_GROUP_LABEL}
    >
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
      {sending ? <Spinner label={sendingLabel} /> : null}
    </div>
  );
}

/** A structurally-usable schema object, or not — the stream already rejects a
 * non-object schema, so this is the defensive last line: a schema that slips through
 * as a non-object renders the loud notice below rather than a dropped control. */
function isSchemaObject(value: unknown): value is JsonSchema {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** A question's form words resolved to the caller's or the widget's own; the two lines
 *  stay `null` when the send set none, so the form draws its own. */
function resolveFormWords(form: FormWording | null | undefined) {
  return {
    submit: form?.submit ?? FORM_SUBMIT_LABEL,
    back: form?.back ?? FORM_BACK_LABEL,
    fields: form?.fields ?? {},
    updateFailed: form?.updateFailed ?? null,
    stepProgress: form?.stepProgress ?? null,
  };
}

function FormAnswer({
  question,
  sending,
  onSubmit,
  schema,
  formData,
  pages,
  reactions,
  interactionId,
  idPrefix,
}: ControlsProps & {
  readonly schema: unknown;
  readonly formData: FormPrefill | null;
  readonly pages: readonly FormPage[] | null;
  readonly reactions: FormReactions | null;
  readonly interactionId: string;
  readonly idPrefix: string;
}): ReactElement {
  if (!isSchemaObject(schema)) {
    return <MalformedNotice message="This form is malformed: its schema must be an object." />;
  }
  const form = resolveFormWords(question.wording?.form);
  return (
    <SchemaFormAnswer
      schema={schema}
      formData={formData}
      pages={pages}
      reactions={reactions}
      interactionId={interactionId}
      sending={sending}
      onSubmit={onSubmit}
      idPrefix={idPrefix}
      submitLabel={form.submit}
      // The last step of a paged form carries the page's own primary button, not
      // the one-page `form.submit` member; the web draws its generic word there.
      steppedSubmitLabel="Submit"
      sendingLabel={question.wording?.status?.sending ?? SENDING_LABEL}
      backLabel={form.back}
      fieldWording={form.fields}
      fieldPlaceholder={FIELD_PLACEHOLDER}
      updateFailedLine={form.updateFailed}
      stepProgressLine={form.stepProgress}
    />
  );
}
