/**
 * The Storage feature over the REAL stack: the studio_stack loads
 * `tai42-storage-local`, so the provider card names its class and the resource
 * browser round-trips one object through the door — upload (text), the row, the
 * stat dialog, a real browser download whose bytes match, and a confirmed delete.
 */
import { readFileSync } from 'node:fs';
import { expect, test } from '@playwright/test';
import { seedCredential, uniq } from './helpers';

test('storage: provider card, upload, stat, download, delete', async ({ page }) => {
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

  // The row appears in the resource table.
  const statButton = page.getByRole('button', { name: `Stat ${id}` });
  await expect(statButton).toBeVisible();

  // Stat opens and names the resource.
  await statButton.click();
  const statDialog = page.getByRole('dialog', { name: 'Resource stat' });
  await expect(statDialog).toBeVisible();
  await expect(statDialog.getByText(id, { exact: true })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(statDialog).toBeHidden();

  // Download yields a real browser download event whose bytes equal the upload.
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    page.getByRole('button', { name: `Download ${id}` }).click(),
  ]);
  const path = await download.path();
  expect(readFileSync(path).toString()).toBe(content);

  // Delete confirms and the row is gone.
  await page.getByRole('button', { name: `Delete ${id}` }).click();
  const confirm = page.getByRole('dialog', { name: 'Delete resource' });
  await confirm.getByRole('button', { name: 'Delete' }).click();
  await expect(page.getByRole('button', { name: `Stat ${id}` })).toHaveCount(0);
});
