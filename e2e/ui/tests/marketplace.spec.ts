/**
 * The Studio marketplace page over the REAL stack + a harness-run tai42-marketplace
 * registry (opt-in: `TAI_E2E_MARKETPLACE=1`). Drives browse/search/facet, a REAL
 * install CLICK on beta (the picker-less page installs its single published
 * version honestly), then pins alpha `0.1.0` through the API — the arc the page
 * itself cannot select a non-latest version for — to exercise the advisory and
 * update affordances. Every effect is asserted through BOTH the UI and the API,
 * and the arc uninstalls everything so the shared venv is left clean. The beta
 * install and uninstall receipts are also checked for their converged fleet
 * reload, and their result card for a clean receipt with no fleet warning.
 *
 * The studio stack is MULTIWORKER(2): an install/uninstall/update response returns
 * only after the awaited fan-out collects the sibling worker's reload reply — but a
 * sibling that misses the apply window is reported, not raised, so every API-side
 * proof still polls to convergence rather than reading once.
 */
import { expect, type APIRequestContext, type Page } from '@playwright/test';
import { apiHeaders, awaitMutation, MP_URL, mpAdminHeaders, seedCredential, sendRepeatable, uniq } from './helpers';
import { needs, test } from './needs';

needs('kind:marketplace_store', 'mutable', 'helper:registry', 'helper:package-index');

test.skip(
  process.env.TAI_E2E_MARKETPLACE !== '1',
  'marketplace area is opt-in (TAI_E2E_MARKETPLACE=1)',
);

const ALPHA_REF = 'tai42/e2e-alpha';
const BETA_REF = 'tai42/e2e-beta';
const GAMMA_REF = 'tai42/e2e-gamma';
const ALPHA_TOOL = 'e2e_market_probe';
const BETA_TOOL = 'e2e_market_beta_probe';

interface InstalledRow {
  readonly ref: string;
  readonly version: string;
}

interface FanoutRow {
  readonly name: string;
  readonly outcome: string;
}

interface ReloadReceipt {
  readonly fanout: { readonly mode: string; readonly results?: readonly FanoutRow[] };
  readonly recycle: unknown;
  readonly awaiting_respawn: readonly unknown[];
}

/** The attribution inventory as `ref@version` strings, over the same-origin API. */
async function installedRefs(request: APIRequestContext): Promise<string[]> {
  const res = await sendRepeatable(request, 'get', '/api/marketplace/installed', { headers: apiHeaders() });
  expect(res.ok(), await res.text()).toBeTruthy();
  const body = (await res.json()) as { data: { installed: InstalledRow[] } };
  return body.data.installed.map((row) => `${row.ref}@${row.version}`);
}

/** The registered tool names over the same-origin `GET /api/tools`. */
async function apiToolNames(request: APIRequestContext): Promise<string[]> {
  const res = await sendRepeatable(request, 'get', '/api/tools', { headers: apiHeaders() });
  expect(res.ok(), await res.text()).toBeTruthy();
  const body = (await res.json()) as { data: string[] };
  return body.data;
}

/** The three plugin-mutation doors, matched on the pathname so `install/preview` —
 *  which the detail page fires alongside them — is never mistaken for the mutation. */
const MUTATION_PATH = /\/api\/marketplace\/(install|uninstall|update)$/;

/** Confirm a mounted ConfirmDialog by its title, wait for it to close, and return the
 *  mutation's response body. */
async function confirmDialog<T>(page: Page, title: string, confirmLabel: string): Promise<T> {
  const dialog = page.getByRole('dialog', { name: title });
  await expect(dialog).toBeVisible();
  // The dialog stays mounted until the mutation resolves, so its unmount is driven by
  // the mutation RESPONSE — observe that response rather than polling for the unmount
  // against a fixed budget the server-side work can outrun. Armed before the click so
  // a fast mutation cannot land first.
  const mutated = awaitMutation(
    page,
    (r) => MUTATION_PATH.test(new URL(r.url()).pathname) && r.request().method() === 'POST',
  );
  await dialog.getByRole('button', { name: confirmLabel, exact: true }).click();
  const response = await mutated;
  expect(response.ok(), await response.text()).toBeTruthy();
  await expect(dialog).toBeHidden();
  return (await response.json()) as T;
}

/** A receipt's `reload` must be the converged fan-out over the stack's fleet (two serve
 *  workers and the backend worker): every member applied, no recycle roll, no worker
 *  awaiting a respawn — the input the result card renders as a clean receipt with no
 *  fleet warning. */
function expectConvergedReload(reload: ReloadReceipt | null): void {
  expect(reload).not.toBeNull();
  const r = reload as ReloadReceipt;
  expect(r.fanout.mode).toBe('fleet');
  expect((r.fanout.results ?? []).map((row) => row.outcome)).toEqual(['applied', 'applied', 'applied']);
  expect(r.recycle).toBeNull();
  expect(r.awaiting_respawn).toEqual([]);
}

