import { cleanup, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { mountRoot, okUpdate, reactions, stubFetch } from '@/form-reaction.test-support';
import { formQuestion, renderCard } from '@/question-card.test-support';
import type { FormOptionData, FormPage, JsonSchema } from '@/transcript-model';

const TAGS: JsonSchema = {
  type: 'object',
  properties: { tags: { type: 'array', title: 'Tags', items: { type: 'string' } } },
};

const CHOICES: readonly FormOptionData[] = [
  { value: 'a', label: 'Alpha' },
  { value: 'b', label: 'Beta' },
  { value: 'c', label: 'Gamma' },
];

/** A reaction's replacement list: keeps Beta, drops Alpha and Gamma, adds Delta. */
const REPLACED: readonly FormOptionData[] = [
  { value: 'b', label: 'Beta' },
  { value: 'd', label: 'Delta' },
];

afterEach(() => {
  cleanup();
  document.getElementById('root')?.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

beforeEach(() => {
  mountRoot();
});

describe('SchemaFormAnswer multi-choice field', () => {
  it('draws an array field with a choice list as a checkbox group named by its title', () => {
    renderCard(formQuestion(TAGS, { values: {}, options: { tags: CHOICES } }, null));

    const group = screen.getByRole('group', { name: 'Tags' });
    expect(within(group).getAllByRole('checkbox')).toHaveLength(3);
    expect(within(group).getByRole('checkbox', { name: 'Alpha' })).not.toBeChecked();
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
  });

  it('posts the checked values in the choices order, not the click order', async () => {
    const user = userEvent.setup();
    const { onAnswer } = renderCard(
      formQuestion(TAGS, { values: {}, options: { tags: CHOICES } }, null),
    );

    await user.click(screen.getByRole('checkbox', { name: 'Gamma' }));
    await user.click(screen.getByRole('checkbox', { name: 'Alpha' }));
    await user.click(screen.getByRole('button', { name: 'Answer' }));

    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith('int-1', { tags: ['a', 'c'] }));
  });

  it('posts an empty list once the last checked box is unchecked', async () => {
    const user = userEvent.setup();
    const { onAnswer } = renderCard(
      formQuestion(TAGS, { values: {}, options: { tags: CHOICES } }, null),
    );

    await user.click(screen.getByRole('checkbox', { name: 'Beta' }));
    await user.click(screen.getByRole('checkbox', { name: 'Beta' }));
    await user.click(screen.getByRole('button', { name: 'Answer' }));

    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith('int-1', { tags: [] }));
  });

  it('advances a step past checked boxes and posts them on Submit', async () => {
    const user = userEvent.setup();
    const schema: JsonSchema = {
      type: 'object',
      properties: { ...TAGS.properties, note: { type: 'string', title: 'Note' } },
    };
    const pages: readonly FormPage[] = [
      { title: 'Pick', fields: ['tags'], display: [], kind: 'input', buttonLabel: null },
      { title: 'Say', fields: ['note'], display: [], kind: 'input', buttonLabel: null },
    ];
    const { onAnswer } = renderCard(
      formQuestion(schema, { values: {}, options: { tags: CHOICES } }, pages),
    );

    await user.click(screen.getByRole('checkbox', { name: 'Alpha' }));
    await user.click(screen.getByRole('checkbox', { name: 'Beta' }));
    await user.click(screen.getByRole('button', { name: 'Next' }));
    await user.type(await screen.findByRole('textbox', { name: 'Note' }), 'hi');
    await user.click(screen.getByRole('button', { name: 'Submit' }));

    await waitFor(() =>
      expect(onAnswer).toHaveBeenCalledWith('int-1', { tags: ['a', 'b'], note: 'hi' }),
    );
  });

  it('shows only the prefilled values that are among the choices as checked', () => {
    renderCard(
      formQuestion(TAGS, { values: { tags: ['b', 'zz'] }, options: { tags: CHOICES } }, null),
    );

    expect(screen.getByRole('checkbox', { name: 'Beta' })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: 'Alpha' })).not.toBeChecked();
    expect(screen.getByRole('checkbox', { name: 'Gamma' })).not.toBeChecked();
    expect(screen.getAllByRole('checkbox')).toHaveLength(3);
  });

  it('toggles a box from the keyboard: Tab reaches it, Space checks it', async () => {
    const user = userEvent.setup();
    renderCard(formQuestion(TAGS, { values: {}, options: { tags: CHOICES } }, null));

    const first = screen.getByRole('checkbox', { name: 'Alpha' });
    while (document.activeElement !== first) await user.tab();
    await user.keyboard(' ');
    expect(first).toBeChecked();
    await user.tab();
    expect(screen.getByRole('checkbox', { name: 'Beta' })).toHaveFocus();
  });

  it('shows the field error under the group', async () => {
    const user = userEvent.setup();
    const schema: JsonSchema = {
      type: 'object',
      properties: {
        tags: { type: 'array', title: 'Tags', items: { type: 'string' }, minItems: 1 },
      },
      required: ['tags'],
    };
    const { onAnswer } = renderCard(
      formQuestion(schema, { values: {}, options: { tags: CHOICES } }, null),
    );

    await user.click(screen.getByRole('button', { name: 'Answer' }));

    const group = screen.getByRole('group', { name: 'Tags' });
    expect(within(group).getByRole('alert')).toHaveTextContent('must have at least 1 item(s)');
    expect(onAnswer).not.toHaveBeenCalled();
  });

  it("redraws a reaction's replaced choices and drops a checked value they no longer offer", async () => {
    const user = userEvent.setup();
    stubFetch(okUpdate({ options: { tags: REPLACED } }));
    const schema: JsonSchema = {
      type: 'object',
      properties: { qty: { type: 'string', title: 'Qty' }, ...TAGS.properties },
    };
    const { onAnswer } = renderCard(
      formQuestion(
        schema,
        { values: {}, options: { tags: CHOICES } },
        null,
        reactions({ fieldChanged: ['qty'], choices: ['tags'] }),
      ),
    );

    await user.click(screen.getByRole('checkbox', { name: 'Alpha' }));
    await user.click(screen.getByRole('checkbox', { name: 'Beta' }));
    await user.type(screen.getByRole('textbox', { name: 'Qty' }), 'x');

    await waitFor(() =>
      expect(screen.getByRole('checkbox', { name: 'Delta' })).toBeInTheDocument(),
    );
    expect(screen.queryByRole('checkbox', { name: 'Alpha' })).not.toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: 'Beta' })).toBeChecked();
    await user.click(screen.getByRole('button', { name: 'Answer' }));

    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith('int-1', { qty: 'x', tags: ['b'] }));
  });
  it('posts the checked values a submit reaction still offers', async () => {
    const user = userEvent.setup();
    stubFetch(okUpdate({ options: { tags: REPLACED } }));
    const { onAnswer } = renderCard(
      formQuestion(
        TAGS,
        { values: { tags: ['a', 'b'] }, options: { tags: CHOICES } },
        null,
        reactions({ submitted: true, choices: ['tags'] }),
      ),
    );

    await user.click(screen.getByRole('button', { name: 'Answer' }));

    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith('int-1', { tags: ['b'] }));
  });

  it("holds the form when a submit reaction's choices leave too few boxes checked", async () => {
    const user = userEvent.setup();
    stubFetch(okUpdate({ options: { tags: REPLACED } }));
    const schema: JsonSchema = {
      type: 'object',
      properties: {
        tags: { type: 'array', title: 'Tags', items: { type: 'string' }, minItems: 2 },
      },
      required: ['tags'],
    };
    const { onAnswer } = renderCard(
      formQuestion(
        schema,
        { values: { tags: ['a', 'b'] }, options: { tags: CHOICES } },
        null,
        reactions({ submitted: true, choices: ['tags'] }),
      ),
    );

    await user.click(screen.getByRole('button', { name: 'Answer' }));

    const group = await screen.findByRole('group', { name: 'Tags' });
    await waitFor(() =>
      expect(within(group).getByRole('alert')).toHaveTextContent('must have at least 2 item(s)'),
    );
    expect(screen.queryByText('must be one of the allowed values')).not.toBeInTheDocument();
    expect(onAnswer).not.toHaveBeenCalled();
  });
});
