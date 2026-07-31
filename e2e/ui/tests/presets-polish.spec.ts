/**
 * The presets "polish" surfaces over the REAL versioning store: the rename
 * referee preflight, the version-history compare, per-version tag editing, the
 * dry-run validate verdict, and the absence of the conflicted section when no
 * preset is quarantined. Every effect is driven through the live Studio and the
 * real skeleton doors it calls.
 */
import { expect, test, type Page } from '@playwright/test';
import { apiHeaders, seedCredential, uniq } from './helpers';

/** Create a preset over `e2e_echo` baking `payload`, landing on its detail view. */
async function createEchoPreset(page: Page, name: string, payload: string): Promise<void> {
  await page.goto('/presets');
  await page.getByRole('button', { name: 'Create preset' }).click();
  const dialog = page.getByRole('dialog', { name: 'Create preset' });
  await dialog.getByRole('textbox', { name: 'Name' }).fill(name);
  // Description is REQUIRED (R16) and gates submit; the bound tool's LLM-facing docstring.
  await dialog.getByRole('textbox', { name: 'Description' }).fill(`echo preset ${name}`);
  const baseTool = dialog.getByRole('combobox', { name: 'Base tool' });
  await baseTool.focus();
  await baseTool.pressSequentially('e2e_echo');
  await expect(baseTool).toHaveText(/^e2e_echo(?!_)/);
  await dialog.getByRole('textbox', { name: 'Fixed kwargs JSON' }).fill(JSON.stringify({ payload }));
  await dialog.getByRole('button', { name: 'Create preset' }).click();
  await expect(page.getByRole('link', { name: `Open preset ${name}` })).toBeVisible();
}

test('rename preflight lists referees and disables submit', async ({ page, request }) => {
  const base = uniq('base');
  const composer = uniq('composer');
  await seedCredential(page);
  await createEchoPreset(page, base, uniq('payload'));

  // Seed the referee: an authored-agent preset whose composition names `base` as a
  // tool. Composing agents through the UI is covered by authored_agent.spec.ts; here
  // the referee is only the fixture the rename PREFLIGHT is about, so it is created
  // through the same door the compose UI calls — a deterministic setup, not a second
  // exercise of the (long-option-list) compose picker.
  const created = await request.post('/api/presets', {
    headers: apiHeaders(),
    data: {
      name: composer,
      base_tool: 'tools_agent',
      description: 'agent composing the base preset',
      fixed_kwargs: { tool_names: [base] },
    },
  });
  expect(created.status(), await created.text()).toBe(200);

  // Renaming the referenced preset is preflighted: the callout lists the referee
  // and the submit button is disabled.
  await page.goto(`/presets?preset=${base}`);
  await page.getByRole('button', { name: `Rename preset ${base}` }).click();
  const renameDialog = page.getByRole('dialog', { name: `Rename preset — ${base}` });
  await expect(renameDialog).toBeVisible();
  const callout = page.getByRole('alert');
  await expect(callout).toContainText(`Referenced by: ${composer}`);
  await expect(renameDialog.getByRole('button', { name: 'Rename' })).toBeDisabled();
});

