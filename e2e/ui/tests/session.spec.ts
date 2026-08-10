/**
 * Session expiry (shell auth). A logged-in session whose key is
 * revoked server-side must not render broken authed pages — the first failing
 * authed call clears the optimistic token and bounces to `/login?redirect=…`
 * (the same idiom `login.spec.ts` proves for a bad key at sign-in). A DISPOSABLE
 * key is used so the shared stack's root key is never revoked.
 */
import { expect, test } from '@playwright/test';
import { apiHeaders, loginViaUi, uniq } from './helpers';

test('a revoked session bounces to /login?redirect on the next navigation', async ({
  page,
  request,
}) => {
  const userId = uniq('user');

  // Mint a disposable full-scope key and sign in with it through the real screen.
  const mint = await request.post('/api/auth/api-keys', {
    headers: apiHeaders(),
    data: { user_id: userId, description: 'e2e session-expiry disposable key', scopes: ['studio'] },
  });
  expect(mint.status(), await mint.text()).toBe(200);
  const disposableKey = ((await mint.json()) as { data: { api_key: string } }).data.api_key;

  await loginViaUi(page, disposableKey);
  await expect(page).toHaveURL(/\/observability/);

  // Revoke the key server-side — the session's credential is now invalid.
  const revoke = await request.delete(`/api/auth/api-keys/${userId}`, { headers: apiHeaders() });
  expect(revoke.status(), await revoke.text()).toBe(200);

  // API: the revoked key is denied over the same origin.
  const denied = await request.get('/api/tools', { headers: { 'x-api-key': disposableKey } });
  expect([401, 403]).toContain(denied.status());

  // UI: navigating makes the next authed call fail → the shell clears the token
  // and bounces to /login with a redirect param, never a broken authed page.
  await page.goto('/agents');
  await page.waitForURL(/\/login\?/);
  await expect(page).toHaveURL(/\/login\?.*redirect/);
});
