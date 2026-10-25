/**
 * The Connectors directory and its connect flow. The pytest twin proves
 * connect-on-A / complete-on-B + token-encrypted-at-rest against the fixture
 * provider + stub IdP (`tests/connectors/test_oauth_and_refresh.py`); the browser
 * never clicked through it. The studio stack carries the same fixture connector
 * provider + stub IdP (the `build_studio_stack` extension), so here the real
 * browser drives it from the directory: the provider's card opens the connect flow,
 * Connect opens the OAuth popup, the threaded stub IdP redirects back through the
 * deployment's callback bridge, and the flow advances to Pick tools, where a pick over
 * the connection's managed server is saved through the tool-filter write before Done.
 * The connection lands (UI list + API connections list) and is then DISCONNECTED from
 * the UI.
 *
 * The loud negative scripts the stub IdP to DENY consent (`/_deny`): the popup
 * comes back with an OAuth error, the UI surfaces the failed sign-in, and no
 * connection is created.
 *
 * The stack also runs an OAuth-protected MCP server fixture that is its own sign-in
 * service (registering apps by itself, sending `iss` on its redirect). An MCP server is
 * added from the UI by that fixture's address; adding a server that registers apps by
 * itself opens the connect flow for it at once, and that flow drives through the popup to
 * Pick tools and Done before the connection is disconnected and the server is removed
 * from its row's actions menu in the directory's list view.
 */
import { expect, type APIRequestContext, type Locator, type Page } from '@playwright/test';
import {
  apiHeaders,
  awaitMutation,
  IDP_CONTROL_URL,
  OAUTH_MCP_URL,
  seedCredential,
  uniq,
} from './helpers';
import { needs, test } from './needs';

needs('kind:connectors', 'mutable', 'helper:idp', 'helper:oauth-mcp', 'setting:fixture-connector');

const PROVIDER = 'E2E Stub IdP';

/** The connections list under `data.items`, over the same origin. */
async function connectionAliases(request: APIRequestContext): Promise<string[]> {
  const res = await request.get('/api/connectors/connections', { headers: apiHeaders() });
  expect(res.status(), await res.text()).toBe(200);
  const body = (await res.json()) as { data: { items: Array<{ alias: string }> } };
  return body.data.items.map((c) => c.alias);
}

/**
 * Open the Connectors directory in its card view: the "Servers and apps" grid is a
 * `list` whose cards are its `listitem`s, so a card is located by what it names.
 */
async function openDirectory(page: Page): Promise<void> {
  await page.goto('/connectors');
  await page
    .getByRole('radiogroup', { name: 'Servers and apps view' })
    .getByRole('radio', { name: 'Card view' })
    .click();
}

/**
 * Open the Connectors directory in its list view: the "Servers and apps" entries are the
 * rows of a table, each row carrying its entry's actions menu.
 */
async function openDirectoryList(page: Page): Promise<void> {
  await page.goto('/connectors');
  await page
    .getByRole('radiogroup', { name: 'Servers and apps view' })
    .getByRole('radio', { name: 'List view' })
    .click();
}

/**
 * The fixture provider's OWN card in the directory. Never a `.first()` Connect
 * button: a provider that already carries a connection offers "Add another account"
 * instead of "Connect", so a first-match Connect belongs to a DIFFERENT provider and
 * its flow is titled for that one.
 */
function providerCard(page: Page): Locator {
  return page
    .getByRole('list', { name: 'Servers and apps' })
    .getByRole('listitem')
    .filter({ hasText: PROVIDER });
}

/** The "Your connections" region below the directory, one alias link per connection. */
function yourConnections(page: Page): Locator {
  return page.getByRole('region', { name: 'Your connections' });
}

/** Open the fixture provider's connect flow at its Connect step and name the connection. */
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

/** Providers a test added by address, registered once the add has answered 200. The
 *  afterEach removes each through the API, after the connections (a provider removal is
 *  refused while one lives). */
const addedProviders = new Set<string>();

