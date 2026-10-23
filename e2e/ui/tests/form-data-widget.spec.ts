/**
 * Per-send form data + pages — the web WIDGET leg of the composed path.
 *
 * The platform + channel halves are proven elsewhere: the callback-form-page path in
 * `e2e/tests/interactions/test_form_data_pages.py` (a real ask → the server-rendered page
 * → the union POST → the resolved ask) and the channel adapter tests. THIS leg proves the
 * remaining seam a web participant's traffic actually takes: the REAL widget bundle, served by
 * the skeleton at the channel's public chat page, rendering a REAL web-channel delivery.
 *
 * A blocking `ask(channel="web", answer_format="form", data=..., pages=...)` is fired
 * at the browser visitor's own conversation (its server-side visitor id read from the SUT
 * Redis — the web channel never discloses it to the client, exactly as the pytest harness's
 * `registered_visitor_id` reads it). The question streams into the widget, which shows the
 * prefilled values, the per-send option labels in a native `<select>`, and the pages as
 * steps ("Step 1 of 2 · Basics"); stepping through and submitting resolves the ask with the
 * union of every page's fields — asserted through the run-tool door the ask blocked on.
 */
import { mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { expect, type APIRequestContext, type APIResponse, type Locator, type Page } from '@playwright/test';

import { apiHeaders, mintKey, resolveWebVisitorId, uniq, WEB_SESSION_COOKIE } from './helpers';
import { needs, test } from './needs';

needs('kind:channels:web', 'kind:interactions', 'store:redis');

/** Where the widget shots land: `TAI_E2E_SHOTS_DIR` when set, else a per-suite output
 * dir beside the tests (untracked). */
const SHOTS_DIR =
  process.env.TAI_E2E_SHOTS_DIR ?? fileURLToPath(new URL('../form-shots-output', import.meta.url));
mkdirSync(SHOTS_DIR, { recursive: true });

/** The question card rises in on a `tcw-rise` opacity/translate animation
 * (`--tai-motion-base`, 250ms) carried by its enclosing `.tcw-row`. Shoot only
 * once that has fully settled, or the frame catches the card mid-fade: wait for
 * the row to reach full opacity, then a short grace for the paint to flush. */
async function settleCardEntry(target: Page, card: Locator): Promise<void> {
  const row = target.locator('.tcw-row', { has: card });
  await expect
    .poll(() => row.evaluate((el) => getComputedStyle(el).opacity), { timeout: 5_000 })
    .toBe('1');
  await target.waitForTimeout(300);
}

/** The channel-deliverable answer schema. Two string fields carry an `inputMode` so the widget
 * draws the matching HTML keyboard — `contact` the email control, `code` the numeric one (its
 * value stays the typed string, never coerced). `colour` carries `control: chips` over a schema
 * enum (the SDK draws a chip group); `start`/`end` are a `rangeStart` date pair (the SDK draws one
 * range group of two labelled date inputs); `notes` carries `control: textarea` with a drawn
 * `maxLength` (the SDK draws a multi-line text control). Abstract fields only. */
const SCHEMA = {
  type: 'object',
  required: [] as string[],
  properties: {
    date: { type: 'string', title: 'Date' },
    count: { type: 'integer', title: 'Count' },
    contact: { type: 'string', title: 'Contact', inputMode: 'email' },
    code: { type: 'string', title: 'Code', inputMode: 'numeric' },
    colour: { type: 'string', title: 'Colour', enum: ['red', 'green', 'blue'], control: 'chips' },
    start: { type: 'string', title: 'Start', format: 'date' },
    end: { type: 'string', title: 'End', format: 'date', rangeStart: 'start', minDays: 1, maxDays: 30 },
    notes: { type: 'string', title: 'Notes', control: 'textarea', maxLength: 500 },
  },
};

/** Per-send enrichment: `date` is prefilled to an option value and carries a per-send
 * label list; `count` is prefilled to a known integer. The visitor fills the rest. */
const DATA = {
  values: { date: 'a', count: 3 },
  options: { date: [{ value: 'a', label: 'Option A' }, { value: 'b', label: 'Option B' }] },
};

/** Two ordered steps, each carrying its own caller `button_label` (the page's primary button):
 * every top-level property appears exactly once across the pages. */
const PAGES = [
  {
    title: 'Basics',
    fields: ['date', 'count', 'contact', 'code', 'colour', 'start', 'end'],
    button_label: 'Next step',
  },
  { title: 'Extras', fields: ['notes'], button_label: 'Send it all' },
];

test('a per-send form with data + pages renders in the widget, steps, and resolves with the union', async ({
  page,
  request,
  browserName,
}) => {
  // The widget's SSE feed is a fetch-stream; CI's Linux WebKit delivers those unreliably
  // (as the interactions inbox spec documents), so this stream-dependent flow flakes only
  // there. Chromium + Firefox cover the stream path.
  test.skip(browserName === 'webkit', 'Linux CI WebKit delivers fetch-streams unreliably');

  const identity = uniq('formsite').replace(/_/g, '-');
  const question = uniq('form_q');
  const notes = uniq('notes');
  const contact = 'alice@example.com';
  const code = '007';
  const start = '2026-02-01';
  const end = '2026-02-04';
  const answer = { date: 'a', count: 3, contact, code, colour: 'green', start, end, notes };

  // Open the standalone chat page: the skeleton serves the widget bundle and the page door
  // mints + registers this visitor's session (the `tai_web_session` cookie).
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: 'light' });
  await page.goto(`/api/channels/web/chat/${identity}`);

  // The visitor's session cookie carries a secret token only; resolve the server-side
  // visitor id it is registered against — the address a web ask names its recipient by.
  const cookies = await page.context().cookies();
  const token = cookies.find((c) => c.name === WEB_SESSION_COOKIE)?.value;
  expect(token, 'the chat page minted no web-session cookie').toBeTruthy();
  const visitorId = await resolveWebVisitorId(token as string);

  // Fire the blocking web-channel form ask at this visitor WITHOUT awaiting: the run-tool
  // call parks until the widget submits the answer below.
  const askPromise = request.post('/api/run-tool', {
    headers: apiHeaders(),
    timeout: 90_000,
    data: {
      tool_name: 'ask',
      arguments: {
        question,
        channel: 'web',
        recipient: `${identity}:${visitorId}`,
        answer_format: 'form',
        schema: SCHEMA,
        data: DATA,
        pages: PAGES,
        timeout: 120,
      },
    },
  });

  // The question streams into the widget as a stepped form on its first page. Scope every
  // control to the form card: the page also carries a message composer (a textbox) that
  // must never be mistaken for the form's own fields.
  const card = page.locator('.tcw-question-form');
  await expect(card.getByText(/Step 1 of 2 . Basics/)).toBeVisible();
  // Prefill: the known values are shown filled in from first render.
  await expect(card.getByRole('spinbutton')).toHaveValue('3');
  await expect(card.getByRole('combobox')).toHaveValue('a');
  // The per-send option list renders as a labelled native select (labels shown, values
  // posted), REPLACING the property's schema choices for this send.
  await expect(card.getByRole('option', { name: 'Option A' })).toHaveAttribute('value', 'a');
  await expect(card.getByRole('option', { name: 'Option B' })).toHaveAttribute('value', 'b');

  // The widget shots the screenshot tooling reads: the stepped, prefilled first page in both
  // themes (the widget's tokens follow the OS colour-scheme preference). Each frame is
  // shot on a FIRST-LOAD render. Toggling `prefers-color-scheme` to dark AFTER load hits a
  // Chromium form-control repaint artifact — a native `<input>`/`<select>` keeps its light
  // background on the runtime toggle — so a faithful dark frame needs the scheme emulated
  // BEFORE the document paints. Reloading under the dark scheme is exactly a returning
  // participant's own path: the SSE stream replays the transcript backlog, re-delivering the
  // still-parked ask, and the controls paint dark from the first frame.
  await settleCardEntry(page, card);
  await page.screenshot({ path: `${SHOTS_DIR}/form-pages-widget-light.png` });

  await page.emulateMedia({ colorScheme: 'dark' });
  await page.reload();
  await expect(card.getByText(/Step 1 of 2 . Basics/)).toBeVisible();
  await expect(card.getByRole('spinbutton')).toHaveValue('3');
  await expect(card.getByRole('combobox')).toHaveValue('a');
  await settleCardEntry(page, card);
  await page.screenshot({ path: `${SHOTS_DIR}/form-pages-widget-dark.png` });

  // The inputMode fields draw their HTML keyboards in the real widget: email -> type="email";
  // numeric -> a plain text input carrying inputmode="numeric", so the typed "007" survives.
  const contactField = card.getByLabel('Contact', { exact: true });
  await expect(contactField).toHaveAttribute('type', 'email');
  const codeField = card.getByLabel('Code', { exact: true });
  await expect(codeField).toHaveAttribute('type', 'text');
  await expect(codeField).toHaveAttribute('inputmode', 'numeric');
  await contactField.fill(contact);
  await codeField.fill(code);

  // The chips control draws the enum as a tap-to-select chip group; selecting one picks its value.
  await card.getByRole('radio', { name: 'green' }).click();
  // The rangeStart pair draws ONE group of two labelled date inputs (start and end), not two
  // independent date fields.
  const startField = card.getByLabel('Start', { exact: true });
  const endField = card.getByLabel('End', { exact: true });
  await expect(startField).toHaveAttribute('type', 'date');
  await expect(endField).toHaveAttribute('type', 'date');
  await startField.fill(start);
  await endField.fill(end);

  // Step to the second page: the first page's primary button reads the caller's `button_label`
  // (not the default "Next"); the progress advances and Back appears.
  await card.getByRole('button', { name: 'Next step' }).click();
  await expect(card.getByText(/Step 2 of 2 . Extras/)).toBeVisible();
  await expect(card.getByRole('button', { name: 'Back' })).toBeVisible();
  // The textarea control draws a multi-line text input for the notes field.
  await card.getByLabel('Notes', { exact: true }).fill(notes);
  // The terminal page's primary button reads the caller's `button_label` (not the default "Submit").
  await card.getByRole('button', { name: 'Send it all' }).click();

  // The widget settles the card once the answer door accepts it.
  await expect(page.getByText('Answered')).toBeVisible();

  // The blocked ask wakes and returns the union of every page's fields — including the
  // per-send option value `date=a`, which is not a schema enum.
  const askRes = await askPromise;
  expect(askRes.status(), await askRes.text()).toBe(200);
  const body = (await askRes.json()) as { data: { kind: string; result?: unknown } };
  expect(body.data).toEqual({
    kind: 'result',
    result: { answer: answer, reply_to: null, params: null },
  });
});

