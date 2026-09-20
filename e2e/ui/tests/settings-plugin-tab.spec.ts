/**
 * The Studio-plugin settings-tab + nav surface over the REAL stack. The
 * studio_stack serves the reference plugin, which registers a settings tab and a
 * sidebar nav entry. This asserts the tab mounts in Settings AFTER the core tabs
 * and renders its content, and that the plugin nav entry still renders — a
 * regression guard on the shell's host-state nav path.
 */
import { expect, test } from '@playwright/test';
import { loginViaUi } from './helpers';

test('the reference plugin contributes a Settings tab (after core tabs) and a nav entry', async ({
  page,
}) => {
  await loginViaUi(page); // lands on the Dashboard (observability) view; the shell starts the plugin load pass
  // The contributions commit at the same module-eval point that sets __pluginReact.
  await page.waitForFunction(() => '__pluginReact' in window);

  // The reference plugin declares no core section, so its nav entry renders under the
  // single generic "Plugins" section (its list named by the "Plugins" header) within the
  // Primary nav — there is no separate "Plugins" navigation landmark. `exact` so the link
  // name never substrings the per-entry provenance badge ("Plugin: reference_plugin …").
  const pluginsList = page
    .getByRole('navigation', { name: 'Primary' })
    .getByRole('list', { name: 'Plugins' });
  await expect(
    pluginsList.getByRole('link', { name: 'Reference', exact: true }),
  ).toBeVisible();

  // The plugin settings tab appears in the Settings workbench, sorted after the
  // five core tabs, and renders its content once selected.
  await page.goto('/settings');
  const tablist = page.getByRole('tablist');
  const pluginTab = tablist.getByRole('tab', { name: 'Reference' });
  await expect(pluginTab).toBeVisible();
  expect(await tablist.getByRole('tab').allInnerTexts()).toEqual([
    'Settings',
    'Environment',
    'Profiles',
    'API keys',
    'Backup',
    'Roles',
    'Reference',
  ]);

  await pluginTab.click();
  await expect(page.getByTestId('reference-settings-tab')).toBeVisible();
});
