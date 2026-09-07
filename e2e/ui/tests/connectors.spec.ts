/**
 * Connectors OAuth flow. The pytest twin proves
 * connect-on-A / complete-on-B + token-encrypted-at-rest against the fixture
 * provider + stub IdP (`tests/connectors/test_oauth_and_refresh.py`); the browser
 * never clicked through it. The studio stack carries the same fixture connector
 * provider + stub IdP (the `build_studio_stack` extension), so here the real
 * browser drives the OAuth popup: Connect opens the popup, the threaded stub IdP
 * redirects back through the deployment's callback bridge, and the connection
 * lands (UI badge + API connections list). It is then DISCONNECTED from the UI.
 *
 * The loud negative scripts the stub IdP to DENY consent (`/_deny`): the popup
 * comes back with an OAuth error, the UI surfaces the failed sign-in, and no
 * connection is created.
 */
import { expect, test, type APIRequestContext, type Locator, type Page } from '@playwright/test';
import { apiHeaders, awaitMutation, IDP_CONTROL_URL, seedCredential, uniq } from './helpers';

const PROVIDER = 'E2E Stub IdP';

/** The connections list under `data.items`, over the same origin. */
async function connectionAliases(request: APIRequestContext): Promise<string[]> {
  const res = await request.get('/api/connectors/connections', { headers: apiHeaders() });
  expect(res.status(), await res.text()).toBe(200);
  const body = (await res.json()) as { data: { items: Array<{ alias: string }> } };
  return body.data.items.map((c) => c.alias);
}

/**
 * The fixture provider's OWN card in the Providers section. Never a `.first()`
 * Connect button: a provider that already carries a connection offers "Add another
 * account" instead of "Connect", so a first-match Connect belongs to a DIFFERENT
 * provider and its dialog is titled for that one.
 */
function providerCard(page: Page): Locator {
  return page.getByRole('region', { name: 'Providers' }).locator('.tai-card').filter({ hasText: PROVIDER });
}

/** Open the fixture provider's connect dialog and name the connection. */
async function openConnectDialog(page: Page, alias: string): Promise<Locator> {
  await providerCard(page)
    .getByRole('button', { name: /^(Connect|Add another account)$/ })
    .click();
  const dialog = page.getByRole('dialog', { name: `Connect ${PROVIDER}` });
  await dialog.getByRole('textbox', { name: 'Alias' }).fill(alias);
  return dialog;
}

/** Aliases a test asked the stack to connect. The afterEach net-removes whatever a
 *  FAILING test left behind — the happy path disconnects its own as an assertion, so
 *  on success this is a no-op. Residue is not inert here: while the fixture provider
 *  holds a connection its card offers "Add another account", so a leftover changes
 *  what the next test drives. */
const connectedAliases = new Set<string>();

test.afterEach(async ({ request }) => {
  const aliases = new Set(connectedAliases);
  connectedAliases.clear();

  // The denial knob is stack-global and outlives the test that flipped it, so restore
  // the granting default here rather than inside a test body — a test that ends early
  // (a timeout) never reaches its own restore.
  const allow = await request.post(`${IDP_CONTROL_URL}/_allow`);
  expect(allow.status()).toBe(200);

  if (aliases.size === 0) return;
  const res = await request.get('/api/connectors/connections', { headers: apiHeaders() });
  expect(res.status(), await res.text()).toBe(200);
  const body = (await res.json()) as {
    data: { items: Array<{ alias: string; connection_id: string }> };
  };
  for (const item of body.data.items) {
    if (!aliases.has(item.alias)) continue;
    const deleted = await request.delete(`/api/connectors/connections/${item.connection_id}`, {
      headers: apiHeaders(),
    });
    expect(deleted.status(), await deleted.text()).toBe(200);
  }
});

