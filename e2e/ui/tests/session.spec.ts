/**
 * Session expiry (shell auth). A logged-in session whose key is
 * revoked server-side must not render broken authed pages — the first failing
 * authed call clears the optimistic token and bounces to `/login?redirect=…`
 * (the same idiom `login.spec.ts` proves for a bad key at sign-in). A DISPOSABLE
 * key is used so the shared stack's root key is never revoked.
 */
import { expect } from '@playwright/test';
import { loginViaUi, mintKey, revokeKey, uniq } from './helpers';
import { needs, test } from './needs';

needs('kind:identity');

test('a revoked session bounces to /login?redirect on the next navigation', async ({
  page,
  request,
}) => {
  const userId = uniq('user');

  // Mint a disposable key holding the login key's own scopes and sign in with it through
  // the real screen.
  const disposableKey = await mintKey(request, { userId, description: 'e2e session-expiry disposable key' });

  // The key holds the login key's own scopes, so it is revoked even when signing in fails.
  try {
    await loginViaUi(page, disposableKey);
    await expect(page).toHaveURL(/\/observability/);
  } finally {
    // Revoke the key server-side — the session's credential is now invalid.
    await revokeKey(request, userId);
  }

  // API: the revoked key is denied over the same origin.
  const denied = await request.get('/api/tools', { headers: { 'x-api-key': disposableKey } });
  expect([401, 403]).toContain(denied.status());

  // UI: navigating makes the next authed call fail → the shell clears the token
  // and bounces to /login with a redirect param, never a broken authed page.
  await page.goto('/agents');
  await page.waitForURL(/\/login\?/);
  await expect(page).toHaveURL(/\/login\?.*redirect/);
});
