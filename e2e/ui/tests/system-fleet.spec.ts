/**
 * The System page's worker fleet over the REAL worker bus: the studio_stack runs
 * MULTIWORKER(2) with the bus enabled, so the Workers card renders the live bus
 * presence census (`GET /api/fleet/workers`, every subscribed `{kind}-{uuid}`
 * origin) INDEPENDENT of any task backend, and the admin-only fleet soft-restart
 * (`POST /api/fleet/reload-config`) reloads every worker through the reload-framed
 * dialog.
 *
 * The reload control is capability-gated on `useCanWrite('/api/fleet/reload-config',
 * 'POST')`: an admin reaches the enabled control, a non-admin (viewer) does not — the
 * button is not rendered (a read-only note stands in its place) — yet the viewer still
 * reads the unfenced census. Two sessions prove both sides of the gate.
 */
import { expect, test, type APIRequestContext } from '@playwright/test';
import { apiHeaders, seedCredential, uniq } from './helpers';

/** A password comfortably over the accounts provider's minimum length. */
const VIEWER_PASSWORD = 'e2e-viewer-password-000';

/**
 * Create a viewer account through the accounts plugin (admin mints the invite, the
 * public accept sets a password + returns a `tai-sess-` session token) and return
 * that session token. The viewer's seeded jq role condition denies the admin-fenced
 * `POST /api/fleet/reload-config` while leaving the read-only census reachable — the
 * same role-creation pattern the accounts suite uses. The session token authenticates
 * as `x-api-key` (the gate reads `tai-sess-` from either auth header).
 */
async function createViewerSession(request: APIRequestContext): Promise<string> {
  const created = await request.post('/api/auth/users', {
    headers: apiHeaders(),
    data: { email: `${uniq('viewer')}@e2e.test`, role: 'viewer' },
  });
  expect(created.status(), await created.text()).toBe(200);
  const inviteToken = ((await created.json()) as { data: { invite_token: string } }).data.invite_token;

  const accepted = await request.post('/api/login/invite/accept', {
    data: { invite_token: inviteToken, password: VIEWER_PASSWORD, password_confirm: VIEWER_PASSWORD },
  });
  expect(accepted.status(), await accepted.text()).toBe(200);
  return ((await accepted.json()) as { data: { token: string } }).data.token;
}

test('system (admin): worker census + enabled fleet-reload → reload-framed convergence', async ({
  page,
}) => {
  await seedCredential(page);
  await page.goto('/system');

  await expect(page.getByRole('heading', { level: 1, name: 'System' })).toBeVisible();

  // The worker census renders from the bus presence read, with or without a backend.
  // The heading carries the live origin count and each row is a `{kind}-{uuid}` origin.
  await expect(page.getByRole('heading', { name: /^Workers \(\d+\)$/ })).toBeVisible();
  await expect(
    page.getByRole('checkbox', { name: /^Select (serve|backend)-[0-9a-f]+$/ }).first(),
  ).toBeVisible();

  // An admin reaches the enabled fleet-reload control; no read-only note is shown.
  await expect(page.getByRole('note', { name: /admin action/ })).toHaveCount(0);
  const reload = page.getByRole('button', { name: 'Reload config (all)' });
  await expect(reload).toBeEnabled();

  // Drive the fleet soft-restart through the reload-framed dialog.
  await reload.click();
  const dialog = page.getByRole('dialog', { name: 'Reload worker config' });
  await dialog.getByRole('button', { name: 'Reload config' }).click();

  // A healthy fleet converges: the System page shows the reload-framed success status
  // ("reload" framing, NOT the config-save "Change saved" framing). A non-converged fleet
  // would instead render the shared <FleetReport> alert; a real degraded state is not forcible
  // through this harness, so convergence is the honest observable here.
  //
  // The request stays PENDING (the dialog's Reload button spins) until the fan-out converges:
  // the publisher awaits every sibling's terminal reload reply up to the bus apply_timeout
  // (TAI_BUS_APPLY_TIMEOUT, 30s). Under CI load a slow sibling reload pushes convergence toward
  // that ceiling, so the success banner can take ~20-30s to render — far past the default 10s
  // expect budget. Wait past the apply_timeout so the assertion observes the settled convergence
  // rather than a mid-flight spinner; a genuine non-convergence still renders the <FleetReport>
  // alert (not this text) within the same window and fails.
  await expect(page.getByText('Reload converged across the fleet.')).toBeVisible({ timeout: 45_000 });
});

test('system (non-admin viewer): census visible, fleet-reload button gated out', async ({
  page,
  request,
}) => {
  const viewerToken = await createViewerSession(request);
  await seedCredential(page, viewerToken);
  await page.goto('/system');

  await expect(page.getByRole('heading', { level: 1, name: 'System' })).toBeVisible();

  // The census is an unfenced read — a viewer still sees the WorkersCard populated.
  await expect(page.getByRole('heading', { name: /^Workers \(\d+\)$/ })).toBeVisible();
  await expect(
    page.getByRole('checkbox', { name: /^Select (serve|backend)-[0-9a-f]+$/ }).first(),
  ).toBeVisible();

  // The capability gate hides the reload control: no reload button at all, and the
  // read-only note stands in its place (proving the projection ⊆ gate, not a 403).
  await expect(page.getByRole('button', { name: /^Reload config/ })).toHaveCount(0);
  await expect(page.getByTestId('reload-read-only-note')).toHaveText(
    'Reloading worker config is an admin action.',
  );
});
