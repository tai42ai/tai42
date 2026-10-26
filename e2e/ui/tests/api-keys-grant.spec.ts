/**
 * The API keys tab's write state follows the caller's mint grant, and each row's History
 * follows the policy-history read. A member session (`editor`, `viewer`) reaches the mint
 * route through its projection, so it sees "Create key" and, on a key it owns, "Edit key";
 * neither role's projection covers the policy-history read, so no row shows History. An
 * owned key can never mint (a per-request rule the route table cannot show), so its tab
 * reads the "not permitted" status line instead of "Create key".
 *
 * The API arranges: the root admin invites each member and mints one key owned by that
 * member (an admin's named owner is kept; a non-admin lists only the keys it owns), and
 * mints the scoped owned key. The browser reads only what the tab draws.
 */
import { type APIRequestContext, expect, type Page } from '@playwright/test';
import { apiHeaders, createMemberSession, mintKey, seedCredential, uniq } from './helpers';
import { needs, test } from './needs';

needs('kind:identity', 'kind:accounts', 'mutable');

/** The member's own id, read from its projection (`GET /api/auth/me`). */
async function memberUserId(request: APIRequestContext, sessionToken: string): Promise<string> {
  const res = await request.get('/api/auth/me', { headers: apiHeaders(sessionToken) });
  expect(res.status(), await res.text()).toBe(200);
  return ((await res.json()) as { data: { user_id: string } }).data.user_id;
}

/** Mint a key owned by `ownerUserId` as the root admin and return the key's id. */
async function mintKeyOwnedBy(request: APIRequestContext, ownerUserId: string): Promise<string> {
  const keyId = uniq('member-key');
  const res = await request.post('/api/auth/api-keys', {
    headers: apiHeaders(),
    data: {
      user_id: keyId,
      description: 'e2e member-owned key',
      scopes: ['studio'],
      owner_user_id: ownerUserId,
    },
  });
  expect(res.status(), await res.text()).toBe(200);
  return keyId;
}

/** Open Settings and switch to the API keys tab (a member may land on an earlier tab). */
async function openApiKeysTab(page: Page, credential: string): Promise<void> {
  await seedCredential(page, credential);
  await page.goto('/settings');
  await expect(page.getByRole('heading', { level: 1, name: 'Settings' })).toBeVisible();
  await page.getByRole('tab', { name: 'API keys' }).click();
}

for (const role of ['editor', 'viewer'] as const) {
  test(`api keys (${role} session): the mint grant draws Create key and Edit, no History`, async ({
    page,
    request,
  }) => {
    const sessionToken = await createMemberSession(request, role);
    const keyId = await mintKeyOwnedBy(request, await memberUserId(request, sessionToken));

    await openApiKeysTab(page, sessionToken);

    await expect(page.getByRole('button', { name: 'Create key' })).toBeVisible();
    await expect(page.getByRole('button', { name: `Edit key ${keyId}` })).toBeVisible();
    await expect(page.getByRole('button', { name: `Policy history for ${keyId}` })).toHaveCount(0);
  });
}

test('api keys (owned key): no mint grant, the status line stands in for Create key', async ({
  page,
  request,
}) => {
  const ownedKey = await mintKey(request, { userId: uniq('owned'), scopes: ['studio'] });

  await openApiKeysTab(page, ownedKey);

  await expect(page.getByRole('status').filter({ hasText: 'Your access does not permit minting keys.' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Create key' })).toHaveCount(0);
});
