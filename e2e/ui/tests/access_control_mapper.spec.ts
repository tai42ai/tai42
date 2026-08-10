/**
 * Scope ↔ URL mapping enforces. The mapper is a SUB-VIEW
 * of `/settings` (the "API keys" tab), not its own route. Via the mapper: create
 * a scope and assign `/api/templates` to it (the click-based, non-DnD path).
 * Asserts the UI shows the route under the scope and `GET /api/auth/scopes`
 * returns the `{url: scope_id}` mapping.
 *
 * ENFORCEMENT — deny-wins AND-across-tiers: after the mapping, `/api/templates`
 * resolves to BOTH the mapper-created scope (exact route row) AND `studio` (the
 * boot recipe's blanket authed-`/api` pattern). The guard requires the caller be
 * authorized for EVERY resolved id, so a disposable key holding only `studio` is
 * denied until the mapper scope is added too. The root `*`-scope key (the live
 * session) passes throughout — the suite never locks itself out.
 *
 * Precondition: the Settings write surface renders only when config mode is
 * writable (`GET /api/config/mode` returns `{config_mode}`; the client treats the
 * absent `read_only` as `false`).
 */
import { expect, test } from '@playwright/test';
import { apiHeaders, seedCredential, uniq } from './helpers';

test('map /api/templates to a new scope; UI + API + AND-across-tiers enforcement', async ({ page, request }) => {
  const scope = uniq('scope');
  const route = '/api/templates';

  await seedCredential(page);
  await page.goto('/settings');
  await page.getByRole('tab', { name: 'API keys' }).click();
  await expect(page.getByRole('heading', { name: 'Access control' })).toBeVisible();

  // Create the scope (a client-side pending scope until its first route lands).
  await page.getByRole('textbox', { name: 'New scope name' }).fill(scope);
  await page.getByRole('button', { name: 'Add scope' }).click();

  // Assign the route through the scope's own "Add route" affordance (persists it).
  const zone = page.getByRole('group', { name: new RegExp(`^Scope ${scope},`) });
  await zone.getByRole('textbox', { name: `Add route to ${scope}` }).fill(route);
  await zone.getByRole('button', { name: 'Add route' }).click();

  // UI: the route chip now sits inside the scope's zone. ``exact`` — the chip and
  // its own "Remove URL /api/templates" control both carry the route text, so a
  // substring match resolves to two elements (strict-mode violation).
  await expect(zone.getByRole('button', { name: route, exact: true })).toBeVisible();

  // API: the scope map carries the assignment.
  const scopesRes = await request.get('/api/auth/scopes', { headers: apiHeaders() });
  expect(scopesRes.status(), await scopesRes.text()).toBe(200);
  const scopesBody = (await scopesRes.json()) as { data: Record<string, string> };
  expect(scopesBody.data[route]).toBe(scope);

  // Enforcement with a SECOND, disposable key (never touch the root wildcard key).
  const userId = uniq('user');
  const mintRes = await request.post('/api/auth/api-keys', {
    headers: apiHeaders(),
    data: { user_id: userId, description: 'e2e disposable enforcement key', scopes: ['studio'] },
  });
  expect(mintRes.status(), await mintRes.text()).toBe(200);
  const disposableKey = ((await mintRes.json()) as { data: { api_key: string } }).data.api_key;

  // It passes the blanket `/api` pattern (studio) but lacks the mapper scope → 403.
  const denied = await request.get(route, { headers: { 'x-api-key': disposableKey } });
  expect(denied.status()).toBe(403);

  // Grant the mapper scope too (PUT replaces the stored set, so resend both).
  const grant = await request.put(`/api/auth/api-keys/${userId}`, {
    headers: apiHeaders(),
    data: { scopes: ['studio', scope] },
  });
  expect(grant.status(), await grant.text()).toBe(200);

  // Now the key is authorized for EVERY resolved id → 200.
  const allowed = await request.get(route, { headers: { 'x-api-key': disposableKey } });
  expect(allowed.status(), await allowed.text()).toBe(200);
});