test('a notify_user form with data + pages opens in the widget already filled in and stepped', async ({
  page,
  request,
  browserName,
}) => {
  // The widget's SSE feed is a fetch-stream; CI's Linux WebKit delivers those unreliably, so this
  // stream-dependent flow flakes only there. Chromium + Firefox cover the stream path.
  test.skip(browserName === 'webkit', 'Linux CI WebKit delivers fetch-streams unreliably');

  const identity = uniq('notifyformsite').replace(/_/g, '-');

  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: 'light' });
  await page.goto(`/api/channels/web/chat/${identity}`);

  const cookies = await page.context().cookies();
  const token = cookies.find((c) => c.name === WEB_SESSION_COOKIE)?.value;
  expect(token, 'the chat page minted no web-session cookie').toBeTruthy();
  const visitorId = await resolveWebVisitorId(token as string);

  // Fire an ask-less notify_user FORM (fire-and-forget) at this visitor: the web channel delivers a
  // form-data notification the widget renders — prefilled and stepped, exactly as an ask form is.
  const notifyRes = await request.post('/api/run-tool', {
    headers: apiHeaders(),
    timeout: 30_000,
    data: {
      tool_name: 'notify_user',
      arguments: {
        message: uniq('notify_form_q'),
        channel: 'web',
        recipient: `${identity}:${visitorId}`,
        schema: SCHEMA,
        data: DATA,
        pages: PAGES,
      },
    },
  });
  expect(notifyRes.status(), await notifyRes.text()).toBe(200);

  // The delivered form opens on its first page, prefilled, with the per-send option labels.
  const card = page.locator('.tcw-question-form');
  await expect(card.getByText(/Step 1 of 2 . Basics/)).toBeVisible();
  await expect(card.getByRole('spinbutton')).toHaveValue('3');
  await expect(card.getByRole('combobox')).toHaveValue('a');
  await expect(card.getByRole('option', { name: 'Option A' })).toHaveAttribute('value', 'a');
  await expect(card.getByRole('option', { name: 'Option B' })).toHaveAttribute('value', 'b');

  // The inputMode fields draw their HTML keyboards in the delivered form too: email -> type="email";
  // numeric -> a plain text input carrying inputmode="numeric".
  await expect(card.getByLabel('Contact', { exact: true })).toHaveAttribute('type', 'email');
  const codeField = card.getByLabel('Code', { exact: true });
  await expect(codeField).toHaveAttribute('type', 'text');
  await expect(codeField).toHaveAttribute('inputmode', 'numeric');

  await settleCardEntry(page, card);
  await page.screenshot({ path: `${SHOTS_DIR}/notify-form-widget-light.png` });

  // Stepping advances the progress and reveals the second page's field; the first page's primary
  // button reads the caller's `button_label` and Back appears on the second step.
  await card.getByRole('button', { name: 'Next step' }).click();
  await expect(card.getByText(/Step 2 of 2 . Extras/)).toBeVisible();
  await expect(card.getByRole('button', { name: 'Back' })).toBeVisible();
});

