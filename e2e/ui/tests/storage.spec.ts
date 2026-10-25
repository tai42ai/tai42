/**
 * The Storage feature over the REAL stack: the studio_stack loads
 * `tai42-storage-local`, so the provider card names its class and the resource
 * browser round-trips objects through the door. The list selects and the detail acts:
 * a file row opens the object's detail (`?id=`), which shows its kind and content, and
 * holds Download (a real browser download whose bytes match) and Delete (confirmed, the
 * row gone and the selection cleared). A binary object opens with no text preview and
 * keeps both actions.
 */
import { readFileSync } from 'node:fs';
import { expect, type Locator, type Page } from '@playwright/test';
import { seedCredential, uniq } from './helpers';
import { needs, test } from './needs';

needs('kind:storage:local');

/** Open an object's detail from its list row; the detail is a region named by its id. */
async function openDetail(page: Page, id: string): Promise<Locator> {
  await page.getByRole('link', { name: `Open ${id}`, exact: true }).click();
  await expect.poll(() => new URL(page.url()).searchParams.get('id')).toBe(id);
  const detail = page.getByRole('region', { name: id, exact: true });
  await expect(detail.getByRole('heading', { level: 2, name: id, exact: true })).toBeVisible();
  return detail;
}

/** Delete the open object from its detail; the row leaves the list and `?id=` clears. */
async function deleteFromDetail(page: Page, detail: Locator, id: string): Promise<void> {
  await detail.getByRole('button', { name: /^Delete/ }).click();
  const confirm = page.getByRole('dialog', { name: 'Delete resource' });
  await confirm.getByRole('button', { name: 'Delete' }).click();
  await expect(confirm).toBeHidden();
  await expect(page.getByRole('link', { name: `Open ${id}`, exact: true })).toHaveCount(0);
  await expect.poll(() => new URL(page.url()).searchParams.get('id')).toBeNull();
}

test('storage: provider card, upload, text detail, download, delete', async ({ page }) => {
  const id = `${uniq('note')}.txt`;
  const content = `hello ${uniq('body')}`;

  await seedCredential(page);
  await page.goto('/storage');

  // The Storage nav item renders in the primary nav.
  await expect(
    page.getByRole('navigation', { name: 'Primary' }).getByRole('link', { name: 'Storage' }),
  ).toBeVisible();

  // The provider card names tai42-storage-local's provider class.
  await expect(page.getByTestId('storage-page')).toBeVisible();
  await expect(page.getByText('LocalStorage')).toBeVisible();

  // Upload one text object through the dialog (text mode is the default).
  await page.getByRole('button', { name: 'Upload' }).click();
  const dialog = page.getByRole('dialog', { name: 'Upload resource' });
  await dialog.getByRole('textbox', { name: 'Resource id' }).fill(id);
  await dialog.getByRole('textbox', { name: 'Text content' }).fill(content);
  await dialog.getByRole('button', { name: 'Upload' }).click();
  await expect(dialog).toBeHidden();

  // The text detail names the kind and shows the content.
  const detail = await openDetail(page, id);
  await expect(detail.getByText('Text', { exact: true })).toBeVisible();
  await expect(detail.getByRole('heading', { name: 'Content', exact: true })).toBeVisible();
  await expect(detail.locator('pre code')).toHaveText(content);

  // Download from the detail yields a real browser download whose bytes equal the upload.
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    detail.getByRole('button', { name: /^Download/ }).click(),
  ]);
  const path = await download.path();
  expect(readFileSync(path).toString()).toBe(content);

  await deleteFromDetail(page, detail, id);
});

test('storage: a PNG uploaded through Storage opens with no text preview and keeps its actions', async ({
  page,
}) => {
  const id = `${uniq('pixel')}.png`;
  // The PNG signature and an IHDR chunk header: bytes that are not UTF-8 text.
  const png = Buffer.from([
    0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x00, 0x00, 0x0d, 0x49, 0x48, 0x44, 0x52,
  ]);

  await seedCredential(page);
  await page.goto('/storage');

  // Upload the binary object in files mode; a wholly successful batch closes the dialog.
  await page.getByRole('button', { name: 'Upload' }).click();
  const dialog = page.getByRole('dialog', { name: 'Upload resource' });
  await dialog.getByRole('radio', { name: 'Files' }).check();
  await dialog.getByLabel('Choose files').setInputFiles([{ name: id, mimeType: 'image/png', buffer: png }]);
  await dialog.getByRole('button', { name: 'Upload' }).click();
  await expect(dialog).toBeHidden();

  // The binary detail names the kind and draws no content preview.
  const detail = await openDetail(page, id);
  await expect(detail.getByText('Binary', { exact: true })).toBeVisible();
  await expect(detail.getByText('Binary object: no text preview.')).toBeVisible();
  await expect(detail.locator('pre code')).toHaveCount(0);

  // Download still fetches the stored bytes unchanged.
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    detail.getByRole('button', { name: /^Download/ }).click(),
  ]);
  expect(readFileSync(await download.path()).equals(png)).toBe(true);

  await deleteFromDetail(page, detail, id);
});
