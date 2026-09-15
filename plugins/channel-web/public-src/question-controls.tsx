/**
 * The per-format answer controls for one `ask_user` question: a text field, a
 * Yes/No pair, one button per option, a schema-driven form, or — for `external` —
 * a link out to the question's own callback page. The dispatch switch has NO default
 * arm: a new answer format has to be given a widget here before it type-checks.
 */
import type { JsonSchema } from '@tai42/studio-sdk';
import { Button, ExternalLinkButton, Spinner, TextInput } from '@tai42/studio-sdk';
import type { ReactElement } from 'react';

import { MalformedNotice, SchemaFormAnswer } from '@/schema-form-answer';
import type { ChatItem, FormPage, FormPrefill } from '@/transcript-model';

/** The transcript item this card renders. */
export type QuestionItem = Extract<ChatItem, { kind: 'question' }>;

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
      submitLabel="Answer"
      steppedSubmitLabel="Submit"
      sendingLabel="Sending your answer"
    />
  );
}
