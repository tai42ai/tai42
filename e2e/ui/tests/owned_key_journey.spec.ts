/**
 * The scoped-owned-key onboarding journey end to end. The pytest twin proves
 * projection ⊆ gate, claim-link lifecycle, and per-identity isolation over the API
 * (`tests/owned_keys/`); the browser never drove the owner→QR→scoped-Studio path.
 * Here the API ARRANGES (mint an owner, have it mint a
 * scoped OWNED key, mint a one-time claim link, and fire an `ask_user` addressed to
 * that owned identity), and the browser exercises exactly what only a browser can:
 * the `#claim=` login leg, the capability-scoped shell (a scoped nav + the
 * RouteCapabilityBoundary "not available" panel on an uncovered route), the scoped
 * inbox, and answering — while asserting the single-use claim token is never
 * persisted (fragment stripped, no query param, no storage).
 *
 * The scope model on this stack: every authed `/api` route resolves to the blanket
 * `studio` resource, and the studio-root key is admin (`*`) so its shell is the FULL
 * projection. A key minted with `["studio"]` and an owner claim is a SCOPED session
 * (`admin: false`): its nav is the capability projection of the routes it can reach.
 * To give that projection a genuine hole — the operator-only entry a scoped nav must
 * omit — the arrange step maps the `system` feature's route to an extra scope the
 * owned key does not hold, so deny-wins AND-across-tiers keeps it out of reach (the
 * `access_control_mapper` enforcement shape). The admin root still reaches it via `*`.
 */
import { expect, test } from '@playwright/test';
import {
  API_KEY,
  apiHeaders,
  armInboxResynced,
  awaitToolRun,
  createClaimLink,
  mintKey,
  runToolAsync,
  seedCredential,
  submitToolRun,
  uniq,
} from './helpers';

/** The sessionStorage key `useAuth` persists a REMEMBERED credential under; a claim
 * login is `remember=false`, so it must stay empty (the token lives in memory only). */
const SESSION_KEY = 'tai-studio.apiKey';

