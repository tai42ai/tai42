/**
 * The public marketplace web site (opt-in: `TAI_E2E_MARKETPLACE=1`), built
 * and served by the studio runner with its `/api` proxy pointed at the same
 * harness-run registry the Studio talks to. Read-only browse surfaces only — the
 * site has no login and no install: browse, facet filtering, a detail page with
 * items + versions, and the advisory lifecycle rendered through the site (an
 * admin-created advisory appears, and a withdrawn one renders struck-through with
 * a `withdrawn` badge). The advisory summary is uniq'd and the withdrawn
 * rendering is asserted, so this stays order-independent from the Studio spec.
 */
import { expect, test, type Page } from '@playwright/test';
import { MP_URL, MP_WEB_URL, mpAdminHeaders, uniq } from './helpers';

// Publish-circular: the marketplace-web frontend renders against the published
// marketplace backend's browse contract (kind facet / nullable updated_at /
// premium / docs_url), but the e2e installs the backend at the older
// `_MARKETPLACE_PIN`. Un-skipped once the pin is bumped to that published backend.
test.skip(
  true,
  'Publish-circular: the marketplace-web frontend renders against the published ' +
    'marketplace backend browse contract (kind facet / nullable updated_at / premium / ' +
    'docs_url), but the e2e installs the backend at the older `_MARKETPLACE_PIN`. ' +
    'Un-skipped once the pin is bumped to that published backend.',
);

test.skip(
  process.env.TAI_E2E_MARKETPLACE !== '1',
  'marketplace area is opt-in (TAI_E2E_MARKETPLACE=1)',
);

const ALPHA_REF = 'tai42/e2e-alpha';
const BETA_REF = 'tai42/e2e-beta';
const GAMMA_REF = 'tai42/e2e-gamma';
// The router/middleware fixture the studio runner seeds into this browse catalog —
// the only listing whose kinds are neither `tool` nor `extension`.
const EPSILON_REF = 'tai42/e2e-epsilon';

// The result card's link is named by the item name now, not the ref — the ref
// renders beside it as non-interactive `<p class="mp-ref">` text. Alpha provides
// exactly one item, so this uniquely identifies alpha's card.
const ALPHA_ITEM = 'e2e_market_probe';

/** Reload until the assertion body passes — the detail advisories query reads a
 * fresh fetch on load, so the registry's just-changed state converges here. */
async function reloadUntil(page: Page, assertion: () => Promise<void>): Promise<void> {
  await expect(async () => {
    await page.reload();
    await assertion();
  }).toPass({ timeout: 20_000 });
}