/** The receipt card after a converged fan-out: the receipt line, and no fleet warning
 *  (no alert panel, no restart notice, no "Worker recycle" section). */
async function expectCleanReceipt(page: Page, line: string): Promise<void> {
  const receipt = page.getByRole('status').filter({ hasText: line });
  await expect(receipt).toBeVisible();
  await expect(receipt.getByRole('alert')).toHaveCount(0);
  await expect(
    receipt.getByText(/did not converge|was not reached|a restart to apply some settings/),
  ).toHaveCount(0);
  await expect(receipt.getByRole('heading', { name: 'Worker recycle' })).toHaveCount(0);
}

test('browse, install beta via UI, then the API-pinned alpha advisory + update arc', async ({
  page,
}) => {
  // This arc drives FIVE REAL pip mutations end to end — two installs, two uninstalls and an
  // update — each a pip subprocess plus this worker's reload AND the awaited sibling reload
  // (MULTIWORKER(2)), measured at 12s to 30s server-side apiece on a loaded CI runner. Their
  // sum alone approaches 150s, before the browse/facet steps and the two advisory
  // reload-to-convergence loops; budget for the whole real-registry arc rather than a single
  // mutation.
  test.setTimeout(300_000);
  await seedCredential(page);

  // 1. Browse renders the three seeded listings.
  await page.goto('/marketplace');
  await expect(page.getByText(ALPHA_REF)).toBeVisible();
  await expect(page.getByText(BETA_REF)).toBeVisible();
  await expect(page.getByText(GAMMA_REF)).toBeVisible();

  // 2. Text search: every fixture is a probe, so `probe` keeps them all; clearing
  // returns the full set.
  const searchBox = page.getByPlaceholder('Search plugins…');
  await searchBox.fill('probe');
  await page.getByRole('button', { name: 'Search', exact: true }).click();
  await expect(page.getByText(ALPHA_REF)).toBeVisible();
  await expect(page.getByText(BETA_REF)).toBeVisible();
  await expect(page.getByText(GAMMA_REF)).toBeVisible();
  await searchBox.fill('');
  await page.getByRole('button', { name: 'Search', exact: true }).click();
  await expect(page.getByText(GAMMA_REF)).toBeVisible();

  // 3. Kind facet: `extension` leaves only beta's extension item; alpha's tool
  // listing drops out. Clearing the chip restores the full set.
  const kindGroup = page.getByRole('group', { name: 'Filter by kind' });
  await kindGroup.getByRole('button', { name: 'extension', exact: true }).click();
  await expect(page.getByText(BETA_REF)).toBeVisible();
  await expect(page.getByText(ALPHA_REF)).toHaveCount(0);
  await kindGroup.getByRole('button', { name: 'extension', exact: true }).click();
  await expect(page.getByText(ALPHA_REF)).toBeVisible();

  // 4. The UI install CLICK on beta (one published version — no picker needed).
  await page.goto(`/marketplace?plugin=${encodeURIComponent(BETA_REF)}`);
  await expect(page.getByRole('heading', { name: BETA_REF })).toBeVisible();
  await expect(page.getByText(BETA_TOOL)).toBeVisible();
  await page.getByRole('button', { name: 'Install', exact: true }).click();
  const installed = await confirmDialog<{ data: { reload: ReloadReceipt } }>(page, 'Install plugin', 'Install');
  await expect(page.getByText('Installed v0.1.0')).toBeVisible();
  // The receipt's reload is a converged fleet fan-out, and the result card shows it
  // as a clean receipt: no fleet warning next to the install line.
  expectConvergedReload(installed.data.reload);
  await expectCleanReceipt(page, `Installed ${BETA_REF} 0.1.0`);

  // 5. API-side proof of the UI install, polled to convergence across workers.
  await expect
    .poll(() => installedRefs(page.request), { timeout: 30_000 })
    .toContain(`${BETA_REF}@0.1.0`);
  await expect.poll(() => apiToolNames(page.request), { timeout: 30_000 }).toContain(BETA_TOOL);

  // Uninstall beta through the UI; the round-trip leaves nothing behind.
  await page.getByRole('button', { name: 'Uninstall', exact: true }).click();
  const uninstalled = await confirmDialog<{ data: { reload: ReloadReceipt | null } }>(
    page,
    'Uninstall plugin',
    'Uninstall',
  );
  await expect(page.getByText('Installed v0.1.0')).toBeHidden();
  // Beta's tool and extension are manifest entries, so the uninstall removes them and its
  // receipt carries the reload; the result card shows that converged reload the same way.
  expectConvergedReload(uninstalled.data.reload);
  await expectCleanReceipt(page, `Uninstalled ${BETA_REF}`);
  await expect
    .poll(() => installedRefs(page.request), { timeout: 30_000 })
    .not.toContain(`${BETA_REF}@0.1.0`);

  // 6. Pin alpha 0.1.0 through the API — the page has no version picker and
  // alpha's latest is 0.2.0, so the advisory/update arc needs the OLD version
  // installed out-of-band. Then the UI detail reflects it.
  const pin = await page.request.post('/api/marketplace/install', {
    headers: apiHeaders(),
    data: { ref: ALPHA_REF, version: '0.1.0' },
  });
  expect(pin.ok(), await pin.text()).toBeTruthy();
  await page.goto(`/marketplace?plugin=${encodeURIComponent(ALPHA_REF)}`);
  await expect(page.getByText('Installed v0.1.0')).toBeVisible();
  await expect.poll(() => apiToolNames(page.request), { timeout: 30_000 }).toContain(ALPHA_TOOL);

  // 7. Advisories through the UI: an admin-created advisory for alpha <=0.1.0
  // surfaces on the page (within the 1s poll), and a withdrawal clears it. The
  // detail page reads a cached snapshot on load, so reload until it converges.
  const summary = uniq('advisory');
  const created = await page.request.post(`${MP_URL}/api/v1/admin/advisories`, {
    headers: mpAdminHeaders(),
    data: {
      listing: ALPHA_REF,
      affected_versions: '<=0.1.0',
      severity: 'high',
      summary,
    },
  });
  expect(created.status(), await created.text()).toBe(201);
  const advisoryId = ((await created.json()) as { data: { id: number } }).data.id;

  await expect(async () => {
    await page.reload();
    await expect(page.getByText(summary)).toBeVisible({ timeout: 2_000 });
  }).toPass({ timeout: 45_000 });

  const withdrawn = await page.request.post(
    `${MP_URL}/api/v1/admin/advisories/${String(advisoryId)}/withdraw`,
    { headers: mpAdminHeaders() },
  );
  expect(withdrawn.ok(), await withdrawn.text()).toBeTruthy();
  await expect(async () => {
    await page.reload();
    await expect(page.getByText(summary)).toBeHidden({ timeout: 2_000 });
  }).toPass({ timeout: 45_000 });

  // 8. Update through the UI: the update-available affordance to 0.2.0.
  await expect(page.getByText('Update available: v0.2.0')).toBeVisible();
  await page.getByRole('button', { name: 'Update', exact: true }).click();
  await confirmDialog(page, 'Update plugin', 'Update');
  await expect
    .poll(() => installedRefs(page.request), { timeout: 30_000 })
    .toContain(`${ALPHA_REF}@0.2.0`);

  // 9. Uninstall alpha through the UI — the venv is left clean.
  await page.getByRole('button', { name: 'Uninstall', exact: true }).click();
  await confirmDialog(page, 'Uninstall plugin', 'Uninstall');
  await expect
    .poll(() => installedRefs(page.request), { timeout: 30_000 })
    .not.toContain(`${ALPHA_REF}@0.2.0`);
});

