/**
 * The setup + sign-in front door over the REAL accounts-enabled studio stack, driven
 * through the Studio's `/login` screen. This spec runs against its OWN stack — the UNSEEDED
 * accounts-enabled studio stack the runner boots alongside the seeded one, on `SETUP_URL`
 * (the file-level `test.use` below points every navigation at it) — because it needs a
 * deployment with NO owner: `GET /api/login/methods` reports `needs_setup: true`, the screen
 * shows the "Set up this deployment" entry, and the setup door mints the first owner + key
 * live. The API-key-paste specs keep the seeded stack (an owner already exists there, so
 * `needs_setup` is false).
 *
 * The order below is LOAD-BEARING and the whole spec runs serially (workers: 1): the
 * `needs_setup: true` assertions run while no owner exists, then one test creates the owner
 * through the setup door — flipping `needs_setup` false for good — and the later tests sign
 * that owner in. Each test still gets a fresh browser context, so no session leaks between
 * tests; only the server-side owner row persists. Because that setup mutation is one-shot,
 * the spec pins itself to chromium — a second engine would find the deployment already
 * initialized and fail the `needs_setup=true` assertions.
 *
 * Every string and role/label matches the login page as built: the setup copy lives in the
 * page's `SETUP_COPY`, the sign-in form is the accounts provider's `Sign in` method.
 */
import { expect, test, type Page } from '@playwright/test';

import { SETUP_TOKEN, SETUP_URL } from './helpers';

// Drive the unseeded setup stack, not the seeded one the other specs share.
test.use({ baseURL: SETUP_URL });
// The setup flow initializes the stack exactly once, so run it on one engine.
test.skip(
  ({ browserName }) => browserName !== 'chromium',
  'The setup stack is initialized once; the login flow runs on chromium only.',
);

// The owner created once through the setup door and reused by the later sign-in tests. The
// password clears the accounts plugin's 10-character minimum; the display name uses the
// Acme placeholder.
const OWNER_NAME = 'Acme Owner';
const OWNER_EMAIL = 'owner@e2e.test';
const OWNER_PASSWORD = 'e2e-owner-password-123';

/** Count the `POST /api/setup` requests the page fires from the moment this is called, so a
 * test can assert the setup door was hit exactly once. */
function countSetupPosts(page: Page): () => number {
  let count = 0;
  page.on('request', (request) => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/setup') count += 1;
  });
  return () => count;
}

/** The setup token input. The token step's `<form>` and its field share the "Setup token"
 * accessible name, so a bare label lookup is ambiguous; scope to the single input inside the
 * token form landmark. */
function tokenField(page: Page) {
  return page.getByRole('form', { name: 'Setup token' }).locator('input');
}

/** The revealed owner form, scoped by its own submit button so the token step's controls and
 * the key-paste fallback never match. */
function ownerForm(page: Page) {
  return page.locator('form').filter({ has: page.getByRole('button', { name: 'Create owner and key' }) });
}

/** The accounts provider's password sign-in form (state B), scoped by its `Sign in` heading
 * so the page's `Sign in to the Studio` title and the key-paste form never match. */
function signInForm(page: Page) {
  return page.locator('form').filter({ has: page.getByRole('heading', { name: 'Sign in', exact: true }) });
}

test('needs_setup: the setup entry shows first, with no sign-in form and the key-paste fallback behind its toggle', async ({
  page,
}) => {
  await page.goto('/login');
  await expect(page.getByRole('heading', { name: 'Set up this deployment' })).toBeVisible();
  // State A stands in place of the sign-in screen: no "Sign in to the Studio" title.
  await expect(page.getByRole('heading', { name: 'Sign in to the Studio' })).toHaveCount(0);
  // The token step is first: the Setup token field and a primary Continue, with no owner
  // form yet (Continue reveals it client-side).
  await expect(tokenField(page)).toBeVisible();
  await expect(page.getByRole('button', { name: 'Continue', exact: true })).toBeVisible();
  await expect(page.getByLabel('Display name')).toHaveCount(0);
  // The key-paste fallback stays reachable behind its collapsed toggle.
  const keyToggle = page.getByRole('button', { name: 'Use an API key instead' });
  await expect(keyToggle).toBeVisible();
  await expect(page.getByLabel('API key')).toHaveCount(0);
  await keyToggle.click();
  await expect(page.getByLabel('API key')).toBeVisible();
});

