/**
 * Connectors OAuth flow (Mission 9 connectors UI). The pytest twin proves
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
import { expect, test } from '@playwright/test';
import { apiHeaders, IDP_CONTROL_URL, seedCredential, uniq } from './helpers';

const PROVIDER = 'E2E Stub IdP';

/** The connections list under `data.items`, over the same origin. */
async function connectionAliases(
  request: import('@playwright/test').APIRequestContext,
): Promise<string[]> {
  const res = await request.get('/api/connectors/connections', { headers: apiHeaders() });
  expect(res.status(), await res.text()).toBe(200);
  const body = (await res.json()) as { data: { items: Array<{ alias: string }> } };
  return body.data.items.map((c) => c.alias);
}

test('connect a provider through the OAuth popup, then disconnect it', async ({ page, request }) => {
  const alias = uniq('conn');

  await seedCredential(page);
  await page.goto('/connectors');

  // The fixture provider is listed, disconnected.
  await expect(page.getByText(PROVIDER)).toBeVisible();

  // Open the connect dialog for the provider and start the OAuth flow.
  await page
    .getByRole('button', { name: 'Connect' })
    .first()
    .click();
  const dialog = page.getByRole('dialog', { name: `Connect ${PROVIDER}` });
  await dialog.getByRole('textbox', { name: 'Alias' }).fill(alias);

  // Clicking Connect calls startConnect → opens the OAuth popup at the stub IdP,
  // which redirects back through the callback bridge and postMessages the code to
  // the opener; the flow completes without any manual popup interaction.
  const popupPromise = page.waitForEvent('popup');
  await dialog.getByRole('button', { name: 'Connect' }).click();
  const popup = await popupPromise;
  // The popup self-drives (IdP redirect → bridge → callback → postMessage → close).
  await popup.waitForEvent('close');

  // UI: the connection lands in the Connections list (its alias becomes a link).
  // This paints only after POST /api/connectors/oauth/complete returns, and that
  // completion reloads this worker AND awaits the sibling worker's reload
  // (MULTIWORKER(2)) — inherently 5-10s server-side, beyond the global 10s
  // expect budget.
  await expect(page.getByRole('link', { name: alias })).toBeVisible({ timeout: 30_000 });

  // API: the same connection is present over the same origin. The connections GET
  // round-robins across the MULTIWORKER(2) port, so it can land on the sibling worker
  // whose reload has not yet converged; give the poll the same convergence budget the
  // UI assertions above use (the global expect budget is too short on CI).
  await expect.poll(async () => await connectionAliases(request), { timeout: 30_000 }).toContain(alias);

  // DISCONNECT from the UI: open the connection, confirm the disconnect dialog.
  await page.getByRole('link', { name: alias }).click();
  await expect(page.getByRole('heading', { name: alias })).toBeVisible();
  await page.getByRole('button', { name: 'Disconnect' }).click();
  await page
    .getByRole('dialog', { name: 'Disconnect this connection?' })
    .getByRole('button', { name: 'Disconnect' })
    .click();

  // UI: back on the list, the connection is gone. This paints only after the
  // DELETE returns, and the delete reloads this worker AND awaits the sibling
  // worker's reload (MULTIWORKER(2)) — inherently 5-10s server-side, beyond the
  // global 10s expect budget.
  await expect(page.getByRole('link', { name: alias })).toHaveCount(0, { timeout: 30_000 });
  // API: the connections list no longer carries it. Same MULTIWORKER round-robin as the
  // connect assertion above — the sibling worker's disconnect reload converges within the
  // convergence budget, not the shorter global expect budget.
  await expect.poll(async () => await connectionAliases(request), { timeout: 30_000 }).not.toContain(alias);
});

test('a denied authorization surfaces a loud failure and creates no connection', async ({
  page,
  request,
}) => {
  const alias = uniq('denied');

  // Script the stub IdP to refuse consent on the next authorize.
  const deny = await request.post(`${IDP_CONTROL_URL}/_deny`);
  expect(deny.status()).toBe(200);

  try {
    await seedCredential(page);
    await page.goto('/connectors');

    await page
      .getByRole('button', { name: 'Connect' })
      .first()
      .click();
    const dialog = page.getByRole('dialog', { name: `Connect ${PROVIDER}` });
    await dialog.getByRole('textbox', { name: 'Alias' }).fill(alias);

    const popupPromise = page.waitForEvent('popup');
    await dialog.getByRole('button', { name: 'Connect' }).click();
    const popup = await popupPromise;
    // The IdP redirects back with error=access_denied; the callback relays it and
    // the popup closes without granting a code.
    await popup.waitForEvent('close');

    // UI: the dialog shows the loud sign-in-cancelled notice, not a silent return.
    await expect(dialog.locator('[data-kind="cancelled"]')).toBeVisible();

    // API: no connection was created for the denied alias.
    expect(await connectionAliases(request)).not.toContain(alias);
  } finally {
    // Restore the IdP to granting so the stack is left clean.
    const allow = await request.post(`${IDP_CONTROL_URL}/_allow`);
    expect(allow.status()).toBe(200);
  }
});
