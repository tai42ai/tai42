/**
 * The server-rendered callback form page in a real browser: a channel-delivered form
 * question mints a callback ticket, and `GET /api/interactions/callback/{ticket}` serves
 * the schema-rendered page under its own content-security policy. A display block's
 * image must actually LOAD there — the policy is enforced by the browser alone, so only a
 * browser proves a display image is drawn rather than blocked.
 *
 * The web channel is the delivery vehicle (the core renders the same page for every
 * channel that opens it); the ticket is read from the SUT Redis, as the pytest harness
 * reads it, because the admin API never serializes it. The remote https image is served
 * by a Playwright route, so the load needs no network while the page policy still governs
 * whether the browser may issue it.
 *
 * The `@docs` test also writes the page's docs screenshot, `form-field-error.png`, into
 * `SHOTS_OUT` (a gitignored local default when unset): the form with a broken email
 * address and the platform's sentence under the field. The timed-range test writes
 * `callback-page-range-times-error.png` there too: the range group with each time under
 * its date and the time sentence in the group's row. The page has no colour theme, so each
 * shot is one image, cropped to what it shows.
 */
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { expect, type APIRequestContext, type APIResponse, type Page } from '@playwright/test';

import { apiHeaders, resolveCallbackTicket, resolveWebVisitorId, uniq, WEB_SESSION_COOKIE } from './helpers';
import { needs, test } from './needs';

needs('kind:channels:web', 'kind:interactions', 'store:redis');

/** Where the page shots land: `SHOTS_OUT` when set, else a gitignored dir beside the tests. */
const SHOTS_OUT = process.env.SHOTS_OUT ?? fileURLToPath(new URL('../docs-shots-output', import.meta.url));

/** A 1x1 PNG: the bytes the routed remote image answers with. */
const PNG_1X1 = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC',
  'base64',
);

/** The remote https source a display block names; answered by the route below. */
const DISPLAY_IMAGE_URL = 'https://images.e2e.test/display.png';

const SCHEMA = {
  type: 'object',
  required: [] as string[],
  properties: { notes: { type: 'string', title: 'Notes' } },
};

const PAGES = [
  {
    title: 'Look',
    fields: ['notes'],
    display: [{ kind: 'image', src: DISPLAY_IMAGE_URL, alt: 'A display image' }],
  },
];

/** A parked form ask on the callback page: its ticket and the blocked `run-tool` call. */
interface CallbackForm {
  ticket: string;
  answered: Promise<APIResponse>;
}

/**
 * Park a blocking web-channel form ask at a freshly registered browser visitor and open its
 * callback page: the chat page registers the visitor, the ask fires WITHOUT being awaited
 * (it parks until the page submits), and the page is loaded from the ticket minted for it.
 */
async function openCallbackForm(
  page: Page,
  request: APIRequestContext,
  prefix: string,
  ask: Record<string, unknown>,
): Promise<CallbackForm> {
  const identity = uniq(prefix).replace(/_/g, '-');
  const question = uniq(`${prefix}_q`);

  await page.goto(`/api/channels/web/chat/${identity}`);
  const cookies = await page.context().cookies();
  const token = cookies.find((c) => c.name === WEB_SESSION_COOKIE)?.value;
  expect(token, 'the chat page minted no web-session cookie').toBeTruthy();
  const visitorId = await resolveWebVisitorId(token as string);

  const answered = request.post('/api/run-tool', {
    headers: apiHeaders(),
    timeout: 90_000,
    data: {
      tool_name: 'ask',
      arguments: {
        question,
        channel: 'web',
        recipient: `${identity}:${visitorId}`,
        answer_format: 'form',
        timeout: 120,
        ...ask,
      },
    },
  });

  // The pending interaction's id, then the callback ticket minted for it.
  let interactionId = '';
  await expect
    .poll(
      async () => {
        const listRes = await request.get('/api/interactions', { headers: apiHeaders() });
        expect(listRes.status(), await listRes.text()).toBe(200);
        const items = (
          (await listRes.json()) as { data: { items: { interaction_id: string; question: string }[] } }
        ).data.items;
        interactionId = items.find((item) => item.question === question)?.interaction_id ?? '';
        return interactionId;
      },
      { timeout: 15_000, message: `pending interaction for ${question} never appeared` },
    )
    .not.toBe('');
  const ticket = await resolveCallbackTicket(interactionId);
  return { ticket, answered };
}

/** Navigate to the callback page of `ticket`, asserting it is served. */
async function gotoCallbackPage(page: Page, ticket: string): Promise<void> {
  const res = await page.goto(`/api/interactions/callback/${ticket}`);
  expect(res?.status()).toBe(200);
}

