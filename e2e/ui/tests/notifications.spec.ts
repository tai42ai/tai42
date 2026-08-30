/**
 * Internal notifications sink + Studio notifications screen. The pytest
 * twin drives `notify_user(channel=None)` → sink → `GET /api/notifications` at
 * the API level across workers (`tests/channels/test_notify.py`); the browser
 * never opened the notifications inbox. Here the builtin `notify_user` USER TOOL
 * is fired with NO channel through the same-origin run-tool door (recording one
 * message to the internal sink), the message is asserted to RENDER in the Studio
 * `/notifications` screen, AND the same-origin `GET /api/notifications` feed door
 * is asserted to carry it — the UI + API twin. The loud negative reads the authed
 * feed door with no credential and asserts it is denied, never a silent empty feed.
 *
 * The screen is a read-only inbox: it ships no mark-read/dismiss affordance and
 * the sink feed has no delete door, so state is NOT restored — a `uniq()` message
 * keeps the recorded row from colliding with any sibling spec on the shared stack.
 */
import { expect, test, type APIRequestContext } from '@playwright/test';
import { apiHeaders, runTool, seedCredential, uniq } from './helpers';

/** One record the sink feed `GET /api/notifications` exposes under `data.notifications`. */
interface Notification {
  id: string;
  message: string;
  recipient: string | null;
  created_at: string;
}

/** The internal sink feed, newest-first, read through the authed feed door. */
async function readFeed(request: APIRequestContext): Promise<Notification[]> {
  const res = await request.get('/api/notifications', { headers: apiHeaders() });
  expect(res.status(), await res.text()).toBe(200);
  const body = (await res.json()) as { data: { notifications: Notification[] } };
  return body.data.notifications;
}

test('notify_user with no channel records to the internal sink and renders in the notifications screen', async ({
  page,
  request,
}) => {
  const message = uniq('notify');

  // Fire the builtin notify_user with the channel OMITTED → recorded to the
  // internal notifications sink (never sent over an external channel). The
  // fire-and-forget call returns its confirmation string immediately.
  const result = await runTool(request, 'notify_user', { message });
  expect(JSON.stringify(result)).toContain('internal sink');

  // API: the same-origin authed feed door carries the recorded message.
  await expect
    .poll(async () => (await readFeed(request)).some((n) => n.message === message))
    .toBe(true);

  // UI: the notifications inbox renders the recorded message on a card (the
  // 6.3 sink inbox replaced the table with a card list).
  await seedCredential(page);
  await page.goto('/notifications');
  const card = page
    .getByTestId('notifications-list')
    .getByTestId('notification-card')
    .filter({ hasText: message });
  await expect(card).toBeVisible();
});

test('the notifications feed door denies an unauthenticated read', async ({ request }) => {
  // GET /api/notifications matches the studio_authed access-control pattern — a
  // read with no credential is denied loudly (401 unauthenticated / 403
  // unauthorized), never a silent empty feed.
  const res = await request.get('/api/notifications');
  expect([401, 403]).toContain(res.status());
});
