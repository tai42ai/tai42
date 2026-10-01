import { act, cleanup, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { formQuestion, renderCard } from '@/question-card.test-support';
import type { DisplayBlock, FormPage, FormReactions, JsonSchema } from '@/transcript-model';

// The reaction-door URL template the page door writes onto `#root` — the per-interaction
// react sibling of this plugin's own answer door, with the `{interaction_id}` placeholder.
const REACTION_ENDPOINT = '/api/channels/web/questions/{interaction_id}/react';

/** A mount carrying the reaction-endpoint template, so `reactToForm` finds its door. */
function mountRoot(endpoint: string | null = REACTION_ENDPOINT): void {
  const root = document.createElement('div');
  root.id = 'root';
  if (endpoint !== null) root.dataset.reactionEndpoint = endpoint;
  document.body.appendChild(root);
}

/** A reaction door 2xx carrying a form update under the platform `{data}` envelope. */
function okUpdate(update: Record<string, unknown>): Response {
  return { ok: true, status: 200, text: async () => JSON.stringify({ data: update }) } as Response;
}

/** A reaction door refusal carrying the platform `{error}` envelope. */
function doorError(status: number, error: string): Response {
  return { ok: false, status, text: async () => JSON.stringify({ error }) } as Response;
}

/** A full reactions declaration from the parts a case cares about. */
function reactions(overrides: Partial<FormReactions> = {}): FormReactions {
  return { fieldChanged: [], pageAdvanced: [], submitted: false, choices: [], ...overrides };
}

/** A display block with every optional field explicit (as the parser yields it). */
function block(partial: Partial<DisplayBlock> & { kind: DisplayBlock['kind'] }): DisplayBlock {
  return { text: null, src: null, alt: null, slot: null, ...partial };
}

function stubFetch(response: Response): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn().mockResolvedValue(response);
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

/** A promise a case resolves by hand, so two reaction round-trips can be made to
 * settle in a chosen order regardless of the order they were fired. */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

/** Drain the microtask queue enough times for a settled reaction round-trip
 * (fetch → read body → apply) to run all its continuations. */
async function flushMicrotasks(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 10; i += 1) await Promise.resolve();
  });
}