/**
 * The `wording` catalogue the web widget draws, per send. Each test fires a real web-channel
 * send carrying one chapter of `wording` and asserts the REAL widget bundle renders the
 * caller's own words where it would otherwise draw the platform's default — the answer
 * controls, field picker placeholders, a field's sentence for an answer outside its shape, the
 * settled-state badges, the live countdown and the select ask's option-group name.
 *
 * Locators are EXACT: a caller word can substring-collide with a default a sibling control
 * still draws (a decline word carrying "No", a confirm word carrying "Yes"), so every
 * assertion pins the whole accessible name rather than a fragment. The words are a distinct,
 * non-English set so a default word can never satisfy a caller assertion by accident.
 */
test.describe('the web widget draws the per-send wording catalogue', () => {
  /** Open the standalone chat page for a fresh visitor and resolve the server-side visitor id
   * a web send names its recipient by — the same bootstrap the composed-path tests above use. */
  async function registerVisitor(page: Page): Promise<{ identity: string; visitorId: string }> {
    const identity = uniq('wordingsite').replace(/_/g, '-');
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`/api/channels/web/chat/${identity}`);
    const cookies = await page.context().cookies();
    const token = cookies.find((c) => c.name === WEB_SESSION_COOKIE)?.value;
    expect(token, 'the chat page minted no web-session cookie').toBeTruthy();
    const visitorId = await resolveWebVisitorId(token as string);
    return { identity, visitorId };
  }

  /** Fire a blocking web `ask` WITHOUT awaiting: the run-tool call parks until the widget
   * answers it (or it times out). */
  function fireAsk(
    request: APIRequestContext,
    recipient: string,
    args: Record<string, unknown>,
  ): Promise<APIResponse> {
    return request.post('/api/run-tool', {
      headers: apiHeaders(),
      timeout: 90_000,
      data: { tool_name: 'ask', arguments: { channel: 'web', recipient, ...args } },
    });
  }

  test('a one-page form draws the caller terminal button and field picker placeholder', async ({
    page,
    request,
    browserName,
  }) => {
    test.skip(browserName === 'webkit', 'Linux CI WebKit delivers fetch-streams unreliably');
    const { identity, visitorId } = await registerVisitor(page);
    const question = uniq('form_q');
    const schema = {
      type: 'object',
      required: [] as string[],
      properties: {
        when: { type: 'string', title: 'When' },
        count: { type: 'integer', title: 'Count' },
      },
    };
    const data = {
      values: { count: 2 },
      options: { when: [{ value: 'a', label: 'Option A' }, { value: 'b', label: 'Option B' }] },
    };
    // A one-page form (no `pages`) admits `form.submit`; its one picker field takes a per-field
    // `choice_label`.
    const wording = { form: { submit: 'Enviar respuesta', fields: { when: { choice_label: 'Elige una fecha' } } } };

    const askPromise = fireAsk(request, `${identity}:${visitorId}`, {
      question,
      answer_format: 'form',
      schema,
      data,
      wording,
      timeout: 120,
    });

    const card = page.locator('.tcw-question-form');
    // The terminal button of a one-page form reads the caller's `form.submit`, not "Answer".
    await expect(card.getByRole('button', { name: 'Enviar respuesta', exact: true })).toBeVisible();
    // The per-send picker's empty-state option reads the caller's `choice_label`, not "Choose an
    // option" — it is the leading placeholder (value ""), kept even once a value is picked.
    await expect(card.getByRole('option', { name: 'Elige una fecha', exact: true })).toHaveAttribute('value', '');

    // Answer it: pick an option and submit — the blocked ask returns the union.
    await card.getByRole('combobox').selectOption('a');
    await card.getByRole('button', { name: 'Enviar respuesta', exact: true }).click();
    // Once answered the card's controls (`.tcw-question-form`) are withdrawn; the settled
    // badge rides the question card's own head, so assert it at the `.tcw-question` scope.
    await expect(
      page.locator('.tcw-question').filter({ hasText: question }).getByText('Answered', { exact: true }),
    ).toBeVisible();

    const askRes = await askPromise;
    expect(askRes.status(), await askRes.text()).toBe(200);
    const body = (await askRes.json()) as { data: { kind: string; result?: unknown } };
    expect(body.data).toEqual({
      kind: 'result',
      result: { answer: { count: 2, when: 'a' }, reply_to: null, params: null },
    });
  });

  test('a stepped form draws the caller back button on a later step', async ({
    page,
    request,
    browserName,
  }) => {
    test.skip(browserName === 'webkit', 'Linux CI WebKit delivers fetch-streams unreliably');
    const { identity, visitorId } = await registerVisitor(page);
    const question = uniq('form_q');
    const notes = uniq('notes');
    const schema = {
      type: 'object',
      required: [] as string[],
      properties: {
        count: { type: 'integer', title: 'Count' },
        notes: { type: 'string', title: 'Notes' },
      },
    };
    const data = { values: { count: 5 } };
    // Two steps so the Back control is drawn on the second. `form.back` is admitted on `form`
    // (it is vacuous on a one-page form). The terminal button of a paged form is the widget's own
    // stepped word ("Submit" on an ask), so `form.submit` is not set here.
    const pages = [
      { title: 'One', fields: ['count'] },
      { title: 'Two', fields: ['notes'] },
    ];
    const wording = { form: { back: 'Atrás' } };

    const askPromise = fireAsk(request, `${identity}:${visitorId}`, {
      question,
      answer_format: 'form',
      schema,
      data,
      pages,
      wording,
      timeout: 120,
    });

    const card = page.locator('.tcw-question-form');
    await expect(card.getByText(/Step 1 of 2 . One/)).toBeVisible();
    await card.getByRole('button', { name: 'Next', exact: true }).click();
    await expect(card.getByText(/Step 2 of 2 . Two/)).toBeVisible();
    // The Back control reads the caller's `form.back`, not "Back".
    await expect(card.getByRole('button', { name: 'Atrás', exact: true })).toBeVisible();

    await card.getByLabel('Notes', { exact: true }).fill(notes);
    await card.getByRole('button', { name: 'Submit', exact: true }).click();
    // Once answered the card's controls (`.tcw-question-form`) are withdrawn; the settled
    // badge rides the question card's own head, so assert it at the `.tcw-question` scope.
    await expect(
      page.locator('.tcw-question').filter({ hasText: question }).getByText('Answered', { exact: true }),
    ).toBeVisible();

    const askRes = await askPromise;
    expect(askRes.status(), await askRes.text()).toBe(200);
    const body = (await askRes.json()) as { data: { kind: string; result?: unknown } };
    expect(body.data).toEqual({
      kind: 'result',
      result: { answer: { count: 5, notes }, reply_to: null, params: null },
    });
  });

  test("a form field shows the caller's sentence for an answer outside its shape", async ({
    page,
    request,
    browserName,
  }) => {
    test.skip(browserName === 'webkit', 'Linux CI WebKit delivers fetch-streams unreliably');
    const { identity, visitorId } = await registerVisitor(page);
    const question = uniq('form_q');
    const schema = {
      type: 'object',
      required: [] as string[],
      properties: { code: { type: 'string', title: 'Code', pattern: '^[0-9]{4}$' } },
    };
    // The field's `invalid` sentence replaces the widget's own word for a value that breaks the
    // field's declared shape.
    const wording = { form: { fields: { code: { invalid: 'Escribe cuatro cifras' } } } };

    const askPromise = fireAsk(request, `${identity}:${visitorId}`, {
      question,
      answer_format: 'form',
      schema,
      wording,
      timeout: 120,
    });

    const card = page.locator('.tcw-question-form');
    const code = card.getByLabel('Code', { exact: true });
    const answer = card.getByRole('button', { name: 'Answer', exact: true });
    // "12a4" breaks the pattern: the card stays open and shows the caller's sentence, not the
    // widget's own "must match the required pattern".
    await code.fill('12a4');
    await answer.click();
    await expect(card.getByText('Escribe cuatro cifras', { exact: true })).toBeVisible();
    await expect(card.getByText('must match the required pattern')).toHaveCount(0);

    // A value in the field's shape resolves the parked ask.
    await code.fill('1234');
    await answer.click();
    await expect(
      page.locator('.tcw-question').filter({ hasText: question }).getByText('Answered', { exact: true }),
    ).toBeVisible();

    const askRes = await askPromise;
    expect(askRes.status(), await askRes.text()).toBe(200);
    const body = (await askRes.json()) as { data: { kind: string; result?: unknown } };
    expect(body.data).toEqual({
      kind: 'result',
      result: { answer: { code: '1234' }, reply_to: null, params: null },
    });
  });

  test("a form field reads the caller's required sentence and a list reads the caller's list words", async ({
    page,
    request,
    browserName,
  }) => {
    test.skip(browserName === 'webkit', 'Linux CI WebKit delivers fetch-streams unreliably');
    const { identity, visitorId } = await registerVisitor(page);
    const question = uniq('form_q');
    const tag = uniq('tag');
    const schema = {
      type: 'object',
      required: ['label'] as string[],
      properties: {
        label: { type: 'string', title: 'Label' },
        tags: { type: 'array', title: 'Tags', items: { type: 'string' } },
      },
    };
    // `required` replaces the widget's own missing-value message; the four list words replace the
    // free-text list's empty line, add button, item label (`{n}` the item's number) and remove button.
    const wording = {
      form: {
        fields: {
          label: { required: 'Escribe una etiqueta' },
          tags: {
            add_item: 'Añadir etiqueta',
            remove_item: 'Quitar',
            item_label: 'Etiqueta {n}',
            no_items: 'Sin etiquetas',
          },
        },
      },
    };

    const askPromise = fireAsk(request, `${identity}:${visitorId}`, {
      question,
      answer_format: 'form',
      schema,
      wording,
      timeout: 120,
    });

    const card = page.locator('.tcw-question-form');
    const answer = card.getByRole('button', { name: 'Answer', exact: true });
    await expect(card.getByText('Sin etiquetas', { exact: true })).toBeVisible();

    // An empty required label keeps the card open with the caller's sentence, not '"label" is required'.
    await answer.click();
    await expect(card.getByText('Escribe una etiqueta', { exact: true })).toBeVisible();
    await expect(card.getByText('"label" is required')).toHaveCount(0);

    await card.getByRole('button', { name: 'Añadir etiqueta', exact: true }).click();
    await expect(card.getByText('Sin etiquetas', { exact: true })).toHaveCount(0);
    await expect(card.getByRole('button', { name: 'Quitar Etiqueta 1', exact: true })).toBeVisible();
    await card.getByLabel('Etiqueta 1', { exact: true }).fill(tag);
    await card.getByLabel('Label', { exact: true }).fill('Alpha');
    await answer.click();
    await expect(
      page.locator('.tcw-question').filter({ hasText: question }).getByText('Answered', { exact: true }),
    ).toBeVisible();

    const askRes = await askPromise;
    expect(askRes.status(), await askRes.text()).toBe(200);
    const body = (await askRes.json()) as { data: { kind: string; result?: unknown } };
    expect(body.data).toEqual({
      kind: 'result',
      result: { answer: { label: 'Alpha', tags: [tag] }, reply_to: null, params: null },
    });
  });

  test('a confirm question draws the caller decline button', async ({
    page,
    request,
    browserName,
  }) => {
    test.skip(browserName === 'webkit', 'Linux CI WebKit delivers fetch-streams unreliably');
    const { identity, visitorId } = await registerVisitor(page);
    const question = uniq('confirm_q');
    // The affirmative and negative controls each read their own caller word.
    const wording = { answer: { confirm: 'Sí, acepto', decline: 'No, gracias' } };

    const askPromise = fireAsk(request, `${identity}:${visitorId}`, {
      question,
      answer_format: 'confirm',
      wording,
      timeout: 120,
    });

    const card = page.locator('.tcw-question').filter({ hasText: question });
    // Exact names: the caller decline "No, gracias" carries the default word "No" as a substring,
    // so a non-exact match would be ambiguous.
    await expect(card.getByRole('button', { name: 'No, gracias', exact: true })).toBeVisible();
    await expect(card.getByRole('button', { name: 'Sí, acepto', exact: true })).toBeVisible();

    // Confirm to settle; the blocked ask returns the affirmative.
    await card.getByRole('button', { name: 'Sí, acepto', exact: true }).click();
    await expect(card.getByText('Answered', { exact: true })).toBeVisible();

    const askRes = await askPromise;
    expect(askRes.status(), await askRes.text()).toBe(200);
    const body = (await askRes.json()) as { data: { kind: string; result?: unknown } };
    expect(body.data).toEqual({
      kind: 'result',
      result: { answer: true, reply_to: null, params: null },
    });
  });

  test('a text question draws the caller send button and input placeholder', async ({
    page,
    request,
    browserName,
  }) => {
    test.skip(browserName === 'webkit', 'Linux CI WebKit delivers fetch-streams unreliably');
    const { identity, visitorId } = await registerVisitor(page);
    const question = uniq('text_q');
    const answer = uniq('answer');
    // The empty-input hint and the send button each read their own caller word.
    const wording = { answer: { reply_hint: 'Escribe tu respuesta aquí', submit: 'Enviar respuesta' } };

    const askPromise = fireAsk(request, `${identity}:${visitorId}`, {
      question,
      answer_format: 'text',
      wording,
      timeout: 120,
    });

    const card = page.locator('.tcw-question').filter({ hasText: question });
    // The reply field (named by the question) carries the caller's placeholder, not "Type your
    // answer".
    const field = card.getByRole('textbox', { name: question });
    await expect(field).toHaveAttribute('placeholder', 'Escribe tu respuesta aquí');
    // The send button reads the caller's `answer.submit`, not "Answer".
    await expect(card.getByRole('button', { name: 'Enviar respuesta', exact: true })).toBeVisible();

    await field.fill(answer);
    await card.getByRole('button', { name: 'Enviar respuesta', exact: true }).click();
    await expect(card.getByText('Answered', { exact: true })).toBeVisible();

    const askRes = await askPromise;
    expect(askRes.status(), await askRes.text()).toBe(200);
    const body = (await askRes.json()) as { data: { kind: string; result?: unknown } };
    expect(body.data).toEqual({
      kind: 'result',
      result: { answer: answer, reply_to: null, params: null },
    });
  });

  test('a select question names its option group and draws the caller countdown + answered badge', async ({
    page,
    request,
    browserName,
  }) => {
    test.skip(browserName === 'webkit', 'Linux CI WebKit delivers fetch-streams unreliably');
    const { identity, visitorId } = await registerVisitor(page);
    const question = uniq('select_q');
    // A budget under a minute so the card is in its countdown window from first render, and well
    // above the 30s half-minute threshold so it stays in the "under a minute" band throughout the
    // assertions (the visible per-second line fills `{seconds}`, the spoken line is coarse).
    const wording = {
      choice: { group_label: 'Elige una opción' },
      status: {
        answered: 'Respondido',
        countdown: {
          seconds_left: 'Quedan {seconds} segundos',
          under_minute: 'Menos de un minuto para responder',
        },
      },
    };

    const askPromise = fireAsk(request, `${identity}:${visitorId}`, {
      question,
      answer_format: 'select',
      options: ['uno', 'dos', 'tres'],
      wording,
      timeout: 55,
    });

    const card = page.locator('.tcw-question').filter({ hasText: question });
    // The flat group of option buttons is named by the caller's `choice.group_label`, not
    // "Choose an option"; the options live inside it.
    const group = card.getByRole('group', { name: 'Elige una opción', exact: true });
    await expect(group).toBeVisible();
    await expect(group.getByRole('button', { name: 'uno', exact: true })).toBeVisible();
    // The visible per-second countdown reads the caller's template with the live count filled in.
    await expect(card.locator('.tcw-countdown')).toHaveText(/^Quedan \d+ segundos$/);
    // The spoken (coarse) live region reads the caller's under-a-minute line.
    await expect(card.getByRole('status')).toHaveText('Menos de un minuto para responder');

    // Answer to settle; the settled badge reads the caller's `status.answered`, not "Answered".
    await group.getByRole('button', { name: 'dos', exact: true }).click();
    await expect(card.getByText('Respondido', { exact: true })).toBeVisible();

    const askRes = await askPromise;
    expect(askRes.status(), await askRes.text()).toBe(200);
    const body = (await askRes.json()) as { data: { kind: string; result?: unknown } };
    expect(body.data).toEqual({
      kind: 'result',
      result: { answer: 'dos', reply_to: null, params: null },
    });
  });

  test('a question past its deadline draws the caller expired badge', async ({
    page,
    request,
    browserName,
  }) => {
    test.skip(browserName === 'webkit', 'Linux CI WebKit delivers fetch-streams unreliably');
    const { identity, visitorId } = await registerVisitor(page);
    const question = uniq('expire_q');
    const wording = { status: { expired: 'Caducado' } };

    // A short budget: the card streams in live, then its OWN clock crosses the deadline
    // (`timeout_at` = now + the budget) and the expired badge replaces the controls — the
    // caller's `status.expired`, not "Expired".
    const askPromise = fireAsk(request, `${identity}:${visitorId}`, {
      question,
      answer_format: 'text',
      wording,
      timeout: 6,
    });

    const card = page.locator('.tcw-question').filter({ hasText: question });
    await expect(card).toBeVisible();
    await expect(card.getByText('Caducado', { exact: true })).toBeVisible({ timeout: 20_000 });

    // The unanswered ask ends in a loud timeout rather than a result.
    const askRes = await askPromise;
    expect(askRes.ok(), await askRes.text()).toBe(false);
  });

  // Submitting an ask-less form bridges the values into the visitor's conversation as a
  // participant message, which needs a channel route bound to `(web, identity)` to carry it
  // — so this one test, unlike its `ask` siblings (which resolve a parked ask with no route),
  // binds a route to a probe tool first. Its extra needs ride a nested group so the ask-based
  // tests above keep their lighter declaration.
  test.describe('and submits into a bound web route', () => {
    needs('kind:channels:web', 'kind:interactions', 'store:redis', 'probe-tools', 'mutable');

    test('an ask-less form card draws the caller terminal button and sent badge', async ({
      page,
      request,
      browserName,
    }) => {
      test.skip(browserName === 'webkit', 'Linux CI WebKit delivers fetch-streams unreliably');
      const { identity, visitorId } = await registerVisitor(page);

      // A web channel route for this identity so the submission can bridge into a conversation.
      // The target is a silent probe tool (`reply_expr: null`), run under a freshly minted
      // execution principal; the submission door returns 200 at message acceptance.
      const execId = uniq('web-exec');
      await mintKey(request, { userId: execId, scopes: ['*'] });
      const routeName = uniq('web-route').replace(/_/g, '-');
      const routeRes = await request.post(`/api/conversations/${routeName}`, {
        headers: apiHeaders(),
        data: {
          door: 'channel',
          target_kind: 'tool',
          target_name: 'e2e_record',
          execution_key: execId,
          channel: 'web',
          our_identity: identity,
          start_expr: { content: '{key: .message, value: .message}' },
          reply_expr: { content: 'null' },
        },
      });
      expect(routeRes.status(), await routeRes.text()).toBe(200);

      const schema = {
        type: 'object',
        required: [] as string[],
        properties: { count: { type: 'integer', title: 'Count' } },
      };
      const data = { values: { count: 7 } };
      // An ask-less form (a `notify_user` form) draws `form.submit` on its terminal button and, once
      // submitted, `status.sent` on its local badge.
      const wording = { form: { submit: 'Enviar' }, status: { sent: 'Enviado' } };

      const notifyRes = await request.post('/api/run-tool', {
        headers: apiHeaders(),
        timeout: 30_000,
        data: {
          tool_name: 'notify_user',
          arguments: {
            message: uniq('notify_form_q'),
            channel: 'web',
            recipient: `${identity}:${visitorId}`,
            schema,
            data,
            wording,
          },
        },
      });
      expect(notifyRes.status(), await notifyRes.text()).toBe(200);

      const card = page.locator('.tcw-question-form');
      // The terminal button reads the caller's `form.submit`, not "Send".
      await expect(card.getByRole('button', { name: 'Enviar', exact: true })).toBeVisible();
      await card.getByRole('button', { name: 'Enviar', exact: true }).click();
      // The local sent badge reads the caller's `status.sent`, not "Sent".
      await expect(page.locator('.tcw-form').getByText('Enviado', { exact: true })).toBeVisible();
    });
  });
});

