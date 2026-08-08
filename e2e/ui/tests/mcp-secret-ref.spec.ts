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
import { apiHeaders, seedCredential, uniq } from './helpers';

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
  const res = await request.post('/api/mcp-config', {
    headers: apiHeaders(),
    data: { mcp: [{ title, config: { command: '/bin/true', env: { [ENV_ENTRY]: envMarker(key) } } }] },
  });
  expect(res.status(), await res.text()).toBe(200);
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
    // Best-effort restore of the mounted MCP config to its pre-test shape.
    await request.post('/api/mcp-config', { headers: apiHeaders(), data: { mcp: originalMcp } });
    originalMcp = null;
  }
  for (const key of createdEnvKeys) {
    await request.post('/api/config/env', { headers: apiHeaders(), data: { [key]: '' } });
  }
  createdEnvKeys.clear();
});

test('an existing !ENV secret reference renders a masked, revealable chip', async ({ page, request }) => {
  const keyPre = uniq('E2E_MCP_REF').toUpperCase();
  createdEnvKeys.add(keyPre);
  // The referenced key must exist or the save-time dangling-!ENV validator refuses it.
  const env = await request.post('/api/config/env', {
    headers: apiHeaders(),
    data: { [keyPre]: uniq('secret') },
  });
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
  const keyPre = uniq('E2E_MCP_PRE').toUpperCase();
  createdEnvKeys.add(keyPre);
  const seedEnv = await request.post('/api/config/env', {
    headers: apiHeaders(),
    data: { [keyPre]: uniq('secret') },
  });
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
  await field.getByLabel(ENV_ENTRY).fill(uniq('pasted-secret'));
  await field.getByRole('button', { name: 'Use secret' }).click();

  // The combined op generated exactly one new env key; the pre-existing key is intact.
  let generated = '';
  await expect
    .poll(async () => {
      const keys = Object.keys(await storedEnv(request)).filter(
        (k) => !before.has(k) && k !== 'TAI_ENV_SECRET_KEYS',
      );
      if (keys.length === 1) generated = keys[0] ?? '';
      return keys.length;
    })
    .toBe(1);
  createdEnvKeys.add(generated);
  expect((await storedEnv(request))[keyPre]).toBeDefined();

  // Drop the entry (and thus the generated reference), then save: the sweep deletes
  // the session-generated key (ACCEPT) but never the pre-existing one (DECLINE — it
  // was orphaned by the paste yet this editor did not generate it).
  await page.getByRole('button', { name: 'Remove server 1' }).click();
  await saveMcpConfig(page);

  await expect.poll(async () => (await storedEnv(request))[generated]).toBeUndefined();
  expect((await storedEnv(request))[keyPre]).toBeDefined();
});
