import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ChatApiError } from '@/api';
import { FormCard, type FormCardItem } from '@/form-card';
import type { FormPage, FormPrefill, JsonSchema, MediaItem, Wording } from '@/transcript-model';

afterEach(() => {
  cleanup();
});

const SCHEMA: JsonSchema = {
  type: 'object',
  properties: { note: { type: 'string', title: 'Note' } },
};

function formItem(overrides: Partial<FormCardItem> = {}): FormCardItem {
  return {
    kind: 'form',
    id: 'f1',
    text: 'Fill this in',
    schema: SCHEMA,
    token: 'tok-0123456789abcdef0123456789abcdef',
    media: null,
    location: null,
    formData: null,
    pages: null,
    wording: null,
    ts: new Date().toISOString(),
    ...overrides,
  };
}

/** A form card carrying an explicit schema plus the per-send prefill/steps — the
 * enrichment a tool's reply part rides onto the `chat.form` frame. */
function enrichedItem(
  schema: JsonSchema,
  formData: FormPrefill | null,
  pages: readonly FormPage[] | null,
): FormCardItem {
  return formItem({ schema, formData, pages });
}

function renderCard(item: FormCardItem = formItem(), overrides = {}) {
  const onSubmitForm = vi.fn().mockResolvedValue(undefined);
  render(<FormCard item={item} onSubmitForm={onSubmitForm} locked={false} {...overrides} />);
  return { onSubmitForm };
}

