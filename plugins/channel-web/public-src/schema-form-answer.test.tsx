import { cleanup, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { FORM_SCHEMA, formQuestion, question, renderCard } from '@/question-card.test-support';
import type { FormPage, JsonSchema } from '@/transcript-model';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('SchemaFormAnswer', () => {
  it('builds and sends the object for the schema-driven form format', async () => {
    const user = userEvent.setup();
    const { onAnswer, onAnswered } = renderCard(question('form'));

    await user.type(screen.getByRole('textbox'), 'ship it');
    await user.click(screen.getByRole('button', { name: 'Answer' }));

    await waitFor(() => expect(onAnswered).toHaveBeenCalled());
    expect(onAnswer).toHaveBeenCalledWith('int-1', { note: 'ship it' });
  });

  it('prefills a form value from the per-send data and submits it', async () => {
    const user = userEvent.setup();
    const { onAnswer, onAnswered } = renderCard(
      formQuestion(FORM_SCHEMA, { values: { note: 'hello' }, options: {} }, null),
    );

    // The known value is shown filled in from first render.
    expect(screen.getByRole('textbox')).toHaveValue('hello');
    await user.click(screen.getByRole('button', { name: 'Answer' }));

    await waitFor(() => expect(onAnswered).toHaveBeenCalled());
    expect(onAnswer).toHaveBeenCalledWith('int-1', { note: 'hello' });
  });

  it('renders a per-send option list as a labelled select and posts the chosen value', async () => {
    const user = userEvent.setup();
    const schema: JsonSchema = {
      type: 'object',
      properties: { colour: { type: 'string', title: 'Colour' } },
    };
    const { onAnswer, onAnswered } = renderCard(
      formQuestion(
        schema,
        {
          values: {},
          options: {
            colour: [
              { value: 'r', label: 'Red' },
              { value: 'b', label: 'Blue' },
            ],
          },
        },
        null,
      ),
    );

    // Labels are shown; the values ride the submission.
    expect(screen.getByRole('option', { name: 'Red' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'Blue' })).toBeInTheDocument();
    await user.selectOptions(screen.getByRole('combobox'), 'b');
    await user.click(screen.getByRole('button', { name: 'Answer' }));

    await waitFor(() => expect(onAnswered).toHaveBeenCalled());
    expect(onAnswer).toHaveBeenCalledWith('int-1', { colour: 'b' });
  });

  it('steps through pages and submits the union of every step', async () => {
    const user = userEvent.setup();
    const schema: JsonSchema = {
      type: 'object',
      properties: { first: { type: 'string' }, second: { type: 'string' } },
    };
    const pages: readonly FormPage[] = [
      { title: 'Your name', fields: ['first'] },
      { title: 'Your note', fields: ['second'] },
    ];
    const { onAnswer, onAnswered } = renderCard(formQuestion(schema, null, pages));

    // Step 1: the progress line and only the first page's field.
    expect(screen.getByText(/Step 1 of 2 . Your name/)).toBeInTheDocument();
    await user.type(screen.getByRole('textbox'), 'Ada');
    await user.click(screen.getByRole('button', { name: 'Next' }));

    // Step 2: progress advances, Back appears, the button becomes Submit.
    expect(screen.getByText(/Step 2 of 2 . Your note/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Back' })).toBeInTheDocument();
    await user.type(screen.getByRole('textbox'), 'ship it');
    await user.click(screen.getByRole('button', { name: 'Submit' }));

    await waitFor(() => expect(onAnswered).toHaveBeenCalled());
    expect(onAnswer).toHaveBeenCalledWith('int-1', { first: 'Ada', second: 'ship it' });
  });

  it('moves focus to the shown step first control on Next and on Back', async () => {
    const user = userEvent.setup();
    const schema: JsonSchema = {
      type: 'object',
      properties: { first: { type: 'string' }, second: { type: 'string' } },
    };
    const pages: readonly FormPage[] = [
      { title: 'Your name', fields: ['first'] },
      { title: 'Your note', fields: ['second'] },
    ];
    renderCard(formQuestion(schema, null, pages));

    // Next: focus lands on the second step's control, not on the now-hidden first one.
    await user.click(screen.getByRole('button', { name: 'Next' }));
    const stepTwoInput = screen.getByRole('textbox');
    await waitFor(() => expect(stepTwoInput).toHaveFocus());

    // Back: focus lands on the first step's control.
    await user.click(screen.getByRole('button', { name: 'Back' }));
    const stepOneInput = screen.getByRole('textbox');
    await waitFor(() => expect(stepOneInput).toHaveFocus());
  });
});
