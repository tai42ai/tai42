/**
 * The dark-pages browser smoke. The route-coverage guard
 * (`tests/routing/test_default_router_coverage.py`) already proves every default
 * router answers non-404 at the API level; this is the UI-level companion. The
 * studio stack now serves the REAL default surface (`build_studio_stack` runs
 * `default_routers="all"`), so each Studio nav page is reachable — this walks the
 * grouped nav, opens every feature page FROM the nav, and asserts it RENDERS:
 * its own page heading is present AND no loud failure surface is shown.
 *
 * "Renders" = the real page heading is visible and NEITHER loud failure surface is
 * present: the app/route error boundary and the reused inline `ErrorState` both
 * render the string "Something went wrong", and the capability boundary's sealed
 * panel renders the "isn't available for your session" copy. Asserting all three
 * absent, plus the page's own `<h1>`, is the browser-level proof that every Studio
 * nav page comes up cleanly under the default-mounted router set. Dashboard
 * (observability) is the page this studio stack did not serve before it moved to
 * `default_routers="all"`; the others were already served here and are covered as
 * full-default-surface regression. A curated manifest that omits a router is what
 * leaves the corresponding page dark — the failure class this guards.
 */
import { expect, test, type Page } from '@playwright/test';
import { seedCredential } from './helpers';

/**
 * Every primary-nav page: its nav link's accessible name (the label rendered in
 * the sidebar) paired with the page's own `<h1>` text. The two differ for the
 * Dashboard row (nav "Dashboard" → observability page `<h1>` "Dashboard") and the
 * extensions page (nav "Extensions" → `<h1>` "Tool extensions"). Ordered as the
 * grouped nav renders: the Dashboard lead row, then Capabilities, Integrations,
 * Activity, Administration.
 */
const NAV_PAGES: ReadonlyArray<{ readonly navLabel: string; readonly heading: string }> = [
  // Dashboard lead row.
  { navLabel: 'Dashboard', heading: 'Dashboard' },
  // Capabilities.
  { navLabel: 'Tools', heading: 'Tools' },
  { navLabel: 'Agents', heading: 'Agents' },
  { navLabel: 'Presets', heading: 'Presets' },
  { navLabel: 'Extensions', heading: 'Tool extensions' },
  { navLabel: 'Templates', heading: 'Templates' },
  // Integrations.
  { navLabel: 'Connectors', heading: 'Connectors' },
  { navLabel: 'Hooks', heading: 'Hooks' },
  { navLabel: 'Storage', heading: 'Storage' },
  // Activity.
  { navLabel: 'Scheduling', heading: 'Scheduling' },
  { navLabel: 'Interactions', heading: 'Interactions' },
  { navLabel: 'Notifications', heading: 'Notifications' },
  // Administration.
  { navLabel: 'Manifest', heading: 'Manifest' },
  { navLabel: 'Settings', heading: 'Settings' },
  { navLabel: 'System', heading: 'System' },
];

/** The grouped nav's four section headers, in render order under the Dashboard row. */
const NAV_SECTIONS = ['Capabilities', 'Integrations', 'Activity', 'Administration'] as const;

/**
 * Assert the routed feature page came up cleanly: its own heading is visible and
 * neither loud failure surface (the error boundary / inline ErrorState "Something
 * went wrong", or the capability boundary's sealed panel) is on the page.
 */
async function assertPageRenders(page: Page, heading: string): Promise<void> {
  await expect(page.getByRole('heading', { name: heading, level: 1 })).toBeVisible();
  await expect(page.getByText('Something went wrong')).toHaveCount(0);
  await expect(page.getByText("isn't available for your session")).toHaveCount(0);
}

test('the grouped primary nav renders its Dashboard lead row and four sections', async ({
  page,
}) => {
  await seedCredential(page);
  await page.goto('/tools');

  const nav = page.getByRole('navigation', { name: 'Primary' });
  await expect(nav).toBeVisible();

  // The standalone Dashboard lead row is a link above the labelled sections.
  await expect(nav.getByRole('link', { name: 'Dashboard', exact: true })).toBeVisible();

  // Each section header renders (as the uppercase muted group label).
  for (const section of NAV_SECTIONS) {
    await expect(nav.getByText(section, { exact: true })).toBeVisible();
  }

  // Every feature nav link is present under the primary nav.
  for (const { navLabel } of NAV_PAGES) {
    await expect(nav.getByRole('link', { name: navLabel, exact: true })).toBeVisible();
  }
});

for (const { navLabel, heading } of NAV_PAGES) {
  test(`the ${navLabel} page opens from the nav and renders`, async ({ page }) => {
    await seedCredential(page);
    await page.goto('/tools');

    // Reach the page through the primary nav (proves the grouped-nav wiring), then
    // assert the routed page rendered cleanly.
    await page
      .getByRole('navigation', { name: 'Primary' })
      .getByRole('link', { name: navLabel, exact: true })
      .click();
    await assertPageRenders(page, heading);
  });
}
