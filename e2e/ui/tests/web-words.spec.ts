/**
 * A web route's own `transport` words in the REAL widget bundle: the words door stores the
 * identity's sentences, the chat page carries them as `data-words`, and a refused answer
 * reads the route's sentence with the answer door's own reason filled into `{reason}`.
 *
 * The refusal comes from the answer door itself: the form's `multipleOf` shape is checked by
 * the door, not by the widget, so the widget sends `3` and the door refuses it with a reason.
 * The ask sets no `answer.rejected` sentence of its own, so the line is the route's.
 */
import { expect, type APIResponse } from '@playwright/test';

import { apiHeaders, resolveWebVisitorId, uniq, WEB_SESSION_COOKIE } from './helpers';
import { needs, test } from './needs';

needs('kind:channels:web', 'kind:interactions', 'store:redis');

const SCHEMA = {
  type: 'object',
  required: ['even'] as string[],
  properties: { even: { type: 'integer', title: 'Even', multipleOf: 2 } },
};

test("a refused answer reads the route's own transport sentence with the door's reason", async ({
  page,
  request,
  browserName,
}) => {
  test.skip(browserName === 'webkit', 'Linux CI WebKit delivers fetch-streams unreliably');
  const identity = uniq('wordsite').replace(/_/g, '-');
  const words = { transport: { answer_refused: 'Not this time: {reason}' } };
  const put = await request.put(`/api/channels/web/words/${identity}`, { headers: apiHeaders(), data: words });
  expect(put.status(), await put.text()).toBe(200);
  try {
    await page.goto(`/api/channels/web/chat/${identity}`);
    const token = (await page.context().cookies()).find((c) => c.name === WEB_SESSION_COOKIE)?.value;
    expect(token, 'the chat page minted no web-session cookie').toBeTruthy();
    const visitorId = await resolveWebVisitorId(token as string);

    const question = uniq('words_q');
    const askPromise: Promise<APIResponse> = request.post('/api/run-tool', {
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
          timeout: 120,
        },
      },
    });

    const card = page.locator('.tcw-question-form');
    const answer = card.getByRole('button', { name: 'Answer', exact: true });
    await card.getByLabel('Even', { exact: true }).fill('3');
    const refused = page.waitForResponse(
      (res) => res.request().method() === 'POST' && /\/questions\/[^/]+\/answer$/.test(res.url()),
    );
    await answer.click();
    const refusal = await refused;
    expect(refusal.status()).toBe(400);
    const body = (await refusal.json()) as { error: string; code: string; reason: string };
    expect(body.code).toBe('answer_refused');
    expect(body.reason).toBeTruthy();
    await expect(page.locator('.tcw-question-error')).toHaveText(`Not this time: ${body.reason}`);

    // The question stays answerable: a value in the shape resolves the parked ask.
    await card.getByLabel('Even', { exact: true }).fill('4');
    await answer.click();
    const askRes = await askPromise;
    expect(askRes.status(), await askRes.text()).toBe(200);
    expect(((await askRes.json()) as { data: unknown }).data).toEqual({
      kind: 'result',
      result: { answer: { even: 4 }, reply_to: null, params: null },
    });
  } finally {
    const cleared = await request.delete(`/api/channels/web/words/${identity}`, { headers: apiHeaders() });
    expect(cleared.status(), await cleared.text()).toBe(200);
  }
});
