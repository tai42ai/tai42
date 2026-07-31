/**
 * The accessibility audit's AUTOMATED pass over the public tai-marketplace-web
 * site (opt-in: `TAI_E2E_MARKETPLACE=1`), built and served by the studio runner.
 * `viewport-sweep.spec.ts` proves browse alone carries no critical/serious axe
 * violation; this walks each of the site's surfaces — browse, a plugin detail
 * (whose default Readme tab is the prose surface, carrying the ingested GFM
 * table), the categories index, and the not-found page — in BOTH themes and holds
 * every one to the same bar. The site's only theme source is
 * `prefers-color-scheme`, so emulating the OS scheme is what pins the theme; a
 * redesign that restyles light and dark separately can regress one theme's
 * contrast or name/role/value while the other stays clean.
 *
 * axe is scoped to the WCAG 2.0/2.1 A + AA rule tags — the mission's conformance
 * target — so any violation returned is a genuine conformance failure. The
 * blocking assertion is zero critical/serious per surface/theme; the full
 * violation list is written to `test-results/a11y-mp-<theme>.json` as evidence.
 *
 * Chromium-only, like the sweep: this file sets its own concerns and the
 * firefox/webkit projects `testIgnore` it (a new project name would never run —
 * CI runs `--project=chromium` on PR and widens onto firefox+webkit when the ui
 * suite changed or the run is manual).
 */
import { writeFileSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';
import { MP_WEB_URL } from './helpers';

const WCAG_AA_TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'] as const;

const ALPHA_REF = 'tai42/e2e-alpha';
const ALPHA_DETAIL_PATH = '/tai42/e2e-alpha';

/** The four public surfaces and the settle signal that proves each one rendered
 * its real content (not a loading state) before axe reads the laid-out document. */
const SURFACES: { name: string; path: string; ready: (page: Page) => Promise<void> }[] = [
  {
    name: 'browse',
    path: '',
    ready: async (page) => {
      await expect(page.getByText(ALPHA_REF).first()).toBeVisible();
    },
  },
  {
    name: 'detail (prose/readme)',
    path: ALPHA_DETAIL_PATH,
    ready: async (page) => {
      await expect(page.getByText(`tai plugins install ${ALPHA_REF}`)).toBeVisible();
      // The default Readme tab renders the ingested GFM table — the prose surface.
      await expect(page.getByRole('columnheader', { name: 'Option', exact: true })).toBeVisible();
    },
  },
  {
    name: 'categories',
    path: '/categories',
    ready: async (page) => {
      await expect(page.getByRole('heading', { level: 1, name: 'Categories' })).toBeVisible();
    },
  },
  {
    name: 'not-found',
    path: '/this-route-does-not-exist',
    ready: async (page) => {
      await expect(page.getByRole('heading', { level: 1, name: 'Page not found' })).toBeVisible();
    },
  },
];

interface SurfaceReport {
  surface: string;
  violations: {
    id: string;
    impact: string | null | undefined;
    help: string;
    nodes: number;
    targets: string[];
    data: unknown[];
  }[];
}

for (const scheme of ['light', 'dark'] as const) {
  test.describe(`marketplace axe A+AA sweep (${scheme})`, () => {
    // The surfaces are all tai-marketplace-web pages, served only under the opt-in gate.
    test.skip(process.env.TAI_E2E_MARKETPLACE !== '1', 'marketplace-web is opt-in (TAI_E2E_MARKETPLACE=1)');
    test.use({ viewport: { width: 1280, height: 900 }, colorScheme: scheme });

    test('every public surface carries no critical or serious violation', async ({ page }) => {
      // Disable animations for the scan: axe reads the painted document at one
      // instant, and a control caught mid-transition samples an intermediate
      // colour pair neither stable state rests at (which WCAG does not evaluate).
      // Reduced-motion snaps every control to its final colour; the theme pin is
      // preserved.
      await page.emulateMedia({ colorScheme: scheme, reducedMotion: 'reduce' });
      const report: SurfaceReport[] = [];
      const blocking: string[] = [];

      for (const surface of SURFACES) {
        await page.goto(`${MP_WEB_URL}${surface.path}`);
        await surface.ready(page);
        await page.evaluate(() => document.fonts.ready);

        const results = await new AxeBuilder({ page }).withTags([...WCAG_AA_TAGS]).analyze();
        report.push({
          surface: surface.name,
          violations: results.violations.map((v) => ({
            id: v.id,
            impact: v.impact,
            help: v.help,
            nodes: v.nodes.length,
            targets: v.nodes.flatMap((n) => n.target.map((t) => String(t))),
            data: v.nodes.flatMap((n) => n.any.map((a): unknown => a.data)),
          })),
        });
        for (const v of results.violations) {
          if (v.impact === 'critical' || v.impact === 'serious') {
            blocking.push(`${surface.name} [${scheme}]: ${v.id} (${v.impact}) — ${v.help}`);
          }
        }
      }

      const evidenceDir = join(test.info().project.outputDir, 'a11y-evidence');
      mkdirSync(evidenceDir, { recursive: true });
      writeFileSync(join(evidenceDir, `a11y-mp-${scheme}.json`), JSON.stringify(report, null, 2));
      expect(blocking, blocking.join('\n')).toEqual([]);
    });
  });
}
