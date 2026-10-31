/**
 * Shared helpers for the browser-e2e specs. Every spec drives the Studio served
 * by the skeleton and asserts through BOTH the UI and the API (same origin, the
 * `x-api-key` header the app itself uses). The few lines of login idiom are
 * copied from tai-studio's own suite rather than imported across repos so the
 * two Node lockfiles stay uncoupled.
 */
import { readFileSync } from 'node:fs';
import { connect } from 'node:net';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  expect,
  test,
  type APIRequestContext,
  type APIResponse,
  type Locator,
  type Page,
  type Request,
  type Response,
} from '@playwright/test';

import { TARGET } from './target';

/** The pinned ports + seeded key; defaults MUST match the studio_runner. Against a
 * target the key is the target's login key. */
export const UI_PORT = Number(process.env.TAI_E2E_UI_PORT ?? 8770);
export const LLM_PORT = Number(process.env.TAI_E2E_UI_LLM_PORT ?? 8771);
export const IDP_PORT = Number(process.env.TAI_E2E_UI_IDP_PORT ?? 8772);
export const API_KEY =
  TARGET?.key ?? process.env.TAI_E2E_UI_API_KEY ?? 'sk-e2e-ui-DO-NOT-USE-IN-PRODUCTION-000';

/** The scripted-LLM control origin the authored-agent spec drives over HTTP. */
export const LLM_CONTROL_URL = `http://127.0.0.1:${String(LLM_PORT)}`;

/** The stub OAuth IdP control origin the connectors spec flips its denial knob on. */
export const IDP_CONTROL_URL = `http://127.0.0.1:${String(IDP_PORT)}`;

/** The pinned port of the OAuth-protected MCP server fixture (its own sign-in service). */
export const OAUTH_MCP_PORT = Number(process.env.TAI_E2E_UI_OAUTH_MCP_PORT ?? 8773);

/** The fixture's MCP endpoint: the address the connectors spec adds a server by. */
export const OAUTH_MCP_URL = `http://127.0.0.1:${String(OAUTH_MCP_PORT)}/mcp`;

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
 * The pinned setup token. MUST equal ``tai42_e2e.manifests._SETUP_TOKEN`` (the value every
 * accounts-enabled studio stack sets as ``TAI_SETUP_TOKEN``), so the login spec drives the
 * gated ``POST /api/setup`` initialize path deterministically.
 */
export const SETUP_TOKEN = 'e2e-setup-token';

/**
 * The pinned app port + origin of the UNSEEDED accounts-enabled studio stack the login spec's
 * ``needs_setup`` flow runs against. The studio runner boots it alongside the seeded stack the
 * API-key-paste specs share (one runner process, two ports). The default MUST match the
 * ``TAI_E2E_UI_SETUP_PORT`` the ``webServer`` env in ``playwright.config.ts`` forwards to the
 * runner (its ``ui_setup_port``).
 */
export const SETUP_PORT = Number(process.env.TAI_E2E_UI_SETUP_PORT ?? 8780);
export const SETUP_URL = `http://127.0.0.1:${String(SETUP_PORT)}`;

/** The app's auth header (Bearer or X-Api-Key are both accepted; the app uses X-Api-Key). */
export function apiHeaders(key: string = API_KEY): Record<string, string> {
  return { 'x-api-key': key, 'content-type': 'application/json' };
}

/**
 * A server may close an idle keep-alive connection as the client writes on it; that first
 * write then fails before any reply (Node: `socket hang up` / ECONNRESET). Playwright keeps
 * only the error message across its channel, so the message is what identifies the reset.
 */
const STALE_CONNECTION_RESET = /\bECONNRESET\b|socket hang up/;

function isStaleConnectionReset(error: unknown): error is Error {
  return error instanceof Error && STALE_CONNECTION_RESET.test(error.message);
}