test('a wrong token is rejected inline after Create, without initializing, and Change token keeps the value', async ({
  page,
}) => {
  const setupPosts = countSetupPosts(page);
  const wrongToken = 'definitely-not-the-token';
  await page.goto('/login');
  await tokenField(page).fill(wrongToken);
  await page.getByRole('button', { name: 'Continue', exact: true }).click();
  // The owner form is revealed client-side; the token has not been sent yet.
  const form = ownerForm(page);
  await expect(form.getByLabel('Display name')).toBeVisible();
  await form.getByLabel('Display name').fill(OWNER_NAME);
  await form.getByLabel('Email').fill(OWNER_EMAIL);
  await form.getByLabel('Password', { exact: true }).fill(OWNER_PASSWORD);
  await form.getByRole('button', { name: 'Create owner and key' }).click();
  // The setup door refuses the token: the inline alert shows, nothing initializes.
  await expect(page.getByRole('alert')).toContainText(
    'The setup token was not accepted. Check the server log for the current token and try again.',
  );
  await expect(page.getByRole('heading', { name: 'Deployment set up' })).toHaveCount(0);
  expect(setupPosts()).toBe(1);
  // Change token returns to the token step with the entered value kept.
  await page.getByRole('button', { name: 'Change token' }).click();
  await expect(tokenField(page)).toHaveValue(wrongToken);
  await expect(page.getByLabel('Display name')).toHaveCount(0);
});

test('the correct token creates the owner and key through one POST /api/setup, shows the key once, and signs in', async ({
  page,
}) => {
  const setupPosts = countSetupPosts(page);
  await page.goto('/login');
  await tokenField(page).fill(SETUP_TOKEN);
  await page.getByRole('button', { name: 'Continue', exact: true }).click();

  const form = ownerForm(page);
  await expect(form.getByLabel('Display name')).toBeVisible();
  // The accounts provider attaches a login (setup_login.kinds carries password + invite), so
  // the owner form carries Email, the login radio (password default), and a password field.
  await expect(form.getByLabel('Email')).toBeVisible();
  await expect(form.getByRole('radio', { name: 'Set a password now' })).toBeChecked();
  await expect(form.getByRole('radio', { name: 'Send me an invite link' })).toBeVisible();
  await form.getByLabel('Display name').fill(OWNER_NAME);
  await form.getByLabel('Email').fill(OWNER_EMAIL);
  await form.getByLabel('Password', { exact: true }).fill(OWNER_PASSWORD);
  await form.getByRole('button', { name: 'Create owner and key' }).click();

  // The success view: the owner, the once-shown key, its warning, and the password line.
  await expect(page.getByRole('heading', { name: 'Deployment set up' })).toBeVisible();
  await expect(page.getByText(`Signed-in owner: ${OWNER_NAME}`)).toBeVisible();
  // The once-shown owner key: a CopyField renders the "Owner API key" label beside the key in
  // a <code> value (not a form field), under the `setup-api-key` test id.
  await expect(page.getByText('Owner API key')).toBeVisible();
  const keyValue = page.getByTestId('setup-api-key').locator('code');
  await expect(keyValue).toBeVisible();
  expect(await keyValue.innerText()).toMatch(/^sk-/);
  await expect(page.getByText('Shown once — store it now. It cannot be retrieved again.')).toBeVisible();
  await expect(page.getByText(`Password set for ${OWNER_EMAIL}.`)).toBeVisible();
  // Exactly one setup door hit initialized the deployment.
  expect(setupPosts()).toBe(1);

  // Continue to the Studio signs the operator in as the new owner and lands on the
  // capability-gated landing's first covered entry — Dashboard, served at /observability.
  await page.getByRole('button', { name: 'Continue to the Studio' }).click();
  await page.waitForURL('**/observability');
  await expect(page.getByRole('heading', { name: 'Deployment set up' })).toHaveCount(0);
});

test('after setup the screen is the plain sign-in form, with no setup entry', async ({ page }) => {
  await page.goto('/login');
  await expect(page.getByRole('heading', { name: 'Sign in to the Studio' })).toBeVisible();
  // The owner now exists, so needs_setup is false: no setup entry.
  await expect(page.getByRole('heading', { name: 'Set up this deployment' })).toHaveCount(0);
  await expect(signInForm(page).getByRole('heading', { name: 'Sign in', exact: true })).toBeVisible();
});

test('password login lands on the requested returnTo', async ({ page }) => {
  await page.goto(`/login?redirect=${encodeURIComponent('/settings')}`);
  const form = signInForm(page);
  await form.getByLabel('Email').fill(OWNER_EMAIL);
  await form.getByLabel('Password').fill(OWNER_PASSWORD);
  await form.getByRole('button', { name: 'Sign in' }).click();
  await page.waitForURL('**/settings');
  await expect(page).toHaveURL(/\/settings/);
});

test('a wrong password shows the form error and does not navigate', async ({ page }) => {
  await page.goto('/login');
  const form = signInForm(page);
  await form.getByLabel('Email').fill(OWNER_EMAIL);
  await form.getByLabel('Password').fill('wrong-password-000');
  await form.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('alert')).toContainText('Sign-in failed');
  await expect(page).toHaveURL(/\/login/);
  await expect(page.getByRole('heading', { name: 'Sign in to the Studio' })).toBeVisible();
});
