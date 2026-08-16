/**
 * Shared helpers for the browser-e2e specs. Every spec drives the Studio served
 * by the skeleton and asserts through BOTH the UI and the API (same origin, the
 * `x-api-key` header the app itself uses). The few lines of login idiom are
 * copied from tai-studio's own suite rather than imported across repos so the
 * two Node lockfiles stay uncoupled.
 */
import { expect, type APIRequestContext, type APIResponse, type Page } from '@playwright/test';

/** The pinned ports + seeded key; defaults MUST match the studio_runner. */
export const UI_PORT = Number(process.env.TAI_E2E_UI_PORT ?? 8770);
export const LLM_PORT = Number(process.env.TAI_E2E_UI_LLM_PORT ?? 8771);
export const IDP_PORT = Number(process.env.TAI_E2E_UI_IDP_PORT ?? 8772);
export const API_KEY = process.env.TAI_E2E_UI_API_KEY ?? 'sk-e2e-ui-DO-NOT-USE-IN-PRODUCTION-000';

/** The scripted-LLM control origin the authored-agent spec drives over HTTP. */
export const LLM_CONTROL_URL = `http://127.0.0.1:${String(LLM_PORT)}`;

/** The stub OAuth IdP control origin the connectors spec flips its denial knob on. */
export const IDP_CONTROL_URL = `http://127.0.0.1:${String(IDP_PORT)}`;

/**
 * The pinned marketplace coordinates, forwarded to the runner by
 * `playwright.config.ts` and addressed directly by the marketplace specs. The
 * defaults MUST match `tai42_e2e.studio_runner.StudioRunnerSettings` (ui_mp_port /
 * ui_mp_web_port / ui_mp_admin_token). The token is an obviously test-only
 * constant — the registry admin surface is harness-local, so a fixed value is
 * allowed here.
 */
export const MP_PORT = Number(process.env.TAI_E2E_UI_MP_PORT ?? 8778);
export const MP_WEB_PORT = Number(process.env.TAI_E2E_UI_MP_WEB_PORT ?? 8779);
export const MP_ADMIN_TOKEN =
  process.env.TAI_E2E_UI_MP_ADMIN_TOKEN ?? 'mp-admin-e2e-DO-NOT-USE-IN-PRODUCTION-000';

/** The pinned registry origin (admin advisory calls address it directly). */
export const MP_URL = `http://127.0.0.1:${String(MP_PORT)}`;

/** The pinned public marketplace web origin (the read-only browse site). */
export const MP_WEB_URL = `http://127.0.0.1:${String(MP_WEB_PORT)}`;

/** The registry admin bearer header for advisory create/withdraw calls. */
export function mpAdminHeaders(): Record<string, string> {
  return { authorization: `Bearer ${MP_ADMIN_TOKEN}`, 'content-type': 'application/json' };
}

/** The sessionStorage key `useAuth` reads/writes for the "remember" opt-in. */
const SESSION_KEY = 'tai-studio.apiKey';

/** A unique suffix so specs sharing one live stack never collide on names. */
export function uniq(prefix = 'e2e'): string {
  return `${prefix}_${Math.random().toString(16).slice(2, 10)}`;
}

/**
 * The pinned first-owner bootstrap token. MUST match
 * ``tai42_e2e.manifests._ACCOUNTS_BOOTSTRAP_TOKEN`` (the value the studio stack sets
 * as ``TAI_ACCOUNTS_BOOTSTRAP_TOKEN``), so the login spec can create the owner
 * through the gated bootstrap form deterministically.
 */
export const BOOTSTRAP_TOKEN = 'e2e-accounts-bootstrap-token';

/** The app's auth header (Bearer or X-Api-Key are both accepted; the app uses X-Api-Key). */
export function apiHeaders(key: string = API_KEY): Record<string, string> {
  return { 'x-api-key': key, 'content-type': 'application/json' };
}

/**
 * POST a config mutation, retrying past the reload gate's documented retriable
 * `503` ("reloading — the server is applying a config reload; retry shortly") that
 * a `/api/config/*` or `/api/mcp-config` write can hit when it races an in-flight
 * reload (a fanned-out apply on a MULTIWORKER stack). Retries on its own cadence
 * (never a raw sleep) until the write leaves the gate (any non-503 response), then
 * returns it — the caller asserts the final status, exactly as on a direct POST.
 */
export async function postConfig(
  request: APIRequestContext,
  url: string,
  data: unknown,
  key: string = API_KEY,
): Promise<APIResponse> {
  let res!: APIResponse;
  await expect(async () => {
    res = await request.post(url, { headers: apiHeaders(key), data });
    expect(res.status(), await res.text()).not.toBe(503);
    // A reload-gated write can race a fanned-out reload; the gate frees only once the
    // in-flight reload completes, which on a MULTIWORKER stack can exceed 20s (epoch
    // build + swap). Give the retry window room for a full reload cycle.
  }).toPass({ timeout: 45_000 });
  return res;
}