/** A range whose two `date` ends each have a `timeOf` time, the times declared after both dates. */
const TIMED_RANGE_SCHEMA = {
  type: 'object',
  required: ['start', 'end'] as string[],
  properties: {
    start: { type: 'string', title: 'From', format: 'date' },
    end: { type: 'string', title: 'To', format: 'date', rangeStart: 'start' },
    start_time: { type: 'string', title: 'From time', format: 'time', timeOf: 'start' },
    end_time: { type: 'string', title: 'To time', format: 'time', timeOf: 'end' },
  },
};

test('a timed range draws each time after its date in the widget and stops an earlier end time', async ({
  page,
  request,
  browserName,
}) => {
  test.skip(browserName === 'webkit', 'Linux CI WebKit delivers fetch-streams unreliably');

  const identity = uniq('formtimes').replace(/_/g, '-');
  const question = uniq('form_times_q');

  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: 'light' });
  await page.goto(`/api/channels/web/chat/${identity}`);
  const cookies = await page.context().cookies();
  const token = cookies.find((c) => c.name === WEB_SESSION_COOKIE)?.value;
  expect(token, 'the chat page minted no web-session cookie').toBeTruthy();
  const visitorId = await resolveWebVisitorId(token as string);

  const askPromise = request.post('/api/run-tool', {
    headers: apiHeaders(),
    timeout: 90_000,
    data: {
      tool_name: 'ask',
      arguments: {
        question,
        channel: 'web',
        recipient: `${identity}:${visitorId}`,
        answer_format: 'form',
        schema: TIMED_RANGE_SCHEMA,
        pages: [{ title: 'When', fields: ['start', 'end', 'start_time', 'end_time'], button_label: 'Send times' }],
        timeout: 120,
      },
    },
  });

  // The four controls draw with each time right after its date, whatever order the schema
  // declares them in; the widget draws a time as a text field.
  const card = page.locator('.tcw-question-form');
  await expect(card.getByLabel('From', { exact: true })).toBeVisible();
  const drawn = await card
    .locator('input')
    .evaluateAll((els) =>
      (els as HTMLInputElement[]).map((el) => [el.type, el.labels?.[0]?.textContent?.trim()]),
    );
  expect(drawn).toEqual([
    ['date', 'From'],
    ['text', 'From time'],
    ['date', 'To'],
    ['text', 'To time'],
  ]);

  // The same date with an earlier end time is held back with the SDK's time sentence.
  await card.getByLabel('From', { exact: true }).fill('2026-05-01');
  await card.getByLabel('To', { exact: true }).fill('2026-05-01');
  await card.getByLabel('From time', { exact: true }).fill('10:00');
  await card.getByLabel('To time', { exact: true }).fill('09:00');
  await card.getByRole('button', { name: 'Send times' }).click();
  await expect(card.getByText('must be on or after the start time')).toBeVisible();

  // Both themes, each painted from a first load (see the stepped-form test above for why).
  await settleCardEntry(page, card);
  await card.screenshot({ path: `${SHOTS_DIR}/form-range-times-widget-light.png` });
  await page.emulateMedia({ colorScheme: 'dark' });
  await page.reload();
  await expect(card.getByLabel('From', { exact: true })).toBeVisible();
  await card.getByLabel('From', { exact: true }).fill('2026-05-01');
  await card.getByLabel('To', { exact: true }).fill('2026-05-01');
  await card.getByLabel('From time', { exact: true }).fill('10:00');
  await card.getByLabel('To time', { exact: true }).fill('09:00');
  await card.getByRole('button', { name: 'Send times' }).click();
  await expect(card.getByText('must be on or after the start time')).toBeVisible();
  await settleCardEntry(page, card);
  await card.screenshot({ path: `${SHOTS_DIR}/form-range-times-widget-dark.png` });

  // A later end time sends, and the ask resolves with the four strings as entered.
  await card.getByLabel('To time', { exact: true }).fill('11:30');
  await card.getByRole('button', { name: 'Send times' }).click();
  await expect(page.getByText('Answered')).toBeVisible();
  const askRes = await askPromise;
  expect(askRes.status(), await askRes.text()).toBe(200);
  const body = (await askRes.json()) as { data: { kind: string; result?: unknown } };
  expect(body.data).toEqual({
    kind: 'result',
    result: {
      answer: { start: '2026-05-01', end: '2026-05-01', start_time: '10:00', end_time: '11:30' },
      reply_to: null,
      params: null,
    },
  });
});
