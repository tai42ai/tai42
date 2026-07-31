/**
 * The Studio TOOLS screen tool_meta surface (PLAN_3), driven over the real
 * multi-worker stack and DOUBLE-ASSERTED through the same-origin tool-meta API the
 * app itself uses. The screen merges each tool's native tags (`/api/tools/tags`)
 * with its overlay row (`/api/tool-meta`): a display name over the real name, merged
 * tag grouping/filtering, a folder explorer (breadcrumb + subfolders), and the full
 * four-field edit dialog. An effectively hidden tool is excluded outright — unhiding
 * is a CLI/API operation (overlay `hidden:false`), never a screen toggle.
 *
 * Every fixture is SEEDED through the API and, where the UI mutates state (the edit
 * dialog), the effect is read back through the API — so a green assertion proves the
 * served bundle drove the real overlay store, not a client-only illusion. The overlay
 * is DB-backed and this stack is shared/serial, so each test cleans up the rows and
 * folders it created.
 */
import { expect, test, type APIRequestContext, type Locator, type Page } from '@playwright/test';
import { apiHeaders, seedCredential, uniq } from './helpers';

/** The tools whose overlay a test may touch; reset before and after so a shared,
 *  serial stack never leaks organizational state between specs. */
const TOUCHED_TOOLS = ['e2e_echo', 'generate_uuid'];

/** A tool renders ONCE PER merged-tag group it belongs to (e2e_echo carries the
 *  native `e2e` tag plus any overlay tag, so it appears in several groups), and the
 *  bare-name link is a substring match that also catches longer tool names — so every
 *  tool-scoped assertion is made INSIDE one named group: the `TagGroup` <section>
 *  whose heading reads "<tag> <count>". Pair with `exact: true` on the link/button. */
function tagGroup(page: Page, tag: string): Locator {
  return page
    .locator('section')
    .filter({ has: page.getByRole('heading', { name: new RegExp(`^${tag} \\d`) }) });
}

async function resetOverlay(request: APIRequestContext): Promise<void> {
  for (const name of TOUCHED_TOOLS) {
    await request.delete(`/api/tool-meta/tools/${name}`, { headers: apiHeaders() });
  }
  // Drop every folder — leaves are safe to delete, and the tree is shallow here.
  const meta = (await (await request.get('/api/tool-meta', { headers: apiHeaders() })).json()) as {
    data: { folders: Array<{ id: string; parent_id: string | null }> };
  };
  // Delete deepest-first so a parent is never blocked by a child: a >=2-level
  // nested folder must lose its leaf before its parent, or the parent delete 409s.
  // A root-vs-nonroot split only orders one level, so walk each folder's parent
  // chain for its true depth and sort by that.
  const parentOf = new Map(meta.data.folders.map((f) => [f.id, f.parent_id]));
  const depthOf = (folder: { id: string; parent_id: string | null }): number => {
    let depth = 0;
    let cursor = folder.parent_id;
    // Bounded by the folder count — a cycle can never be created (the API rejects
    // it), so the chain always terminates at a root.
    while (cursor !== null) {
      depth += 1;
      cursor = parentOf.get(cursor) ?? null;
    }
    return depth;
  };
  const byDepth = [...meta.data.folders].sort((a, b) => depthOf(b) - depthOf(a));
  for (const folder of byDepth) {
    await request.delete(`/api/tool-meta/folders/${folder.id}`, { headers: apiHeaders() });
  }
}

async function upsert(request: APIRequestContext, tool: string, patch: Record<string, unknown>): Promise<void> {
  const res = await request.patch(`/api/tool-meta/tools/${tool}`, { headers: apiHeaders(), data: patch });
  expect(res.status(), await res.text()).toBe(200);
}

async function createFolder(request: APIRequestContext, name: string): Promise<string> {
  const res = await request.post('/api/tool-meta/folders', { headers: apiHeaders(), data: { name } });
  expect(res.status(), await res.text()).toBe(200);
  return ((await res.json()) as { data: { id: string } }).data.id;
}

test.beforeEach(async ({ request }) => {
  await resetOverlay(request);
});
test.afterEach(async ({ request }) => {
  await resetOverlay(request);
});

