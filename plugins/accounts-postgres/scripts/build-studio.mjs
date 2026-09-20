/**
 * Build this plugin's Studio front-end bundle and emit its `studio-manifest.json`.
 * Run as `pnpm build`; CI runs it before the freshness gate.
 *
 * The bundle is a single content-hashed ESM entry (the users-admin surface has no
 * dynamic `import()` boundaries, so no lazy chunks are emitted; the code below
 * still tolerates them so a future split needs no build change). `react`,
 * `react-dom`, their subpath entries (`react/jsx-runtime`, `react-dom/client`),
 * and `@tai42/studio-sdk` are marked EXTERNAL — exactly the modules the host serves
 * through the import map, so the bundle binds the host's singletons rather than
 * shipping a second copy. The import map's integrity block covers every emitted JS
 * file, so any lazy chunk is SRI-verified browser-side like the entry.
 *
 * CSS: the bundle's stylesheet import (the plugin's own `styles.css`) is emitted
 * as ONE content-hashed `.css` asset. At build time a PostCSS pass scopes EVERY
 * selector under the plugin root class (`.tai42_accounts_postgres-root`) and
 * namespaces every `@keyframes` (and its `animation` references) with the plugin
 * prefix, so the host-injected stylesheet can never leak into the shell. A
 * post-build assertion re-parses the emitted asset and fails the build loudly on
 * any unscoped selector, un-namespaced keyframe, or root-level rule.
 *
 * The manifest carries the plugin name, version, the targeted
 * `STUDIO_PLUGIN_API_VERSION` (read from the SDK source so it can never drift),
 * the contributions, the entry filename, and a sha384 integrity entry for every
 * emitted file — JS and CSS alike; the host injects `.css` integrity entries as
 * SRI'd `<link>` tags before importing the bundle. The skeleton registry re-hashes
 * each served file at startup and rejects loudly on any mismatch, so these hashes
 * must be real.
 */
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { readdirSync, readFileSync, writeFileSync, existsSync } from 'node:fs';
import { build } from 'vite';
import react from '@vitejs/plugin-react';
import postcss from 'postcss';
import prefixSelector from 'postcss-prefix-selector';

const scriptDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(scriptDir, '..');
const srcDir = resolve(repoRoot, 'studio-src');
const entryFile = resolve(srcDir, 'index.tsx');
const outDir = resolve(repoRoot, 'src', 'tai42_accounts_postgres', 'studio');
// The SDK is resolved through this repo's own install. Its published `files` list
// ships both `dist` and `src`, so the type and version paths below resolve from
// the registry install.
const sdkRoot = resolve(repoRoot, 'node_modules', '@tai42', 'studio-sdk');
const sdkVersionFile = resolve(sdkRoot, 'src', 'plugin', 'version.ts');
const sdkTypesFile = resolve(sdkRoot, 'dist', 'index.d.ts');

/** The shared modules the host provides through the import map. A plugin that
 * bundled its own copy would break the React / SDK singleton. */
const EXTERNAL = [
  'react',
  'react-dom',
  'react/jsx-runtime',
  'react-dom/client',
  '@tai42/studio-sdk',
];

/** The plugin root class every stylesheet selector is scoped under. The plugin
 * renders this class itself on its page root; the name is prefixed with the
 * plugin's package name per the Studio styling contract. */
const ROOT_CLASS = '.tai42_accounts_postgres-root';

/** The plugin prefix stamped onto every `@keyframes` name. */
const KEYFRAMES_PREFIX = 'tap-';

const KEYFRAMES_AT_RULES = new Set([
  'keyframes',
  '-webkit-keyframes',
  '-moz-keyframes',
  '-o-keyframes',
  '-ms-keyframes',
]);

/**
 * PostCSS plugin: namespace every `@keyframes <name>` with the plugin prefix and
 * rewrite the matching `animation` / `animation-name` references. Runs per-file (a
 * stylesheet's keyframes and their references live together), so a single
 * collect-then-rewrite pass suffices. Names already carrying the prefix are left
 * untouched.
 */