/**
 * Wait until the fleet's reload gates are FREE, so a reload-heavy spec (a profile apply /
 * env write / mcp-config save) leaves the stack QUIESCENT and does not cascade a mid-reload
 * `503`/`500`/element-not-found into the next serial spec (workers:1).
 *
 * Polls the reload-gated door with an EMPTY target set — a verified no-op that reloads
 * NOBODY (empty fan-out, env untouched) yet still returns the reload gate's retriable
 * `503` while a worker is mid-reload — and requires several CONSECUTIVE `200`s so the
 * round-robin across the workers behind the one MULTIWORKER port covers every worker.
 */
export async function waitForReloadSettle(request: APIRequestContext, key: string = API_KEY): Promise<void> {
  const deadline = Date.now() + 60_000;
  let settled = 0;
  while (settled < 4) {
    if (Date.now() > deadline) throw new Error('fleet reload gates never settled within 60s');
    const res = await request.post('/api/config/reload', { headers: apiHeaders(key), data: { targets: [] } });
    if (res.status() === 503) {
      settled = 0;
      continue;
    }
    expect(res.status(), await res.text()).toBe(200);
    settled += 1;
  }
}

/**
 * Pre-seed the credential into sessionStorage BEFORE any page script runs, so
 * the shell boots authenticated and survives full-page navigations. Used by the
 * specs that are not specifically exercising the login screen.
 */
export async function seedCredential(page: Page, key: string = API_KEY): Promise<void> {
  await page.addInitScript(
    ([storageKey, value]) => {
      window.sessionStorage.setItem(storageKey, value);
    },
    [SESSION_KEY, key] as const,
  );
}

/**
 * Log in through the real credential screen with an API key (paste key + remember
 * + submit).
 *
 * Toggle-aware: when an accounts provider is installed, `/api/login/methods` is
 * non-empty and the renderer collapses the key-paste form behind a "Use an API key
 * instead" toggle; with no accounts provider the key field is rendered directly.
 *
 * The screen paints before that read settles and re-renders into the other shape when
 * it does, so nothing observed on the credential screen is a fact the next line can
 * build on: the shape a look at the toggle reported can be the other one by the time
 * the next action runs, a field that already took the key can be replaced by a fresh
 * empty one, and a form that was filled and ticked can be torn down before its submit
 * button is reachable. The whole key-paste interaction — expand behind the toggle when
 * one is showing, paste, tick remember, submit — is therefore driven as ONE retried
 * unit that only completes once the credential screen has actually been left, so a
 * swap at any point inside it just costs an attempt. Each action carries its OWN short
 * bound: aimed at an element the swap is about to remove, an unbounded action would
 * wait out the whole test budget instead of failing fast into the next attempt, while
 * the bound on the unit as a whole is what gives the fetch room to land. Re-entering is
 * safe because the guard skips a unit that has already navigated, `fill` and `check`
 * are unconditional writes rather than accumulating ones, and the toggle — the one
 * step that is not idempotent on its own — converges through the retry: a click that
 * collapsed the form leaves the toggle showing again, so the next attempt re-expands
 * it. That is correct on stacks with AND without accounts, under either settle
 * ordering. The submit button is scoped to the form that owns the key field because a
 * rendered password method also carries a "Sign in" button (its title), which would
 * otherwise be ambiguous.
 *
 * A full-projection session's lander navigates to its first covered feature entry —
 * Dashboard, whose route is /observability — so that is where a completed login settles.
 */
export async function loginViaUi(page: Page, key: string = API_KEY): Promise<void> {
  await page.goto('/login');
  const toggle = page.getByRole('button', { name: 'Use an API key instead' });
  const keyField = page.getByLabel('API key');
  const remember = page.getByRole('checkbox', { name: 'Remember on this device (this browser session)' });
  const keyForm = page.locator('form').filter({ has: keyField });
  await expect(async () => {
    if (new URL(page.url()).pathname.startsWith('/login')) {
      if (await toggle.isVisible()) await toggle.click({ timeout: 2_000 });
      await keyField.fill(key, { timeout: 2_000 });
      await remember.check({ timeout: 2_000 });
      await keyForm.getByRole('button', { name: 'Sign in' }).click({ timeout: 2_000 });
    }
    await page.waitForURL('**/observability', { timeout: 5_000 });
  }).toPass({ timeout: 45_000 });
}