test('display name + merged native/overlay tags render; edit dialog round-trips through the API', async ({
  page,
  request,
}) => {
  const displayName = uniq('Echo');
  const overlayTag = uniq('team');
  await upsert(request, 'e2e_echo', { display_name: displayName, tags: [overlayTag] });

  await seedCredential(page);
  await page.goto('/tools');

  // The tool renders its display name; its real name stays visible for identity.
  // Scope to the overlay tag's group (only e2e_echo carries this unique tag) so the
  // per-group duplication and substring name matches never trip strict mode.
  const group = tagGroup(page, overlayTag);
  const link = group.getByRole('link', { name: `Open tool e2e_echo`, exact: true });
  await expect(link).toBeVisible();
  await expect(link).toContainText(displayName);
  await expect(link).toContainText('e2e_echo');

  // MERGED tags: e2e_echo is grouped under BOTH its native `e2e` tag and its overlay
  // tag — each tag produces a section that lists the tool, proving the overlay tag
  // merged onto the native set. (The filter-chip row caps how many tags it shows,
  // collapsing the rest into a STATIC "+N more"; on a many-tagged stack neither tag is
  // among the first chips, so the merge is asserted through the groups the tags
  // produce — the real per-tag rendering — not the capped chip row.)
  await expect(
    tagGroup(page, 'e2e').getByRole('link', { name: `Open tool e2e_echo`, exact: true }),
  ).toBeVisible();
  await expect(group.getByRole('link', { name: `Open tool e2e_echo`, exact: true })).toBeVisible();

  // Filtering by the overlay tag keeps the tool and reflects the pressed state. The tag
  // is reached through the shell-owned `?tags=` param (a deep-link the app owns): a
  // selected tag is always surfaced as a pressed chip regardless of the visible-chip
  // cap, and its group still lists the tool.
  await page.goto(`/tools?tags=${overlayTag}`);
  const overlayChip = page
    .getByRole('group', { name: 'Filter tools by tag' })
    .getByRole('button', { name: new RegExp(`^${overlayTag} \\(`) });
  await expect(overlayChip).toHaveAttribute('aria-pressed', 'true');
  await expect(group.getByRole('link', { name: `Open tool e2e_echo`, exact: true })).toBeVisible();

  // Edit dialog round-trip: change the display name through the UI, read it back via API.
  await group.getByRole('button', { name: `Edit tool e2e_echo`, exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Edit e2e_echo' });
  await expect(dialog).toBeVisible();
  const renamed = uniq('Renamed');
  const nameField = dialog.getByLabel('Display name');
  await nameField.fill(renamed);
  await dialog.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(dialog).toBeHidden();

  await expect
    .poll(async () => {
      const meta = (await (await request.get('/api/tool-meta', { headers: apiHeaders() })).json()) as {
        data: { meta: Array<{ tool_name: string; display_name: string | null }> };
      };
      return meta.data.meta.find((row) => row.tool_name === 'e2e_echo')?.display_name ?? null;
    })
    .toBe(renamed);
});

test('folder explorer: a filed tool lives inside its folder, reached via the breadcrumb', async ({
  page,
  request,
}) => {
  const folderName = uniq('Probes');
  const folderId = await createFolder(request, folderName);
  await upsert(request, 'e2e_echo', { folder_id: folderId });

  await seedCredential(page);
  await page.goto('/tools');

  // At the root the tool is NOT listed; its folder is offered as a subfolder to open.
  // `exact` so a longer-named tool at the root never substring-matches e2e_echo.
  await expect(page.getByRole('link', { name: `Open tool e2e_echo`, exact: true })).toBeHidden();
  const folderButton = page.getByRole('button', { name: folderName });
  await expect(folderButton).toBeVisible();

  // Opening the folder navigates inward; the breadcrumb shows the path and the tool
  // appears. Inside the folder e2e_echo carries its native `e2e` tag, so it renders in
  // that group — scope the assertion there.
  await folderButton.click();
  const breadcrumb = page.getByRole('navigation', { name: 'Folder path' });
  await expect(breadcrumb.getByRole('button', { name: folderName })).toHaveAttribute('aria-current', 'page');
  await expect(
    tagGroup(page, 'e2e').getByRole('link', { name: `Open tool e2e_echo`, exact: true }),
  ).toBeVisible();

  // The root crumb navigates back out.
  await breadcrumb.getByRole('button', { name: 'All tools' }).click();
  await expect(page.getByRole('link', { name: `Open tool e2e_echo`, exact: true })).toBeHidden();
});

test('a hidden tool is excluded from the list; unhiding via the API reveals it', async ({
  page,
  request,
}) => {
  await upsert(request, 'generate_uuid', { hidden: true });

  await seedCredential(page);
  await page.goto('/tools');

  // Hidden: absent from the list, with no screen affordance to reveal it.
  await expect(page.getByRole('link', { name: `Open tool generate_uuid` })).toBeHidden();

  // Unhide is a CLI/API operation (`tai tool-meta … --visibility shown`, i.e. overlay
  // `hidden:false`); once shown the tool reappears on the screen.
  await upsert(request, 'generate_uuid', { hidden: false });
  await page.reload();
  await expect(page.getByRole('link', { name: `Open tool generate_uuid` })).toBeVisible();
});
