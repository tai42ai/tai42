/**
 * The cross-viewport sweep of the public tai-marketplace-web site (opt-in:
 * `TAI_E2E_MARKETPLACE=1`), built and served by the studio runner. The
 * marketplace-web.spec drives the browse arc; this walks each of the site's
 * surfaces — browse, a plugin detail, the categories index, the not-found page,
 * and a detail whose README carries a wide table — across the 320 → 1920 ladder
 * and asserts the one thing only a laid-out document reveals: it never scrolls
 * SIDEWAYS, and a wide README table is caught by its own scroll region rather
 * than the page.
 *
 * These specs are chromium-only: they live inside the `chromium` project and the
 * firefox/webkit projects `testIgnore` this file (a NEW project name would never
 * run — CI runs `--project=chromium` on PR and widens onto firefox+webkit when
 * the ui suite changed or the run is manual). Each
 * viewport is set BEFORE the first paint, so every measurement is of a page laid
 * out at that width rather than one restyled into it. `@axe-core/playwright`
 * anchors the a11y audit that follows; the smoke below proves browse — the
 * surface the redesign fully owns — carries no critical/serious violation.
 */
import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';
import { MP_WEB_URL } from './helpers';

// Publish-circular: the marketplace-web frontend (M34 PLAN_5) renders against the
// M34 marketplace backend's browse contract (kind facet / nullable updated_at /
// premium / docs_url), but the e2e installs the backend at the pre-M34
// _MARKETPLACE_PIN. Un-skipped post-release after the pin bump (see MISSION_END_TASKS).
// Every surface in this file is a tai-marketplace-web page, so this gates the whole file.
test.skip(
  true,
  'Publish-circular: the marketplace-web frontend (M34 PLAN_5) renders against the M34 ' +
    'marketplace backend browse contract (kind facet / nullable updated_at / premium / ' +
    'docs_url), but the e2e installs the backend at the pre-M34 _MARKETPLACE_PIN. ' +
    'Un-skipped post-release after the pin bump (see MISSION_END_TASKS).',
);

const ALPHA_REF = 'tai42/e2e-alpha';
/** Alpha's plugin-detail path (`/$namespace/$name`); `vite preview`'s SPA
 * fallback hands the client router any unmatched path. */
const ALPHA_DETAIL_PATH = '/tai42/e2e-alpha';

/** The document's horizontal overflow in CSS px (<= 0 means it does not scroll
 * sideways). `clientWidth` already excludes the vertical scrollbar. */
async function horizontalOverflow(page: Page): Promise<number> {
  return page.evaluate(() => {
    const root = document.documentElement;
    return root.scrollWidth - root.clientWidth;
  });
}

const LADDER = [320, 390, 640, 768, 1024, 1920] as const;

for (const width of LADDER) {
  test.describe(`the marketplace ladder at ${String(width)} px`, () => {
    // Every surface is a tai-marketplace-web page, served only under the opt-in gate.
    test.skip(process.env.TAI_E2E_MARKETPLACE !== '1', 'marketplace-web is opt-in (TAI_E2E_MARKETPLACE=1)');
    test.use({ viewport: { width, height: 900 } });

    test('browse never scrolls sideways', async ({ page }) => {
      await page.goto(MP_WEB_URL);
      await expect(page.getByText(ALPHA_REF).first()).toBeVisible();
      expect(
        await horizontalOverflow(page),
        `browse overflows horizontally at ${String(width)} px`,
      ).toBeLessThanOrEqual(0);
    });

    test('the plugin detail never scrolls sideways', async ({ page }) => {
      await page.goto(`${MP_WEB_URL}${ALPHA_DETAIL_PATH}`);
      // The install command confirms the detail header rendered.
      await expect(page.getByText(`tai plugins install ${ALPHA_REF}`)).toBeVisible();
      expect(
        await horizontalOverflow(page),
        `plugin detail overflows horizontally at ${String(width)} px`,
      ).toBeLessThanOrEqual(0);
    });

    test('a wide README table is contained by its own scroll region', async ({ page }) => {
      await page.goto(`${MP_WEB_URL}${ALPHA_DETAIL_PATH}`);
      // The Readme tab is the default; the ingested GFM renders a real table that
      // is wrapped in a `.mp-scroll-region` while it overflows.
      const region = page
        .locator('.mp-scroll-region')
        .filter({ has: page.getByRole('columnheader', { name: 'Option', exact: true }) });
      await expect(region.getByRole('table')).toBeVisible();

      // Containment proof: the table lives inside the region (asserted above) and the
      // document does not scroll sideways (asserted below) — so the region, not the
      // page, absorbs any overflow. If the region stopped containing it, the wide
      // table would push the document wide and this assertion would fail.
      expect(
        await horizontalOverflow(page),
        `the README table pushed the document sideways at ${String(width)} px`,
      ).toBeLessThanOrEqual(0);
    });

    test('the categories index never scrolls sideways', async ({ page }) => {
      await page.goto(`${MP_WEB_URL}/categories`);
      await expect(page.getByRole('heading', { level: 1, name: 'Categories' })).toBeVisible();
      expect(
        await horizontalOverflow(page),
        `categories overflows horizontally at ${String(width)} px`,
      ).toBeLessThanOrEqual(0);
    });

    test('the not-found page never scrolls sideways', async ({ page }) => {
      await page.goto(`${MP_WEB_URL}/this-route-does-not-exist`);
      await expect(page.getByRole('heading', { level: 1, name: 'Page not found' })).toBeVisible();
      expect(
        await horizontalOverflow(page),
        `not-found overflows horizontally at ${String(width)} px`,
      ).toBeLessThanOrEqual(0);
    });
  });
}

test.describe('marketplace accessibility smoke at 1280', () => {
  // browse is a tai-marketplace-web page, served only under the opt-in gate.
  test.skip(process.env.TAI_E2E_MARKETPLACE !== '1', 'marketplace-web is opt-in (TAI_E2E_MARKETPLACE=1)');
  test.use({ viewport: { width: 1280, height: 900 } });

  test('browse has no critical or serious axe violations', async ({ page }) => {
    await page.goto(MP_WEB_URL);
    await expect(page.getByText(ALPHA_REF).first()).toBeVisible();
    const results = await new AxeBuilder({ page }).analyze();
    const blocking = results.violations.filter(
      (v) => v.impact === 'critical' || v.impact === 'serious',
    );
    expect(blocking.map((v) => v.id)).toEqual([]);
  });
});
