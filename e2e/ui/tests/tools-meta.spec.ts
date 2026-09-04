/**
 * The Studio TOOLS screen tool_meta surface, driven over the real
 * multi-worker stack and DOUBLE-ASSERTED through the same-origin tool-meta API the
 * app itself uses. The screen merges each tool's native tags (`/api/tools/tags`)
 * with its overlay row (`/api/tool-meta`): a display name over the real name, a flat
 * file-explorer list (breadcrumb + subfolders) with a tag-chip OR filter, and the full
 * four-field edit dialog. An effectively hidden tool is excluded outright — unhiding
 * is a CLI/API operation (overlay `hidden:false`), never a screen toggle.
 *
 * The list paginates client-side (24 entries per page by default) over a catalog larger
 * than one page, and the shell-owned `?tags=` deep link filters the whole current
 * directory BEFORE the page slice — so every navigation here scopes the list with it,
 * keeping each asserted tool inside a single page.
 *
 * Every fixture is SEEDED through the API and, where the UI mutates state (the edit
 * dialog), the effect is read back through the API — so a green assertion proves the
 * served bundle drove the real overlay store, not a client-only illusion. The overlay
 * is DB-backed and this stack is shared/serial, so each test cleans up the rows and
 * folders it created.
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import { apiHeaders, seedCredential, uniq } from './helpers';

/** The tools whose overlay a test may touch; reset before and after so a shared,
 *  serial stack never leaks organizational state between specs. */
const TOUCHED_TOOLS = ['e2e_echo', 'generate_uuid'];

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

/** Navigate to a scoped tools URL and wait for GET responses on `/api/tools/tags` and
 *  `/api/tool-meta`. While those reads are pending every tool lacks tags and overlay
 *  state, so an absence assertion could hold for the wrong reason — navigations that
 *  assert absence go through here. The waits resolve on the wire (the shell issues a
 *  second `/api/tool-meta` read, so that leg may resolve on either); the gap to the
 *  committed merge is covered by each call site's rendered anchor. */
async function gotoToolsSettled(page: Page, url: string): Promise<void> {
  const sideReads = ['/api/tools/tags', '/api/tool-meta'].map((path) =>
    page.waitForResponse(
      (response) =>
        response.url().includes(path) && response.request().method() === 'GET' && response.ok(),
    ),
  );
  await page.goto(url);
  await Promise.all(sideReads);
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
  // Few tools bear `e2e`, so the scoped list fits one page.
  await page.goto('/tools?tags=e2e');

  // The tool renders its display name; its real name stays visible for identity. The
  // flat explorer lists each tool once and the aria-label is exact, so no group scoping
  // is needed to disambiguate substring or per-group-duplicate matches.
  const link = page.getByRole('link', { name: `Open tool e2e_echo`, exact: true });
  await expect(link).toBeVisible();
  await expect(link).toContainText(displayName);
  await expect(link).toContainText('e2e_echo');

  // MERGED tags: e2e_echo carries BOTH its native `e2e` tag and its overlay tag, and the
  // tag-chip OR filter keeps the tool under either — so both are on its merged set. The
  // native leg is the filter already applied above; a selected tag is always surfaced as
  // a PRESSED chip regardless of the visible-chip cap (which collapses the rest into a
  // static "+N more"), so the pressed chip proves the filter UI reflects it.
  await expect(
    page
      .getByRole('group', { name: 'Filter tools by tag' })
      .getByRole('button', { name: /^e2e \(/ }),
  ).toHaveAttribute('aria-pressed', 'true');

  // The overlay leg — filtering by the overlay tag keeps the tool and its chip is pressed,
  // proving the overlay tag merged onto the native set.
  await page.goto(`/tools?tags=${overlayTag}`);
  const overlayChip = page
    .getByRole('group', { name: 'Filter tools by tag' })
    .getByRole('button', { name: new RegExp(`^${overlayTag} \\(`) });
  await expect(overlayChip).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('link', { name: `Open tool e2e_echo`, exact: true })).toBeVisible();

  // Edit dialog round-trip: change the display name through the UI, read it back via API.
  await page.getByRole('button', { name: `Edit tool e2e_echo`, exact: true }).click();
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
  // Folders are never tag-filtered, so the `e2e` filter narrows only the tool list: the
  // folder stays offered and the absence assertions run against a short, fully-merged
  // list.
  await gotoToolsSettled(page, '/tools?tags=e2e');

  // At the root the folder is offered as a subfolder to open; the filed tool is NOT
  // listed. `exact` so a longer-named tool at the root never substring-matches e2e_echo.
  const folderButton = page.getByRole('button', { name: folderName, exact: true });
  await expect(folderButton).toBeVisible();
  await expect(page.getByRole('link', { name: `Open tool e2e_echo`, exact: true })).toBeHidden();

  // Opening the folder navigates inward; the breadcrumb shows the path and the tool
  // appears in the folder's flat list.
  await folderButton.click();
  const breadcrumb = page.getByRole('navigation', { name: 'Folder path' });
  await expect(breadcrumb.getByRole('button', { name: folderName, exact: true })).toHaveAttribute('aria-current', 'page');
  await expect(
    page.getByRole('link', { name: `Open tool e2e_echo`, exact: true }),
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
  // The toolbox declares the native `uuid` tag on generate_uuid alone, so the list the
  // tool is asserted absent from — and then present in — is one page long.
  await gotoToolsSettled(page, '/tools?tags=uuid');

  // The filter row overflows into a static "+N more" only once the tags read has
  // COMMITTED — before that the vocabulary holds just the Untagged bucket and the
  // synthesized selected chip, and the same empty state renders for the wrong reason —
  // so the overflow anchors the absence assertions to the merged list.
  await expect(
    page.getByRole('group', { name: 'Filter tools by tag' }).getByText(/^\+\d+ more$/),
  ).toBeVisible();

  // Hidden: the merged scoped list is genuinely empty and the tool absent, with no
  // screen affordance to reveal it.
  await expect(page.getByText('No tools match')).toBeVisible();
  await expect(page.getByRole('link', { name: `Open tool generate_uuid` })).toBeHidden();

  // Unhide is a CLI/API operation (`tai tool-meta … --visibility shown`, i.e. overlay
  // `hidden:false`); once shown the tool reappears on the screen.
  await upsert(request, 'generate_uuid', { hidden: false });
  await page.reload();
  await expect(page.getByRole('link', { name: `Open tool generate_uuid` })).toBeVisible();
});