test('connect a provider through the OAuth popup, then disconnect it', async ({ page, request }) => {
  const alias = uniq('conn');
  connectedAliases.add(alias);

  await seedCredential(page);
  await page.goto('/connectors');

  // The fixture provider is listed, disconnected.
  await expect(page.getByText(PROVIDER)).toBeVisible();

  // Open the connect dialog for the provider and start the OAuth flow.
  const dialog = await openConnectDialog(page, alias);

  // Clicking Connect calls startConnect → opens the OAuth popup at the stub IdP,
  // which redirects back through the callback bridge and postMessages the code to
  // the opener; the flow completes without any manual popup interaction.
  //
  // The popup closes the moment it hands the code back — BEFORE the app's completion
  // POST returns — so nothing on screen yet reflects the completion. Arm the wait for
  // the completion RESPONSE before the click and await it: that response IS the event
  // the list repaint hangs off, and it carries the reload fan-out's full server-side
  // cost. Asserting the repaint against a fixed budget started at popup close races
  // that work instead of observing it.
  const completed = awaitMutation(
    page,
    (r) => r.url().includes('/api/connectors/oauth/complete') && r.request().method() === 'POST',
  );
  const popupPromise = page.waitForEvent('popup');
  await dialog.getByRole('button', { name: 'Connect' }).click();
  const popup = await popupPromise;
  // The popup self-drives (IdP redirect → bridge → callback → postMessage → close).
  await popup.waitForEvent('close');
  const completion = await completed;
  expect(completion.status(), await completion.text()).toBe(200);

  // UI: the connection lands in the Connections list (its alias becomes a link).
  await expect(page.getByRole('link', { name: alias })).toBeVisible();

  // API: the same connection is present over the same origin. The connections GET
  // round-robins across the MULTIWORKER(2) port, so it can land on the sibling worker
  // whose reload has not yet converged; poll rather than read once.
  await expect.poll(async () => await connectionAliases(request)).toContain(alias);

  // DISCONNECT from the UI: open the connection, confirm the disconnect dialog. Same
  // shape as the connect above — the DELETE reloads this worker and awaits the
  // sibling's reload, so the list repaint is asserted after that response lands.
  await page.getByRole('link', { name: alias }).click();
  await expect(page.getByRole('heading', { name: alias })).toBeVisible();
  await page.getByRole('button', { name: 'Disconnect' }).click();
  const disconnected = awaitMutation(
    page,
    (r) =>
      /\/api\/connectors\/connections\/[^/]+$/.test(new URL(r.url()).pathname) &&
      r.request().method() === 'DELETE',
  );
  await page
    .getByRole('dialog', { name: 'Disconnect this connection?' })
    .getByRole('button', { name: 'Disconnect' })
    .click();
  const deletion = await disconnected;
  expect(deletion.status(), await deletion.text()).toBe(200);
  connectedAliases.delete(alias);

  // UI: back on the list, the connection is gone.
  await expect(page.getByRole('link', { name: alias })).toHaveCount(0);
  // API: the connections list no longer carries it. Same MULTIWORKER round-robin as the
  // connect assertion above.
  await expect.poll(async () => await connectionAliases(request)).not.toContain(alias);
});

test('a denied authorization surfaces a loud failure and creates no connection', async ({
  page,
  request,
}) => {
  const alias = uniq('denied');
  connectedAliases.add(alias);

  // Script the stub IdP to refuse consent on the next authorize. The afterEach restores
  // the granting default whether this test passes, fails, or times out.
  const deny = await request.post(`${IDP_CONTROL_URL}/_deny`);
  expect(deny.status()).toBe(200);

  await seedCredential(page);
  await page.goto('/connectors');

  const dialog = await openConnectDialog(page, alias);

  // The IdP redirects back with error=access_denied; the callback relays it and the
  // popup closes without granting a code. The app still POSTs the completion (carrying
  // the error), and the dialog's notice paints off that response — so observe it.
  const completed = awaitMutation(
    page,
    (r) => r.url().includes('/api/connectors/oauth/complete') && r.request().method() === 'POST',
  );
  const popupPromise = page.waitForEvent('popup');
  await dialog.getByRole('button', { name: 'Connect' }).click();
  const popup = await popupPromise;
  await popup.waitForEvent('close');
  const completion = await completed;
  expect(completion.status(), await completion.text()).toBe(200);

  // UI: the dialog shows the loud sign-in-cancelled notice, not a silent return.
  await expect(dialog.locator('[data-kind="cancelled"]')).toBeVisible();

  // API: no connection was created for the denied alias.
  expect(await connectionAliases(request)).not.toContain(alias);
});
