/**
 * The Studio marketplace page over the REAL stack + a harness-run tai42-marketplace
 * registry (opt-in: `TAI_E2E_MARKETPLACE=1`). Drives browse/search/facet, a REAL
 * install CLICK on beta (the picker-less page installs its single published
 * version honestly), then pins alpha `0.1.0` through the API — the arc the page
 * itself cannot select a non-latest version for — to exercise the advisory and
 * update affordances. Every effect is asserted through BOTH the UI and the API,
 * and the arc uninstalls everything so the shared venv is left clean.
 *
 * The studio stack is MULTIWORKER(2): an install/uninstall/update reload fans out
 * to the sibling worker asynchronously, so every API-side proof polls to
 * convergence rather than reading once.
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import { apiHeaders, MP_URL, mpAdminHeaders, seedCredential, uniq } from './helpers';

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

/** The attribution inventory as `ref@version` strings, over the same-origin API. */
async function installedRefs(request: APIRequestContext): Promise<string[]> {
  const res = await request.get('/api/marketplace/installed', { headers: apiHeaders() });
  expect(res.ok(), await res.text()).toBeTruthy();
  const body = (await res.json()) as { data: { installed: InstalledRow[] } };
  return body.data.installed.map((row) => `${row.ref}@${row.version}`);
}

/** The registered tool names over the same-origin `GET /api/tools`. */
async function apiToolNames(request: APIRequestContext): Promise<string[]> {
  const res = await request.get('/api/tools', { headers: apiHeaders() });
  expect(res.ok(), await res.text()).toBeTruthy();
  const body = (await res.json()) as { data: string[] };
  return body.data;
}

/** Confirm a mounted ConfirmDialog by its title, then wait for it to close. */
async function confirmDialog(page: Page, title: string, confirmLabel: string): Promise<void> {
  const dialog = page.getByRole('dialog', { name: title });
  await expect(dialog).toBeVisible();
  await dialog.getByRole('button', { name: confirmLabel, exact: true }).click();
  await expect(dialog).toBeHidden();
}

test('browse, install beta via UI, then the API-pinned alpha advisory + update arc', async ({
  page,
}) => {
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
  await confirmDialog(page, 'Install plugin', 'Install');
  await expect(page.getByText('Installed v0.1.0')).toBeVisible();

  // 5. API-side proof of the UI install, polled to convergence across workers.
  await expect
    .poll(() => installedRefs(page.request), { timeout: 30_000 })
    .toContain(`${BETA_REF}@0.1.0`);
  await expect.poll(() => apiToolNames(page.request), { timeout: 30_000 }).toContain(BETA_TOOL);

  // Uninstall beta through the UI; the round-trip leaves nothing behind.
  await page.getByRole('button', { name: 'Uninstall', exact: true }).click();
  await confirmDialog(page, 'Uninstall plugin', 'Uninstall');
  await expect(page.getByText('Installed v0.1.0')).toBeHidden();
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
  }).toPass({ timeout: 20_000 });

  const withdrawn = await page.request.post(
    `${MP_URL}/api/v1/admin/advisories/${String(advisoryId)}/withdraw`,
    { headers: mpAdminHeaders() },
  );
  expect(withdrawn.ok(), await withdrawn.text()).toBeTruthy();
  await expect(async () => {
    await page.reload();
    await expect(page.getByText(summary)).toBeHidden({ timeout: 2_000 });
  }).toPass({ timeout: 20_000 });

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