describe('FormCard', () => {
  it('renders the prompt, builds the values from the schema, and submits by token', async () => {
    const user = userEvent.setup();
    const { onSubmitForm } = renderCard();

    expect(screen.getByText('Fill this in')).toBeInTheDocument();
    await user.type(screen.getByRole('textbox'), 'ship it');
    await user.click(screen.getByRole('button', { name: 'Send' }));

    await waitFor(() =>
      expect(onSubmitForm).toHaveBeenCalledWith('tok-0123456789abcdef0123456789abcdef', {
        note: 'ship it',
      }),
    );
  });

  it('opens prefilled from the per-send data and submits the edited values', async () => {
    // The reporter's case: a reply-part form with prefill must render filled in, not
    // blank. The known value shows from first render; the visitor edits and sends it.
    const user = userEvent.setup();
    const { onSubmitForm } = renderCard(
      enrichedItem(SCHEMA, { values: { note: 'draft' }, options: {} }, null),
    );

    const field = screen.getByRole('textbox');
    expect(field).toHaveValue('draft');
    await user.clear(field);
    await user.type(field, 'ship it');
    await user.click(screen.getByRole('button', { name: 'Send' }));

    await waitFor(() =>
      expect(onSubmitForm).toHaveBeenCalledWith('tok-0123456789abcdef0123456789abcdef', {
        note: 'ship it',
      }),
    );
  });

  it('replaces a property choices with the per-send option list and posts the chosen value', async () => {
    const user = userEvent.setup();
    const schema: JsonSchema = {
      type: 'object',
      properties: { colour: { type: 'string', title: 'Colour' } },
    };
    const { onSubmitForm } = renderCard(
      enrichedItem(
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

    // Labels shown, values posted — the per-send list replaces the schema's choices.
    expect(screen.getByRole('option', { name: 'Red' })).toBeInTheDocument();
    await user.selectOptions(screen.getByRole('combobox'), 'b');
    await user.click(screen.getByRole('button', { name: 'Send' }));

    await waitFor(() =>
      expect(onSubmitForm).toHaveBeenCalledWith(expect.any(String), { colour: 'b' }),
    );
  });

  it('paginates a stepped form and sends the union of every step', async () => {
    const user = userEvent.setup();
    const schema: JsonSchema = {
      type: 'object',
      properties: { first: { type: 'string' }, second: { type: 'string' } },
    };
    const pages: readonly FormPage[] = [
      { title: 'Your name', fields: ['first'], display: [], kind: 'input', buttonLabel: null },
      { title: 'Your note', fields: ['second'], display: [], kind: 'input', buttonLabel: null },
    ];
    const { onSubmitForm } = renderCard(enrichedItem(schema, null, pages));

    // Step 1: the progress line and only the first page's field.
    expect(screen.getByText(/Step 1 of 2 . Your name/)).toBeInTheDocument();
    await user.type(screen.getByRole('textbox'), 'Ada');
    await user.click(screen.getByRole('button', { name: 'Next' }));

    // Step 2: progress advances, Back appears, and the terminal button still Sends.
    expect(screen.getByText(/Step 2 of 2 . Your note/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Back' })).toBeInTheDocument();
    await user.type(screen.getByRole('textbox'), 'ship it');
    await user.click(screen.getByRole('button', { name: 'Send' }));

    await waitFor(() =>
      expect(onSubmitForm).toHaveBeenCalledWith(expect.any(String), {
        first: 'Ada',
        second: 'ship it',
      }),
    );
  });

  it('renders today empty form unchanged when the card carries no per-send data', async () => {
    const user = userEvent.setup();
    const { onSubmitForm } = renderCard(enrichedItem(SCHEMA, null, null));

    // No prefill, no steps: a single blank field and the Send button.
    const field = screen.getByRole('textbox');
    expect(field).toHaveValue('');
    expect(screen.queryByText(/Step 1 of/)).not.toBeInTheDocument();
    await user.type(field, 'ship it');
    await user.click(screen.getByRole('button', { name: 'Send' }));

    await waitFor(() =>
      expect(onSubmitForm).toHaveBeenCalledWith(expect.any(String), { note: 'ship it' }),
    );
  });

  it('renders the card media through the shared media components', () => {
    const media: MediaItem[] = [
      { kind: 'image', url: 'https://example.com/a.png', caption: 'A shot', filename: null },
      { kind: 'link', url: 'https://docs.example/p', caption: 'The doc', filename: null },
    ];
    renderCard(formItem({ media }));

    expect(screen.getByRole('img', { name: 'A shot' })).toHaveAttribute(
      'src',
      'https://example.com/a.png',
    );
    const anchor = screen.getByRole('link', { name: 'The doc' });
    expect(anchor).toHaveAttribute('href', 'https://docs.example/p');
    // The form is still there — media is context, not a control.
    expect(screen.getByRole('textbox')).toBeInTheDocument();
  });

  it('settles into a local Sent badge and stays fillable — a resubmission is its own message', async () => {
    // The chips precedent: no durable per-card claimed state. The badge is this
    // page session's alone, and a second send goes through as a second message.
    const user = userEvent.setup();
    const { onSubmitForm } = renderCard();

    await user.type(screen.getByRole('textbox'), 'first');
    await user.click(screen.getByRole('button', { name: 'Send' }));
    await screen.findByText('Sent');

    expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled();
    await user.clear(screen.getByRole('textbox'));
    await user.type(screen.getByRole('textbox'), 'second');
    await user.click(screen.getByRole('button', { name: 'Send' }));

    await waitFor(() => expect(onSubmitForm).toHaveBeenCalledTimes(2));
    expect(onSubmitForm).toHaveBeenLastCalledWith(expect.any(String), { note: 'second' });
  });

  it('disables the button while a send is in flight — the double-click guard', async () => {
    const user = userEvent.setup();
    let settle = (): void => {};
    const onSubmitForm = vi.fn().mockReturnValue(
      new Promise<void>((resolve) => {
        settle = () => resolve();
      }),
    );
    render(<FormCard item={formItem()} onSubmitForm={onSubmitForm} locked={false} />);

    await user.type(screen.getByRole('textbox'), 'ship it');
    await user.click(screen.getByRole('button', { name: 'Send' }));

    expect(screen.getByRole('button')).toBeDisabled();
    settle();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled());
    expect(onSubmitForm).toHaveBeenCalledTimes(1);
  });

  it('shows the refusal on the card and keeps the form submittable', async () => {
    const user = userEvent.setup();
    const onSubmitForm = vi
      .fn()
      .mockRejectedValue(new ChatApiError("That form couldn't be sent.", 422, null));
    render(<FormCard item={formItem()} onSubmitForm={onSubmitForm} locked={false} />);

    await user.type(screen.getByRole('textbox'), 'ship it');
    await user.click(screen.getByRole('button', { name: 'Send' }));

    expect(await screen.findByRole('alert')).toHaveTextContent("That form couldn't be sent.");
    expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled();
  });

  it('says the form is no longer available on a 404 and withdraws the controls', async () => {
    // The token no longer resolves (expired, or never this conversation's):
    // retrying the same token can only 404 again, so no retry is offered. The
    // visitor can still type into the composer — only this card's controls go.
    const user = userEvent.setup();
    const onSubmitForm = vi.fn().mockRejectedValue(new ChatApiError('form not found', 404, null));
    render(<FormCard item={formItem()} onSubmitForm={onSubmitForm} locked={false} />);

    await user.type(screen.getByRole('textbox'), 'ship it');
    await user.click(screen.getByRole('button', { name: 'Send' }));

    expect(await screen.findByText('This form is no longer available.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Send' })).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('takes no submission while the page is locked out, and says why', () => {
    renderCard(formItem(), { locked: true });

    expect(screen.getByText('Session ended')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Send' })).not.toBeInTheDocument();
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  });

  it("draws the caller's terminal button, sent badge, session-ended badge and no-longer-available line", async () => {
    const user = userEvent.setup();
    const cardWording: Wording = {
      form: { submit: 'File it', back: null, updateFailed: null, stepProgress: null, fields: {} },
      choice: null,
      answer: null,
      status: {
        sending: null,
        answered: null,
        expired: null,
        closed: 'Chat over',
        sent: 'Filed',
        unavailable: 'That form is closed.',
        countdown: null,
      },
    };
    const onSubmitForm = vi.fn().mockResolvedValue(undefined);
    render(
      <FormCard
        item={formItem({ wording: cardWording })}
        onSubmitForm={onSubmitForm}
        locked={false}
      />,
    );

    await user.type(screen.getByRole('textbox'), 'ship it');
    await user.click(screen.getByRole('button', { name: 'File it' }));
    expect(await screen.findByText('Filed')).toBeInTheDocument();
  });

  it("draws the caller's session-ended badge when the page is locked", () => {
    const cardWording: Wording = {
      form: null,
      choice: null,
      answer: null,
      status: {
        sending: null,
        answered: null,
        expired: null,
        closed: 'Chat over',
        sent: null,
        unavailable: null,
        countdown: null,
      },
    };
    renderCard(formItem({ wording: cardWording }), { locked: true });
    expect(screen.getByText('Chat over')).toBeInTheDocument();
  });

  it("draws the caller's no-longer-available line on a 404", async () => {
    const user = userEvent.setup();
    const cardWording: Wording = {
      form: null,
      choice: null,
      answer: null,
      status: {
        sending: null,
        answered: null,
        expired: null,
        closed: null,
        sent: null,
        unavailable: 'That form is closed.',
        countdown: null,
      },
    };
    const onSubmitForm = vi.fn().mockRejectedValue(new ChatApiError('form not found', 404, null));
    render(
      <FormCard
        item={formItem({ wording: cardWording })}
        onSubmitForm={onSubmitForm}
        locked={false}
      />,
    );

    await user.type(screen.getByRole('textbox'), 'ship it');
    await user.click(screen.getByRole('button', { name: 'Send' }));
    expect(await screen.findByText('That form is closed.')).toBeInTheDocument();
  });
});

describe('FormCard — a date range with a time on each end', () => {
  // The times are declared LAST; each draws and reads back right after its own date.
  const TIMED: JsonSchema = {
    type: 'object',
    properties: {
      from: { type: 'string', format: 'date', title: 'From' },
      to: { type: 'string', format: 'date', title: 'To', rangeStart: 'from' },
      note: { type: 'string', title: 'Note' },
      from_time: { type: 'string', format: 'time', title: 'From time', timeOf: 'from' },
      to_time: { type: 'string', format: 'time', title: 'To time', timeOf: 'to' },
    },
  };

  function drawnLabels(): (string | undefined)[] {
    return Array.from(document.querySelectorAll('input')).map(
      (input) => input.labels?.[0]?.textContent ?? undefined,
    );
  }

  it('draws each time as its own text field right after its date', () => {
    renderCard(enrichedItem(TIMED, null, null));
    expect(drawnLabels()).toEqual(['From', 'From time', 'To', 'To time', 'Note']);
    expect(screen.getByLabelText('From time')).toHaveAttribute('type', 'text');
  });

  it('refuses an earlier same-day end time under the end time and sends nothing', async () => {
    const user = userEvent.setup();
    const values = { from: '2026-03-01', to: '2026-03-01', from_time: '10:00', to_time: '09:00' };
    const { onSubmitForm } = renderCard(enrichedItem(TIMED, { values, options: {} }, null));
    await user.click(screen.getByRole('button', { name: 'Send' }));
    expect(await screen.findByText('must be on or after the start time')).toBeInTheDocument();
    expect(screen.getByLabelText('To time')).toHaveAccessibleDescription(
      'must be on or after the start time',
    );
    expect(onSubmitForm).not.toHaveBeenCalled();
  });

  it('reads each time back right after its date on a review page', async () => {
    const user = userEvent.setup();
    const values = { from: '2026-03-01', to: '2026-03-02', from_time: '10:00', to_time: '09:00' };
    const pages: readonly FormPage[] = [
      {
        title: 'When',
        fields: ['from', 'to', 'note', 'from_time', 'to_time'],
        display: [],
        kind: 'input',
        buttonLabel: null,
      },
      { title: 'Review', fields: [], kind: 'review', display: [], buttonLabel: null },
    ];
    renderCard(enrichedItem(TIMED, { values, options: {} }, pages));
    expect(drawnLabels()).toEqual(['From', 'From time', 'To', 'To time', 'Note']);
    await user.click(screen.getByRole('button', { name: 'Next' }));
    const review = await screen.findByTestId('form-review');
    expect(Array.from(review.querySelectorAll('dt')).map((term) => term.textContent)).toEqual([
      'From',
      'From time',
      'To',
      'To time',
      'Note',
    ]);
  });
});
