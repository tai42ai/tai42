import { cleanup, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { formQuestion, renderCard } from '@/question-card.test-support';
import type { FieldWording, FormPage, FormWording, JsonSchema, Wording } from '@/transcript-model';

const REACTION_ENDPOINT = '/api/channels/web/questions/{interaction_id}/react';

/** A field's words with only the given members set. */
function fieldWords(partial: Partial<FieldWording>): FieldWording {
  return {
    choiceLabel: null,
    invalid: null,
    trueLabel: null,
    falseLabel: null,
    required: null,
    addItem: null,
    removeItem: null,
    itemLabel: null,
    noItems: null,
    ...partial,
  };
}

/** A `Wording` whose form chapter carries only the given members. */
function formWording(partial: Partial<FormWording>): Wording {
  return {
    form: {
      submit: null,
      back: null,
      updateFailed: null,
      stepProgress: null,
      fields: {},
      ...partial,
    },
    choice: null,
    answer: null,
    status: null,
  };
}

/** A reaction door refusal; `reason` rides the envelope when given. */
function doorError(status: number, error: string, reason: string | null): Response {
  const body = reason === null ? { error } : { error, reason };
  return { ok: false, status, text: async () => JSON.stringify(body) } as Response;
}

const QTY: JsonSchema = { type: 'object', properties: { qty: { type: 'string', title: 'Qty' } } };
const REACTS = { fieldChanged: ['qty'], pageAdvanced: [], submitted: false, choices: [] };

beforeEach(() => {
  const root = document.createElement('div');
  root.id = 'root';
  root.dataset.reactionEndpoint = REACTION_ENDPOINT;
  document.body.appendChild(root);
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  cleanup();
  document.getElementById('root')?.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('the could-not-update line', () => {
  it("draws the caller's line with the door's own reason", async () => {
    const user = userEvent.setup();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(doorError(400, 'qty is odd', 'qty is odd')));
    const wording = formWording({ updateFailed: 'No update ({reason})' });
    renderCard(formQuestion(QTY, null, null, REACTS, wording));

    await user.type(screen.getByRole('textbox'), 'x');

    await waitFor(() =>
      expect(screen.getByTestId('form-reaction-error')).toHaveTextContent('No update (qty is odd)'),
    );
  });

  it('fills {reason} with nothing when the door gave none, never the widget English', async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(doorError(502, 'the reaction was refused', null)),
    );
    const wording = formWording({ updateFailed: 'No update.{reason}' });
    renderCard(formQuestion(QTY, null, null, REACTS, wording));

    await user.type(screen.getByRole('textbox'), 'x');

    await waitFor(() =>
      expect(screen.getByTestId('form-reaction-error')).toHaveTextContent(/^No update\.$/),
    );
  });

  it("keeps the widget's own line when the send set none", async () => {
    const user = userEvent.setup();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(doorError(400, 'qty is odd', 'qty is odd')));
    renderCard(formQuestion(QTY, null, null, REACTS));

    await user.type(screen.getByRole('textbox'), 'x');

    await waitFor(() =>
      expect(screen.getByTestId('form-reaction-error')).toHaveTextContent(
        "The form could not update: That form couldn't be sent.",
      ),
    );
  });
});

describe('the step-progress line', () => {
  const schema: JsonSchema = {
    type: 'object',
    properties: { name: { type: 'string' }, note: { type: 'string' } },
  };
  const pages: readonly FormPage[] = [
    { title: 'Name', fields: ['name'], display: [], kind: 'input', buttonLabel: null },
    { title: 'Note', fields: ['note'], display: [], kind: 'input', buttonLabel: null },
  ];

  it("draws the caller's line with {step} and {steps} filled, then the page title", () => {
    renderCard(
      formQuestion(schema, null, pages, null, formWording({ stepProgress: 'Page {step}/{steps}' })),
    );

    expect(screen.getByText('Page 1/2 · Name')).toHaveClass('tcw-form-progress');
  });

  it("keeps the widget's own line when the send set none", () => {
    renderCard(formQuestion(schema, null, pages, null));

    expect(screen.getByText('Step 1 of 2 · Name')).toHaveClass('tcw-form-progress');
  });
});

describe('the review readback of a yes/no field', () => {
  const schema: JsonSchema = {
    type: 'object',
    properties: {
      agree: { type: 'boolean', title: 'Agree' },
      news: { type: 'boolean', title: 'News' },
    },
  };
  const pages: readonly FormPage[] = [
    { title: 'Ask', fields: ['agree', 'news'], display: [], kind: 'input', buttonLabel: null },
    { title: 'Review', fields: [], display: [], kind: 'review', buttonLabel: null },
  ];
  const values = { values: { agree: true, news: false }, options: {} };

  it("reads each value back in the field's own words", async () => {
    const user = userEvent.setup();
    const wording = formWording({
      fields: {
        agree: fieldWords({ trueLabel: 'Agreed', falseLabel: 'Declined' }),
        news: fieldWords({ trueLabel: 'Subscribed', falseLabel: 'Not subscribed' }),
      },
    });
    renderCard(formQuestion(schema, values, pages, null, wording));

    await user.click(screen.getByRole('button', { name: 'Next' }));

    const review = await screen.findByTestId('form-review');
    expect(Array.from(review.querySelectorAll('dd')).map((cell) => cell.textContent)).toEqual([
      'Agreed',
      'Not subscribed',
    ]);
  });

  it('reads Yes and No when the field set no words', async () => {
    const user = userEvent.setup();
    renderCard(formQuestion(schema, values, pages, null));

    await user.click(screen.getByRole('button', { name: 'Next' }));

    const review = await screen.findByTestId('form-review');
    expect(Array.from(review.querySelectorAll('dd')).map((cell) => cell.textContent)).toEqual([
      'Yes',
      'No',
    ]);
  });
});
