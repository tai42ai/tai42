/**
 * Author + run an agent (Mission 9 PLAN_4 UI). The LLM is the scripted stub:
 * turn 1 calls the baked tool `e2e_echo`, turn 2 returns the final content. Via
 * the compose UI: author an agent over the spec-runnable `tools_agent` with
 * `e2e_echo` as a baked tool, then run it from the UI and assert the streamed
 * run ends with the scripted final content (UI). API: the authored preset
 * exists, and the stub recorded exactly 2 completion requests — the whole
 * LLM → tool → LLM loop crossed the browser, the server, and the stub socket.
 */
import { expect, test } from '@playwright/test';
import { apiHeaders, LLM_CONTROL_URL, seedCredential, uniq } from './helpers';

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
  // Add exactly ONE tool. The picker is a multi-add Radix Select (each selection
  // appends a chip). It is a popper-positioned listbox over 40+ tag-grouped options
  // with NO internal scroll container, so the e2e_echo option (in the trailing
  // "Untagged" group, far below the fold) is off-screen and a click cannot reach
  // it — there is no scrollable ancestor for click auto-scroll to move. Select it by
  // typeahead instead: OPEN the listbox first, then type the name. With the listbox
  // OPEN, typeahead only moves the HIGHLIGHT (it does NOT fire onChange per keystroke
  // the way CLOSED-trigger typeahead would — that is what cascades a growing search
  // string across the e2e_echo* group and adds several), and Enter then selects the
  // highlighted option regardless of its scroll position. "e2e_echo" is the first
  // option matching that prefix, so this lands exactly one tool: e2e_echo.
  await dialog.getByRole('combobox', { name: 'Add a tool' }).click();
  await page.getByRole('option', { name: 'e2e_echo', exact: true }).waitFor();
  // Type with a per-key delay: rapid keystrokes are dropped/misordered by Radix's
  // typeahead (leaving the initial option highlighted), whereas a small inter-key
  // delay lets each keystroke land on its own tick and stays well inside Radix's
  // per-key typeahead-accumulation window, so the search resolves to "e2e_echo".
  await page.keyboard.type('e2e_echo', { delay: 80 });
  await page.keyboard.press('Enter');
  await expect(dialog.getByRole('button', { name: 'Remove tool e2e_echo', exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: 'Compose agent' }).click();

  // Run it from the UI (all run-time inputs are optional; the scripted stub
  // drives the loop regardless of the message content).
  await page.getByRole('button', { name: `Run authored agent ${name}` }).click();
  await page.getByRole('button', { name: 'Run' }).click();

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
