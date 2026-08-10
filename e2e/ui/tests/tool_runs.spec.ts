/**
 * Background tool-runs view. The pytest twin proves a
 * background run reaches a terminal state cross-worker
 * (`tests/tool_runs/test_background_runs.py`); the browser never opened the runs
 * surface. The runs surface is the `BackgroundRuns` PANEL inside the `/tools`
 * feature (not a `/tool-runs` route): the run panel's "Run in background" button
 * detaches the run and the panel polls `GET /api/tool-runs/{id}` to a terminal
 * state. Here a background run of `e2e_echo` is launched from the UI, its detail
 * reaches `Succeeded` with the baked result, and the same-origin API agrees. The
 * loud negative launches `e2e_fail`, whose run surfaces its error verbatim.
 */
import { expect, test } from '@playwright/test';
import { apiHeaders, seedCredential, uniq } from './helpers';

test('launch a background e2e_echo run; UI detail terminal + API record agree', async ({
  page,
  request,
}) => {
  const payload = uniq('payload');

  await seedCredential(page);
  await page.goto('/tools?tool=e2e_echo');

  // Fill the auto-form's `payload` field and launch the DETACHED run.
  await page.getByRole('textbox', { name: 'payload' }).fill(payload);
  await page.getByRole('button', { name: 'Run in background' }).click();

  // UI: the run-detail panel polls to the terminal Succeeded state and renders
  // the baked result through the ResultViewer.
  const detail = page.getByTestId('tool-run-detail');
  await expect(detail).toBeVisible();
  await expect(detail.getByText('Succeeded')).toBeVisible();
  await expect(detail).toContainText(payload);

  // The detail header carries the run id (a `<code>`), the correlation handle for
  // the API assertion below.
  const runId = (await detail.locator('code').first().innerText()).trim();
  expect(runId.length).toBeGreaterThan(0);

  // API: the same run record is terminal-succeeded with the baked result.
  const recordRes = await request.get(`/api/tool-runs/${runId}`, { headers: apiHeaders() });
  expect(recordRes.status(), await recordRes.text()).toBe(200);
  const record = (await recordRes.json()) as { data: { status: string; result: unknown; tool_name: string } };
  expect(record.data.status).toBe('succeeded');
  expect(record.data.tool_name).toBe('e2e_echo');
  expect(record.data.result).toBe(payload);

  // API: the tool's recent-runs list carries this run.
  const listRes = await request.get('/api/tool-runs?tool_name=e2e_echo', { headers: apiHeaders() });
  expect(listRes.status(), await listRes.text()).toBe(200);
  const list = (await listRes.json()) as { data: Array<{ run_id: string }> };
  expect(list.data.some((r) => r.run_id === runId)).toBe(true);
});

test('a failing background run surfaces its error in the detail view and the API', async ({
  page,
  request,
}) => {
  const message = uniq('boom');

  await seedCredential(page);
  await page.goto('/tools?tool=e2e_fail');

  // e2e_fail always raises with its `message`; run it in the background.
  await page.getByRole('textbox', { name: 'message' }).fill(message);
  await page.getByRole('button', { name: 'Run in background' }).click();

  // UI: the detail reaches the terminal Failed state and shows the error loudly.
  const detail = page.getByTestId('tool-run-detail');
  await expect(detail).toBeVisible();
  await expect(detail.getByText('Failed')).toBeVisible();
  await expect(detail).toContainText(message);

  const runId = (await detail.locator('code').first().innerText()).trim();

  // API: the run record is terminal-failed and carries the error message.
  const recordRes = await request.get(`/api/tool-runs/${runId}`, { headers: apiHeaders() });
  expect(recordRes.status(), await recordRes.text()).toBe(200);
  const record = (await recordRes.json()) as { data: { status: string; error: string } };
  expect(record.data.status).toBe('failed');
  expect(record.data.error).toContain(message);
});
