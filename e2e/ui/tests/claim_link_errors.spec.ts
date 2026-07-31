/**
 * The claim-link error UX. A one-time claim link that is already burned, and a
 * garbage token that was never issued, both exchange to the
 * uniform 404 the public leg answers (no oracle) — the login screen must surface
 * that as its loud inline error, expand the key-paste fallback for an immediate
 * recovery, establish NO session, and never persist the dead token. The success
 * path + the isolation invariants live in `owned_key_journey.spec.ts` and the
 * pytest twin (`tests/owned_keys/test_claim_links.py`); this pins the failure UX the
 * browser owns.
 */
import { expect, test, type Page } from '@playwright/test';
import { API_KEY, createClaimLink, mintKey, uniq } from './helpers';

/** The login screen's own reaction to a failed one-time exchange: a loud inline
 * error, the key-paste fallback expanded, no navigation off `/login`, and no trace
 * of the dead token in the URL or storage. */
async function expectDeadClaim(page: Page, token: string): Promise<void> {
  // The inline error card renders (the exchange failure is loud, not silent).
  await expect(page.getByRole('alert')).toBeVisible();
  // The key-paste fallback is expanded so the operator has an immediate recovery.
  await expect(page.getByLabel('API key')).toBeVisible();
  // No session was established: the screen stays put on the credential front door and
  // never advances to the authenticated lander's first feature page. Asserting the exact
  // /login pathname is landing-agnostic — any successful claim would navigate off it.
  await expect(page).toHaveURL(/\/login/);
  expect(new URL(page.url()).pathname).toBe('/login');
  await expect(page.getByRole('heading', { name: 'Sign in to the Studio' })).toBeVisible();
  // The dead token is not persisted: fragment stripped, no query param, no storage.
  expect(await page.evaluate(() => window.location.hash)).toBe('');
  expect(page.url()).not.toContain('claim');
  const storageDump = await page.evaluate(() =>
    JSON.stringify([...Object.entries(localStorage), ...Object.entries(sessionStorage)]),
  );
  expect(storageDump).not.toContain(token);
}

test('a burned claim link (already exchanged once) shows the inline error and no session', async ({
  page,
  request,
}) => {
  // Arrange: mint a key, link it, and exchange the link once over the public leg so
  // the second (browser) exchange of the SAME token is a miss.
  const key = await mintKey(request, { userId: uniq('burned'), scopes: ['studio'] });
  const { token } = await createClaimLink(request, key);
  const burn = await request.post('/api/login/claim', { data: { token } });
  expect(burn.status(), await burn.text()).toBe(200);

  await page.goto(`/login#claim=${token}`);
  await expectDeadClaim(page, token);
});

test('a garbage claim token shows the inline error and no session', async ({ page }) => {
  // A token that was never issued exchanges to the same uniform 404 as a burned one.
  const token = `clm-${uniq('garbage')}`;
  await page.goto(`/login#claim=${token}`);
  await expectDeadClaim(page, token);
});
