/**
 * Agent-run error state (Mission 9 agents UI, run-view.tsx). The registered
 * `tools_agent` is run against the scripted LLM stub with an EMPTY script: the
 * stub answers the model call with a loud 500 ("no scripted turn"), by design.
 * The run view must surface a terminal, loud error state — never an eternal
 * spinner or a silent stop. UI: the run status settles to Failed. API: the stub
 * recorded the completion request it refused, proving the loop reached the model
 * and the failure is genuine (not a client-side no-op).
 *
 * The plain registered agent is driven (not an authored one) so the run creates
 * no preset/authored-agent residue on the shared stack.
 */
import { expect, test } from '@playwright/test';
import { LLM_CONTROL_URL, seedCredential } from './helpers';

test('a registered agent whose model turn errors surfaces a loud failed state', async ({
  page,
  request,
}) => {
  // Empty the script: the next completion call has no scripted turn → a loud 500.
  const reset = await request.post(`${LLM_CONTROL_URL}/_reset`);
  expect(reset.status()).toBe(200);

  await seedCredential(page);
  await page.goto('/agents');

  // Open the run view for the registered tools_agent and start a run — the model
  // call hits the unscripted stub and 500s.
  await page.locator('[data-agent="tools_agent"]').getByRole('button', { name: 'Run' }).click();
  await expect(page.getByRole('heading', { name: 'tools_agent' })).toBeVisible();
  await page.getByRole('button', { name: 'Run' }).click();

  // UI: the run reaches a terminal Failed state (never a stuck Running… spinner).
  await expect(page.getByText('Failed')).toBeVisible();
  await expect(page.getByText('Running…')).toHaveCount(0);

  // API: the stub recorded the completion request it refused — the loop truly
  // reached the model.
  const recorded = await request.get(`${LLM_CONTROL_URL}/_requests`);
  expect(recorded.status()).toBe(200);
  const body = (await recorded.json()) as { count: number };
  expect(body.count).toBeGreaterThanOrEqual(1);
});
