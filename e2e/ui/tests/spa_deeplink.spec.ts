/**
 * The SPA deep-link regression (Mission 20 Feature 2, needs PLAN_7). With access
 * control ON and the Studio SPA served by the skeleton, bookmarking or refreshing an
 * inner client route (`/agents`) WITHOUT a session must serve the dataless SPA shell
 * (`index.html`, HTTP 200) — not a 403 — so the app can then render its login gate
 * client-side (all data sits behind the authed `/api/*`). The hardened boundary is the
 * other half: the shell fix must NOT have opened the API or the operational routes —
 * `/health` keeps its own public handler and every `/api/*` stays gated (401/403).
 *
 * Modeled on `owned_key_journey.spec.ts` (a reload/deep-link surviving load): here the
 * point is the UNauthenticated deep link, so no credential is seeded — the browser hits
 * `/agents` cold, exactly as a bookmark does.
 *
 * PLAN_7 is landed in the skeleton the studio stack boots, so this runs un-skipped. If
 * PLAN_7 ever regresses, the deep link would 403 here rather than serve the 200 shell.
 */
import { expect, test } from '@playwright/test';

test('deep-link /agents with no session serves the 200 SPA shell; /health + /api/* stay gated', async ({
  page,
  request,
}) => {
  // -- API level: the unauthenticated deep link serves the SPA shell (200 HTML) --------
  const shell = await request.get('/agents');
  expect(shell.status(), await shell.text()).toBe(200);
  expect(shell.headers()['content-type'] ?? '').toContain('html');
  expect((await shell.text()).toLowerCase()).toContain('<html');

  // `/health` keeps its own public handler (200), it is not the SPA shell.
  const health = await request.get('/health');
  expect(health.status(), await health.text()).toBe(200);

  // The API stays gated for an unauthenticated caller — the shell fix opened neither the
  // data API nor the agents feature route it fronts.
  const apiTools = await request.get('/api/tools');
  expect([401, 403], `GET /api/tools should stay gated, got ${apiTools.status()}`).toContain(apiTools.status());
  const apiAgents = await request.get('/api/agents');
  expect([401, 403], `GET /api/agents should stay gated, got ${apiAgents.status()}`).toContain(apiAgents.status());

  // -- Browser level: a cold bookmark to /agents loads the shell (200) and the app shows
  // its login gate client-side (no seeded session) ------------------------------------
  const response = await page.goto('/agents');
  expect(response?.status()).toBe(200);
  // The client-side gate renders: the login screen's key-paste affordance (a toggle when
  // an accounts provider is present, else the key field directly) becomes visible.
  const toggle = page.getByRole('button', { name: 'Use an API key instead' });
  const keyField = page.getByLabel('API key');
  await expect(toggle.or(keyField).first()).toBeVisible();
});