test('version history compare shows the changed path row and per-version tags', async ({ page }) => {
  const name = uniq('preset');
  await seedCredential(page);
  await createEchoPreset(page, name, uniq('p1'));
  await page.goto(`/presets?preset=${name}`);

  // Save two new versions, each changing the baked payload.
  for (const payload of [uniq('p2'), uniq('p3')]) {
    await page.getByRole('button', { name: 'New version' }).click();
    const saveDialog = page.getByRole('dialog', { name: `Save version — ${name}` });
    await saveDialog
      .getByRole('textbox', { name: 'Fixed kwargs JSON' })
      .fill(JSON.stringify({ payload }));
    await saveDialog.getByRole('button', { name: 'Save as new version' }).click();
    await expect(saveDialog).toBeHidden();
  }

  // Compare v1 → v2: the two-click arm opens the diff, which lists the changed
  // baked-payload path as its own row.
  await page.getByRole('button', { name: 'Compare version 1' }).click();
  await page.getByRole('button', { name: 'Compare version 2' }).click();
  const compareDialog = page.getByRole('dialog', { name: 'v1 compared with v2' });
  await expect(compareDialog).toBeVisible();
  await expect(compareDialog.getByTestId('diff-row-fixed_kwargs.payload')).toBeVisible();
  await compareDialog.getByRole('button', { name: 'Close' }).click();

  // Edit version 2's tags: the chip appears in that version's history row.
  const tag = uniq('tag');
  await page.getByRole('button', { name: 'Edit tags for version 2' }).click();
  const tagsDialog = page.getByRole('dialog', { name: 'Edit tags — version 2' });
  await tagsDialog.getByRole('textbox', { name: 'Tags' }).fill(tag);
  await tagsDialog.getByRole('textbox', { name: 'Tags' }).press('Enter');
  await tagsDialog.getByRole('button', { name: 'Save tags' }).click();
  await expect(page.getByTestId('version-row-2').getByText(tag)).toBeVisible();
});

test('the presets edit-details dialog writes the overlay display name + tags', async ({ page, request }) => {
  const name = uniq('preset');
  await seedCredential(page);
  await createEchoPreset(page, name, uniq('payload'));

  // The presets screen edits the tool_meta overlay's display name + tags (no folder /
  // hide controls here — those are centralized on the tools screen).
  await page.goto(`/presets?preset=${name}`);
  await page.getByRole('button', { name: `Edit details for ${name}` }).click();
  const dialog = page.getByRole('dialog', { name: `Edit details — ${name}` });
  await expect(dialog).toBeVisible();
  const displayName = uniq('Nice name');
  const tag = uniq('team');
  await dialog.getByLabel('Display name').fill(displayName);
  await dialog.getByRole('textbox', { name: 'Tags' }).fill(tag);
  await dialog.getByRole('textbox', { name: 'Tags' }).press('Enter');
  await dialog.getByRole('button', { name: 'Save details' }).click();
  await expect(dialog).toBeHidden();

  // Read the overlay back through the API twin: the merge-patch wrote both fields.
  await expect
    .poll(async () => {
      const meta = (await (await request.get('/api/tool-meta', { headers: apiHeaders() })).json()) as {
        data: { meta: Array<{ tool_name: string; display_name: string | null; tags: string[] }> };
      };
      const row = meta.data.meta.find((r) => r.tool_name === name);
      return row ? { display_name: row.display_name, hasTag: row.tags.includes(tag) } : null;
    })
    .toEqual({ display_name: displayName, hasTag: true });
});

test('validate reports a clean bind, and no conflicted section when none is quarantined', async ({
  page,
}) => {
  await seedCredential(page);

  // The list-level conflicted section is absent while every preset is healthy.
  await page.goto('/presets');
  await expect(page.getByTestId('presets-conflicted-section')).toHaveCount(0);

  // The Validate button on a valid create draft returns the clean-bind verdict.
  await page.getByRole('button', { name: 'Create preset' }).click();
  const dialog = page.getByRole('dialog', { name: 'Create preset' });
  await dialog.getByRole('textbox', { name: 'Name' }).fill(uniq('draft'));
  // Description is REQUIRED (R16): the validate door mirrors the create rule and
  // rejects an empty one, so the draft must carry it to bind cleanly.
  await dialog.getByRole('textbox', { name: 'Description' }).fill('a draft that binds cleanly');
  const baseTool = dialog.getByRole('combobox', { name: 'Base tool' });
  await baseTool.focus();
  await baseTool.pressSequentially('e2e_echo');
  await expect(baseTool).toHaveText(/^e2e_echo(?!_)/);
  await dialog.getByRole('textbox', { name: 'Fixed kwargs JSON' }).fill(JSON.stringify({ payload: 'x' }));
  await dialog.getByRole('button', { name: 'Validate' }).click();
  await expect(dialog.getByText('Draft binds cleanly')).toBeVisible();
});
