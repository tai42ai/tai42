import { defineConfig, devices } from '@playwright/test';

import {
  API_KEY,
  IDP_PORT,
  LLM_PORT,
  MP_ADMIN_TOKEN,
  MP_PORT,
  MP_WEB_PORT,
  UI_PORT,
} from './tests/helpers';

/**
 * The live browser-e2e harness. The Studio is SERVED BY THE SKELETON (never
 * `vite preview`): the skeleton stamps the import map + CSP nonce into
 * index.html at serve time and serves plugin bundles under
 * `/api/plugins/{name}/studio/`, so only a real skeleton exercises the serving
 * seam. `webServer` runs the console script that boots the `studio_stack`
 * profile (multi-worker skeleton + backend + metrics, access control ON, the
 * built dist served) on a KNOWN port; Playwright polls it, then owns its
 * lifecycle.
 *
 * The stack boot needs loopback Redis + Postgres from `docker compose up -d`
 * (repo root) and the built Studio dist. The port/key/llm-port defaults here
 * MUST match `tai42_e2e.studio_runner.StudioRunnerSettings`, and are forwarded to
 * the runner so both sides agree on the origin.
 */
// The pinned ports/key/token are defined once in ./tests/helpers.ts (the specs
// read the same constants), so config, runner, and specs agree on one origin.
const baseURL = `http://127.0.0.1:${String(UI_PORT)}`;

export default defineConfig({
  testDir: './tests',
  // One shared live stack: serial, no auto-rerun (a flaky UI e2e is a real race
  // until proven otherwise — the mission's no-auto-rerun policy).
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: !!process.env.CI,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI ? [['github'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  // The specs run on chromium as the PR/default browser; firefox + webkit widen
  // the matrix on the nightly schedule only (the CI job gates them behind
  // `github.event_name == 'schedule'`). Every project shares the one webServer and
  // the tall viewport below.
  projects: [
    {
      name: 'chromium',
      // Override the desktop default's 720px height: the compose/create dialogs
      // grow once a base tool/agent is chosen (they render the tool's input hints),
      // and the modal does not scroll its own body, so a short viewport pushes the
      // submit button below the fold where it is unreachable. A tall viewport keeps
      // the whole dialog — including its footer buttons — on screen. The two
      // nightly browsers inherit the same tall viewport for the same reason.
      use: { ...devices['Desktop Chrome'], viewport: { width: 1280, height: 1600 } },
    },
    {
      name: 'firefox',
      use: { ...devices['Desktop Firefox'], viewport: { width: 1280, height: 1600 } },
    },
    {
      name: 'webkit',
      use: { ...devices['Desktop Safari'], viewport: { width: 1280, height: 1600 } },
    },
  ],
  webServer: {
    // Invoke the console script DIRECTLY from the project venv rather than through
    // `uv run`: `uv run` is a supervisor that exits as soon as it forwards the
    // shutdown signal, so Playwright then sees its tracked webServer process gone
    // and SIGKILLs the whole tree — cutting the runner's teardown short and
    // orphaning the stack (a leaked `tai serve` holding the fixed port, which a
    // reused-server rerun then serves in a degraded state). With the runner as
    // Playwright's direct child, the SIGTERM below reaches its own handler and its
    // grace window covers the full leak-checked teardown. `uv sync` (CI) / a local
    // `uv run` first materializes this venv.
    command: '.venv/bin/tai42-e2e-studio-stack',
    // The console script lives in the tai42-e2e Python project one level up.
    cwd: '..',
    url: baseURL,
    reuseExistingServer: !process.env.CI,
    // The runner spawns each stack process as its OWN session leader (for
    // leak-checked teardown), so they are NOT in the webServer's process group —
    // only the runner's own SIGTERM handler reaps them. Give it a real SIGTERM +
    // grace window so it can run that teardown before Playwright SIGKILLs it;
    // without this the stack orphans and a reused-server rerun serves a degraded
    // stack (e.g. a dead LLM stub).
    gracefulShutdown: { signal: 'SIGTERM', timeout: 30_000 },
    // The runner brings up a multi-process stack against Docker services; give
    // it room on a cold boot.
    timeout: 300_000,
    stdout: 'pipe',
    stderr: 'pipe',
    env: {
      TAI_E2E_UI_PORT: String(UI_PORT),
      TAI_E2E_UI_LLM_PORT: String(LLM_PORT),
      TAI_E2E_UI_IDP_PORT: String(IDP_PORT),
      TAI_E2E_UI_API_KEY: API_KEY,
      // The opt-in marketplace gate + its pinned coordinates. When unset the
      // runner boots nothing marketplace-flavored and the specs skip; the pnpm
      // site build (only under the gate) lands inside the timeout above.
      TAI_E2E_MARKETPLACE: process.env.TAI_E2E_MARKETPLACE ?? '0',
      TAI_E2E_UI_MP_PORT: String(MP_PORT),
      TAI_E2E_UI_MP_WEB_PORT: String(MP_WEB_PORT),
      TAI_E2E_UI_MP_ADMIN_TOKEN: MP_ADMIN_TOKEN,
    },
  },
});
