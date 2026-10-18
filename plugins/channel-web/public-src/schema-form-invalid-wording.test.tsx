import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { FormCard, type FormCardItem } from '@/form-card';
import { formQuestion, renderCard } from '@/question-card.test-support';
import type { FieldWording, FormPage, JsonSchema, Wording } from '@/transcript-model';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const SENTENCE = 'Use your work address';
const PATTERN_WORD = 'must match the required pattern';

/** One string field whose value must carry an `@`; `required` when asked. */
function emailSchema(required = false): JsonSchema {
  return {
    type: 'object',
    properties: { email: { type: 'string', title: 'Email', pattern: '^[^@]+@[^@]+$' } },
    ...(required ? { required: ['email'] } : {}),
  };
}

/** A `Wording` whose form chapter carries the given per-field words and nothing else. */
function fieldsWording(fields: Readonly<Record<string, FieldWording>>): Wording {
  return {
    form: { submit: null, back: null, fields },
    choice: null,
    answer: null,
    status: null,
  };
}

const EMAIL_INVALID = fieldsWording({ email: { choiceLabel: null, invalid: SENTENCE } });

describe("a form field's declared invalid sentence", () => {
  it('shows under the field in place of the SDK word after an answer outside its shape', async () => {
    const user = userEvent.setup();
    const { onAnswer } = renderCard(formQuestion(emailSchema(), null, null, null, EMAIL_INVALID));

    await user.type(screen.getByRole('textbox'), 'not-an-address');
    await user.click(screen.getByRole('button', { name: 'Answer' }));

    expect(await screen.findByText(SENTENCE)).toBeInTheDocument();
    expect(screen.queryByText(PATTERN_WORD)).not.toBeInTheDocument();
    expect(onAnswer).not.toHaveBeenCalled();
  });

  it("keeps the SDK word when the field's words carry no invalid sentence", async () => {
    const user = userEvent.setup();
    const wording = fieldsWording({ email: { choiceLabel: 'Pick one', invalid: null } });
    const { onAnswer } = renderCard(formQuestion(emailSchema(), null, null, null, wording));

    await user.type(screen.getByRole('textbox'), 'not-an-address');
    await user.click(screen.getByRole('button', { name: 'Answer' }));

    expect(await screen.findByText(PATTERN_WORD)).toBeInTheDocument();
    expect(onAnswer).not.toHaveBeenCalled();
  });

  it('never replaces the missing-required message', async () => {
    const user = userEvent.setup();
    const { onAnswer } = renderCard(
      formQuestion(emailSchema(true), null, null, null, EMAIL_INVALID),
    );

    await user.click(screen.getByRole('button', { name: 'Answer' }));

    expect(await screen.findByText('"email" is required')).toBeInTheDocument();
    expect(screen.queryByText(SENTENCE)).not.toBeInTheDocument();
    expect(onAnswer).not.toHaveBeenCalled();
  });

  it('shows on a stepped page when Next is refused', async () => {
    const user = userEvent.setup();
    const schema: JsonSchema = {
      type: 'object',
      properties: { ...emailSchema().properties, note: { type: 'string' } },
    };
    const pages: readonly FormPage[] = [
      { title: 'Contact', fields: ['email'], display: [], kind: 'input', buttonLabel: null },
      { title: 'Note', fields: ['note'], display: [], kind: 'input', buttonLabel: null },
    ];
    renderCard(formQuestion(schema, null, pages, null, EMAIL_INVALID));

    await user.type(screen.getByRole('textbox'), 'not-an-address');
    await user.click(screen.getByRole('button', { name: 'Next' }));

    expect(await screen.findByText(SENTENCE)).toBeInTheDocument();
    expect(screen.getByText(/Step 1 of 2 . Contact/)).toBeInTheDocument();
  });

  it('shows on a reacting form and holds the submit reaction', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const reactions = { fieldChanged: [], pageAdvanced: [], submitted: true, choices: [] };
    const { onAnswer } = renderCard(
      formQuestion(emailSchema(), null, null, reactions, EMAIL_INVALID),
    );

    await user.type(screen.getByRole('textbox'), 'not-an-address');
    await user.click(screen.getByRole('button', { name: 'Answer' }));

    expect(await screen.findByText(SENTENCE)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(onAnswer).not.toHaveBeenCalled();
  });

  it('shows on an ask-less form card', async () => {
    const user = userEvent.setup();
    const item: FormCardItem = {
      kind: 'form',
      id: 'f1',
      text: 'Fill this in',
      schema: emailSchema(),
      token: 'tok-0123456789abcdef0123456789abcdef',
      media: null,
      location: null,
      formData: null,
      pages: null,
      wording: EMAIL_INVALID,
      ts: new Date().toISOString(),
    };
    const onSubmitForm = vi.fn().mockResolvedValue(undefined);
    render(<FormCard item={item} onSubmitForm={onSubmitForm} locked={false} />);

    await user.type(screen.getByRole('textbox'), 'not-an-address');
    await user.click(screen.getByRole('button', { name: 'Send' }));

    expect(await screen.findByText(SENTENCE)).toBeInTheDocument();
    expect(screen.queryByText(PATTERN_WORD)).not.toBeInTheDocument();
    expect(onSubmitForm).not.toHaveBeenCalled();
  });
});