test('browse lists the plugins as cards, switches to a table and back, and a card opens its detail', async ({
  page,
}) => {
  await seedCredential(page);
  await page.goto('/marketplace');

  // Browse opens on the card view: one card per seeded listing in the "Plugins" list.
  const viewToggle = page.getByRole('radiogroup', { name: 'Plugins view' });
  await expect(viewToggle.getByRole('radio', { name: 'Card view', exact: true })).toBeChecked();
  const cards = page.getByRole('list', { name: 'Plugins' }).getByRole('listitem');
  for (const ref of [ALPHA_REF, BETA_REF, GAMMA_REF]) {
    await expect(cards.filter({ hasText: ref })).toHaveCount(1);
  }

  // The list view draws the same listings as table rows, and the choice survives a reload.
  await viewToggle.getByRole('radio', { name: 'List view', exact: true }).click();
  await expect(page.getByRole('list', { name: 'Plugins' })).toHaveCount(0);
  const table = page.getByRole('table');
  await expect(table.getByRole('columnheader', { name: 'Plugin', exact: true })).toBeVisible();
  for (const ref of [ALPHA_REF, BETA_REF, GAMMA_REF]) {
    await expect(table.getByRole('row').filter({ hasText: ref })).toHaveCount(1);
  }
  await page.reload();
  await expect(
    page.getByRole('radiogroup', { name: 'Plugins view' }).getByRole('radio', { name: 'List view', exact: true }),
  ).toBeChecked();
  await expect(page.getByRole('table').getByRole('row').filter({ hasText: BETA_REF })).toHaveCount(1);

  // Back on the cards, a click anywhere on a card — here its reference line — opens the detail.
  await viewToggle.getByRole('radio', { name: 'Card view', exact: true }).click();
  const betaCard = page.getByRole('list', { name: 'Plugins' }).getByRole('listitem').filter({ hasText: BETA_REF });
  await betaCard.getByText(BETA_REF, { exact: true }).click();
  await expect(page.getByRole('heading', { name: BETA_REF })).toBeVisible();
});
