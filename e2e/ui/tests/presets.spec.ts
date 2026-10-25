/**
 * Versioned presets over the REAL stack. The create dialog builds a preset over base
 * `e2e_echo` that is a LIVE tool (running it returns the baked payload), and authors the
 * input/output maps the detail then shows as body cards. Each write control on the detail
 * is gated on the caller's access: an editor session reaches every control, a viewer
 * session reads the preset with the no-grant line in place of "New version".
 */
import { expect, type APIRequestContext, type Locator, type Page } from '@playwright/test';
import { apiHeaders, createMemberSession, fillFixedKwargs, runTool, seedCredential, uniq } from './helpers';
import { needs, test } from './needs';

needs('kind:versioning', 'probe-tools', 'mutable');

/**
 * Open the create dialog and fill the name, the description and the base tool `e2e_echo`,
 * returning the dialog for the caller's remaining fields.
 */
async function openCreateDialog(page: Page, name: string): Promise<Locator> {
  await page.getByRole('button', { name: 'Create preset' }).click();
  const dialog = page.getByRole('dialog', { name: 'Create preset' });
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
  return dialog;
}

/** A detail body card: the `section` headed by its `h3` title. */
function bodyCard(page: Page, title: string): Locator {
  return page.locator('section').filter({ has: page.getByRole('heading', { level: 3, name: title, exact: true }) });
}

/**
 * Create a preset over `e2e_echo` with two versions through the admin API, so the detail
 * has a version history whose non-current row offers a roll back.
 */
async function createTwoVersionPreset(request: APIRequestContext, name: string): Promise<void> {
  const created = await request.post('/api/presets', {
    headers: apiHeaders(),
    data: {
      name,
      base_tool: 'e2e_echo',
      description: `echo preset ${name}`,
      fixed_kwargs: { payload: uniq('p1') },
    },
  });
  expect(created.status(), await created.text()).toBe(200);
  const saved = await request.post(`/api/presets/${name}/versions`, {
    headers: apiHeaders(),
    data: { fixed_kwargs: { payload: uniq('p2') } },
  });
  expect(saved.status(), await saved.text()).toBe(200);
}

/**
 * Open a preset's detail as the seeded session, returning once the caller's capability
 * projection (`GET /api/auth/me`) has answered and the version history is drawn, so a
 * write control's absence is read against a settled projection.
 */
async function openPresetDetail(page: Page, name: string): Promise<void> {
  const projection = page.waitForResponse(
    (res) => new URL(res.url()).pathname === '/api/auth/me' && res.request().method() === 'GET',
  );
  await page.goto(`/presets?preset=${name}`);
  expect((await projection).status()).toBe(200);
  await expect(page.getByRole('heading', { level: 2, name })).toBeVisible();
  await expect(page.getByTestId('version-row-2')).toBeVisible();
}

test('create a preset over e2e_echo; UI row + API list + live tool', async ({ page, request }) => {
  const name = uniq('preset');
  const payload = uniq('payload');

  await seedCredential(page);
  await page.goto('/presets');

  const dialog = await openCreateDialog(page, name);
  await fillFixedKwargs(dialog, { payload });
  await dialog.getByRole('button', { name: 'Create preset' }).click();

  // UI: the new preset appears as a row (its name cell links to the detail view).
  await expect(page.getByRole('link', { name: `Open preset ${name}` })).toBeVisible();

  // API: the presets list carries it.
  const list = await request.get('/api/presets', { headers: apiHeaders() });
  expect(list.status(), await list.text()).toBe(200);
  const listBody = (await list.json()) as { data: Array<{ name: string; active_version: number }> };
  const row = listBody.data.find((r) => r.name === name);
  expect(row, `preset ${name} missing from /api/presets`).toBeTruthy();
  expect(row?.active_version).toBe(1);

  // The UI-created preset is a live tool: running it returns the baked payload.
  const result = await runTool(request, name, {});
  expect(result).toBe(payload);
});

test('author the input and output maps in the create dialog; the detail shows their cards', async ({ page }) => {
  const name = uniq('preset');
  const inputSchema = { type: 'object', properties: { text: { type: 'string' } }, required: ['text'] };
  const inputMap = '{payload: .text}';
  const outputMap = '{echoed: .}';

  await seedCredential(page);
  await page.goto('/presets');

  // No fixed payload: the input map builds `payload` from the caller's `text`, which the
  // input schema (required by an input map) declares.
  const dialog = await openCreateDialog(page, name);
  await dialog.getByLabel('Input schema JSON').fill(JSON.stringify(inputSchema));
  await dialog.getByTestId('binding-jq-input-map').getByRole('textbox').first().fill(inputMap);
  await dialog.getByTestId('binding-jq-output-map').getByRole('textbox').first().fill(outputMap);
  await dialog.getByRole('button', { name: 'Create preset' }).click();
  await expect(dialog).toBeHidden();

  await page.getByRole('link', { name: `Open preset ${name}` }).click();
  await expect(page.getByRole('heading', { level: 2, name })).toBeVisible();

  // The body cards show the authored schema and both programs.
  await expect(bodyCard(page, 'Input schema')).toContainText('text');
  await expect(bodyCard(page, 'Input map').locator('pre code')).toContainText(inputMap);
  await expect(bodyCard(page, 'Output map').locator('pre code')).toContainText(outputMap);
});

test.describe('the preset write controls follow the caller\'s access', () => {
  needs('kind:identity', 'kind:accounts', 'kind:tool_meta');

  test('an editor reaches every write control with no read-only line', async ({ page, request }) => {
    const name = uniq('preset');
    await createTwoVersionPreset(request, name);
    await seedCredential(page, await createMemberSession(request, 'editor'));
    await openPresetDetail(page, name);

    await expect(page.getByRole('button', { name: 'New version' })).toBeVisible();
    await expect(page.getByRole('button', { name: `Edit details for ${name}` })).toBeVisible();
    await expect(page.getByRole('button', { name: `Rename preset ${name}` })).toBeVisible();
    await expect(page.getByRole('button', { name: `Delete preset ${name}` })).toBeVisible();
    // Every history row carries its roll back; the current version's is disabled.
    await expect(page.getByRole('button', { name: 'Roll back to version 1' })).toBeEnabled();
    await expect(page.getByRole('button', { name: 'Roll back to version 2' })).toBeDisabled();
    await expect(page.getByRole('status').filter({ hasText: 'is outside your access' })).toHaveCount(0);
  });

  test('a viewer reads the preset with the no-grant line and no write control', async ({ page, request }) => {
    const name = uniq('preset');
    await createTwoVersionPreset(request, name);
    await seedCredential(page, await createMemberSession(request, 'viewer'));
    await openPresetDetail(page, name);

    await expect(page.getByRole('status').filter({ hasText: 'Saving a new version' })).toHaveText(
      'Saving a new version is outside your access — this preset is shown read-only.',
    );
    await expect(page.getByRole('button', { name: 'New version' })).toHaveCount(0);
    await expect(page.getByRole('button', { name: `Edit details for ${name}` })).toHaveCount(0);
    await expect(page.getByRole('button', { name: `Rename preset ${name}` })).toHaveCount(0);
    await expect(page.getByRole('button', { name: `Delete preset ${name}` })).toHaveCount(0);
    await expect(page.getByRole('button', { name: /^Roll back to version / })).toHaveCount(0);
  });
});
