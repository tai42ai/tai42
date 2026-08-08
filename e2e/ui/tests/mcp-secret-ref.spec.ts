/**
 * The MCP tab's secret-reference flow (`/manifest` → MCP) over the REAL stack. An
 * MCP entry's `config.env` map is secret-bearing: the editor mounts a masked
 * `SecretRefField` per env value that EITHER references an existing env key (stored
 * as an `!ENV ${KEY}` manifest leaf) OR pastes a new secret (a combined
 * store-then-mark op that generates the key). On save, an env key THIS editor
 * generated via a paste and no longer referenced is swept; a key it did not generate
 * is NEVER swept. This orphan handling is NOT a prompt/dialog — it is an automatic,
 * per-key, provenance-based sweep at save time (McpTab's `orphanedKeysOf`): "accept"
 * (drop) is what happens to a session-generated orphan, "decline" (keep) is what
 * happens to a picked/pre-existing one. The three PLAN_6 F7 / E5c orphan passes are
 * driven here end to end over the live combined-op + reload seam (accept a
 * session-generated key, decline a picked key, and the per-key split of a two-secret
 * server), the browser-level counterpart of the pytest unit proofs in `McpTab.test.tsx`.
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

/** Seed one MCP entry whose `config.env` carries the given `{leaf: referencedKey}` refs
 *  (each leaf becomes an `!ENV ${key}` marker mounting a SecretRefField). */
async function seedEntryEnv(
  request: APIRequestContext,
  title: string,
  env: Record<string, string>,
): Promise<void> {
  // The caller seeds the referenced env keys just before this — that env write fanned a
  // reload out to the fleet. Drain it FIRST so this mcp-config POST lands on a free reload
  // gate instead of racing the in-flight reload into a (retriable, but slow) 503 storm.
  await waitForReloadSettle(request);
  const leaves = Object.fromEntries(Object.entries(env).map(([leaf, key]) => [leaf, envMarker(key)]));
  const res = await postConfig(request, '/api/mcp-config', {
    mcp: [{ title, config: { command: '/bin/true', env: leaves } }],
  });
  expect(res.status(), await res.text()).toBe(200);
  // The seed fanned a reload out; wait for the fleet to settle so the subsequent page load /
  // field read does not race a mid-reload worker.
  await waitForReloadSettle(request);
}

/** Seed one MCP entry referencing `key` from its `config.env.REFVAL` leaf. */
async function seedEntry(request: APIRequestContext, title: string, key: string): Promise<void> {
  await seedEntryEnv(request, title, { [ENV_ENTRY]: key });
}

/** Open the MCP tab of the Manifest page and wait for the entry's `leaf` SecretRefField. */
async function openMcpField(page: Page, leaf: string = ENV_ENTRY): Promise<void> {
  await page.goto('/manifest');
  // ``exact`` so the "MCP" tab is not ambiguous with the "Sub-MCP" tab (a substring match).
  await page.getByRole('tab', { name: 'MCP', exact: true }).click();
  await expect(page.getByTestId(`mcp-secret-0-${leaf}`)).toBeVisible();
}

/**
 * Paste a fresh secret into entry 0's `leaf` SecretRefField and return the server-generated
 * env key. The combined store-then-mark op (`POST /api/mcp-config/secret-env`) writes a new
 * env key then repoints the leaf's `!ENV` marker at it — and a key generated THIS way is the
 * only kind the save-time orphan sweep may later delete (McpTab's `sessionGeneratedKeysRef`).
 * The op's response carries a COUNT, not the key name, so the generated key is discovered as
 * the single env key present after the op that was absent before it.
 */
