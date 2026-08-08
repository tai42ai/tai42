/**
 * The MCP tab's secret-reference flow (`/manifest` → MCP) over the REAL stack. An
 * MCP entry's `config.env` map is secret-bearing: the editor mounts a masked
 * `SecretRefField` per env value that EITHER references an existing env key (stored
 * as an `!ENV ${KEY}` manifest leaf) OR pastes a new secret (a combined
 * store-then-mark op that generates the key). On save, an env key THIS editor
 * generated via a paste and no longer referenced is swept; a key it did not generate
 * is NEVER swept — the orphan accept/decline the pytest side proves at unit level
 * (`McpTab.test.tsx`), here over the live combined-op + reload seam.
 *
 * Real-UX notes (verified against tai-studio source, not the plan's wording):
 *  - The entry is seeded via the API (`POST /api/mcp-config`) rather than authored
 *    through the schema-driven form: the focus is the SecretRefField + save-time
 *    sweep, not driving the nested transport/record form. `env` is launcher-only, so
 *    the seeded entry carries a `command` transport.
 *  - McpTab NEVER passes a `{source:'paste'}` value to SecretRefField — the field's
 *    value is always derived from the stored `!ENV` marker. So a paste does NOT show
 *    the field's "New secret" chip; it surfaces as a freshly GENERATED env key plus a
 *    masked key-reference chip. The honest observable of a paste is the generated key
 *    (asserted through the API) and the masked chip.
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import { apiHeaders, postConfig, seedCredential, uniq, waitForReloadSettle } from './helpers';

/** The env-map key on the seeded MCP entry whose value is the secret reference. */
const ENV_ENTRY = 'REFVAL';
/** SecretRefField's masked stand-in (8 bullets) — display only. */
const MASK = '••••••••';

/** An `!ENV ${KEY}` manifest leaf referencing env key `key` (McpTab's wire form). */
function envMarker(key: string): string {
  return `!ENV \${${key}}`;
}

/** The stored env map GET /api/config/env exposes under `data.env`. */
async function storedEnv(request: APIRequestContext): Promise<Record<string, string>> {
  const res = await request.get('/api/config/env', { headers: apiHeaders() });
  expect(res.status(), await res.text()).toBe(200);
  return ((await res.json()) as { data: { env: Record<string, string> } }).data.env;
}

/** The preserved manifest's mcp array (`!ENV` markers intact). */
async function preservedMcp(request: APIRequestContext): Promise<unknown[]> {
  const res = await request.get('/api/manifest/preserved', { headers: apiHeaders() });
  expect(res.status(), await res.text()).toBe(200);
  return ((await res.json()) as { data: { mcp: unknown[] } }).data.mcp;
}

/** Seed one MCP entry referencing `key` from its `config.env.REFVAL` leaf. */
async function seedEntry(request: APIRequestContext, title: string, key: string): Promise<void> {
  // The caller seeds the referenced env key just before this — that env write fanned a
  // reload out to the fleet. Drain it FIRST so this mcp-config POST lands on a free reload
  // gate instead of racing the in-flight reload into a (retriable, but slow) 503 storm.
  await waitForReloadSettle(request);
  const res = await postConfig(request, '/api/mcp-config', {
    mcp: [{ title, config: { command: '/bin/true', env: { [ENV_ENTRY]: envMarker(key) } } }],
  });
  expect(res.status(), await res.text()).toBe(200);
  // The seed fanned a reload out; wait for the fleet to settle so the subsequent page load /
  // field read does not race a mid-reload worker.
  await waitForReloadSettle(request);
}

/** Open the MCP tab of the Manifest page and wait for the seeded entry's field. */
async function openMcpField(page: Page): Promise<void> {
  await page.goto('/manifest');
  // ``exact`` so the "MCP" tab is not ambiguous with the "Sub-MCP" tab (a substring match).
  await page.getByRole('tab', { name: 'MCP', exact: true }).click();
  await expect(page.getByTestId(`mcp-secret-0-${ENV_ENTRY}`)).toBeVisible();
}

/** Click Save config, retrying the documented reload 503 on its own cadence. */
async function saveMcpConfig(page: Page): Promise<void> {
  await expect(async () => {
    const posted = page.waitForResponse(
      (r) => r.url().endsWith('/api/mcp-config') && r.request().method() === 'POST',
    );
    await page.getByRole('button', { name: 'Save config' }).click();
    const response = await posted;
    expect(response.status(), await response.text()).toBe(200);
  }).toPass({ timeout: 30_000 });
}

/** The MCP config as it stood before a test seeded it, restored in afterEach; plus
 *  any env keys a test wrote to the shared stack. */
let originalMcp: unknown[] | null = null;
const createdEnvKeys = new Set<string>();

test.beforeEach(async ({ request }) => {
  originalMcp = await preservedMcp(request);
});

test.afterEach(async ({ request }) => {
  if (originalMcp !== null) {
    // Best-effort restore of the mounted MCP config to its pre-test shape (past the reload gate).
    await postConfig(request, '/api/mcp-config', { mcp: originalMcp });
    originalMcp = null;
  }
  for (const key of createdEnvKeys) {
    await postConfig(request, '/api/config/env', { [key]: '' });
  }
  createdEnvKeys.clear();
  // Leave the stack QUIESCENT for the next serial spec: the restore + env cleanups above
  // fan reloads out to the fleet.
  await waitForReloadSettle(request);
});

