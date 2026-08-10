/**
 * The sign-in front door over the REAL accounts-enabled studio stack. The stack
 * installs the Postgres accounts provider alongside the redis key provider, so
 * `/api/login/methods` is non-empty and the screen renders its metadata-driven
 * password form around the permanent key-paste fallback (collapsed behind the
 * "Use an API key instead" toggle).
 *
 * These tests share ONE live stack and its Postgres database, and run serially
 * (workers: 1). The order below is load-bearing: the first-run assertions run
 * while no owner exists, then one test creates the owner through the bootstrap
 * form; every later test reuses that owner. Each Playwright test still gets a
 * fresh browser context, so the session never leaks between tests — only the
 * server-side owner row persists.
 */
import { expect, test, type Page } from '@playwright/test';
import { BOOTSTRAP_TOKEN, apiHeaders, loginViaUi } from './helpers';

// The owner created once through the bootstrap form and reused by the later
// password-login tests. The password clears the plugin's 10-char minimum.
const OWNER_EMAIL = 'owner@e2e.test';
const OWNER_PASSWORD = 'e2e-owner-password-123';

/** Fill and submit the metadata-driven password form, scoping to that form so a
 * fresh-stack bootstrap form (same Email/Password labels) is never matched. */
async function passwordLogin(
  page: Page,
  email: string,
  password: string,
  redirect?: string,
): Promise<void> {
  await page.goto(redirect === undefined ? '/login' : `/login?redirect=${encodeURIComponent(redirect)}`);
  const form = page.locator('form').filter({ has: page.getByRole('heading', { name: 'Sign in', exact: true }) });
  await form.getByLabel('Email').fill(email);
  await form.getByLabel('Password').fill(password);
  await form.getByRole('button', { name: 'Sign in' }).click();
}

test('the login screen renders the metadata-driven password form with the key-paste fallback behind a toggle', async ({
  page,
}) => {
  await page.goto('/login');
  await expect(page.getByRole('heading', { name: 'Sign in to the Studio' })).toBeVisible();
  // The password method rendered from the accounts provider's login metadata.
  await expect(page.getByRole('heading', { name: 'Sign in', exact: true })).toBeVisible();
  // The key-paste form is collapsed: the field is absent until the toggle is used.
  const toggle = page.getByRole('button', { name: 'Use an API key instead' });
  await expect(toggle).toBeVisible();
  await expect(page.getByLabel('API key')).toHaveCount(0);
  await toggle.click();
  await expect(page.getByLabel('API key')).toBeVisible();
});

test('a fresh deployment shows the first-run owner-creation form', async ({ page }) => {
  await page.goto('/login');
  // The bootstrap status + the owner-creation form appear only while no account exists.
  await expect(page.getByText('No accounts exist yet — create the admin account.')).toBeVisible();
  const bootstrapForm = page
    .locator('form')
    .filter({ has: page.getByRole('heading', { name: 'Create the first owner' }) });
  await expect(bootstrapForm).toBeVisible();
  // The gated posture carries the bootstrap-token field (the gate is not open).
  await expect(bootstrapForm.getByLabel('Bootstrap token')).toBeVisible();
});

test('the bootstrap form creates the owner and lands authenticated', async ({ page }) => {
  await page.goto('/login');
  const bootstrapForm = page
    .locator('form')
    .filter({ has: page.getByRole('heading', { name: 'Create the first owner' }) });
  await bootstrapForm.getByLabel('Email').fill(OWNER_EMAIL);
  await bootstrapForm.getByLabel('Password').fill(OWNER_PASSWORD);
  await bootstrapForm.getByLabel('Bootstrap token').fill(BOOTSTRAP_TOKEN);
  await bootstrapForm.getByRole('button', { name: 'Create the first owner' }).click();
  // Default returnTo is `/`: the lander redirects the signed-in owner to its first
  // covered feature entry — Dashboard, served at /observability for a full projection.
  await page.waitForURL('**/observability');
  // The owner exists now, so the bootstrap form is gone from the login screen.
  await page.goto('/login');
  await expect(page.getByRole('heading', { name: 'Create the first owner' })).toHaveCount(0);
  await expect(page.getByRole('heading', { name: 'Sign in', exact: true })).toBeVisible();
});

test('password login lands on the requested returnTo', async ({ page }) => {
  await passwordLogin(page, OWNER_EMAIL, OWNER_PASSWORD, '/settings');
  await page.waitForURL('**/settings');
  await expect(page).toHaveURL(/\/settings/);
});

test('a wrong password shows the error state and does not navigate', async ({ page }) => {
  await passwordLogin(page, OWNER_EMAIL, 'wrong-password-000');
  // The server 401 surfaces as the generic sign-in error; the screen stays put.
  await expect(page.getByRole('alert')).toContainText('Sign-in failed');
  await expect(page).toHaveURL(/\/login/);
  await expect(page.getByRole('heading', { name: 'Sign in', exact: true })).toBeVisible();
});

test('a valid key logs in via the key-paste fallback, lands on the Dashboard, and the API agrees', async ({
  page,
  request,
}) => {
  await loginViaUi(page);
  await expect(page).toHaveURL(/\/observability/);
  // We left the credential screen behind.
  await expect(page.getByRole('heading', { name: 'Sign in to the Studio' })).toHaveCount(0);
  // API: the same key lists tools over the same origin.
  const res = await request.get('/api/tools', { headers: apiHeaders() });
  expect(res.status(), await res.text()).toBe(200);
  const body = (await res.json()) as { data: unknown };
  expect(body).toHaveProperty('data');
});

test('a garbage key is rejected by the API and bounces back to /login', async ({ page, request }) => {
  const bad = 'sk-not-a-real-key-000';
  // API: the guard denies it (401 unauthenticated / 403 unauthorized).
  const res = await request.get('/api/tools', { headers: { 'x-api-key': bad } });
  expect([401, 403]).toContain(res.status());
  // UI: login is optimistic (it stores the key and navigates), so the bounce is
  // driven by the first authed call failing 401 — the app clears the token and
  // redirects back to /login with a `redirect` search param. Reach the key-paste
  // form through the toggle (accounts present), then submit the bad key.
  await page.goto('/login');
  await page.getByRole('button', { name: 'Use an API key instead' }).click();
  const keyField = page.getByLabel('API key');
  await keyField.fill(bad);
  await page.getByRole('checkbox', { name: 'Remember on this device (this browser session)' }).check();
  await page.locator('form').filter({ has: keyField }).getByRole('button', { name: 'Sign in' }).click();
  await page.waitForURL(/\/login\?/);
  await expect(page).toHaveURL(/\/login\?.*redirect/);
});

test('the accounts plugin mounts its users-admin page in the Studio shell after an admin login', async ({
  page,
}) => {
  await passwordLogin(page, OWNER_EMAIL, OWNER_PASSWORD);
  await page.waitForURL('**/observability');
  // The accounts plugin's users-admin entry declares section 'Administration', so it
  // lands in the Administration group of the primary nav.
  const usersNav = page
    .getByRole('navigation', { name: 'Primary' })
    .getByRole('list', { name: 'Administration' })
    .getByRole('link', { name: 'Users' });
  await expect(usersNav).toBeVisible();
  await usersNav.click();
  await expect(page).toHaveURL(/\/plugins\/tai42_accounts_postgres\/users/);
  // The page's own toolbar heading + the users table populated with the owner row.
  await expect(page.getByRole('heading', { name: 'Users' })).toBeVisible();
  await expect(page.getByRole('table')).toBeVisible();
  await expect(page.getByRole('cell', { name: OWNER_EMAIL })).toBeVisible();
});
