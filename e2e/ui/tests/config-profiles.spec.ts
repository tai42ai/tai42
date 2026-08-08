/**
 * Settings profiles (Mission 31) through the Studio Profiles tab over the REAL
 * stack. The pytest side proves the profile store / diff / apply pipeline
 * (`tests/config_profiles/*`, `operations/test_profile_ops.py`); here the browser
 * drives the tab end to end and asserts through BOTH the UI and the API.
 *
 * Two independent, idempotent tests — split because the browser e2e stack is a BARE
 * (unsupervised) deployment (`TAI_SUPERVISED` unset ⇒ `Shape.bare`):
 *
 *  1. CREATE → EDIT → DIFF. A profile carrying a user-secret-marked key AND a
 *     recycle-class key (`TAI_BUS_ACK_TIMEOUT`, a `TAI_BUS_*` field). The diff
 *     preview masks EVERY value (`••••••`) and surfaces the recycle callout. This
 *     test never applies the profile and never touches the live env band — a
 *     recycle-class diff CANNOT be applied on a bare shape (refused wholesale), so
 *     the recycle callout is asserted on the diff alone.
 *  2. APPLY → REVERT. A hot-only profile (safe to apply on bare) seeded via the API
 *     as `{...baseline, MARKER}` — a SUPERSET of the current stored env, because
 *     apply FULL-REPLACES the stored band (a key the profile omits is deleted), so a
 *     superset makes apply purely additive and `@previous` restores the exact
 *     baseline on revert. Re-entering the whole baseline band through the editor is
 *     impractical, hence the API seed; the create/edit path is covered by test 1.
 *
 * FleetReport note: on a converged (healthy) apply `<FleetReport>` renders NOTHING
 * (same as `system-fleet.spec.ts`); the observable is the dedicated apply report
 * (`data-testid=apply-report`) with its Hot-swapped section, not a fleet alert.
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import { apiHeaders, seedCredential, uniq } from './helpers';

/** The client-side mask ProfilesTab renders for a masked value (6 bullets). */
const MASK = '••••••';

/** A `TAI_BUS_*` field: recycle-class on every shape, never in the bare-shape
 *  refused set (only the two bus URLs are), so a diff carrying it shows the recycle
 *  callout without a refusal. */
const RECYCLE_KEY = 'TAI_BUS_ACK_TIMEOUT';

/** The stored env map GET /api/config/env exposes under `data.env`. */
async function storedEnv(request: APIRequestContext): Promise<Record<string, string>> {
  const res = await request.get('/api/config/env', { headers: apiHeaders() });
  expect(res.status(), await res.text()).toBe(200);
  return ((await res.json()) as { data: { env: Record<string, string> } }).data.env;
}

async function openProfilesTab(page: Page): Promise<void> {
  await page.goto('/settings');
  await page.getByRole('tab', { name: 'Profiles' }).click();
  await expect(page.getByRole('heading', { name: 'Settings profiles' })).toBeVisible();
}

/** Drive the destructive apply/revert inside its dialog. On a MULTIWORKER stack a
 *  reload-gated apply can land on a worker still applying a sibling reload and gets
 *  the documented retriable 503 — `toPass` re-clicks on its own cadence (never a raw
 *  sleep) until the /apply POST returns 200. On success the button is replaced by
 *  Close, so a first-try success ends the loop. */
async function applyInDialog(page: Page, dialogName: string): Promise<void> {
  const dialog = page.getByRole('dialog', { name: dialogName });
  await expect(async () => {
    const posted = page.waitForResponse(
      (r) => r.url().includes('/apply') && r.request().method() === 'POST',
    );
    await dialog.getByRole('button', { name: 'Apply profile', exact: true }).click();
    const response = await posted;
    expect(response.status(), await response.text()).toBe(200);
  }).toPass({ timeout: 40_000 });
  await expect(dialog.getByTestId('apply-report')).toBeVisible();
}

/** Named profiles a test created and the marker env keys an apply wrote. The
 *  afterEach net-removes whatever a FAILING test left on the shared stack — a happy
 *  path deletes/restores its own as assertions, so on success this is a no-op. */
const createdProfiles = new Set<string>();
const createdEnvKeys = new Set<string>();

test.afterEach(async ({ request }) => {
  for (const name of createdProfiles) {
    // Best-effort: a soft-delete on an already-absent name is a tolerable 404.
    await request.delete(`/api/config/profiles/${name}`, { headers: apiHeaders() });
  }
  for (const key of createdEnvKeys) {
    // Posting '' merge-deletes the marker key; a transient reload 503 on the rare
    // failure path is tolerable (the run tears the stack down after).
    await request.post('/api/config/env', { headers: apiHeaders(), data: { [key]: '' } });
  }
  createdProfiles.clear();
  createdEnvKeys.clear();
});

