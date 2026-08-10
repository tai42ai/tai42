/**
 * Config / settings editing. The pytest side proves the
 * flock'd config lost-update seam (`tests/crossworker/test_file_config_race.py`);
 * the browser never edited config. Here the Environment tab of `/settings` sets a
 * UNIQUE env key, persists it (asserted through the API AND a browser reload),
 * edits it in place (an update, not an append), and the loud negative shows the
 * tab's own validation blocking a save with no API write. The key is unset at the
 * end so the shared stack keeps no residue.
 *
 * POST /api/config/env MERGES: a key posted with value '' is deleted — the path
 * the restore below drives.
 */
import { expect, test, type APIRequestContext } from '@playwright/test';
import { apiHeaders, seedCredential, uniq } from './helpers';

/** The stored env map GET /api/config/env exposes under `data.env`. */
async function storedEnv(request: APIRequestContext): Promise<Record<string, string>> {
  const res = await request.get('/api/config/env', { headers: apiHeaders() });
  expect(res.status(), await res.text()).toBe(200);
  const body = (await res.json()) as { data: { env: Record<string, string> } };
  return body.data.env;
}

async function openEnvironmentTab(page: import('@playwright/test').Page): Promise<void> {
  await page.getByRole('tab', { name: 'Environment' }).click();
  await expect(page.getByRole('heading', { name: 'Environment variables' })).toBeVisible();
}

/** Click Save and wait for the merge+reload POST to return 200.
 *
 * On a MULTIWORKER stack a save fans the reload out to the sibling worker; a
 * following save can land on a worker still applying that reload and gets the
 * documented retriable 503 ("reloading — retry shortly"). Retrying the save is
 * the prescribed handling — `toPass` re-clicks on its own polling cadence (never
 * a raw sleep) until the reload settles and the POST returns 200. */
async function saveEnv(page: import('@playwright/test').Page): Promise<void> {
  await expect(async () => {
    const posted = page.waitForResponse(
      (r) => r.url().includes('/api/config/env') && r.request().method() === 'POST',
    );
    await page.getByRole('button', { name: 'Save' }).click();
    const response = await posted;
    expect(response.status(), await response.text()).toBe(200);
    // A MULTIWORKER reload cycle (epoch build + swap + the awaited sibling reload) can
    // exceed 20s under CI load — and the failed-MCP re-probe loop can hold the reload gate
    // for a probe on top — so the documented retriable 503 can persist past a 20s window.
    // Give the sanctioned retry a full cycle's room (matching helpers.postConfig's 45s).
  }).toPass({ timeout: 45_000 });
}

/** Every env key a test writes to the shared stack. The afterEach net-removes one
 *  a FAILING test left behind — the happy path deletes its own key as an assertion,
 *  so on success this is a no-op idempotent merge. Unique keys mean a leftover can
 *  never flake a sibling; the cleanup keeps the shared stack tidy regardless. */
const createdKeys = new Set<string>();

test.afterEach(async ({ request }) => {
  for (const key of createdKeys) {
    // Best-effort: posting '' merge-deletes the key; a transient reload 503 on the
    // rare failure path is tolerable (the run tears the stack down after).
    await request.post('/api/config/env', { headers: apiHeaders(), data: { [key]: '' } });
  }
  createdKeys.clear();
});

test('set, persist, edit, and restore a unique env key through the Environment tab', async ({
  page,
  request,
}) => {
  const key = uniq('E2E_UI_CFG').toUpperCase();
  const first = uniq('v1');
  const second = uniq('v2');
  createdKeys.add(key);

  await seedCredential(page);
  await page.goto('/settings');
  await openEnvironmentTab(page);

  // SET: add a fresh row, name it, give it a value, and save (a hot reload).
  await page.getByRole('button', { name: 'Add variable' }).click();
  await page.getByRole('textbox', { name: /^Name of new variable/ }).fill(key);
  await page.getByRole('textbox', { name: `Value of variable ${key}` }).fill(first);
  await saveEnv(page);

  // API: the stored env carries the new key at its value.
  await expect.poll(async () => (await storedEnv(request))[key]).toBe(first);

  // SURVIVAL: a full page reload re-reads the persisted value into the editor.
  await page.reload();
  await openEnvironmentTab(page);
  await expect(page.getByRole('textbox', { name: `Name of variable ${key}` })).toHaveValue(key);
  await expect(page.getByRole('textbox', { name: `Value of variable ${key}` })).toHaveValue(first);

  // EDIT: change the value in place and save — an UPDATE of the same key, never
  // a second row.
  await page.getByRole('textbox', { name: `Value of variable ${key}` }).fill(second);
  await saveEnv(page);
  await expect.poll(async () => (await storedEnv(request))[key]).toBe(second);

  // NEGATIVE: a blank-named row makes the editor invalid — the tab surfaces a loud
  // validation error and disables Save, so nothing is written.
  await page.getByRole('button', { name: 'Add variable' }).click();
  await expect(page.getByText('Variable names must be unique and non-empty.')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Save' })).toBeDisabled();
  // API: the blocked save left the stored value untouched.
  expect((await storedEnv(request))[key]).toBe(second);

  // RESTORE: unset the test key (posted as '' → deleted) and confirm it is gone.
  // The same retriable 503 applies — retry the delete-merge until it lands.
  await expect(async () => {
    const restore = await request.post('/api/config/env', { headers: apiHeaders(), data: { [key]: '' } });
    expect(restore.status(), await restore.text()).toBe(200);
  }).toPass({ timeout: 45_000 });
  await expect.poll(async () => (await storedEnv(request))[key]).toBeUndefined();
});