async function pasteNewSecret(page: Page, request: APIRequestContext, leaf: string): Promise<string> {
  // Drain any residual reload (the seed's, a prior op's) so the combined secret-env op below
  // lands on a FREE reload gate instead of racing an in-flight reload into a retriable 503 —
  // nothing mutates config between here and the click, so the gate stays free for our op.
  await waitForReloadSettle(request);
  const before = new Set(Object.keys(await storedEnv(request)));
  const field = page.getByTestId(`mcp-secret-0-${leaf}`);
  await field.getByRole('button', { name: 'Change reference' }).click();
  await field.getByRole('button', { name: 'Paste new secret' }).click();
  // Target the paste input by its textbox role + exact name (the leaf key): `getByLabel(leaf)`
  // also matches the row's `role=group` wrapper (`aria-label="<leaf> source"`, a substring hit).
  await field.getByRole('textbox', { name: leaf, exact: true }).fill(uniq('pasted-secret'));
  // Await the combined op's own 200 so the store read below is AFTER the write landed, not mid-op.
  const secretEnvOk = page.waitForResponse(
    (r) => r.url().endsWith('/api/mcp-config/secret-env') && r.request().method() === 'POST',
  );
  await field.getByRole('button', { name: 'Use secret' }).click();
  const res = await secretEnvOk;
  expect(res.status(), await res.text()).toBe(200);
  // Drain the op's reload so GET /api/config/env answers 200 (not a mid-reload 503) below.
  await waitForReloadSettle(request);
  // The op generated exactly one new env key (a transiently reduced mid-swap band cannot
  // inflate this — a dropped PRE-EXISTING key was already in `before`, so never counted new).
  let generated = '';
  await expect
    .poll(
      async () => {
        const keys = Object.keys(await storedEnv(request)).filter(
          (k) => !before.has(k) && k !== 'TAI_ENV_SECRET_KEYS',
        );
        if (keys.length === 1) generated = keys[0] ?? '';
        return keys.length;
      },
      { timeout: 30_000 },
    )
    .toBe(1);
  return generated;
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
  // Drain any residual reload first so the combined op below lands on a FREE reload gate
  // instead of racing an in-flight reload into a retriable 503.
  await waitForReloadSettle(request);
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

// ---------------------------------------------------------------------------
// F7 / E5c — the save-time orphan sweep's three passes, driven end to end. The sweep is
// automatic (no prompt): a removed `!ENV` leaf's key is dropped ("accept") iff THIS editor
// generated it via a paste, and kept ("decline") when it was picked / pre-existing.
// ---------------------------------------------------------------------------

test('F7 accept: removing a server whose secret was PASTED here sweeps the session-generated key', async ({
  page,
  request,
}) => {
  // A seed env write, an mcp-config seed, the combined paste op, and the save-time sweep —
  // each a reload-gated leg followed by a fleet settle. Budget past the 60s default.
  test.setTimeout(120_000);
  const seedKey = uniq('E2E_MCP_SEED').toUpperCase();
  createdEnvKeys.add(seedKey);
  const env = await postConfig(request, '/api/config/env', { [seedKey]: uniq('secret') });
  expect(env.status(), await env.text()).toBe(200);
  await seedEntry(request, uniq('e2e-accept'), seedKey);

  await seedCredential(page);
  await openMcpField(page);

  // Paste a fresh secret over the reference: the leaf now points at a key THIS editor generated.
  const generated = await pasteNewSecret(page, request, ENV_ENTRY);
  createdEnvKeys.add(generated);

  // Remove the server — the sole reference to the generated key — and save. It was generated
  // HERE, so the save-time sweep ACCEPTS it (drops it from the store).
  await page.getByRole('button', { name: 'Remove server 1' }).click();
  await saveMcpConfig(page);
  await waitForReloadSettle(request);

  // The entry is gone AND its session-generated key was swept.
  await expect
    .poll(
      async () => {
        const [mcp, env2] = await Promise.all([preservedMcp(request), storedEnv(request)]);
        return { entries: mcp.length, genPresent: env2[generated] !== undefined };
      },
      { timeout: 30_000 },
    )
    .toEqual({ entries: 0, genPresent: false });
});

test('F7 decline: removing a server whose secret is a PICKED pre-existing key never sweeps it', async ({
  page,
  request,
}) => {
  test.setTimeout(120_000);
  const keyPicked = uniq('E2E_MCP_KEEP').toUpperCase();
  createdEnvKeys.add(keyPicked);
  const env = await postConfig(request, '/api/config/env', { [keyPicked]: uniq('secret') });
  expect(env.status(), await env.text()).toBe(200);
  // The leaf references the pre-existing key directly (a PICKED reference — not pasted here),
  // so this editor never records it as session-generated.
  await seedEntry(request, uniq('e2e-decline'), keyPicked);

  await seedCredential(page);
  await openMcpField(page);
  // A committed key reference (not a paste): the field offers "Change reference".
  await expect(
    page.getByTestId(`mcp-secret-0-${ENV_ENTRY}`).getByRole('button', { name: 'Change reference' }),
  ).toBeVisible();

  // Remove the server — dropping the ONLY reference to the picked key — and save. Because this
  // editor did not generate the key, the sweep DECLINES it: the entry goes, the key stays.
  await page.getByRole('button', { name: 'Remove server 1' }).click();
  await saveMcpConfig(page);
  await waitForReloadSettle(request);

  await expect
    .poll(
      async () => {
        const [mcp, env2] = await Promise.all([preservedMcp(request), storedEnv(request)]);
        return { entries: mcp.length, pickedPresent: env2[keyPicked] !== undefined };
      },
      { timeout: 30_000 },
    )
    .toEqual({ entries: 0, pickedPresent: true });
});

test('F7 split: removing a two-secret server sweeps only the session-generated leaf, keeping the picked one', async ({
  page,
  request,
}) => {
  // The heaviest orphan case: two SecretRefField leaves, a paste on one, then the whole-server
  // removal + per-key sweep. Extra reload-gated legs — budget generously.
  test.setTimeout(150_000);
  const LEAF_GEN = 'REFGEN';
  const LEAF_PICK = 'REFPICK';
  const seedGen = uniq('E2E_MCP_GENSEED').toUpperCase(); // the LEAF_GEN ref before the paste repoints it
  const keyPicked = uniq('E2E_MCP_PICK').toUpperCase(); // the LEAF_PICK picked pre-existing key
  createdEnvKeys.add(seedGen);
  createdEnvKeys.add(keyPicked);
  const env = await postConfig(request, '/api/config/env', { [seedGen]: uniq('s1'), [keyPicked]: uniq('s2') });
  expect(env.status(), await env.text()).toBe(200);
  await seedEntryEnv(request, uniq('e2e-split'), { [LEAF_GEN]: seedGen, [LEAF_PICK]: keyPicked });

  await seedCredential(page);
  await openMcpField(page, LEAF_GEN);
  await expect(page.getByTestId(`mcp-secret-0-${LEAF_PICK}`)).toBeVisible();

  // Paste a fresh secret into ONE leaf → a session-generated key there; the other leaf keeps its
  // PICKED pre-existing reference.
  const generated = await pasteNewSecret(page, request, LEAF_GEN);
  createdEnvKeys.add(generated);

  // Remove the whole server and save. Per-key split: the session-generated key is ACCEPTED
  // (swept) while the picked key is DECLINED (kept) — one orphan sweep, two verdicts.
  await page.getByRole('button', { name: 'Remove server 1' }).click();
  await saveMcpConfig(page);
  await waitForReloadSettle(request);

  await expect
    .poll(
      async () => {
        const [mcp, env2] = await Promise.all([preservedMcp(request), storedEnv(request)]);
        return { entries: mcp.length, genGone: env2[generated] === undefined, pickedKept: env2[keyPicked] !== undefined };
      },
      { timeout: 30_000 },
    )
    .toEqual({ entries: 0, genGone: true, pickedKept: true });
});