test.afterEach(async ({ request }) => {
  const aliases = new Set(connectedAliases);
  connectedAliases.clear();
  const providers = new Set(addedProviders);
  addedProviders.clear();

  // The denial knob is stack-global and outlives the test that flipped it, so restore
  // the granting default here rather than inside a test body — a test that ends early
  // (a timeout) never reaches its own restore. It is ISSUED first and ASSERTED last, so a
  // restore that answers non-200 still cannot skip the connection sweep between them.
  const allow = await request.post(`${IDP_CONTROL_URL}/_allow`);

  if (aliases.size > 0) {
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
  }

  for (const id of providers) {
    const removed = await request.delete(`/api/connectors/providers/${id}`, {
      headers: apiHeaders(),
    });
    expect(removed.status(), await removed.text()).toBe(200);
  }

  expect(allow.status()).toBe(200);
});

test('a provider connects through the OAuth popup and its tools are picked, then it disconnects', async ({
  page,
  request,
}) => {
  const alias = uniq('conn');
  connectedAliases.add(alias);

  await seedCredential(page);
  await openDirectory(page);

  // The fixture provider has its card in the directory.
  await expect(providerCard(page)).toBeVisible();

  // Open the connect flow for the provider and start the OAuth flow.
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

  // Pick tools: the flow asks the connection's managed server for its tools. A new
  // connection mounts every tool, so the all-checkbox starts on. The managed server is a
  // cold stdio child, so its first listing waits out the server's start.
  const mountEvery = dialog.getByRole('checkbox', {
    name: 'Mount every tool, including tools the server adds later',
  });
  await expect(mountEvery).toBeChecked({ timeout: 30_000 });
  await mountEvery.uncheck();
  await dialog.getByRole('button', { name: 'Clear', exact: true }).click();
  await dialog.getByRole('textbox', { name: 'Filter tools' }).fill('ping');
  await dialog.getByRole('checkbox', { name: /ping/ }).check();

  // Save writes the pick through the entry's tool-filter door as an include list.
  const picked = awaitMutation(
    page,
    (r) =>
      /^\/api\/mcp-config\/entries\/[^/]+\/tool-filter$/.test(new URL(r.url()).pathname) &&
      r.request().method() === 'PUT',
  );
  await dialog.getByRole('button', { name: 'Save', exact: true }).click();
  const pick = await picked;
  expect(pick.status(), await pick.text()).toBe(200);
  const written = pick.request().postDataJSON() as { include: string[]; exclude: string[] };
  expect(written.exclude).toEqual([]);
  expect(written.include).toHaveLength(1);
  expect(written.include[0]).toMatch(/ping/);

  // Done names the connected provider; Close returns to the directory.
  await expect(dialog.getByRole('heading', { name: `${PROVIDER} is connected.` })).toBeVisible();
  await dialog.getByRole('button', { name: 'Close' }).click();
  await expect(dialog).toBeHidden();

  // UI: the connection lands in "Your connections" (its alias becomes a link).
  await expect(yourConnections(page).getByRole('link', { name: alias })).toBeVisible();

  // API: the same connection is present over the same origin. The connections GET
  // round-robins across the MULTIWORKER(2) port, so it can land on the sibling worker
  // whose reload has not yet converged; poll rather than read once.
  await expect.poll(async () => await connectionAliases(request)).toContain(alias);

  // DISCONNECT from the UI: open the connection, confirm the disconnect dialog. Same
  // shape as the connect above — the DELETE reloads this worker and awaits the
  // sibling's reload, so the list repaint is asserted after that response lands.
  await yourConnections(page).getByRole('link', { name: alias }).click();
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
  await expect(yourConnections(page).getByRole('link', { name: alias })).toHaveCount(0);
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
  await openDirectory(page);

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

  // UI: the flow stays on Connect with the loud sign-in-cancelled notice, not a silent return.
  await expect(dialog.locator('[data-kind="cancelled"]')).toBeVisible();

  // API: no connection was created for the denied alias.
  expect(await connectionAliases(request)).not.toContain(alias);
});