/**
 * Send a request that is safe to repeat, once more on a fresh connection when the first
 * attempt was reset before any reply. The resend is recorded as a `resent` annotation on the
 * running test. Any other failure propagates at once; a second reset propagates naming both
 * attempts. Only for the HTTP safe methods and a POST its caller has verified is a no-op: a
 * write is never sent through here.
 */
async function resendOnceOnReset(label: string, send: () => Promise<APIResponse>): Promise<APIResponse> {
  try {
    return await send();
  } catch (first) {
    if (!isStaleConnectionReset(first)) throw first;
    test.info().annotations.push({ type: 'resent', description: `${label}: ${first.message}` });
    try {
      return await send();
    } catch (second) {
      if (!isStaleConnectionReset(second)) throw second;
      throw new Error(`${label}: reset on both attempts: ${first.message}; then ${second.message}`, {
        cause: second,
      });
    }
  }
}

/** {@link resendOnceOnReset} for one `get`/`head`, or a `post` the caller has verified is a no-op. */
export function sendRepeatable(
  request: APIRequestContext,
  method: 'get' | 'head' | 'post',
  url: string,
  options?: Parameters<APIRequestContext['post']>[1],
): Promise<APIResponse> {
  const send = (): Promise<APIResponse> => {
    if (method === 'get') return request.get(url, options);
    if (method === 'head') return request.head(url, options);
    return request.post(url, options);
  };
  return resendOnceOnReset(`${method.toUpperCase()} ${url}`, send);
}

/** A request context that sends its reads (GET/HEAD) through {@link resendOnceOnReset};
 * every write and every other member reaches the wrapped context unchanged. */
class RepeatableReadsContext implements APIRequestContext {
  constructor(private readonly inner: APIRequestContext) {}

  get tracing(): APIRequestContext['tracing'] {
    return this.inner.tracing;
  }

  get(...[url, options]: Parameters<APIRequestContext['get']>): Promise<APIResponse> {
    return sendRepeatable(this.inner, 'get', url, options);
  }

  head(...[url, options]: Parameters<APIRequestContext['head']>): Promise<APIResponse> {
    return sendRepeatable(this.inner, 'head', url, options);
  }

  fetch(...[urlOrRequest, options]: Parameters<APIRequestContext['fetch']>): Promise<APIResponse> {
    const url = typeof urlOrRequest === 'string' ? urlOrRequest : urlOrRequest.url();
    const method = (options?.method ?? (typeof urlOrRequest === 'string' ? 'GET' : urlOrRequest.method())).toUpperCase();
    const send = (): Promise<APIResponse> => this.inner.fetch(urlOrRequest, options);
    return method === 'GET' || method === 'HEAD' ? resendOnceOnReset(`${method} ${url}`, send) : send();
  }

  post(...args: Parameters<APIRequestContext['post']>): Promise<APIResponse> {
    return this.inner.post(...args);
  }

  put(...args: Parameters<APIRequestContext['put']>): Promise<APIResponse> {
    return this.inner.put(...args);
  }

  patch(...args: Parameters<APIRequestContext['patch']>): Promise<APIResponse> {
    return this.inner.patch(...args);
  }

  delete(...args: Parameters<APIRequestContext['delete']>): Promise<APIResponse> {
    return this.inner.delete(...args);
  }

  dispose(...args: Parameters<APIRequestContext['dispose']>): Promise<void> {
    return this.inner.dispose(...args);
  }

  storageState(...args: Parameters<APIRequestContext['storageState']>): ReturnType<APIRequestContext['storageState']> {
    return this.inner.storageState(...args);
  }

  cookies(...args: Parameters<APIRequestContext['cookies']>): ReturnType<APIRequestContext['cookies']> {
    return this.inner.cookies(...args);
  }

  addCookies(...args: Parameters<APIRequestContext['addCookies']>): ReturnType<APIRequestContext['addCookies']> {
    return this.inner.addCookies(...args);
  }

  clearCookies(...args: Parameters<APIRequestContext['clearCookies']>): ReturnType<APIRequestContext['clearCookies']> {
    return this.inner.clearCookies(...args);
  }