/** The resolved ask's result, asserting the blocked `run-tool` call returned it. */
async function askResult(answered: Promise<APIResponse>): Promise<unknown> {
  const askRes = await answered;
  expect(askRes.status(), await askRes.text()).toBe(200);
  return ((await askRes.json()) as { data: unknown }).data;
}

test('the callback form page draws a display-block image from a remote https source', async ({
  page,
  request,
}) => {
  const notes = uniq('notes');
  const { ticket, answered } = await openCallbackForm(page, request, 'formpage', { schema: SCHEMA, pages: PAGES });

  await page.route(DISPLAY_IMAGE_URL, (route) =>
    route.fulfill({ status: 200, contentType: 'image/png', body: PNG_1X1 }),
  );
  await gotoCallbackPage(page, ticket);

  // The display image LOADED: a blocked or failed image settles with naturalWidth 0.
  const image = page.locator('img[alt="A display image"]');
  await expect(image).toHaveAttribute('src', DISPLAY_IMAGE_URL);
  await expect
    .poll(() => image.evaluate((el: HTMLImageElement) => (el.complete ? el.naturalWidth : -1)), {
      timeout: 10_000,
    })
    .toBeGreaterThan(0);

  // The page still answers: submitting resolves the parked ask with the entered value.
  await page.locator('[data-field="notes"]').fill(notes);
  await page.getByRole('button', { name: 'Submit' }).click();
  expect(await askResult(answered)).toEqual({
    kind: 'result',
    result: { answer: { notes }, reply_to: null, params: null },
  });
});

/** A form whose fields carry the shapes the page catches itself, and one only the door refuses. */
const SHAPED_SCHEMA = {
  type: 'object',
  required: ['email'] as string[],
  properties: {
    email: { type: 'string', format: 'email', title: 'Email address' },
    code: { type: 'string', pattern: '^[0-9]{4}$', title: 'Code' },
    even: { type: 'integer', multipleOf: 2, title: 'Even' },
  },
};

test('the callback form page stops a broken shape under its field and pins the door refusal', async ({
  page,
  request,
}) => {
  const { ticket, answered } = await openCallbackForm(page, request, 'formshape', {
    schema: SHAPED_SCHEMA,
    wording: { form: { fields: { email: { invalid: 'Enter your full address' } } } },
  });
  await gotoCallbackPage(page, ticket);

  const row = (name: string) => page.locator(`[data-field-row="${name}"]`);
  const answers: number[] = [];
  page.on('request', (req) => {
    if (req.method() === 'POST' && req.url().includes(`/api/interactions/callback/${ticket}`)) {
      answers.push(1);
    }
  });
  const submit = page.getByRole('button', { name: 'Submit' });

  // "dhdhdh" is no address: the page keeps it, shows the caller's sentence under the field and
  // sends nothing.
  await page.locator('[data-field="email"]').fill('dhdhdh');
  await submit.click();
  await expect(row('email').locator('.field-error')).toHaveText('Enter your full address');
  // An address with no dotted domain passes the browser's own check but not the platform's shape.
  await page.locator('[data-field="email"]').fill('alice@example');
  await submit.click();
  await expect(row('email').locator('.field-error')).toHaveText('Enter your full address');

  // A pattern mismatch shows the platform's sentence under its own field.
  await page.locator('[data-field="email"]').fill('alice@example.org');
  await page.locator('[data-field="code"]').fill('12a4');
  await submit.click();
  await expect(row('code').locator('.field-error')).toHaveText('This value is not in the expected form');
  await expect(row('email').locator('.field-error')).toHaveCount(0);
  expect(answers).toEqual([]);

  // A shape only the door checks (multipleOf) leaves the page; the door's refusal is pinned under
  // the field it names, and the ask stays answerable.
  await page.locator('[data-field="code"]').fill('1234');
  await page.locator('[data-field="even"]').fill('3');
  await submit.click();
  await expect(row('even').locator('.field-error')).toHaveText('Not an accepted value');
  expect(answers).toEqual([1]);

  // A clean answer resolves the parked ask.
  await page.locator('[data-field="even"]').fill('4');
  await submit.click();
  expect(await askResult(answered)).toEqual({
    kind: 'result',
    result: { answer: { email: 'alice@example.org', code: '1234', even: 4 }, reply_to: null, params: null },
  });
});

/** Two steps: a required label first, then a free note. */
const REQUIRED_STEP_SCHEMA = {
  type: 'object',
  required: ['label'] as string[],
  properties: { label: { type: 'string', title: 'Label' }, note: { type: 'string', title: 'Note' } },
};
const REQUIRED_STEP_PAGES = [
  { title: 'Label', fields: ['label'] },
  { title: 'Note', fields: ['note'] },
];

