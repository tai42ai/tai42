/**
 * HITL / interactions inbox. The pytest twin proves
 * ask_user blocks on worker A and is answered via worker B over SSE
 * (`tests/interactions/test_hitl_cross_worker.py`); the browser never drove the
 * inbox. Here the built-in `ask_user` USER TOOL is fired through the same-origin
 * run-tool door (it BLOCKS until answered — the interaction under test), the
 * pending question is answered in the browser at `/interactions`, and both sides
 * are asserted: the UI card flips to Answered and the blocked run returns the
 * answered value. The loud negative answers a nonexistent interaction (404).
 */
import { expect, test } from '@playwright/test';
import { apiHeaders, seedCredential, uniq } from './helpers';

test('ask_user blocks a run, is answered in the browser, and the run unblocks', async ({
  page,
  request,
  browserName,
}) => {
  // Server-stream-rendered inbox: CI's Linux WebKit build delivers fetch-streams
  // unreliably, so this stream-dependent flow flakes only there; chromium+firefox
  // cover the stream path and webkit still runs the rest of the suite.
  test.skip(browserName === 'webkit', 'Linux CI WebKit delivers fetch-streams unreliably');
  const question = uniq('question');
  const answer = uniq('answer');

  // Fire the blocking ask_user WITHOUT awaiting: the run-tool call parks until the
  // interaction is answered, so the promise is resolved only after the browser
  // submits the answer below.
  const askPromise = request.post('/api/run-tool', {
    headers: apiHeaders(),
    data: { tool_name: 'ask_user', arguments: { question } },
  });

  await seedCredential(page);
  await page.goto('/interactions');

  // The pending question streams into the inbox (SSE) as a text-format card.
  const card = page
    .getByTestId('interaction-card')
    .filter({ hasText: question });
  await expect(card).toBeVisible();

  // Answer it in the browser (the text renderer's "Your answer" field + Submit).
  await card.getByRole('textbox', { name: 'Your answer' }).fill(answer);
  await card.getByRole('button', { name: 'Submit' }).click();

  // UI: the card flips to the Answered state (the SSE `interaction.answered` frame).
  await expect(card.getByTestId('interaction-answered')).toBeVisible();

  // API: the blocked run wakes and returns the answered value verbatim.
  const askRes = await askPromise;
  expect(askRes.status(), await askRes.text()).toBe(200);
  const body = (await askRes.json()) as { data: unknown };
  expect(JSON.stringify(body.data)).toContain(answer);
});

// A 1x1 PNG data URI passed as the image item's INPUT url. A data:image is stored
// BY REFERENCE server-side (`substitute_media`): the durable record carries a
// served-media reference (`/api/interactions/media/<id>`), never these inline
// bytes, so the inbox renders that reference and fetches the image from the media
// route — the src asserted below is that reference, not this data: url.
const DATA_IMAGE =
  'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC';

// A served-media reference: the fixed route prefix followed by the 43-char urlsafe
// stored-media id. Origin-agnostic — relative on a same-origin deployment, absolute
// (`<api base>/...`) when the SPA is built against a cross-origin API base.
const SERVED_MEDIA_SRC = /^(https?:\/\/[^/]+)?\/api\/interactions\/media\/[A-Za-z0-9_-]{43}$/;