test('owner → scoped owned key → QR-claim login → scoped shell, inbox answer, token not persisted', async ({
  page,
  request,
  browser,
  browserName,
}) => {
  // Server-stream-rendered inbox: CI's Linux WebKit build delivers fetch-streams
  // unreliably, so this stream-dependent flow flakes only there; chromium+firefox
  // cover the stream path and webkit still runs the rest of the suite.
  test.skip(browserName === 'webkit', 'Linux CI WebKit delivers fetch-streams unreliably');
  // -- Arrange over the API (the pinned root key mints; the owner mints its owned key) --
  // Gate the `system` feature's route behind a scope the owned key will NOT hold, so
  // it resolves to `studio` AND this scope (deny-wins) — reachable by the admin root
  // (`*`) but not by a `studio`-only owned key, the operator-only hole its scoped nav
  // must show. Mapped by URL (upsert), so a reused stack re-points cleanly.
  const opsScope = uniq('ops');
  const gate = await request.post('/api/auth/scopes', {
    headers: apiHeaders(),
    data: { scope_id: opsScope, url: '/api/system/kinds' },
  });
  expect(gate.status(), await gate.text()).toBe(200);

  const ownerId = uniq('owner');
  const ownerKey = await mintKey(request, { userId: ownerId, scopes: ['studio'] });
  const ownedId = uniq('owned');
  const ownedKey = await mintKey(request, { userId: ownedId, scopes: ['studio'], by: ownerKey });

  // The scoped owned identity can EXECUTE an allowed tool on the real stack — through
  // the GRANTABLE background run door (`/api/tool-runs`), the path a non-admin operator
  // runs tools by. The synchronous `/api/run-tool` door is a `fenced`, admin-only
  // meta-executor, so a scoped owned key reaches its tools only through the async door.
  const payload = uniq('payload');
  expect(await runToolAsync(request, 'e2e_echo', { payload }, ownedKey)).toBe(payload);

  const { token, claim_path } = await createClaimLink(request, ownedKey);
  // The link carries the token in the fragment leg the login screen consumes.
  expect(claim_path).toBe(`/login#claim=${token}`);

  // -- The scoped SHELL, on a reload-surviving seeded owned session (so a deep link
  // to an uncovered route can be loaded directly) -----------------------------------
  const shellContext = await browser.newContext();
  const shellPage = await shellContext.newPage();
  await seedCredential(shellPage, ownedKey);
  // The tools explorer paginates client-side (24 entries per page by default) over the
  // full projected catalog, so the projection sentinel is read through the `?tags=` deep
  // link on e2e_echo's native `e2e` tag — the filter runs over the whole directory before
  // the page slice, shrinking the list below one page.
  await shellPage.goto('/tools?tags=e2e');
  const shellNav = shellPage.getByRole('navigation', { name: 'Primary' });
  // Sentinel: the covered feature is in the nav; absence: an operator-only entry is not.
  await expect(shellNav.getByRole('link', { name: 'Tools', exact: true })).toBeVisible();
  await expect(shellNav.getByRole('link', { name: 'Interactions', exact: true })).toBeVisible();
  await expect(shellNav.getByRole('link', { name: 'System', exact: true })).toHaveCount(0);
  // The tools page lists the projected tools (the tool-run door is reachable).
  await expect(shellPage.getByRole('link', { name: 'Open tool e2e_echo', exact: true })).toBeVisible();
  // Deep-linking an uncovered admin route seals the page with the neutral panel
  // (the RouteCapabilityBoundary), never a wall of reads the server would 403.
  await shellPage.goto('/system');
  await expect(shellPage.getByText("This area isn't available for your session.")).toBeVisible();
  await shellContext.close();

  // -- The claim login leg (in-memory session; remember=false) ----------------------
  // Submit ask_user AS THE OWNED IDENTITY through the background run door (the grantable
  // path a non-admin uses): the door answers 202 at once with a run id, and the run
  // parks in the background until the browser answers, so the addressed interaction is
  // pending when the scoped inbox opens. The background run carries the SUBMITTER's
  // identity, so with no explicit audience the server's clamp_write_audience scopes the
  // ask to the caller's OWN identity (the owned key's own id, key-keyed isolation — NOT
  // its owner) — which is exactly what the scoped claim session logs in as, so only that
  // session sees it.
  const question = uniq('question');
  const answer = uniq('answer');
  const askRunId = await submitToolRun(request, 'ask_user', { question }, ownedKey);

  // The QR/onboarding link lands here; the one-time token is exchanged automatically,
  // then the lander redirects to its first covered feature entry.
  await page.goto(`/login#claim=${token}`);
  // This is a SCOPED session, so the lander's first covered token need NOT be Dashboard.
  // Assert the post-login URL equals the href of the first visible link in the Primary
  // nav — reading the destination from the lander's own nav rather than hardcoding a
  // path, so the assertion tracks whatever feature the lander picked.
  const landingNav = page.getByRole('navigation', { name: 'Primary' });
  const firstNavLink = landingNav.getByRole('link').first();
  await expect(firstNavLink).toBeVisible();
  const landingPath = await firstNavLink.getAttribute('href');
  expect(landingPath).toBeTruthy();
  await page.waitForURL((url) => url.pathname === landingPath);

  // The token is NOT persisted: the fragment is stripped the instant it is read, it
  // never appears as a query param, and a claim login (remember=false) writes NOTHING
  // to storage — so the single-use token is nowhere recoverable.
  expect(await page.evaluate(() => window.location.hash)).toBe('');
  expect(page.url()).not.toContain('claim');
  expect(await page.evaluate((k) => window.sessionStorage.getItem(k), SESSION_KEY)).toBeNull();
  const storageDump = await page.evaluate(() =>
    JSON.stringify([...Object.entries(localStorage), ...Object.entries(sessionStorage)]),
  );
  expect(storageDump).not.toContain(token);

  // The claim session is itself SCOPED (not the admin shell): the operator-only entry
  // is absent from its nav too.
  const nav = page.getByRole('navigation', { name: 'Primary' });
  await expect(nav.getByRole('link', { name: 'Tools', exact: true })).toBeVisible();
  await expect(nav.getByRole('link', { name: 'System', exact: true })).toHaveCount(0);

  // Client-side navigation preserves the in-memory session (a full reload would drop it).
  // Arm the resync guard BEFORE opening the inbox (so the stream response is not missed).
  const ownedResynced = armInboxResynced(page);
  await nav.getByRole('link', { name: 'Interactions', exact: true }).click();
  await page.waitForURL('**/interactions');

  // The scoped inbox shows exactly the question addressed to this identity (proof the
  // session is the owned identity — the stream is audience-filtered).
  const card = page.getByTestId('interaction-card').filter({ hasText: question });
  await expect(card).toBeVisible();

  // The unrestricted operator (a second, seeded root context) sees the same pending
  // question and, once answered, watches it flip — the answered frame reaches root's
  // open stream even though the interaction is addressed to the owned identity.
  const rootContext = await browser.newContext();
  const rootPage = await rootContext.newPage();
  await seedCredential(rootPage, API_KEY);
  const rootResynced = armInboxResynced(rootPage);
  await rootPage.goto('/interactions');
  const rootCard = rootPage.getByTestId('interaction-card').filter({ hasText: question });
  await expect(rootCard).toBeVisible();

  // Both inboxes must have their stream connected and connect-time refetch of the paged
  // pending base landed before answering: otherwise the answer races that refetch, which
  // — on a slow-connecting engine (Firefox) — lands after it and drops the just-answered
  // card before its `interaction.answered` frame can flip it (see `armInboxResynced`).
  await ownedResynced();
  await rootResynced();

  // Answer it in the owned browser (the text renderer's field + Submit).
  await card.getByRole('textbox', { name: 'Your answer' }).fill(answer);
  await card.getByRole('button', { name: 'Submit' }).click();

  // UI: the owned card flips to Answered, and root's card sees it answered too.
  await expect(card.getByTestId('interaction-answered')).toBeVisible();
  await expect(rootCard.getByTestId('interaction-answered')).toBeVisible();

  // API: the parked owned run wakes and its record carries the answered value verbatim.
  const askResult = await awaitToolRun(request, askRunId, ownedKey);
  expect(JSON.stringify(askResult)).toContain(answer);

  await rootContext.close();
});
