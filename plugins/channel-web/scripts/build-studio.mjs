/**
 * Build this plugin's Studio bundle (the entry-gate route panel) and emit its
 * `studio-manifest.json`. Run as `pnpm run build:studio` (part of `pnpm run build`); CI runs
 * it before the freshness gate.
 *
 * Unlike the self-contained public page, this bundle runs INSIDE the Studio host: `react`,
 * `react-dom`, their subpath entries and `@tai42/studio-sdk` are EXTERNAL — exactly the
 * modules the host serves through its import map — so the panel binds the host's React and
 * design-system singletons. Everything else (`@tanstack/react-query`, `zod`) is bundled in.
 *
 * The panel styles itself with SDK components and `var(--tai-*)` tokens only, so the build
 * emits NO stylesheet; an emitted `.css` asset fails the build, since an unscoped stylesheet
 * injected into the host would leak into the shell.
 *
 * The manifest carries the plugin name (the Python package name), the plugin version (read
 * from pyproject so it can never drift from the Python package), the targeted
 * `STUDIO_PLUGIN_API_VERSION` (read from the installed SDK source), the contributions — the
 * one route panel `index.tsx` registers — the entry filename, and a sha384 integrity entry for
 * every emitted file. The skeleton re-hashes each served file at startup and rejects a
 * mismatch, so the hashes must be real.
 */
import { createHash } from 'node:crypto';
import { existsSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import react from '@vitejs/plugin-react';
import { build, esmExternalRequirePlugin } from 'vite';

const scriptDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(scriptDir, '..');
const srcDir = resolve(repoRoot, 'studio-src');
const entryFile = resolve(srcDir, 'index.tsx');
const outDir = resolve(repoRoot, 'src', 'tai42_channel_web', 'studio');
const sdkVersionFile = resolve(
  repoRoot,
  'node_modules',
  '@tai42',
  'studio-sdk',
  'src',
  'plugin',
  'version.ts',
);

/** MUST equal the Python package name — the skeleton registry rejects a mismatch at
 * startup, and the bundle URL is built from it. */
const PACKAGE_NAME = 'tai42_channel_web';

/** The shared modules the host provides through the import map. */
const EXTERNAL = [
  'react',
  'react-dom',
  'react/jsx-runtime',
  'react-dom/client',
  '@tai42/studio-sdk',
];

/** Read STUDIO_PLUGIN_API_VERSION from the installed SDK source, the single source of truth. */
function readApiVersion() {
  if (!existsSync(sdkVersionFile)) {
    throw new Error(
      `@tai42/studio-sdk is not installed: ${sdkVersionFile} is missing. Run ` +
        '`pnpm install --frozen-lockfile`.',
    );
  }
  const source = readFileSync(sdkVersionFile, 'utf8');
  const match = /export const STUDIO_PLUGIN_API_VERSION\s*=\s*(\d+)\s*;/.exec(source);
  if (match === null) {
    throw new Error(`could not read STUDIO_PLUGIN_API_VERSION from ${sdkVersionFile}`);
  }
  return Number.parseInt(match[1], 10);
}

/** Read the plugin's own version from its pyproject. */
function readPluginVersion() {
  const source = readFileSync(resolve(repoRoot, 'pyproject.toml'), 'utf8');
  const match = /^version\s*=\s*"([^"]+)"/m.exec(source);
  if (match === null) {
    throw new Error('could not read version from pyproject.toml');
  }
  return match[1];
}

function sha384(bytes) {
  return 'sha384-' + createHash('sha384').update(bytes).digest('base64');
}

async function main() {
  // Pin NODE_ENV so the committed dist does not depend on the build environment.
  process.env.NODE_ENV = 'production';
  await build({
    root: srcDir,
    configFile: false,
    logLevel: 'warn',
    publicDir: false,
    plugins: [react()],
    define: { 'process.env.NODE_ENV': JSON.stringify('production') },
    build: {
      outDir,
      emptyOutDir: true,
      target: 'es2022',
      minify: true,
      modulePreload: { polyfill: false },
      lib: { entry: entryFile, formats: ['es'] },
      rollupOptions: {
        // The host's shared runtimes stay external; a bundled CommonJS dependency's
        // `require()` of one is rewritten to an ESM import (the browser has no `require`).
        plugins: [esmExternalRequirePlugin({ external: EXTERNAL })],
        output: {
          entryFileNames: 'tai42-channel-web-[hash].js',
          chunkFileNames: 'tai42-channel-web-chunk-[hash].js',
          assetFileNames: 'tai42-channel-web-[hash][extname]',
        },
      },
    },
  });

  const emitted = readdirSync(outDir);
  const stylesheets = emitted.filter((name) => name.endsWith('.css'));
  if (stylesheets.length > 0) {
    throw new Error(`the studio bundle must emit no stylesheet, got: ${stylesheets.join(', ')}`);
  }
  const entries = emitted.filter((name) => name.endsWith('.js') && !name.includes('-chunk-'));
  if (entries.length !== 1) {
    throw new Error(
      `expected exactly one emitted JS entry, got ${entries.length}: ${emitted.join(', ')}`,
    );
  }
  const entry = entries[0];

  const integrity = {};
  for (const name of emitted) {
    if (name === 'studio-manifest.json') continue;
    integrity[name] = sha384(readFileSync(resolve(outDir, name)));
  }

  const manifest = {
    name: PACKAGE_NAME,
    version: readPluginVersion(),
    api_version: readApiVersion(),
    entry,
    integrity,
    contributions: {
      tool_panels: {},
      pages: [],
      settings_tabs: [],
      nav_entries: [],
      // index.tsx registers exactly the `web` channel's route panel; the host refuses a
      // bundle whose registered route panels differ from this list.
      route_panels: ['web'],
    },
  };

  writeFileSync(
    resolve(outDir, 'studio-manifest.json'),
    JSON.stringify(manifest, null, 2) + '\n',
    'utf8',
  );
  console.log(
    `${PACKAGE_NAME} studio bundle built: ${outDir}\n  entry: ${entry}\n  api_version: ${String(manifest.api_version)}\n  files: ${Object.keys(integrity).join(', ')}`,
  );
}

await main();