test('an existing !ENV secret reference renders a masked, revealable chip', async ({ page, request }) => {
  // Reload-gated seed + settle legs; on the busy full-suite stack these approach the 60s
  // default, so budget past it (see the sibling paste test).
  test.setTimeout(120_000);
  const keyPre = uniq('E2E_MCP_REF').toUpperCase();
  createdEnvKeys.add(keyPre);
  // The referenced key must exist or the save-time dangling-!ENV validator refuses it.
  const env = await postConfig(request, '/api/config/env', { [keyPre]: uniq('secret') });
  expect(env.status(), await env.text()).toBe(200);
  await seedEntry(request, uniq('e2e-ref'), keyPre);

  await seedCredential(page);
  await openMcpField(page);

  const field = page.getByTestId(`mcp-secret-0-${ENV_ENTRY}`);
  // A committed key reference: a masked chip + a "Change reference" affordance.
  await expect(field.getByRole('button', { name: 'Change reference' })).toBeVisible();
  await expect(field.getByText(MASK)).toBeVisible();
  // The key NAME (not a secret) is revealable on click.
  await field.getByRole('button', { name: 'Show value' }).click();
  await expect(field.getByText(keyPre)).toBeVisible();
});

test('pasting a new secret generates a key; the save-time sweep drops it but never a pre-existing key', async ({
  page,
  request,
}) => {
  // Several reload-gated legs (env seed, mcp-config seed, the combined secret-env op, the
  // save-time sweep), each followed by a fleet reload-settle. On a busy shared stack (the
  // full serial suite) their cumulative settle time exceeds the 60s default — budget for it.
  test.setTimeout(120_000);
  const keyPre = uniq('E2E_MCP_PRE').toUpperCase();
  createdEnvKeys.add(keyPre);
  const seedEnv = await postConfig(request, '/api/config/env', { [keyPre]: uniq('secret') });
  expect(seedEnv.status(), await seedEnv.text()).toBe(200);
  await seedEntry(request, uniq('e2e-paste'), keyPre);

  const before = new Set(Object.keys(await storedEnv(request)));

  await seedCredential(page);
  await openMcpField(page);

  // PASTE a new secret into the env value: the combined op stores it under a
  // generated key and rewrites the leaf to reference that key.
  const field = page.getByTestId(`mcp-secret-0-${ENV_ENTRY}`);
  await field.getByRole('button', { name: 'Change reference' }).click();
  await field.getByRole('button', { name: 'Paste new secret' }).click();
  // Target the paste input by its textbox role + exact name: `getByLabel(ENV_ENTRY)` also
  // matches the row's `role=group` wrapper (`aria-label="REFVAL source"`, a substring hit).
  await field.getByRole('textbox', { name: ENV_ENTRY, exact: true }).fill(uniq('pasted-secret'));
  // "Use secret" fires the combined store-then-mark op (`POST /api/mcp-config/secret-env`),
  // which writes the generated env key then fans a reload out. Await that op's own 200 so the
  // key-count assertion below reads the store AFTER the write landed, not mid-op.
  const secretEnvOk = page.waitForResponse(
    (r) => r.url().endsWith('/api/mcp-config/secret-env') && r.request().method() === 'POST',
  );
  await field.getByRole('button', { name: 'Use secret' }).click();
  const secretEnvRes = await secretEnvOk;
  expect(secretEnvRes.status(), await secretEnvRes.text()).toBe(200);
  // Drain the op's reload so GET /api/config/env answers 200 (not a mid-reload 503) below.
  await waitForReloadSettle(request);

  // The combined op generated exactly one new env key AND left the pre-existing key intact
  // (`write_env` MERGES under a lock, so `keyPre` is never dropped by the op). Assert BOTH on
  // the SAME read: a GET that lands mid-swap can transiently expose a reduced band (the new key
  // present before `keyPre` re-surfaces), so a one-condition poll could pass on that inconsistent
  // instant — require the whole consistent band before reading `generated` off it.
  let generated = '';
  await expect
    .poll(
      async () => {
        const env = await storedEnv(request);
        const keys = Object.keys(env).filter((k) => !before.has(k) && k !== 'TAI_ENV_SECRET_KEYS');
        if (keys.length === 1) generated = keys[0] ?? '';
        return { newKeys: keys.length, prePresent: env[keyPre] !== undefined };
      },
      { timeout: 30_000 },
    )
    .toEqual({ newKeys: 1, prePresent: true });
  createdEnvKeys.add(generated);

  // Drop the entry (and thus the generated reference), then save: the sweep deletes
  // the session-generated key (ACCEPT) but never the pre-existing one (DECLINE — it
  // was orphaned by the paste yet this editor did not generate it).
  await page.getByRole('button', { name: 'Remove server 1' }).click();
  await saveMcpConfig(page);
  // The save fanned a reload out; settle the fleet before asserting the swept store state.
  await waitForReloadSettle(request);

  // The sweep dropped the session-generated key but kept the pre-existing one — assert BOTH on
  // the SAME read so a mid-swap GET cannot satisfy either half on an inconsistent instant.
  await expect
    .poll(
      async () => {
        const env = await storedEnv(request);
        return { generatedGone: env[generated] === undefined, prePresent: env[keyPre] !== undefined };
      },
      { timeout: 30_000 },
    )
    .toEqual({ generatedGone: true, prePresent: true });
});
