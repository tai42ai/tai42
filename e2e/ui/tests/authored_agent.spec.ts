/**
 * Author + run an agent. The LLM is the scripted stub:
 * turn 1 calls the baked tool `e2e_echo`, turn 2 returns the final content. Via
 * the compose UI: author an agent over the spec-runnable `tools_agent` with
 * `e2e_echo` as a baked tool, then run it from the UI and assert the streamed
 * run ends with the scripted final content (UI). API: the authored preset
 * exists, and the stub recorded exactly 2 completion requests — the whole
 * LLM → tool → LLM loop crossed the browser, the server, and the stub socket.
 */
import { expect } from '@playwright/test';
import type { APIRequestContext, Page } from '@playwright/test';
import { apiHeaders, createMemberSession, LLM_CONTROL_URL, seedCredential, uniq } from './helpers';
import { needs, test } from './needs';
import { pickTool } from './pickers';

needs('kind:versioning', 'probe-tools', 'mutable', 'setting:agent:tools_agent', 'helper:llm');

/**
 * Fail loudly unless the stack holds no authored agent — no preset whose base tool is a
 * registered agent — which is the state the "No authored agents yet" card describes.
 */
async function expectNoAuthoredAgents(request: APIRequestContext): Promise<void> {
  const agents = await request.get('/api/agents', { headers: apiHeaders() });
  expect(agents.status(), await agents.text()).toBe(200);
  const agentNames = new Set(
    ((await agents.json()) as { data: { items: { name: string }[] } }).data.items.map((agent) => agent.name),
  );
  const presets = await request.get('/api/presets', { headers: apiHeaders() });
  expect(presets.status(), await presets.text()).toBe(200);
  const authored = ((await presets.json()) as { data: { name: string; base_tool: string }[] }).data
    .filter((preset) => agentNames.has(preset.base_tool))
    .map((preset) => preset.name);
  expect(authored, 'the stack must hold no authored agent for the empty-state card').toEqual([]);
}

/** The authoring section's "No authored agents yet" card on the Agents page. */
function noAuthoredAgentsCard(page: Page) {
  return page
    .getByRole('region', { name: 'Agent authoring' })
    .getByRole('status')
    .filter({ hasText: 'No authored agents yet' });
}

// These run before the compose journey below creates the stack's first authored agent.
test.describe('the Agents page says when no authored agent exists', () => {
  needs(
    'kind:versioning',
    'kind:identity',
    'kind:accounts',
    'mutable',
    'setting:agent:tools_agent',
    'setting:no-authored-agents',
  );

  test('a writer sees the card with the hint to compose one', async ({ page, request }) => {
    await expectNoAuthoredAgents(request);
    await seedCredential(page);
    await page.goto('/agents');

    await expect(page.getByRole('button', { name: 'Compose agent', exact: true })).toBeVisible();
    const card = noAuthoredAgentsCard(page);
    await expect(card).toContainText('No authored agents yet');
    await expect(card).toContainText('Compose an agent to bake a prompt, tools, and presets over a base agent.');
  });

  test('a viewer sees the card with no hint and no Compose', async ({ page, request }) => {
    await expectNoAuthoredAgents(request);
    await seedCredential(page, await createMemberSession(request, 'viewer'));
    // Compose is withheld while the capability projection loads; wait for it to land so
    // the absence below is the viewer's projection, not the loading gate.
    const projection = page.waitForResponse(
      (response) => new URL(response.url()).pathname === '/api/auth/me' && response.ok(),
    );
    await page.goto('/agents');
    await projection;

    // The card's text is its title alone: the hint names Compose, which a viewer lacks.
    await expect(noAuthoredAgentsCard(page)).toHaveText('No authored agents yet');
    await expect(page.getByRole('button', { name: 'Compose agent', exact: true })).toHaveCount(0);
  });
});

test('author an agent, run it over the scripted stub, and assert both sides', async ({ page, request }) => {
  const name = uniq('agent');
  const echoed = uniq('echo');
  const finalText = `done ${echoed}`;

  // Script the two-turn conversation: tool_call e2e_echo, then the final answer.
  const reset = await request.post(`${LLM_CONTROL_URL}/_reset`);
  expect(reset.status()).toBe(200);
  const script = await request.post(`${LLM_CONTROL_URL}/_script`, {
    data: {
      turns: [
        { tool_call: { name: 'e2e_echo', arguments: { payload: echoed } } },
        { content: finalText },
      ],
    },
  });
  expect(script.status(), await script.text()).toBe(200);

  await seedCredential(page);
  await page.goto('/agents');

  // Compose: name + base agent + the baked tool.
  await page.getByRole('button', { name: 'Compose agent' }).click();
  const dialog = page.getByRole('dialog', { name: 'Compose an agent' });
  await dialog.getByRole('textbox', { name: 'Name' }).fill(name);
  // Description is REQUIRED and gates Compose; the composed agent's LLM-facing docstring.
  await dialog.getByRole('textbox', { name: 'Description' }).fill(`authored agent ${name}`);
  await dialog.getByRole('combobox', { name: 'Base agent' }).click();
  await page.getByRole('option', { name: /^tools_agent/ }).click();
  // Add exactly one tool: search the tool picker for it and choose it.
  await pickTool(page, dialog.getByRole('combobox', { name: 'Add a tool', exact: true }), 'e2e_echo');
  await expect(dialog.getByRole('button', { name: 'Remove tool e2e_echo', exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: 'Compose agent' }).click();

  // Run it from the UI (all run-time inputs are optional; the scripted stub
  // drives the loop regardless of the message content).
  await page.getByRole('button', { name: `Run authored agent ${name}` }).click();
  await page.getByRole('button', { name: 'Run', exact: true }).click();

  // UI: the streamed run ends with the scripted final content.
  await expect(page.getByTestId('agent-timeline')).toContainText(finalText);

  // API: the authored preset exists.
  const preset = await request.get(`/api/presets/${name}`, { headers: apiHeaders() });
  expect(preset.status(), await preset.text()).toBe(200);

  // API: the stub saw exactly the two completions (tool call + final).
  const recorded = await request.get(`${LLM_CONTROL_URL}/_requests`);
  expect(recorded.status()).toBe(200);
  const body = (await recorded.json()) as { count: number };
  expect(body.count).toBe(2);
});
