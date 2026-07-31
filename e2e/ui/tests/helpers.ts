/**
 * Shared helpers for the browser-e2e specs. Every spec drives the Studio served
 * by the skeleton and asserts through BOTH the UI and the API (same origin, the
 * `x-api-key` header the app itself uses). The few lines of login idiom are
 * copied from tai-studio's own suite rather than imported across repos so the
 * two Node lockfiles stay uncoupled.
 */
import { expect, type APIRequestContext, type Page } from '@playwright/test';

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

/** The pinned public tai-marketplace-web origin (the read-only browse site). */
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
 * instead" toggle. This helper waits for the methods fetch to settle (either the
 * toggle or the key field becomes visible), clicks the toggle when present, then
 * pastes the key — so it stays correct on stacks with AND without accounts. The
 * submit button is scoped to the key-paste form because a rendered password method
 * also carries a "Sign in" button (its title), which would otherwise be ambiguous.
 */
export async function loginViaUi(page: Page, key: string = API_KEY): Promise<void> {
  await page.goto('/login');
  const toggle = page.getByRole('button', { name: 'Use an API key instead' });
  const keyField = page.getByLabel('API key');
  // The methods fetch resolves to one of two shapes: the toggle (accounts present,
  // key-paste collapsed) or the key field directly (no accounts). Wait for whichever
  // lands before deciding.
  await expect(toggle.or(keyField).first()).toBeVisible();
  if (await toggle.isVisible()) await toggle.click();
  await keyField.fill(key);
  await page.getByRole('checkbox', { name: 'Remember on this device (this browser session)' }).check();
  // Scope to the form that owns the API-key field: on an accounts stack a password
  // method's submit button is also labelled "Sign in".
  const keyForm = page.locator('form').filter({ has: keyField });
  await keyForm.getByRole('button', { name: 'Sign in' }).click();
  // A full-projection session's lander navigates to its first covered feature entry —
  // Dashboard, whose route is /observability — so that is where a completed login settles.
  await page.waitForURL('**/observability');
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
