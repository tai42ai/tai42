import { cleanup, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { QuestionItem } from '@/question-card';
import { CALLBACK, externalQuestion, question, renderCard } from '@/question-card.test-support';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

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