test('a media-bearing question renders images + links in the inbox and still answers', async ({
  page,
  request,
  browserName,
}) => {
  // Server-stream-rendered inbox: CI's Linux WebKit build delivers fetch-streams
  // unreliably, so this stream-dependent flow flakes only there; chromium+firefox
  // cover the stream path and webkit still runs the rest of the suite.
  test.skip(browserName === 'webkit', 'Linux CI WebKit delivers fetch-streams unreliably');
  const question = uniq('question');
  const answer = uniq('answer');
  const caption = uniq('caption');
  const linkUrl = `https://example.com/${uniq('shop')}`;

  // Fire the blocking ask_user carrying display-only media WITHOUT awaiting: the
  // call parks until the browser submits the answer below. Media is a data:image
  // item (captioned) plus an https link item.
  const askPromise = request.post('/api/run-tool', {
    headers: apiHeaders(),
    data: {
      tool_name: 'ask_user',
      arguments: {
        question,
        media: [
          { kind: 'image', url: DATA_IMAGE, caption },
          { kind: 'link', url: linkUrl },
        ],
      },
    },
  });

  await seedCredential(page);
  await page.goto('/interactions');

  const card = page
    .getByTestId('interaction-card')
    .filter({ hasText: question });
  await expect(card).toBeVisible();

  // The gallery renders (no loud fallback for these valid items).
  const gallery = card.getByTestId('media-gallery');
  await expect(gallery).toBeVisible();
  await expect(card.getByTestId('media-item-malformed')).toHaveCount(0);
  await expect(card.getByTestId('media-item-blocked')).toHaveCount(0);
  await expect(card.getByTestId('media-image-error')).toHaveCount(0);

  // The image renders from the served-media reference the record carries, resolved
  // against the SPA's configured API base: relative on a same-origin deployment,
  // absolute (`<api base>/...`) when Studio and the API are split origins. The
  // media-image-error count 0 above already proved it LOADED — a page-origin 404
  // (a relative ref resolved against Studio when the API is elsewhere) would have
  // tripped it. It is the reference, never the inline data: bytes.
  const image = gallery.locator('img');
  await expect(image).toHaveAttribute('src', SERVED_MEDIA_SRC);
  await expect(image).toHaveAttribute('alt', caption);
  await expect(image).toHaveAttribute('referrerpolicy', 'no-referrer');
  // The caption text is shown below the image (escaped text, not an attribute).
  await expect(gallery.getByText(caption)).toBeVisible();

  // The link renders as a neutralized-scheme anchor to the exact https url.
  const link = card.getByTestId('media-item-link').locator('a');
  await expect(link).toHaveAttribute('href', linkUrl);
  await expect(link).toHaveAttribute('rel', 'noopener noreferrer external');

  // Answer through the UI — media never interferes with the answer control.
  await card.getByRole('textbox', { name: 'Your answer' }).fill(answer);
  await card.getByRole('button', { name: 'Submit' }).click();

  // UI: the card flips to Answered AND the media is still shown in that state.
  await expect(card.getByTestId('interaction-answered')).toBeVisible();
  await expect(card.getByTestId('media-gallery')).toBeVisible();
  await expect(card.getByTestId('media-item-link').locator('a')).toHaveAttribute('href', linkUrl);

  // API: the blocked run wakes and returns the answered value verbatim — the
  // display-only media changed nothing about answering.
  const askRes = await askPromise;
  expect(askRes.status(), await askRes.text()).toBe(200);
  const body = (await askRes.json()) as { data: unknown };
  expect(JSON.stringify(body.data)).toContain(answer);
});

test('the served SPA response pins the media img-src CSP directive', async ({ request }) => {
  // The one place the whole ecosystem pins the DEPLOYED header: the served SPA
  // document must carry `img-src 'self' data: https:` so the inbox may render
  // remote https images and inline data: images (and only those).
  const res = await request.get('/');
  expect(res.status(), await res.text()).toBe(200);
  const csp = res.headers()['content-security-policy'];
  expect(csp).toBeTruthy();
  // Pin the EXACT img-src directive, not a loose substring: split the
  // `;`-delimited header so a relaxation (e.g. a trailing `http:` or `*`) is a
  // different token and fails, proving these origins are admitted AND ONLY these.
  const directives = csp.split(';').map((directive) => directive.trim());
  expect(directives).toContain("img-src 'self' data: https:");
});

test('answering a nonexistent interaction is a loud 404', async ({ request }) => {
  // No interaction with this id exists — the human answer door rejects it loudly
  // rather than silently accepting a phantom answer.
  const res = await request.post(`/api/interactions/${uniq('ghost')}/answer`, {
    headers: apiHeaders(),
    data: { answer: 'anything' },
  });
  expect(res.status(), await res.text()).toBe(404);
});
