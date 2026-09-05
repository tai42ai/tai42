/**
 * Create a versioned custom node. Via the custom nodes page: create
 * a custom node over base `e2e_echo` baking a unique payload. Asserts the new row in
 * the UI, that the API list carries it, and that it is a LIVE tool on the real
 * stack (running it returns the baked payload) — not just a stored row.
 */
import { expect, test } from '@playwright/test';
import { apiHeaders, runTool, seedCredential, uniq } from './helpers';

test('create a custom node over e2e_echo; UI row + API list + live tool', async ({ page, request }) => {
  const name = uniq('preset');
  const payload = uniq('payload');

  await seedCredential(page);
  await page.goto('/presets');

  await page.getByRole('button', { name: 'Create custom node' }).click();
  const dialog = page.getByRole('dialog', { name: 'Create custom node' });
  await dialog.getByRole('textbox', { name: 'Name' }).fill(name);
  // Description is REQUIRED and gates submit; the bound tool's LLM-facing docstring.
  await dialog.getByRole('textbox', { name: 'Description' }).fill(`echo preset ${name}`);
  // The base-tool listbox holds 40+ tools in a Radix Select whose content sets no
  // max-height/scroll, so an option below the fold (e2e_echo is ~18th) is off
  // screen and unreachable by pointer. Radix typeahead on the focused trigger
  // selects it WITHOUT opening the listbox: typing jumps to the first option
  // whose text starts with the string (e2e_echo, before its _prometheus variants).
  const baseTool = dialog.getByRole('combobox', { name: 'Base tool' });
  // The picker is disabled ("Loading tools…") until the tools query resolves;
  // focus/typeahead on the disabled trigger drops the keystrokes, so gate on the
  // trigger being enabled before typing.
  await expect(baseTool).toBeEnabled();
  await baseTool.focus();
  await baseTool.pressSequentially('e2e_echo');
  // Exactly e2e_echo (typeahead lands on the first prefix match), not an
  // e2e_echo_prometheus_metrics variant — the negative lookahead rejects a
  // trailing underscore.
  await expect(baseTool).toHaveText(/^e2e_echo(?!_)/);
  await dialog.getByRole('textbox', { name: 'Fixed kwargs JSON' }).fill(JSON.stringify({ payload }));
  await dialog.getByRole('button', { name: 'Create custom node' }).click();

  // UI: the new custom node appears as a row (its name cell links to the detail view).
  await expect(page.getByRole('link', { name: `Open custom node ${name}` })).toBeVisible();

  // API: the presets list carries it.
  const list = await request.get('/api/presets', { headers: apiHeaders() });
  expect(list.status(), await list.text()).toBe(200);
  const listBody = (await list.json()) as { data: Array<{ name: string; active_version: number }> };
  const row = listBody.data.find((r) => r.name === name);
  expect(row, `preset ${name} missing from /api/presets`).toBeTruthy();
  expect(row?.active_version).toBe(1);

  // The UI-created custom node is a live tool: running it returns the baked payload.
  const result = await runTool(request, name, {});
  expect(result).toBe(payload);
});