for (const own of [true, false]) {
  test(`the callback form page holds a step on an empty required field ${
    own ? "with the caller's sentence pinned under it" : "with the browser's own check"
  }`, async ({ page, request }) => {
    const { ticket, answered } = await openCallbackForm(page, request, own ? 'formreqown' : 'formreq', {
      schema: REQUIRED_STEP_SCHEMA,
      pages: REQUIRED_STEP_PAGES,
      ...(own ? { wording: { form: { fields: { label: { required: 'Write a label first' } } } } } : {}),
    });
    await gotoCallbackPage(page, ticket);

    const labelStep = page.locator('section.step[data-page-title="Label"]');
    const noteStep = page.locator('section.step[data-page-title="Note"]');
    const labelError = page.locator('[data-field-row="label"] .field-error');
    await page.getByRole('button', { name: 'Next' }).click();

    // The empty required label holds the step either way.
    await expect(labelStep).toBeVisible();
    await expect(noteStep).toBeHidden();
    if (own) {
      await expect(labelError).toHaveText('Write a label first');
    } else {
      // No caller sentence: the browser's own required check holds the step and nothing is pinned.
      await expect(labelError).toHaveCount(0);
      expect(
        await page.locator('[data-field="label"]').evaluate((el) => (el as HTMLInputElement).validity.valueMissing),
      ).toBe(true);
    }

    await page.locator('[data-field="label"]').fill('Alpha');
    await page.getByRole('button', { name: 'Next' }).click();
    await expect(noteStep).toBeVisible();
    await page.getByRole('button', { name: 'Submit' }).click();
    expect(await askResult(answered)).toEqual({
      kind: 'result',
      result: { answer: { label: 'Alpha' }, reply_to: null, params: null },
    });
  });
}

/** The docs shot's form: one required email address, no caller wording. */
const EMAIL_SCHEMA = {
  type: 'object',
  required: ['email'] as string[],
  properties: { email: { type: 'string', format: 'email', title: 'Email address' } },
};

test('the callback form page shows the platform sentence under a broken email address @docs', async ({
  page,
  request,
}) => {
  const { ticket, answered } = await openCallbackForm(page, request, 'formdocs', { schema: EMAIL_SCHEMA });
  await gotoCallbackPage(page, ticket);

  // "alice@" has no domain: the page keeps the answer and shows the platform's own sentence
  // under the field, since the ask sets no wording of its own.
  const form = page.locator('#askform');
  await page.locator('[data-field="email"]').fill('alice@');
  await page.getByRole('button', { name: 'Submit' }).click();
  await expect(page.locator('[data-field-row="email"] .field-error')).toHaveText('Enter a valid email address');
  mkdirSync(SHOTS_OUT, { recursive: true });
  await form.screenshot({ path: join(SHOTS_OUT, 'form-field-error.png') });

  // A full address resolves the parked ask.
  await page.locator('[data-field="email"]').fill('alice@example.org');
  await page.getByRole('button', { name: 'Submit' }).click();
  expect(await askResult(answered)).toEqual({
    kind: 'result',
    result: { answer: { email: 'alice@example.org' }, reply_to: null, params: null },
  });
});

/** A range whose two `date` ends each have a `timeOf` time, the times declared after both dates. */
const TIMED_RANGE_SCHEMA = {
  type: 'object',
  required: ['start', 'end'] as string[],
  properties: {
    start: { type: 'string', format: 'date', title: 'From' },
    end: { type: 'string', format: 'date', title: 'To', rangeStart: 'start' },
    start_time: { type: 'string', format: 'time', title: 'From time', timeOf: 'start' },
    end_time: { type: 'string', format: 'time', title: 'To time', timeOf: 'end' },
  },
};

/** Record every answer the page posts for `ticket`, so a test can tell the page kept one back. */
function countAnswers(page: Page, ticket: string): number[] {
  const answers: number[] = [];
  page.on('request', (req) => {
    if (req.method() === 'POST' && req.url().includes(`/api/interactions/callback/${ticket}`)) {
      answers.push(1);
    }
  });
  return answers;
}