  [Symbol.asyncDispose](): Promise<void> {
    return this.inner[Symbol.asyncDispose]();
  }
}

/** Wrap a request context so its reads resend once past a stale-connection reset. */
export function repeatableReads(request: APIRequestContext): APIRequestContext {
  return new RepeatableReadsContext(request);
}

/**
 * The ceiling on a fleet-mutating request's RESPONSE: a write that fans a reload out and
 * awaits the sibling worker's reload measures at up to ~35s, past Playwright's 30s default.
 * It MUST exceed every `toPass` window that wraps a mutation — a wait that expires inside
 * `toPass` re-issues the mutation, losing the client-side state (a dirty buffer, an
 * orphan-sweep set) the first attempt carried.
 */
export const MUTATION_RESPONSE_CEILING_MS = 60_000;

/**
 * Await the response of a fleet-mutating request under
 * {@link MUTATION_RESPONSE_CEILING_MS}. ARM this BEFORE the click that fires it, so a
 * fast mutation cannot land before the wait is listening. The caller asserts the
 * status — this only bounds the wait.
 */
export function awaitMutation(
  page: Page,
  match: (response: Response) => boolean,
): Promise<Response> {
  return page.waitForResponse(match, { timeout: MUTATION_RESPONSE_CEILING_MS });
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
    const res = await sendRepeatable(request, 'post', '/api/config/reload', {
      headers: apiHeaders(key),
      data: { targets: [] },
    });
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
 * Author a preset's fixed kwargs through the editor's JSON view. The Fixed kwargs
 * editor opens on its Fields view, so its JSON textarea is mounted only once the
 * "JSON" segment is selected; switch to it, then replace the textarea with the
 * serialised object. `scope` is the dialog the kwargs editor lives in.
 */
export async function fillFixedKwargs(scope: Locator, kwargs: unknown): Promise<void> {
  await scope.getByRole('button', { name: 'JSON', exact: true }).click();
  await scope.getByRole('textbox', { name: 'Fixed kwargs JSON' }).fill(JSON.stringify(kwargs));
}

/**
 * Wait until a freshly-opened `/interactions` inbox is LIVE AND RESYNCED, so an
 * answer submitted next cannot race the stream's connect-time refetch of the paged
 * pending base. ARM this BEFORE navigating to the inbox; `await` the returned
 * settle() after the card is on screen and BEFORE answering.
 *
 * WHY THIS EXISTS (the Firefox lane's answered-flip failures share it): the inbox's
 * live list is the paged base (`GET /api/interactions`) overlaid with the tail-only SSE
 * deltas, and `useInteractionsStream` refetches that base whenever its stream fetch
 * connects. An answer submitted before that connect-time refetch LANDS races it: the
 * refetch returns the base WITHOUT the just-answered question (it is no longer pending),
 * which drops the card — and because the `interaction.answered` frame carries only an id
 * (no body) and the base no longer lists the question, the frame cannot promote the card
 * back, so it never flips to "Answered". A real operator's inbox stream is connected for
 * seconds before they answer; this reproduces that precondition deterministically instead
 * of racing it.
 *
 * ENGINE PARITY (Firefox vs Chromium): Firefox's Fetch resolves a streamed response's
 * promise only once the FIRST body byte arrives (Chromium resolves it on the headers). The
 * interactions stream (`GET /api/interactions/stream`) is a TAIL-ONLY SSE with no backlog,
 * so on a freshly opened inbox — whose question was already parked BEFORE the connect — it
 * has nothing live to send. The skeleton's interactions router flushes an immediate
 * `: connected` SSE comment at connect (see `_CONNECT_FRAME` there), so the first body byte
 * arrives in ~0.1s and BOTH engines resolve the stream fetch and land the resync promptly.
 * Without it, Firefox would wait a full keepalive interval (~15s) for the periodic
 * `: keepalive` comment.
 *
 * MECHANISM (correct on BOTH engines, and regardless of connect latency): a page may open
 * more than one interactions stream — every consumer of the inbox (the nav badge and the
 * inbox page) opens its own — and each carries only the events after it connects, so an
 * answer posted between two streams' connects reaches the first and not the second, whose
 * connect-time refetch then drops the card. Readiness is therefore the LAST stream's first
 * response plus a pending-list refetch after it (see `trackInboxResync`). The settle polls
 * the tracker and resolves once it reports nothing outstanding.
 *
 * The 30s poll budget is a DEFENSIVE CEILING, not a live dependency: both engines settle in
 * ~1-3s. It spans two ~15s stream keepalive intervals of headroom, while a genuinely dead
 * stream still fails loudly, just later, with the tracker's account of what never arrived as
 * the last polled value.
 */
export function armInboxResynced(page: Page): () => Promise<void> {
  const tracker = trackInboxResync(page);
  return async () => {
    // `finally` so the listeners are detached even when the poll times out (throws) —
    // otherwise a failing settle would leak four page listeners into the rest of the test.
    try {
      await expect
        .poll(() => tracker.waiting() ?? 'resynced', {
          message: 'the inbox never resynced (the last polled value names what never arrived)',
          // A defensive ceiling, not a live dependency (see the doc comment above): the
          // fixed server settles in ~1-3s; this also covers an unfixed server's ~15s path.
          timeout: 30_000,
        })
        .toBe('resynced');
    } finally {
      tracker.detach();
    }
  };
}

/** What `trackInboxResync` reports: what the inbox still waits for, and the detach. */
export interface InboxResyncTracker {
  /** What has not arrived yet, or `undefined` once the inbox is resynced as of `now`. */
  waiting(now?: number): string | undefined;
  /** Remove the page listeners the tracker added. */
  detach(): void;
}

/**
 * Track a page's interactions streams and pending-list requests from their request
 * lifecycle events. The inbox is resynced once:
 *   - at least one stream has responded and every stream request the page opened has
 *     responded (a stream that fails before any response never connects, so it is not
 *     waited on);
 *   - no pending-list GET is in flight; and
 *   - EITHER a pending-list GET finished after the last stream response — the connect-time
 *     refetch, or (when the engine resolves the stream during the initial load and
 *     `refetchQueries` dedupes into it) the initial load finishing after that response —
 *     OR a short grace has passed since that response with no pending-list GET in flight:
 *     the safety net for an initial load that finished before the stream responded, when
 *     no refetch is coming. The refetch is dispatched synchronously when the stream
 *     response resolves, so it is in flight well within the grace and blocks that path
 *     until it lands.
 * Each new stream response restarts the refetch requirement: a refetch that finished
 * before the last stream connected does not cover that stream.
 */
export function trackInboxResync(page: Page): InboxResyncTracker {
  // The grace path is the rare-case safety net only: the routine settle is a refetch
  // finishing after the last stream response, so a generous window costs nothing in
  // practice while giving the Firefox resync ample room to appear before grace.
  const GRACE_MS = 1_000;
  let lastStreamUpAt: number | null = null;
  let baseFinishedAfterStream = false;
  const inFlightBase = new Set<Request>();
  const inFlightStreams = new Set<Request>();
  // The paged base is `GET /api/interactions` — the collection endpoint itself, matched by
  // its path ending exactly there followed by a query string (`?…`, how the app pages it
  // today) OR end-of-URL (a bare `GET /api/interactions`, so tracking never goes blind if
  // the frontend ever drops the query). A following `/` segment (`/stream`, `/media/…`,
  // `/{id}/answer`) is NOT the base and must not match — the `(?:\?|$)` anchor excludes
  // them. COUPLING: these patterns ARE the "which GET is the base refetch / a stream"
  // contract; if either door moves, update it here or the settle waits on the wrong
  // requests and fails at its ceiling.
  const isBaseGet = (request: Request): boolean =>
    request.method() === 'GET' && /\/api\/interactions(?:\?|$)/.test(request.url());
  const isStreamGet = (request: Request): boolean =>
    request.method() === 'GET' && /\/api\/interactions\/stream(?:\?|$)/.test(request.url());

  const onRequest = (request: Request) => {
    if (isBaseGet(request)) inFlightBase.add(request);
    else if (isStreamGet(request)) inFlightStreams.add(request);
  };
  const onResponse = (response: Response) => {
    if (!inFlightStreams.delete(response.request())) return;
    lastStreamUpAt = Date.now();
    baseFinishedAfterStream = false;
  };
  // requestfinished fires when the body fully lands (a real resync); requestfailed fires
  // for an aborted/superseded request — that only clears the in-flight trackers, it is
  // not a landed resync.
  const onFinished = (request: Request) => {
    if (!inFlightBase.delete(request)) return;
    if (lastStreamUpAt !== null) baseFinishedAfterStream = true;
  };
  const onFailed = (request: Request) => {
    inFlightBase.delete(request);
    inFlightStreams.delete(request);
  };

  page.on('request', onRequest);
  page.on('response', onResponse);
  page.on('requestfinished', onFinished);
  page.on('requestfailed', onFailed);

  return {
    waiting(now: number = Date.now()): string | undefined {
      if (inFlightStreams.size > 0) {
        return `${String(inFlightStreams.size)} interactions stream(s) opened without a response yet`;
      }
      if (lastStreamUpAt === null) return 'no interactions stream has been opened';
      if (inFlightBase.size > 0) {
        return `${String(inFlightBase.size)} pending-list request(s) still in flight`;
      }
      if (!baseFinishedAfterStream && now - lastStreamUpAt < GRACE_MS) {
        return 'no pending-list refetch has finished since the last stream response';
      }
      return undefined;
    },
    detach(): void {
      page.off('request', onRequest);
      page.off('response', onResponse);
      page.off('requestfinished', onFinished);
      page.off('requestfailed', onFailed);
    },
  };
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

/** The scopes the credential `key` holds, read from its own projection (`GET /api/auth/me`). */
export async function credentialScopes(request: APIRequestContext, key: string = API_KEY): Promise<string[]> {
  const res = await request.get('/api/auth/me', { headers: apiHeaders(key) });
  expect(res.status(), await res.text()).toBe(200);
  return ((await res.json()) as { data: { scopes: string[] } }).data.scopes;
}

/**
 * Mint an API key through the live mint door (as the pinned root admin) and return its
 * raw `sk-` token. Every key belongs to a principal: with no `ownerUserId` the key is
 * owned by the admin's own principal. Without `scopes` the key carries the login key's
 * own scopes, which every stack can mint — on a `*` root that is a full admin key. A
 * scope set without `*` makes a SCOPED, non-admin OWNED key (it can mint nothing and is
 * confined to its own isolation slice); a scope only the built stack seeds is a built
 * need of its spec (`setting:seeded-access-control`).
 */
export async function mintKey(
  request: APIRequestContext,
  opts: { userId: string; scopes?: string[]; ownerUserId?: string; description?: string },
): Promise<string> {
  const res = await request.post('/api/auth/api-keys', {
    headers: apiHeaders(),
    data: {
      user_id: opts.userId,
      description: opts.description ?? 'e2e owned-journey key',
      scopes: opts.scopes ?? (await credentialScopes(request)),
      ...(opts.ownerUserId === undefined ? {} : { owner_user_id: opts.ownerUserId }),
    },
  });
  expect(res.status(), await res.text()).toBe(200);
  return ((await res.json()) as { data: { api_key: string } }).data.api_key;
}

/** Revoke the key minted for `userId` through the live revoke door (as the pinned root admin). */
export async function revokeKey(request: APIRequestContext, userId: string): Promise<void> {
  const res = await request.delete(`/api/auth/api-keys/${userId}`, { headers: apiHeaders() });
  expect(res.status(), await res.text()).toBe(200);
}

/**
 * Mint a one-time claim link that carries `apiKey` to a browser via the URL fragment,
 * created as the pinned root admin (an admin may link any key). Returns the
 * `{ token, claim_path }` the login screen exchanges at `/login#claim=`.
 */
export async function createClaimLink(
  request: APIRequestContext,
  apiKey: string,
): Promise<{ token: string; claim_path: string }> {
  const res = await request.post('/api/auth/claim-links', {
    headers: apiHeaders(),
    data: { api_key: apiKey },
  });
  expect(res.status(), await res.text()).toBe(200);
  return ((await res.json()) as { data: { token: string; claim_path: string } }).data;
}

/**
 * Mint a member invite through the generic member-actions seam and return its one-time
 * `invite_token`. The bespoke users-admin route is gone: the catalog
 * (`GET /api/auth/member-actions`) declares the page-scoped "invite" action with an opaque
 * key, and the invoke door (`POST /api/auth/member-actions/invoke`) performs it. The action
 * is resolved by its generic `page` scope, never by a provider label.
 */
export async function createMemberInvite(
  request: APIRequestContext,
  { email, role }: { email: string; role: string },
): Promise<string> {
  const catalog = await request.get('/api/auth/member-actions', { headers: apiHeaders() });
  expect(catalog.status(), await catalog.text()).toBe(200);
  const actions = ((await catalog.json()) as { data: { actions: { key: string; scope: string }[] } }).data.actions;
  const pageActions = actions.filter((action) => action.scope === 'page');
  expect(pageActions.length, `exactly one page-scoped member action expected: ${JSON.stringify(actions)}`).toBe(1);
  const invoked = await request.post('/api/auth/member-actions/invoke', {
    headers: apiHeaders(),
    data: { action_key: pageActions[0].key, target_handle: null, input: { email, role } },
  });
  expect(invoked.status(), await invoked.text()).toBe(200);
  return ((await invoked.json()) as { data: { result: { invite_token: string } } }).data.result.invite_token;
}

/**
 * Create a member holding `role` and return its `tai-sess-` session token: the admin mints
 * the invite ({@link createMemberInvite}) and the public accept sets a password and answers
 * with a session. The token authenticates as `x-api-key` (the gate reads `tai-sess-` from
 * either auth header), so {@link seedCredential} drives the Studio as that member.
 */
export async function createMemberSession(request: APIRequestContext, role: string): Promise<string> {
  const inviteToken = await createMemberInvite(request, { email: `${uniq(role)}@e2e.test`, role });
  const password = `${uniq('Pw')}-Aa1`;
  const accepted = await request.post('/api/login/invite/accept', {
    data: { invite_token: inviteToken, password, password_confirm: password },
  });
  expect(accepted.status(), await accepted.text()).toBe(200);
  return ((await accepted.json()) as { data: { token: string } }).data.token;
}

/**
 * Provision an UNRESTRICTED operator and return its `tai-sess-` session token. The operator
 * is a top-level ADMIN HUMAN — an admin invite, accepted with a password, then logged in
 * for a session — which carries no owner claim, so the isolation gates give it the full
 * cross-identity view. The seeded admin root key is unrestricted too: admin is never
 * confined to an isolation slice.
 */
export async function provisionAdminSession(request: APIRequestContext): Promise<string> {
  const email = `${uniq('operator')}@e2e.test`;
  const password = `${uniq('Pw')}-Aa1`;
  const inviteToken = await createMemberInvite(request, { email, role: 'admin' });
  const accepted = await request.post('/api/login/invite/accept', {
    data: { invite_token: inviteToken, password, password_confirm: password },
  });
  expect(accepted.status(), await accepted.text()).toBe(200);
  const login = await request.post('/api/login/password', { data: { email, password } });
  expect(login.status(), await login.text()).toBe(200);
  return ((await login.json()) as { data: { token: string } }).data.token;
}

/**
 * Run a registered tool synchronously through the skeleton's own execution door
 * (`POST /api/run-tool`) and return its result: the door's run answer must be a
 * finished `result`, whose value is returned. Proves a UI-created preset is a
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
  const body = (await res.json()) as { data: { kind: string; result?: unknown } };
  expect(body.data.kind, JSON.stringify(body.data)).toBe('result');
  return body.data.result;
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

/** The visitor's web-session cookie: the plugin mints it on the chat page and every
 * public web door resolves it. It carries a SECRET token only — never the visitor id. */
export const WEB_SESSION_COOKIE = 'tai_web_session';

/** The plugin's session-registration key shape: a cookie token resolves to a server-side
 * record holding the visitor id. Read directly because the doors never disclose that id. */
const WEB_SESSION_KEY_PREFIX = 'channel:web:session:';

/** The per-run handoff the studio_runner writes at boot: coordinates the specs cannot pin
 * ahead of a dynamically reserved Redis DB. Resolved next to this file (`e2e/ui/`). */
const STACK_HANDOFF_PATH = join(dirname(dirname(fileURLToPath(import.meta.url))), '.stack-handoff.json');

/** The SUT Redis URL the booted stack reserved, read from the runner's handoff file. */
export function stackRedisUrl(): string {
  const raw = readFileSync(STACK_HANDOFF_PATH, 'utf8');
  const url = (JSON.parse(raw) as { redis_url?: unknown }).redis_url;
  if (typeof url !== 'string' || url.length === 0) {
    throw new Error(`stack handoff ${STACK_HANDOFF_PATH} carries no redis_url: ${raw}`);
  }
  return url;
}

/** One RESP reply: a simple/bulk string, an integer, nil, or an array of replies. */
type RespValue = string | number | null | RespValue[];

interface RespParse {
  readonly value: RespValue;
  readonly next: number;
}

/** Parse ONE RESP reply from `buf` at `at`, or `null` when it is incomplete. Handles the
 * simple-string/error/integer line replies, the bulk string (incl. nil) and the array. */
function parseResp(buf: Buffer, at: number): RespParse | null {
  const nl = buf.indexOf('\r\n', at);
  if (nl === -1) return null;
  const type = String.fromCharCode(buf[at]);
  const line = buf.subarray(at + 1, nl).toString('utf8');
  if (type === '-') throw new Error(`redis error reply: ${line}`);
  if (type === ':') return { value: Number(line), next: nl + 2 };
  if (type === '$') {
    const len = Number(line);
    if (len === -1) return { value: null, next: nl + 2 };
    const start = nl + 2;
    const end = start + len;
    if (buf.length < end + 2) return null;
    return { value: buf.subarray(start, end).toString('utf8'), next: end + 2 };
  }
  if (type === '*') {
    const count = Number(line);
    if (count === -1) return { value: null, next: nl + 2 };
    const items: RespValue[] = [];
    let next = nl + 2;
    for (let i = 0; i < count; i++) {
      const item = parseResp(buf, next);
      if (item === null) return null;
      items.push(item.value);
      next = item.next;
    }
    return { value: items, next };
  }
  return { value: line, next: nl + 2 };
}

function respCommand(args: readonly string[]): Buffer {
  let out = `*${args.length}\r\n`;
  for (const arg of args) out += `$${Buffer.byteLength(arg)}\r\n${arg}\r\n`;
  return Buffer.from(out, 'utf8');
}

/** Run `commands` on the SUT Redis over a raw RESP socket — no client dependency, mirroring
 * the pytest harness's raw reads. Selects the URL's DB first and returns one reply per
 * command, in order. */
async function redisCommands(
  redisUrl: string,
  commands: readonly (readonly string[])[],
): Promise<RespValue[]> {
  const url = new URL(redisUrl);
  const db = url.pathname.length > 1 ? url.pathname.slice(1) : '0';
  const host = url.hostname;
  const port = Number(url.port.length > 0 ? url.port : '6379');
  return await new Promise<RespValue[]>((resolve, reject) => {
    const socket = connect({ host, port });
    let buf = Buffer.alloc(0);
    const replies: RespValue[] = [];
    const fail = (err: Error): void => {
      socket.destroy();
      reject(err);
    };
    socket.setTimeout(5000, () => fail(new Error(`redis commands timed out: ${JSON.stringify(commands)}`)));
    socket.on('error', fail);
    socket.on('connect', () => {
      socket.write(respCommand(['SELECT', db]));
      for (const command of commands) socket.write(respCommand(command));
    });
    socket.on('data', (chunk) => {
      buf = Buffer.concat([buf, typeof chunk === 'string' ? Buffer.from(chunk) : chunk]);
      try {
        for (;;) {
          const reply = parseResp(buf, 0);
          if (reply === null) break;
          replies.push(reply.value);
          buf = buf.subarray(reply.next);
          // Reply 0 is SELECT's +OK; the rest answer `commands` in order.
          if (replies.length > commands.length) {
            socket.end();
            resolve(replies.slice(1));
            return;
          }
        }
      } catch (err) {
        fail(err instanceof Error ? err : new Error(String(err)));
      }
    });
  });
}

/** GET one key off the SUT Redis, or `null` when it is absent. */
async function redisGet(redisUrl: string, key: string): Promise<string | null> {
  const [value] = await redisCommands(redisUrl, [['GET', key]]);
  if (value !== null && typeof value !== 'string') {
    throw new Error(`redis GET ${key} answered a non-string reply: ${JSON.stringify(value)}`);
  }
  return value;
}

/** The key shape of an interaction's callback ticket (its value is the interaction id),
 * under whatever namespace the stack's reserved DB carries. */
const CALLBACK_TICKET_KEY_PATTERN = '*:interactions:ticket:*';

/** The callback ticket minted for `interactionId` — a bearer capability the admin API
 * never serializes, read from the SUT Redis exactly as the pytest harness's
 * `_resolve_ticket` reads it. Throws when no ticket maps to the interaction. */
export async function resolveCallbackTicket(interactionId: string): Promise<string> {
  const redisUrl = stackRedisUrl();
  let cursor = '0';
  do {
    const [reply] = await redisCommands(redisUrl, [
      ['SCAN', cursor, 'MATCH', CALLBACK_TICKET_KEY_PATTERN, 'COUNT', '1000'],
    ]);
    if (!Array.isArray(reply) || typeof reply[0] !== 'string' || !Array.isArray(reply[1])) {
      throw new Error(`redis SCAN answered an unexpected reply: ${JSON.stringify(reply)}`);
    }
    cursor = reply[0];
    for (const key of reply[1]) {
      if (typeof key !== 'string') throw new Error(`redis SCAN yielded a non-string key: ${JSON.stringify(key)}`);
      if ((await redisGet(redisUrl, key)) === interactionId) return key.slice(key.lastIndexOf(':') + 1);
    }
  } while (cursor !== '0');
  throw new Error(`no callback ticket maps to interaction ${interactionId}`);
}

/** The server-side visitor id a web-session cookie token is registered against — the
 * conversation address a web `ask` names its recipient by (`<identity>:<visitor>`),
 * which the public doors never disclose to the client. Read from the SUT Redis. */
export async function resolveWebVisitorId(token: string): Promise<string> {
  const raw = await redisGet(stackRedisUrl(), `${WEB_SESSION_KEY_PREFIX}${token}`);
  if (raw === null) {
    throw new Error(`no web session registration for cookie token ${token.slice(0, 8)}...`);
  }
  const visitorId = (JSON.parse(raw) as { visitor_id?: unknown }).visitor_id;
  if (typeof visitorId !== 'string' || visitorId.length === 0) {
    throw new Error(`web session registration carries no visitor_id: ${raw}`);
  }
  return visitorId;
}