/**
 * Mint an API key through the live mint door and return its raw `sk-` token. `by`
 * is the minting credential (defaults to the pinned root key): minting through the
 * root `*` key yields a top-level key that can itself mint, while minting through a
 * non-admin key forces self-ownership so the child is an OWNED key that can mint
 * nothing — the owner→owned chain the owned-key journey arranges over the API.
 */
export async function mintKey(
  request: APIRequestContext,
  opts: { userId: string; scopes: string[]; description?: string; by?: string },
): Promise<string> {
  const res = await request.post('/api/auth/api-keys', {
    headers: apiHeaders(opts.by ?? API_KEY),
    data: { user_id: opts.userId, description: opts.description ?? 'e2e owned-journey key', scopes: opts.scopes },
  });
  expect(res.status(), await res.text()).toBe(200);
  return ((await res.json()) as { data: { api_key: string } }).data.api_key;
}

/**
 * Mint a one-time claim link that carries `apiKey` to a browser via the URL
 * fragment. `by` is the authed creator (defaults to root, which may link any key);
 * returns the `{ token, claim_path }` the login screen exchanges at `/login#claim=`.
 */
export async function createClaimLink(
  request: APIRequestContext,
  apiKey: string,
  by: string = API_KEY,
): Promise<{ token: string; claim_path: string }> {
  const res = await request.post('/api/auth/claim-links', {
    headers: apiHeaders(by),
    data: { api_key: apiKey },
  });
  expect(res.status(), await res.text()).toBe(200);
  return ((await res.json()) as { data: { token: string; claim_path: string } }).data;
}

/**
 * Run a registered tool synchronously through the skeleton's own execution door
 * (`POST /api/run-tool`) and return its result. Proves a UI-created preset is a
 * LIVE, runnable tool on the real stack, not merely a stored row.
 *
 * The sync door is a `fenced`, admin-only meta-executor, so `key` must be an ADMIN
 * credential (the default pinned root key is). A scoped, non-admin identity reaches
 * its tools through the grantable background door instead — see {@link runToolAsync}.
 */
export async function runTool(
  request: APIRequestContext,
  toolName: string,
  args: Record<string, unknown> = {},
  key: string = API_KEY,
): Promise<unknown> {
  const res = await request.post('/api/run-tool', {
    headers: apiHeaders(key),
    data: { tool_name: toolName, arguments: args },
  });
  expect(res.status(), await res.text()).toBe(200);
  const body = (await res.json()) as { data: unknown };
  return body.data;
}

/**
 * Submit a tool to the background run door (`POST /api/tool-runs`) and return its
 * `run_id`. This is the GRANTABLE run path (`action="write"`) a non-admin operator
 * uses: the synchronous `/api/run-tool` door is `fenced` (admin-only), so a scoped
 * owned key reaches its tools through here. The door answers `202` at once and runs
 * the tool as an in-process background task under the SUBMITTER's identity.
 */
export async function submitToolRun(
  request: APIRequestContext,
  toolName: string,
  args: Record<string, unknown> = {},
  key: string = API_KEY,
): Promise<string> {
  const res = await request.post('/api/tool-runs', {
    headers: apiHeaders(key),
    data: { tool_name: toolName, arguments: args },
  });
  expect(res.status(), await res.text()).toBe(202);
  return ((await res.json()) as { data: { run_id: string } }).data.run_id;
}

/**
 * Poll a background run record (`GET /api/tool-runs/{run_id}`) until it reaches a
 * terminal state and return the succeeded run's result. A `failed`/`lost` run raises
 * loudly with its recorded error rather than hanging or passing silently.
 */
export async function awaitToolRun(
  request: APIRequestContext,
  runId: string,
  key: string = API_KEY,
): Promise<unknown> {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    const res = await request.get(`/api/tool-runs/${runId}`, { headers: apiHeaders(key) });
    expect(res.status(), await res.text()).toBe(200);
    const record = ((await res.json()) as { data: { status: string; result?: unknown; error?: string } }).data;
    if (record.status === 'succeeded') return record.result;
    if (record.status === 'failed' || record.status === 'lost') {
      throw new Error(`tool run ${runId} ended ${record.status}: ${record.error ?? ''}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(`tool run ${runId} did not reach a terminal state`);
}

/**
 * Run a tool through the background door end to end (submit, then await its result)
 * — the async twin of {@link runTool} for a scoped, non-admin caller that cannot use
 * the admin-only synchronous door.
 */
export async function runToolAsync(
  request: APIRequestContext,
  toolName: string,
  args: Record<string, unknown> = {},
  key: string = API_KEY,
): Promise<unknown> {
  return awaitToolRun(request, await submitToolRun(request, toolName, args, key), key);
}
