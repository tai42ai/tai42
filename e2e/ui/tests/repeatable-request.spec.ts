/**
 * The resend seam for requests that are safe to repeat, and the request context that
 * routes its reads through it, driven against a fake request context: no stack is
 * addressed.
 */
import { expect, type APIRequestContext, type APIResponse } from '@playwright/test';

import { repeatableReads, sendRepeatable } from './helpers';
import { needs, test } from './needs';

needs();

const RESET = 'apiRequestContext.get: socket hang up';

/** A fake context whose `get`/`post` play the given outcomes in order, recording each call. */
function fakeContext(outcomes: (APIResponse | Error)[]): { request: APIRequestContext; calls: string[] } {
  const calls: string[] = [];
  const play = (method: string) => async (url: string): Promise<APIResponse> => {
    calls.push(`${method} ${url}`);
    const next = outcomes.shift();
    if (next === undefined) throw new Error('fake context: no outcome left');
    if (next instanceof Error) throw next;
    return next;
  };
  return { request: { get: play('GET'), post: play('POST') } as unknown as APIRequestContext, calls };
}

const answer = { status: () => 200 } as unknown as APIResponse;

test('a reset before any reply is resent once and the second answer is returned', async () => {
  const { request, calls } = fakeContext([new Error(RESET), answer]);
  const res = await sendRepeatable(request, 'get', '/api/config/env');
  expect(res).toBe(answer);
  expect(calls).toEqual(['GET /api/config/env', 'GET /api/config/env']);
});

test('a resend is recorded as a test annotation naming the request and the reset', async () => {
  const { request } = fakeContext([new Error(RESET), answer]);
  await sendRepeatable(request, 'get', '/api/config/env');
  expect(test.info().annotations).toContainEqual({ type: 'resent', description: `GET /api/config/env: ${RESET}` });
});

test('an ECONNRESET on the no-op reload probe is resent once', async () => {
  const { request, calls } = fakeContext([new Error('apiRequestContext.post: read ECONNRESET'), answer]);
  const res = await sendRepeatable(request, 'post', '/api/config/reload', { data: { targets: [] } });
  expect(res).toBe(answer);
  expect(calls).toEqual(['POST /api/config/reload', 'POST /api/config/reload']);
});

test('any other failure propagates at once, unsent again', async () => {
  const { request, calls } = fakeContext([new Error('apiRequestContext.get: Timeout 30000ms exceeded'), answer]);
  await expect(sendRepeatable(request, 'get', '/api/config/env')).rejects.toThrow('Timeout 30000ms exceeded');
  expect(calls).toEqual(['GET /api/config/env']);
});

test('a second reset propagates: the request is resent only once', async () => {
  const { request, calls } = fakeContext([new Error(RESET), new Error(RESET), answer]);
  await expect(sendRepeatable(request, 'get', '/api/config/env')).rejects.toThrow(
    `GET /api/config/env: reset on both attempts: ${RESET}; then ${RESET}`,
  );
  expect(calls).toEqual(['GET /api/config/env', 'GET /api/config/env']);
});

test('the routed context resends a read once', async () => {
  const { request, calls } = fakeContext([new Error(RESET), answer]);
  const res = await repeatableReads(request).get('/api/manifest/preserved');
  expect(res).toBe(answer);
  expect(calls).toEqual(['GET /api/manifest/preserved', 'GET /api/manifest/preserved']);
});

test('the routed context never resends a write: the reset propagates after one call', async () => {
  const reset = new Error('apiRequestContext.post: socket hang up');
  const { request, calls } = fakeContext([reset, answer]);
  await expect(repeatableReads(request).post('/api/config/env', { data: {} })).rejects.toBe(reset);
  expect(calls).toEqual(['POST /api/config/env']);
});