test('browse, facet, and the detail readme/items/versions + advisory-withdrawn arc', async ({ page }) => {
  // 1. Browse renders the three seeded listings. The ref is non-interactive text
  // beside each card title, so assert on the ref text rather than a link name.
  await page.goto(MP_WEB_URL);
  await expect(page.getByText(ALPHA_REF).first()).toBeVisible();
  await expect(page.getByText(BETA_REF).first()).toBeVisible();
  await expect(page.getByText(GAMMA_REF).first()).toBeVisible();

  // 2a. Kind facet: `extension` leaves only beta's extension item; alpha drops out.
  await page.getByRole('button', { name: 'extension', exact: true }).click();
  await expect(page.getByText(BETA_REF).first()).toBeVisible();
  await expect(page.getByText(ALPHA_REF)).toHaveCount(0);
  await page.getByRole('button', { name: 'extension', exact: true }).click();
  await expect(page.getByText(ALPHA_REF).first()).toBeVisible();

  // 2b. Category chip: alpha's category (`utilities`) returns alpha's rows only.
  await page.getByRole('button', { name: 'utilities', exact: true }).click();
  await expect(page.getByText(ALPHA_REF).first()).toBeVisible();
  await expect(page.getByText(BETA_REF)).toHaveCount(0);
  await page.getByRole('button', { name: 'utilities', exact: true }).click();

  // 3. Open alpha's detail by its card link (named by the item name now). The
  // install command confirms the detail header rendered.
  await page.getByRole('link', { name: ALPHA_ITEM, exact: true }).first().click();
  await expect(page.getByText(`tai plugins install ${ALPHA_REF}`)).toBeVisible();

  // 3a. Readme tab is the default: the ingested README renders as a real HTML
  // table (the registry rendered the fixture's GFM description). Select by
  // element/role and text only — no class or id is emitted on these tags.
  const readmeTable = page
    .getByRole('table')
    .filter({ has: page.getByRole('columnheader', { name: 'Option', exact: true }) });
  await expect(readmeTable).toBeVisible();
  await expect(readmeTable.getByRole('columnheader', { name: 'Type', exact: true })).toBeVisible();
  await expect(readmeTable.getByRole('columnheader', { name: 'Default', exact: true })).toBeVisible();
  await expect(readmeTable.getByRole('cell', { name: 'retries', exact: true })).toBeVisible();

  // 3b. Items tab: alpha's provided item lives behind the Items tab, hidden until
  // selected.
  await page.getByRole('tab', { name: 'Items' }).click();
  await expect(page.getByText(ALPHA_ITEM)).toBeVisible();

  // 3c. Versions tab: both published versions and a `published` status badge live
  // behind the Versions tab. The status is a table cell (the column header is a
  // columnheader, not a cell), so match the badge as a cell.
  await page.getByRole('tab', { name: 'Versions' }).click();
  await expect(page.getByRole('cell', { name: '0.1.0' })).toBeVisible();
  await expect(page.getByRole('cell', { name: '0.2.0' })).toBeVisible();
  await expect(page.getByRole('cell', { name: 'published' }).first()).toBeVisible();

  // Advisories: create one via the registry admin API, then open the Advisories
  // tab. Clicking it writes `?tab=advisories` to the URL, which a reload preserves
  // — so the reload loop below keeps the panel open.
  const summary = uniq('advisory');
  const created = await page.request.post(`${MP_URL}/api/v1/admin/advisories`, {
    headers: mpAdminHeaders(),
    data: {
      listing: ALPHA_REF,
      affected_versions: '<=0.1.0',
      severity: 'high',
      summary,
    },
  });
  expect(created.status(), await created.text()).toBe(201);
  const advisoryId = ((await created.json()) as { data: { id: number } }).data.id;

  // The Advisories tab's accessible name has several forms (`Advisories`,
  // `Advisories, 1`, `Advisories, loading`), so match it by prefix.
  await page.getByRole('tab', { name: /^Advisories/ }).click();

  // Scope every advisory assertion to THIS test's card (its body is the parent of
  // the uniq summary line, and it carries the `withdrawn` badge), so a reused
  // registry already holding a withdrawn advisory on alpha can neither satisfy the
  // pre-check nor trip a strict-mode multi-match on the post-check.
  const advisoryCard = page.getByText(summary).locator('..');

  await reloadUntil(page, async () => {
    await expect(advisoryCard).toBeVisible({ timeout: 2_000 });
    await expect(advisoryCard.getByText('withdrawn')).toHaveCount(0);
  });

  // Withdraw it — the card stays (it is information), now struck-through with a
  // `withdrawn` badge rather than an active advisory.
  const withdrawn = await page.request.post(
    `${MP_URL}/api/v1/admin/advisories/${String(advisoryId)}/withdraw`,
    { headers: mpAdminHeaders() },
  );
  expect(withdrawn.ok(), await withdrawn.text()).toBeTruthy();
  await reloadUntil(page, async () => {
    await expect(advisoryCard).toBeVisible({ timeout: 2_000 });
    await expect(advisoryCard.getByText('withdrawn')).toBeVisible({ timeout: 2_000 });
  });
});

test('the Kind facet is server-driven and a router/middleware listing browses + filters', async ({ page }) => {
  // The served item-kind vocabulary comes verbatim from the contract enum
  // (`PluginItemKind`), so `GET /api/v1/kinds` is the source of truth for the facet
  // — a new contract kind flows through with zero web changes. The envelope is
  // `{"data":{"kinds":[...]}}`. Never assert against a hardcoded kind list here.
  const served = await page.request.get(`${MP_URL}/api/v1/kinds`);
  expect(served.ok(), await served.text()).toBeTruthy();
  const kinds = ((await served.json()) as { data: { kinds: string[] } }).data.kinds;
  // The fixture's router/middleware items only reach the facet if the contract still
  // serves those kinds; guard the premise rather than hardcode the vocabulary.
  expect(kinds).toContain('router');
  expect(kinds).toContain('middleware');

  await page.goto(MP_WEB_URL);

  // The router/middleware listing is in the browse catalog and renders. Epsilon
  // provides two items (a router and a middleware), so its ref appears on more than
  // one card — assert on the first.
  await expect(page.getByText(EPSILON_REF).first()).toBeVisible();

  // The Kind facet's rows ARE the served vocabulary: the group renders one button
  // per served kind in the contract's declaration order, so asserting the exact,
  // ordered button text against `GET /api/v1/kinds` is the server-driven
  // contract (count and order both, never a literal list). Scope to the Kind group
  // — `storage`/`monitoring` are also Category rows, so the page holds those names
  // twice.
  const kindGroup = page
    .getByRole('group')
    .filter({ has: page.getByRole('heading', { name: 'Kind', exact: true }) });
  await expect(kindGroup.getByRole('button')).toHaveText(kinds);

  // Filtering by a served item-kind narrows to the listing that provides it: the
  // `router` row keeps epsilon and drops every tool/extension listing.
  await page.getByRole('button', { name: 'router', exact: true }).click();
  await expect(page.getByText(EPSILON_REF).first()).toBeVisible();
  await expect(page.getByText(ALPHA_REF)).toHaveCount(0);
  await expect(page.getByText(BETA_REF)).toHaveCount(0);
  await expect(page.getByText(GAMMA_REF)).toHaveCount(0);
});