const namespaceKeyframes = () => ({
  postcssPlugin: 'tap-namespace-keyframes',
  Once(root) {
    const renamed = new Map();
    root.walkAtRules((atRule) => {
      if (!KEYFRAMES_AT_RULES.has(atRule.name)) return;
      const name = atRule.params.trim();
      if (!name || name.startsWith(KEYFRAMES_PREFIX)) return;
      const next = `${KEYFRAMES_PREFIX}${name}`;
      renamed.set(name, next);
      atRule.params = next;
    });
    if (renamed.size === 0) return;
    root.walkDecls(/^(-\w+-)?animation(-name)?$/, (decl) => {
      for (const [from, to] of renamed) {
        // Whole-token replacement so a keyframe name can never match inside a
        // longer identifier.
        decl.value = decl.value.replace(
          new RegExp(`(^|[\\s,])${from}(?=$|[\\s,])`, 'g'),
          `$1${to}`,
        );
      }
    });
  },
});
namespaceKeyframes.postcss = true;

/** Read STUDIO_PLUGIN_API_VERSION from the SDK source — the single source of
 * truth, so a version bump there is reflected here without a manual edit. The
 * preflight asserts the SDK's shipped declarations are present, so typecheck and
 * tests resolve its types. */
function readApiVersion() {
  if (!existsSync(sdkTypesFile)) {
    throw new Error(
      `@tai42/studio-sdk is not installed correctly: ${sdkTypesFile} is missing. ` +
        'Run `pnpm install` first.',
    );
  }
  const source = readFileSync(sdkVersionFile, 'utf8');
  const match = /export const STUDIO_PLUGIN_API_VERSION\s*=\s*(\d+)\s*;/.exec(source);
  if (match === null) {
    throw new Error(`could not read STUDIO_PLUGIN_API_VERSION from ${sdkVersionFile}`);
  }
  return Number.parseInt(match[1], 10);
}

/** Read the plugin's own version from its pyproject so pyproject stays the single
 * version source and the studio manifest can never drift from the Python package. */
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

/**
 * Assert the emitted stylesheet obeys the scoped-CSS contract:
 *   1. every selector of every rule (top-level or nested in @media/@supports/
 *      @layer) starts with the plugin root class;
 *   2. no `html` / `body` / `:root` / bare `*` rule survives anywhere;
 *   3. every `@keyframes` name carries the plugin prefix.
 * Throws (failing the build) on any violation.
 */
