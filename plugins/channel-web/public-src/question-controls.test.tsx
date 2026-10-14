import { cleanup, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { QuestionItem } from '@/question-card';
import {
  CALLBACK,
  externalQuestion,
  formQuestion,
  question,
  renderCard,
} from '@/question-card.test-support';
import type {
  AnswerWording,
  ChoiceWording,
  FormWording,
  StatusWording,
  Wording,
} from '@/transcript-model';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

/** A `Wording` with the named chapters and every other chapter unset. */
function wording(chapters: Partial<Wording>): Wording {
  return { form: null, choice: null, answer: null, status: null, ...chapters };
}

function answerWording(partial: Partial<AnswerWording>): AnswerWording {
  return {
    open: null,
    confirm: null,
    decline: null,
    submit: null,
    replyHint: null,
    rejected: null,
    ...partial,
  };
}

function formWording(partial: Partial<FormWording>): FormWording {
  return { submit: null, back: null, fields: {}, ...partial };
}

function choiceWording(partial: Partial<ChoiceWording>): ChoiceWording {
  return { groupLabel: null, ...partial };
}

function statusWording(partial: Partial<StatusWording>): StatusWording {
  return {
    sending: null,
    answered: null,
    expired: null,
    closed: null,
    sent: null,
    unavailable: null,
    countdown: null,
    ...partial,
  };
}

/** An `onAnswer` that never settles, so the control stays in its in-flight state. */
function pendingAnswer(): () => Promise<void> {
  return () => new Promise<void>(() => {});
}

describe('QuestionControls', () => {
  it('sends a typed answer for the text format', async () => {
    const user = userEvent.setup();
    const { onAnswer, onAnswered } = renderCard(question('text'));

    await user.type(screen.getByRole('textbox'), 'ship it');
    await user.click(screen.getByRole('button', { name: 'Answer' }));

    await waitFor(() => expect(onAnswered).toHaveBeenCalled());
    expect(onAnswer).toHaveBeenCalledWith('int-1', 'ship it');
  });

  it('refuses to send a blank text answer', () => {
    renderCard(question('text'));

    expect(screen.getByRole('button', { name: 'Answer' })).toBeDisabled();
  });

  it('sends a boolean for the confirm format', async () => {
    const user = userEvent.setup();
    const { onAnswer, onAnswered } = renderCard(question('confirm'));

    await user.click(screen.getByRole('button', { name: 'No' }));

    await waitFor(() => expect(onAnswered).toHaveBeenCalled());
    expect(onAnswer).toHaveBeenCalledWith('int-1', false);
  });

  it('offers one button per option and sends the chosen one', async () => {
    const user = userEvent.setup();
    const { onAnswer, onAnswered } = renderCard(question('select'));

    await user.click(screen.getByRole('button', { name: 'Tonight' }));

    await waitFor(() => expect(onAnswered).toHaveBeenCalled());
    expect(onAnswer).toHaveBeenCalledWith('int-1', 'Tonight');
  });

  it('sends an external question out to its own callback page', () => {
    renderCard(question('external'));

    expect(screen.getByRole('link', { name: /open to answer/i })).toHaveAttribute('href', CALLBACK);
  });

  it('neutralizes an external question whose ticket is not a usable link', () => {
    // Not an absolute http(s) URL: opening it would be a same-origin in-app
    // navigation nobody intended, so the link is rendered as plain text instead.
    renderCard(externalQuestion('/api/interactions/callback/ticket-1'));

    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    expect(screen.getByText('Open to answer')).toBeInTheDocument();
  });

  it('shows a loud notice for a form whose schema is not an object, never a dropped control', () => {
    // The stream already rejects a non-object schema, so this is the defensive last
    // line — a schema that slips through non-object is a visible alert, not silence.
    const malformed = { ...question('form'), schema: null } as unknown as QuestionItem;
    renderCard(malformed);

    expect(screen.getByRole('alert')).toHaveTextContent(/malformed/i);
  });
});

describe("QuestionControls draw the send's wording", () => {
  it('draws the platform words when the send set none', () => {
    renderCard(question('text'));
    expect(screen.getByRole('textbox')).toHaveAttribute('placeholder', 'Type your answer');
    expect(screen.getByRole('button', { name: 'Answer' })).toBeInTheDocument();
  });

  it("draws the caller's text placeholder and submit label", async () => {
    const user = userEvent.setup();
    renderCard(
      question('text', {
        wording: wording({
          answer: answerWording({ replyHint: 'Say something', submit: 'Ship it' }),
        }),
      }),
    );
    expect(screen.getByRole('textbox')).toHaveAttribute('placeholder', 'Say something');
    await user.type(screen.getByRole('textbox'), 'go');
    expect(screen.getByRole('button', { name: 'Ship it' })).toBeInTheDocument();
  });

  it("draws the caller's confirm and decline labels", () => {
    renderCard(
      question('confirm', {
        wording: wording({ answer: answerWording({ confirm: 'Approve', decline: 'Reject' }) }),
      }),
    );
    expect(screen.getByRole('button', { name: 'Approve' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Reject' })).toBeInTheDocument();
  });

  it('names the select option group — the default, then the caller word', () => {
    renderCard(question('select'));
    expect(screen.getByRole('group', { name: 'Choose an option' })).toBeInTheDocument();
    cleanup();
    renderCard(
      question('select', {
        wording: wording({ choice: choiceWording({ groupLabel: 'Pick a time' }) }),
      }),
    );
    expect(screen.getByRole('group', { name: 'Pick a time' })).toBeInTheDocument();
  });

  it("draws the caller's open label on an external question", () => {
    renderCard(
      question('external', { wording: wording({ answer: answerWording({ open: 'Answer now' }) }) }),
    );
    expect(screen.getByRole('link', { name: 'Answer now' })).toHaveAttribute('href', CALLBACK);
  });

  it("draws the caller's one-page form submit label and field placeholder", () => {
    const item = formQuestion(
      { type: 'object', properties: { kind: { type: 'string', title: 'Kind' } } },
      { values: {}, options: { kind: [{ value: 'a', label: 'Option A' }] } },
      null,
      null,
      wording({
        form: formWording({ submit: 'File it', fields: { kind: { choiceLabel: 'Pick a kind' } } }),
      }),
    );
    renderCard(item);
    expect(screen.getByRole('button', { name: 'File it' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'Pick a kind' })).toBeInTheDocument();
  });

  it("speaks the caller's in-flight label while a text answer is sending", async () => {
    const user = userEvent.setup();
    renderCard(
      question('text', { wording: wording({ status: statusWording({ sending: 'On its way' }) }) }),
      {
        onAnswer: pendingAnswer(),
      },
    );
    await user.type(screen.getByRole('textbox'), 'go');
    await user.click(screen.getByRole('button', { name: 'Answer' }));
    expect(await screen.findByRole('status', { name: 'On its way' })).toBeInTheDocument();
  });
});
