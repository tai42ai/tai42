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
 */
import { expect } from '@playwright/test';

import { apiHeaders, resolveCallbackTicket, resolveWebVisitorId, uniq, WEB_SESSION_COOKIE } from './helpers';
import { needs, test } from './needs';

needs('kind:channels:web', 'kind:interactions', 'store:redis');

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

test('the callback form page draws a display-block image from a remote https source', async ({
  page,
  request,
}) => {
  const identity = uniq('formpage').replace(/_/g, '-');
  const question = uniq('form_page_q');
  const notes = uniq('notes');

  // Open the standalone chat page so the web channel registers a visitor to deliver to.
  await page.goto(`/api/channels/web/chat/${identity}`);
  const cookies = await page.context().cookies();
  const token = cookies.find((c) => c.name === WEB_SESSION_COOKIE)?.value;
  expect(token, 'the chat page minted no web-session cookie').toBeTruthy();
  const visitorId = await resolveWebVisitorId(token as string);

  // Fire the blocking form ask WITHOUT awaiting: it parks until the page submits below.
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
        pages: PAGES,
        timeout: 120,
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

  await page.route(DISPLAY_IMAGE_URL, (route) =>
    route.fulfill({ status: 200, contentType: 'image/png', body: PNG_1X1 }),
  );
  const res = await page.goto(`/api/interactions/callback/${ticket}`);
  expect(res?.status()).toBe(200);

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
  const askRes = await askPromise;
  expect(askRes.status(), await askRes.text()).toBe(200);
  const body = (await askRes.json()) as { data: { kind: string; result?: unknown } };
  expect(body.data).toEqual({ kind: 'result', result: { notes } });
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
  const identity = uniq('formshape').replace(/_/g, '-');
  const question = uniq('form_shape_q');

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
        schema: SHAPED_SCHEMA,
        wording: { form: { fields: { email: { invalid: 'Enter your full address' } } } },
        timeout: 120,
      },
    },
  });

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
  const res = await page.goto(`/api/interactions/callback/${ticket}`);
  expect(res?.status()).toBe(200);

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
  const askRes = await askPromise;
  expect(askRes.status(), await askRes.text()).toBe(200);
  const body = (await askRes.json()) as { data: { kind: string; result?: unknown } };
  expect(body.data).toEqual({
    kind: 'result',
    result: { email: 'alice@example.org', code: '1234', even: 4 },
  });
});