afterEach(() => {
  cleanup();
  document.getElementById('root')?.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

beforeEach(() => {
  mountRoot();
});

describe('SchemaFormAnswer reactions', () => {
  it('rounds-trips a field_changed reaction to the plugin door and applies the display update', async () => {
    const user = userEvent.setup();
    const fetchMock = stubFetch(okUpdate({ display: { total: 42 } }));
    const schema: JsonSchema = {
      type: 'object',
      properties: { qty: { type: 'string', title: 'Qty' } },
    };
    const pages: readonly FormPage[] = [
      {
        title: '',
        fields: ['qty'],
        display: [block({ kind: 'body', slot: 'total' })],
        kind: 'input',
      },
    ];
    renderCard(formQuestion(schema, null, pages, reactions({ fieldChanged: ['qty'] })));

    await user.type(screen.getByRole('textbox'), 'x');

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/channels/web/questions/int-1/react');
    expect(init.method).toBe('POST');
    const body = JSON.parse(init.body as string) as { event: unknown; values: unknown };
    expect(body.event).toEqual({ kind: 'field_changed', field: 'qty' });
    expect(body.values).toEqual({ qty: 'x' });
    // The returned display slot is filled — a computed total, shown without a reload.
    await waitFor(() => expect(screen.getByTestId('form-slot-total')).toHaveTextContent('42'));
  });

  it('replaces a field choice list from a reaction update', async () => {
    const user = userEvent.setup();
    stubFetch(okUpdate({ options: { colour: [{ value: 'g', label: 'Green' }] } }));
    const schema: JsonSchema = {
      type: 'object',
      properties: {
        qty: { type: 'string', title: 'Qty' },
        colour: { type: 'string', title: 'Colour' },
      },
    };
    renderCard(
      formQuestion(
        schema,
        { values: {}, options: { colour: [{ value: 'r', label: 'Red' }] } },
        null,
        reactions({ fieldChanged: ['qty'], choices: ['colour'] }),
      ),
    );

    expect(screen.getByRole('option', { name: 'Red' })).toBeInTheDocument();
    await user.type(screen.getByRole('textbox'), 'x');

    // The reaction's choice list REPLACES the send's: Green in, Red gone.
    await waitFor(() => expect(screen.getByRole('option', { name: 'Green' })).toBeInTheDocument());
    expect(screen.queryByRole('option', { name: 'Red' })).not.toBeInTheDocument();
  });

  it('holds the form open when a submitted reaction returns per-field errors', async () => {
    const user = userEvent.setup();
    const fetchMock = stubFetch(okUpdate({ errors: { note: 'too short' } }));
    const schema: JsonSchema = {
      type: 'object',
      properties: { note: { type: 'string', title: 'Note' } },
    };
    const { onAnswer } = renderCard(
      formQuestion(
        schema,
        { values: { note: 'hi' }, options: {} },
        null,
        reactions({ submitted: true }),
      ),
    );

    await user.click(screen.getByRole('button', { name: 'Answer' }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const body = JSON.parse(
      (fetchMock.mock.calls[0] as [string, RequestInit])[1].body as string,
    ) as {
      event: unknown;
    };
    expect(body.event).toEqual({ kind: 'submitted' });
    await waitFor(() => expect(screen.getByText('too short')).toBeInTheDocument());
    expect(onAnswer).not.toHaveBeenCalled();
  });

  it('submits the answer once a submitted reaction accepts it', async () => {
    const user = userEvent.setup();
    const fetchMock = stubFetch(okUpdate({}));
    const schema: JsonSchema = {
      type: 'object',
      properties: { note: { type: 'string', title: 'Note' } },
    };
    const { onAnswer } = renderCard(
      formQuestion(
        schema,
        { values: { note: 'hi' }, options: {} },
        null,
        reactions({ submitted: true }),
      ),
    );

    await user.click(screen.getByRole('button', { name: 'Answer' }));

    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith('int-1', { note: 'hi' }));
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('rounds-trips a page_advanced reaction before turning the step', async () => {
    const user = userEvent.setup();
    const fetchMock = stubFetch(okUpdate({ values: { second: 'filled' } }));
    const schema: JsonSchema = {
      type: 'object',
      properties: {
        first: { type: 'string', title: 'First' },
        second: { type: 'string', title: 'Second' },
      },
    };
    const pages: readonly FormPage[] = [
      { title: 'One', fields: ['first'], display: [], kind: 'input' },
      { title: 'Two', fields: ['second'], display: [], kind: 'input' },
    ];
    renderCard(formQuestion(schema, null, pages, reactions({ pageAdvanced: ['One'] })));

    await user.type(screen.getByRole('textbox'), 'a');
    await user.click(screen.getByRole('button', { name: 'Next' }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const body = JSON.parse(
      (fetchMock.mock.calls[0] as [string, RequestInit])[1].body as string,
    ) as {
      event: unknown;
    };
    expect(body.event).toEqual({ kind: 'page_advanced', page: 'One' });
    // The step turned, and the reaction's value landed on the next step's field.
    await waitFor(() => expect(screen.getByText(/Step 2 of 2 . Two/)).toBeInTheDocument());
    expect(screen.getByRole('textbox')).toHaveValue('filled');
  });

  it('surfaces a failed reaction loudly and does not apply a stale value', async () => {
    const user = userEvent.setup();
    stubFetch(doorError(502, 'the form helper timed out'));
    const schema: JsonSchema = {
      type: 'object',
      properties: { qty: { type: 'string', title: 'Qty' } },
    };
    renderCard(formQuestion(schema, null, null, reactions({ fieldChanged: ['qty'] })));

    await user.type(screen.getByRole('textbox'), 'x');

    await waitFor(() => expect(screen.getByTestId('form-reaction-error')).toBeInTheDocument());
  });

  it('surfaces a malformed reaction update loudly', async () => {
    const user = userEvent.setup();
    stubFetch(okUpdate({ values: 'not-an-object' }));
    const schema: JsonSchema = {
      type: 'object',
      properties: { qty: { type: 'string', title: 'Qty' } },
    };
    renderCard(formQuestion(schema, null, null, reactions({ fieldChanged: ['qty'] })));

    await user.type(screen.getByRole('textbox'), 'x');

    await waitFor(() =>
      expect(screen.getByTestId('form-reaction-error')).toHaveTextContent(/malformed form update/),
    );
  });

  it('surfaces a missing reaction endpoint loudly rather than guessing a door', async () => {
    const user = userEvent.setup();
    document.getElementById('root')?.removeAttribute('data-reaction-endpoint');
    stubFetch(okUpdate({}));
    const schema: JsonSchema = {
      type: 'object',
      properties: { qty: { type: 'string', title: 'Qty' } },
    };
    renderCard(formQuestion(schema, null, null, reactions({ fieldChanged: ['qty'] })));

    await user.type(screen.getByRole('textbox'), 'x');

    await waitFor(() =>
      expect(screen.getByTestId('form-reaction-error')).toHaveTextContent(/data-reaction-endpoint/),
    );
  });

  it('a static form (no reactions) never calls the reaction door', async () => {
    const user = userEvent.setup();
    const fetchMock = stubFetch(okUpdate({}));
    const { onAnswer } = renderCard(
      formQuestion(
        { type: 'object', properties: { note: { type: 'string', title: 'Note' } } },
        { values: { note: 'hi' }, options: {} },
        null,
        null,
      ),
    );

    await user.type(screen.getByRole('textbox'), 'y');
    await user.click(screen.getByRole('button', { name: 'Answer' }));

    await waitFor(() => expect(onAnswer).toHaveBeenCalled());
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('applies only the latest reaction when an older response resolves after a newer one', async () => {
    const user = userEvent.setup();
    // Two reaction doors held open by hand: the first fired (older) and the second
    // fired (newer). The test resolves the NEWER one first and the OLDER one last.
    const older = deferred<Response>();
    const newer = deferred<Response>();
    const fetchMock = vi.fn().mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise);
    vi.stubGlobal('fetch', fetchMock);
    const schema: JsonSchema = {
      type: 'object',
      properties: {
        qty: { type: 'string', title: 'Qty' },
        colour: { type: 'string', title: 'Colour' },
      },
    };
    const pages: readonly FormPage[] = [
      {
        title: '',
        fields: ['qty', 'colour'],
        display: [block({ kind: 'body', slot: 'total' })],
        kind: 'input',
      },
    ];
    renderCard(
      formQuestion(
        schema,
        { values: {}, options: { colour: [{ value: 'r', label: 'Red' }] } },
        pages,
        reactions({ fieldChanged: ['qty'], choices: ['colour'] }),
      ),
    );

    // Two field changes fire two reactions, both still in flight: the first keystroke
    // captures the older sequence, the second the newer.
    await user.type(screen.getByRole('textbox'), 'a');
    await user.type(screen.getByRole('textbox'), 'b');
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

    // The NEWER response lands first and its update is applied.
    newer.resolve(
      okUpdate({
        display: { total: 'newer' },
        options: { colour: [{ value: 'n', label: 'Newer' }] },
      }),
    );
    await waitFor(() => expect(screen.getByTestId('form-slot-total')).toHaveTextContent('newer'));
    expect(screen.getByRole('option', { name: 'Newer' })).toBeInTheDocument();

    // The OLDER response lands LATER and must be DISCARDED — a superseded round-trip
    // never overwrites the latest the visitor already sees.
    older.resolve(
      okUpdate({
        display: { total: 'older' },
        options: { colour: [{ value: 'o', label: 'Older' }] },
      }),
    );
    await flushMicrotasks();
    expect(screen.getByTestId('form-slot-total')).toHaveTextContent('newer');
    expect(screen.getByRole('option', { name: 'Newer' })).toBeInTheDocument();
    expect(screen.queryByText('older')).not.toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'Older' })).not.toBeInTheDocument();
  });
});

describe('SchemaFormAnswer display + review', () => {
  it('renders a page heading and body display blocks', () => {
    const schema: JsonSchema = {
      type: 'object',
      properties: { note: { type: 'string', title: 'Note' } },
    };
    const pages: readonly FormPage[] = [
      {
        title: '',
        fields: ['note'],
        display: [
          block({ kind: 'heading', text: 'Welcome' }),
          block({ kind: 'body', text: 'Please fill in' }),
        ],
        kind: 'input',
      },
    ];
    renderCard(formQuestion(schema, null, pages, null));

    expect(screen.getByText('Welcome')).toBeInTheDocument();
    expect(screen.getByText('Please fill in')).toBeInTheDocument();
  });

  it('renders a sourced display image and degrades a source-less one to its alt text', () => {
    const schema: JsonSchema = {
      type: 'object',
      properties: { note: { type: 'string', title: 'Note' } },
    };
    const pages: readonly FormPage[] = [
      {
        title: '',
        fields: ['note'],
        display: [
          block({ kind: 'image', src: 'https://example.com/a.png', alt: 'A shot' }),
          block({ kind: 'image', alt: 'Only alt' }),
        ],
        kind: 'input',
      },
    ];
    renderCard(formQuestion(schema, null, pages, null));

    // The sourced image renders through the scheme-gated image; the source-less one
    // degrades to its alt text as body rather than a broken image.
    expect(screen.getByRole('img', { name: 'A shot' })).toBeInTheDocument();
    expect(screen.getByText('Only alt')).toBeInTheDocument();
  });

  it('renders a review step readback of entered values', async () => {
    const user = userEvent.setup();
    const schema: JsonSchema = {
      type: 'object',
      properties: { name: { type: 'string', title: 'Name' } },
    };
    const pages: readonly FormPage[] = [
      { title: 'Fill', fields: ['name'], display: [], kind: 'input' },
      { title: 'Review', fields: [], display: [], kind: 'review' },
    ];
    renderCard(formQuestion(schema, { values: { name: 'Ada' }, options: {} }, pages, null));

    await user.click(screen.getByRole('button', { name: 'Next' }));

    const review = await screen.findByTestId('form-review');
    expect(review).toHaveTextContent('Name');
    expect(review).toHaveTextContent('Ada');
  });

  it('hides a field whose visibleWhen predicate is unmet and shows it once met', async () => {
    const user = userEvent.setup();
    const schema: JsonSchema = {
      type: 'object',
      properties: {
        trigger: { type: 'string', title: 'Trigger' },
        dependent: {
          type: 'string',
          title: 'Dependent',
          visibleWhen: { field: 'trigger', equals: 'go' },
        },
      },
    };
    renderCard(formQuestion(schema, null, null, null));

    expect(screen.queryByText('Dependent')).not.toBeInTheDocument();
    await user.type(screen.getByRole('textbox', { name: 'Trigger' }), 'go');
    await waitFor(() => expect(screen.getByText('Dependent')).toBeInTheDocument());
  });
});