test('create, edit, and diff a profile — diff masks secrets and surfaces the recycle callout', async ({
  page,
}) => {
  const name = uniq('e2e_prof');
  const secretKey = uniq('E2E_SECRET').toUpperCase();
  const secretVal = uniq('sv');
  createdProfiles.add(name);

  await seedCredential(page);
  await openProfilesTab(page);

  // CREATE: name + description, a secret-marked row and a recycle-class row.
  await page.getByRole('button', { name: 'New profile' }).click();
  const createDialog = page.getByRole('dialog', { name: 'Create profile' });
  await createDialog.getByRole('textbox', { name: 'Profile name' }).fill(name);
  await createDialog.getByRole('textbox', { name: 'Profile description' }).fill('e2e profile');

  // Row 1 — the secret. Fill the value while it is still a plain field, THEN mark it
  // secret (the value survives the swap to the reveal input), so nothing is typed
  // into the masked control.
  await createDialog.getByRole('button', { name: 'Add variable' }).click();
  await createDialog.getByRole('textbox', { name: /^Name of new variable/ }).fill(secretKey);
  await createDialog.getByRole('textbox', { name: `Value of variable ${secretKey}` }).fill(secretVal);
  await createDialog.getByRole('checkbox', { name: 'Secret' }).check();

  // Row 2 — the recycle-class key (drives the diff's recycle callout).
  await createDialog.getByRole('button', { name: 'Add variable' }).click();
  await createDialog.getByRole('textbox', { name: /^Name of new variable/ }).fill(RECYCLE_KEY);
  await createDialog.getByRole('textbox', { name: `Value of variable ${RECYCLE_KEY}` }).fill('5.0');

  await createDialog.getByRole('button', { name: 'Create profile' }).click();
  const row = page.getByTestId(`profile-row-${name}`);
  await expect(row).toBeVisible();

  // EDIT: change the description in place (an update, not a second profile).
  await page.getByRole('button', { name: `Edit profile ${name}` }).click();
  const editDialog = page.getByRole('dialog', { name: `Edit profile — ${name}` });
  await editDialog.getByRole('textbox', { name: 'Profile description' }).fill('edited desc');
  await editDialog.getByRole('button', { name: 'Save profile' }).click();
  await expect(row).toContainText('edited desc');

  // DIFF: the preview masks EVERY value and names the recycle-class key in its
  // callout; a bare-shape refusal callout must NOT appear (the recycle key is not in
  // the bare refused set).
  await page.getByRole('button', { name: `Diff profile ${name}` }).click();
  const diffDialog = page.getByRole('dialog', { name: `Diff — ${name}` });
  await expect(diffDialog.getByTestId('diff-recycle')).toBeVisible();
  await expect(diffDialog.getByTestId('diff-recycle')).toContainText(RECYCLE_KEY);
  await expect(diffDialog.getByTestId('diff-refused')).toHaveCount(0);
  // The secret's real value never renders; the mask does. The diff rows key by env
  // name (`diff-row-<KEY>`).
  await expect(diffDialog.getByTestId(`diff-row-${secretKey}`)).toBeVisible();
  await expect(diffDialog.getByText(secretVal)).toHaveCount(0);
  await expect(diffDialog.getByText(MASK, { exact: false }).first()).toBeVisible();

  await diffDialog.getByRole('button', { name: 'Close' }).click();
});

test('apply a hot-only profile then revert via @previous — round-trips the stored env', async ({
  page,
  request,
}) => {
  test.setTimeout(120_000); // two reload-gated applies, each building + swapping an epoch.

  const name = uniq('e2e_apply');
  const marker = uniq('E2E_UI_PROFILE_MARKER').toUpperCase();
  const markerVal = uniq('applied');
  createdProfiles.add(name);
  createdEnvKeys.add(marker);

  // Snapshot the live band, then seed the profile as baseline ∪ {marker} so apply is
  // purely additive (no baseline key deleted) and @previous restores exactly baseline.
  const baseline = await storedEnv(request);
  const put = await request.put(`/api/config/profiles/${name}`, {
    headers: apiHeaders(),
    data: { description: 'apply e2e', env: { ...baseline, [marker]: markerVal }, secret_keys: [] },
  });
  expect(put.status(), await put.text()).toBe(200);

  await seedCredential(page);
  await openProfilesTab(page);
  await expect(page.getByTestId(`profile-row-${name}`)).toBeVisible();

  // APPLY: review (no refusal on a hot-only diff), fire, and read the dedicated
  // report. FleetReport itself renders nothing on convergence — the apply report and
  // its Hot-swapped section are the observable.
  await page.getByRole('button', { name: `Apply profile ${name}` }).click();
  const applyDialog = page.getByRole('dialog', { name: `Apply profile — ${name}` });
  await expect(applyDialog.getByTestId('diff-refused')).toHaveCount(0);
  await applyInDialog(page, `Apply profile — ${name}`);
  await expect(applyDialog.getByRole('heading', { name: 'Hot-swapped' })).toBeVisible();
  await expect(applyDialog.getByText(/did not converge|was not reached/)).toHaveCount(0);
  await applyDialog.getByRole('button', { name: 'Close' }).click();

  // The stored band now carries the marker at its value (apply added it).
  await expect.poll(async () => (await storedEnv(request))[marker]).toBe(markerVal);

  // REVERT: the reserved @previous holds the pre-apply band; applying it removes the
  // marker and restores baseline exactly.
  await page.getByRole('button', { name: 'Revert last apply' }).click();
  await applyInDialog(page, 'Revert last apply');
  await page.getByRole('dialog', { name: 'Revert last apply' }).getByRole('button', { name: 'Close' }).click();

  await expect.poll(async () => (await storedEnv(request))[marker]).toBeUndefined();
  // The full band is back to the pre-apply snapshot.
  expect(await storedEnv(request)).toEqual(baseline);
});