test('add a server by address, connect it through the flow the add opens, disconnect, remove it', async ({
  page,
  request,
}) => {
  const id = uniq('srv');
  const name = `Fixture server ${id}`;
  const alias = uniq('mcp');

  await seedCredential(page);
  await openDirectory(page);

  // Add a custom server → the fixture's address → Look up.
  await page.getByRole('button', { name: 'Add a custom server' }).click();
  const add = page.getByRole('dialog', { name: 'Add MCP server' });
  await add.getByRole('textbox', { name: 'Server address' }).fill(OAUTH_MCP_URL);
  await add.getByRole('button', { name: 'Look up' }).click();

  // The review: the fixture registers apps by itself, so no client credentials are needed.
  await expect(add.getByText('Automatic')).toBeVisible();
  await add.getByRole('textbox', { name: 'Name' }).fill(name);
  await add.getByRole('textbox', { name: 'Identifier' }).fill(id);
  const added = awaitMutation(
    page,
    (r) => new URL(r.url()).pathname === '/api/connectors/providers' && r.request().method() === 'POST',
  );
  await add.getByRole('button', { name: 'Add server' }).click();
  const addition = await added;
  expect(addition.status(), await addition.text()).toBe(200);
  connectedAliases.add(alias);
  addedProviders.add(id);
  await expect(add).toBeHidden();

  // The server registers apps by itself, so the add opens the connect flow for it at once.
  const connect = page.getByRole('dialog', { name: `Connect ${name}` });
  await expect(connect).toBeVisible();

  // The popup drives the fixture's sign-in (it registers the app, answers with a code and
  // its `iss`) and closes.
  await connect.getByRole('textbox', { name: 'Alias' }).fill(alias);
  const completed = awaitMutation(
    page,
    (r) => r.url().includes('/api/connectors/oauth/complete') && r.request().method() === 'POST',
  );
  const popupPromise = page.waitForEvent('popup');
  await connect.getByRole('button', { name: 'Connect' }).click();
  const popup = await popupPromise;
  await popup.waitForEvent('close');
  const completion = await completed;
  expect(completion.status(), await completion.text()).toBe(200);
  expect(((await completion.json()) as { data: { kind: string } }).data.kind).toBe('success');

  // Pick tools with the stored selection untouched reads "Continue" and advances with no
  // write; the server's first listing waits out its start.
  await connect.getByRole('button', { name: 'Continue', exact: true }).click({ timeout: 30_000 });
  await expect(connect.getByRole('heading', { name: `${name} is connected.` })).toBeVisible();
  await connect.getByRole('button', { name: 'Close' }).click();
  await expect(connect).toBeHidden();

  // The connection is listed (UI and API).
  await expect(yourConnections(page).getByRole('link', { name: alias })).toBeVisible();
  await expect.poll(async () => await connectionAliases(request)).toContain(alias);

  // Disconnect it from its detail view.
  await yourConnections(page).getByRole('link', { name: alias }).click();
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
  expect((await disconnected).status()).toBe(200);
  connectedAliases.delete(alias);
  await expect(yourConnections(page).getByRole('link', { name: alias })).toHaveCount(0);

  // Remove the server from its row's actions menu in the list view; it is gone from the
  // catalog.
  await openDirectoryList(page);
  const row = page.getByRole('table').getByRole('row').filter({ hasText: name });
  await row.getByRole('button', { name: `Actions for ${name}` }).click();
  await page.getByRole('menuitem', { name: 'Remove' }).click();
  const removed = awaitMutation(
    page,
    (r) =>
      new URL(r.url()).pathname === `/api/connectors/providers/${id}` &&
      r.request().method() === 'DELETE',
  );
  await page
    .getByRole('dialog', { name: 'Remove this server?' })
    .getByRole('button', { name: 'Remove' })
    .click();
  const removal = await removed;
  expect(removal.status(), await removal.text()).toBe(200);
  addedProviders.delete(id);
  await expect(row).toHaveCount(0);
  const catalog = await request.get('/api/connectors/providers', { headers: apiHeaders() });
  const body = (await catalog.json()) as { data: { providers: Array<{ id: string }> } };
  expect(body.data.providers.map((p) => p.id)).not.toContain(id);
});