test('the callback form page draws each time under its date and stops an earlier end time', async ({
  page,
  request,
}) => {
  const { ticket, answered } = await openCallbackForm(page, request, 'formtimed', { schema: TIMED_RANGE_SCHEMA });
  await gotoCallbackPage(page, ticket);
  const answers = countAnswers(page, ticket);

  // One range group; each end's time sits under its own date, whatever order the schema declares.
  const group = page.locator('fieldset.daterange');
  await expect(group).toHaveCount(1);
  const drawn = await group
    .locator('[data-field]')
    .evaluateAll((els) => els.map((el) => el.getAttribute('data-field')));
  expect(drawn).toEqual(['start', 'start_time', 'end', 'end_time']);
  await expect(group.locator('.range-end').nth(1).locator('[data-field]')).toHaveCount(2);

  // The same date with an earlier end time: the page keeps the answer, shows the time sentence in
  // the group's row and sends nothing.
  await page.locator('[data-field="start"]').fill('2026-05-01');
  await page.locator('[data-field="end"]').fill('2026-05-01');
  await page.locator('[data-field="start_time"]').fill('10:00');
  await page.locator('[data-field="end_time"]').fill('09:00');
  const submit = page.getByRole('button', { name: 'Submit' });
  await submit.click();
  const rangeRow = page.locator('[data-field-row="start"]');
  await expect(rangeRow.locator('.field-error')).toHaveText('The end time must be on or after the start time');
  expect(answers).toEqual([]);
  mkdirSync(SHOTS_OUT, { recursive: true });
  await rangeRow.screenshot({ path: join(SHOTS_OUT, 'callback-page-range-times-error.png') });

  // A later end time resolves the parked ask with the four strings as entered.
  await page.locator('[data-field="end_time"]').fill('11:30');
  await submit.click();
  expect(await askResult(answered)).toEqual({
    kind: 'result',
    result: {
      answer: { start: '2026-05-01', end: '2026-05-01', start_time: '10:00', end_time: '11:30' },
      reply_to: null,
      params: null,
    },
  });
});

test('the callback form page orders a date start against a date-time end by written date', async ({
  page,
  request,
}) => {
  const { ticket, answered } = await openCallbackForm(page, request, 'formmixed', {
    schema: {
      type: 'object',
      required: ['start', 'end'] as string[],
      properties: {
        start: { type: 'string', format: 'date', title: 'From' },
        end: { type: 'string', format: 'date-time', title: 'To', rangeStart: 'start' },
      },
    },
  });
  await gotoCallbackPage(page, ticket);
  const answers = countAnswers(page, ticket);
  const submit = page.getByRole('button', { name: 'Submit' });

  // An end written on an earlier date is out of order on the page itself.
  await page.locator('[data-field="start"]').fill('2026-05-03');
  await page.locator('[data-field="end"]').fill('2026-05-02T23:00:00Z');
  await submit.click();
  await expect(page.locator('[data-field-row="end"] .field-error')).toHaveText(
    'The end date must be on or after the start date',
  );
  expect(answers).toEqual([]);

  // The same written date is in order, whatever the time of day.
  await page.locator('[data-field="end"]').fill('2026-05-03T00:30:00Z');
  await submit.click();
  expect(await askResult(answered)).toEqual({
    kind: 'result',
    result: { answer: { start: '2026-05-03', end: '2026-05-03T00:30:00Z' }, reply_to: null, params: null },
  });
});

test('the callback form page sends a date-time start with a date end on the same written date', async ({
  page,
  request,
}) => {
  const { ticket, answered } = await openCallbackForm(page, request, 'formmixedrev', {
    schema: {
      type: 'object',
      required: ['start', 'end'] as string[],
      properties: {
        start: { type: 'string', format: 'date-time', title: 'From' },
        end: { type: 'string', format: 'date', title: 'To', rangeStart: 'start' },
      },
    },
  });
  await gotoCallbackPage(page, ticket);

  await page.locator('[data-field="start"]').fill('2026-05-03T18:00:00+02:00');
  await page.locator('[data-field="end"]').fill('2026-05-03');
  await page.getByRole('button', { name: 'Submit' }).click();
  expect(await askResult(answered)).toEqual({
    kind: 'result',
    result: { answer: { start: '2026-05-03T18:00:00+02:00', end: '2026-05-03' }, reply_to: null, params: null },
  });
});

test('the callback form page leaves two date-time ends to the door and pins its time refusal', async ({
  page,
  request,
}) => {
  const { ticket, answered } = await openCallbackForm(page, request, 'forminstant', {
    schema: {
      type: 'object',
      required: ['start', 'end'] as string[],
      properties: {
        start: { type: 'string', format: 'date-time', title: 'From' },
        end: { type: 'string', format: 'date-time', title: 'To', rangeStart: 'start' },
      },
    },
  });
  await gotoCallbackPage(page, ticket);
  const answers = countAnswers(page, ticket);
  const submit = page.getByRole('button', { name: 'Submit' });

  // An end an hour before the start on the same day: the page sends it, and the door's refusal is
  // pinned under the end with the time sentence; the ask stays answerable.
  await page.locator('[data-field="start"]').fill('2026-05-03T10:00:00Z');
  await page.locator('[data-field="end"]').fill('2026-05-03T09:00:00Z');
  await submit.click();
  await expect(page.locator('[data-field-row="end"] .field-error')).toHaveText(
    'The end time must be on or after the start time',
  );
  expect(answers).toEqual([1]);

  await page.locator('[data-field="end"]').fill('2026-05-03T11:00:00Z');
  await submit.click();
  expect(await askResult(answered)).toEqual({
    kind: 'result',
    result: { answer: { start: '2026-05-03T10:00:00Z', end: '2026-05-03T11:00:00Z' }, reply_to: null, params: null },
  });
});