function assertScopedCss(cssPath) {
  const css = readFileSync(cssPath, 'utf8');
  const root = postcss.parse(css, { from: cssPath });
  const violations = [];

  root.walkRules((rule) => {
    const parent = rule.parent;
    const insideKeyframes = parent?.type === 'atrule' && KEYFRAMES_AT_RULES.has(parent.name);
    if (insideKeyframes) return; // keyframe steps (from/to/%) carry no scope.
    for (const selector of rule.selectors) {
      const sel = selector.trim();
      if (!sel.startsWith(ROOT_CLASS)) {
        violations.push(`unscoped selector: ${sel}`);
      }
      if (/(^|[\s>+~,])(html|body|:root)([\s>+~,.:[{]|$)/.test(sel) || sel === '*') {
        violations.push(`root-level rule: ${sel}`);
      }
    }
  });
  root.walkAtRules((atRule) => {
    if (!KEYFRAMES_AT_RULES.has(atRule.name)) return;
    if (!atRule.params.trim().startsWith(KEYFRAMES_PREFIX)) {
      violations.push(`un-namespaced @${atRule.name}: ${atRule.params}`);
    }
  });

  if (violations.length > 0) {
    throw new Error(
      `scoped-CSS assertion failed for ${cssPath}:\n` +
        violations.map((v) => `  - ${v}`).join('\n'),
    );
  }
}

async function main() {
  await build({
    root: srcDir,
    configFile: false,
    logLevel: 'warn',
    publicDir: false,
    // React's JSX runtime is external; the plugin transform emits `react/jsx-runtime`
    // imports that stay external and resolve through the host import map.
    plugins: [react()],
    define: { 'process.env.NODE_ENV': JSON.stringify('production') },
    resolve: {
      alias: { '@': srcDir },
    },
    css: {
      postcss: {
        plugins: [
          // Scope EVERY selector under the plugin root class. Selectors already
          // scoped are left as-is.
          prefixSelector({
            prefix: ROOT_CLASS,
            transform(prefix, selector, prefixedSelector) {
              return selector.startsWith(prefix) ? selector : prefixedSelector;
            },
          }),
          namespaceKeyframes(),
        ],
      },
    },
    build: {
      outDir,
      emptyOutDir: true,
      target: 'es2022',
      minify: true,
      cssCodeSplit: false,
      modulePreload: { polyfill: false },
      lib: {
        entry: entryFile,
        formats: ['es'],
      },
      rollupOptions: {
        external: EXTERNAL,
        output: {
          // Content-hashed output: one entry chunk (plus a lazy chunk per dynamic
          // `import()` boundary, named with the `-chunk-` marker so the entry is
          // identifiable below). Non-JS assets (the scoped stylesheet) are
          // content-hashed too, so every emitted file is immutable-cache-safe and
          // the integrity map covers each by name.
          entryFileNames: 'tai42-accounts-postgres-[hash].js',
          chunkFileNames: 'tai42-accounts-postgres-chunk-[hash].js',
          assetFileNames: 'tai42-accounts-postgres-[hash][extname]',
        },
      },
    },
  });

  // Exactly one ENTRY; N lazy chunks (each carrying the `-chunk-` marker).
  const emitted = readdirSync(outDir).filter((name) => name.endsWith('.js'));
  const entries = emitted.filter((name) => !name.includes('-chunk-'));
  if (entries.length !== 1) {
    throw new Error(
      `expected exactly one emitted JS entry, got ${entries.length}: ${emitted.join(', ')}`,
    );
  }
  const entry = entries[0];
  const chunks = emitted.filter((name) => name.includes('-chunk-'));

  const cssAssets = readdirSync(outDir).filter((name) => name.endsWith('.css'));
  if (cssAssets.length !== 1) {
    throw new Error(
      `expected exactly one emitted CSS asset, got ${cssAssets.length}: ${cssAssets.join(', ')}`,
    );
  }
  assertScopedCss(resolve(outDir, cssAssets[0]));

  const integrity = {};
  for (const name of readdirSync(outDir)) {
    if (name === 'studio-manifest.json') continue;
    integrity[name] = sha384(readFileSync(resolve(outDir, name)));
  }

  // Every emitted file must be integrity-listed — the host serves ONLY listed
  // files, so a miss here would 404 at runtime.
  for (const required of [entry, ...chunks, ...cssAssets]) {
    if (!(required in integrity)) {
      throw new Error(`emitted file missing from the integrity map: ${required}`);
    }
  }

  const manifest = {
    // MUST equal the Python package name — the skeleton registry rejects a
    // mismatch at startup, and the bundle URL is built from this name.
    name: 'tai42_accounts_postgres',
    version: readPluginVersion(),
    api_version: readApiVersion(),
    entry,
    integrity,
    contributions: {
      tool_panels: {},
      pages: ['users'],
      settings_tabs: [],
    },
  };

  const manifestPath = resolve(outDir, 'studio-manifest.json');
  writeFileSync(manifestPath, JSON.stringify(manifest, null, 2) + '\n', 'utf8');

  if (!existsSync(manifestPath)) {
    throw new Error('studio-manifest.json was not written');
  }
  console.log(
    `tai42-accounts-postgres studio bundle built: ${outDir}\n  entry: ${entry}\n  api_version: ${String(manifest.api_version)}\n  files: ${Object.keys(integrity).join(', ')}`,
  );
}

await main();
