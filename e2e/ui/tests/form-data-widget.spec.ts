/**
 * Per-send form data + pages — the web WIDGET leg of the composed path.
 *
 * The platform + channel halves are proven elsewhere: the callback-form-page path in
 * `e2e/tests/interactions/test_form_data_pages.py` (a real ask → the server-rendered page
 * → the union POST → the resolved ask) and the channel adapter tests. THIS leg proves the
 * remaining seam a web guest's traffic actually takes: the REAL widget bundle, served by
 * the skeleton at the channel's public chat page, rendering a REAL web-channel delivery.
 *
 * A blocking `ask_user(channel="web", answer_format="form", data=..., pages=...)` is fired
 * at the browser visitor's own conversation (its server-side visitor id read from the SUT
 * Redis — the web channel never discloses it to the client, exactly as the pytest harness's
 * `registered_visitor_id` reads it). The question streams into the widget, which shows the
 * prefilled values, the per-send option labels in a native `<select>`, and the pages as
 * steps ("Step 1 of 2 · Basics"); stepping through and submitting resolves the ask with the
 * union of every page's fields — asserted through the run-tool door the ask blocked on.
 */
import { expect, test, type Locator, type Page } from '@playwright/test';

import { apiHeaders, resolveWebVisitorId, uniq, WEB_SESSION_COOKIE } from './helpers';

/** Where the orchestrator reads the widget shots from. */
const SHOTS_DIR = '/home/tai/agent-runs/agenda/_ops/shots/form';

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

/** The channel-deliverable answer schema: three scalar properties, no enum (the per-send
 * options replace a property's choices for this send). Abstract fields only. */
const SCHEMA = {
  type: 'object',
  required: [] as string[],
  properties: {
    date: { type: 'string', title: 'Date' },
    count: { type: 'integer', title: 'Count' },
    notes: { type: 'string', title: 'Notes' },
  },
};

/** Per-send enrichment: `date` is prefilled to an option value and carries a per-send
 * label list; `count` is prefilled to a known integer. `notes` is left for the visitor. */
const DATA = {
  values: { date: 'a', count: 3 },
  options: { date: [{ value: 'a', label: 'Option A' }, { value: 'b', label: 'Option B' }] },
};

/** Two ordered steps: every top-level property appears exactly once across the pages. */
const PAGES = [
  { title: 'Basics', fields: ['date', 'count'] },
  { title: 'Extras', fields: ['notes'] },
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
  const answer = { date: 'a', count: 3, notes };

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
      tool_name: 'ask_user',
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

  // The widget shots the orchestrator reads: the stepped, prefilled first page in both
  // themes (the widget's tokens follow the OS colour-scheme preference). Each frame is
  // shot on a FIRST-LOAD render. Toggling `prefers-color-scheme` to dark AFTER load hits a
  // Chromium form-control repaint artifact — a native `<input>`/`<select>` keeps its light
  // background on the runtime toggle — so a faithful dark frame needs the scheme emulated
  // BEFORE the document paints. Reloading under the dark scheme is exactly a returning
  // guest's own path: the SSE stream replays the transcript backlog, re-delivering the
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

  // Step to the second page: the progress advances, Back appears, and the button is Submit.
  await card.getByRole('button', { name: 'Next' }).click();
  await expect(card.getByText(/Step 2 of 2 . Extras/)).toBeVisible();
  await expect(card.getByRole('button', { name: 'Back' })).toBeVisible();
  await card.getByRole('textbox').fill(notes);
  await card.getByRole('button', { name: 'Submit' }).click();

  // The widget settles the card once the answer door accepts it.
  await expect(page.getByText('Answered')).toBeVisible();

  // The blocked ask wakes and returns the union of every page's fields — including the
  // per-send option value `date=a`, which is not a schema enum.
  const askRes = await askPromise;
  expect(askRes.status(), await askRes.text()).toBe(200);
  const body = (await askRes.json()) as { data: unknown };
  expect(body.data).toEqual(answer);
});
