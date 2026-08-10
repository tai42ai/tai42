/**
 * Build the public chat page bundle and emit its `public-manifest.json`.
 * Run as `pnpm build`; CI runs it before the freshness gate.
 *
 * SELF-CONTAINED, unlike a Studio plugin bundle. The page is a standalone
 * document served by this plugin's own door — there is no host, no import map,
 * and no shared singleton to bind — so the externals list is EMPTY and `react`,
 * `react-dom`, and `@tai42/studio-sdk` (components + its `tokens.css` /
 * `components.css` / `fonts.css`) are all bundled in. A post-build assertion
 * re-reads the emitted JS and fails loudly if any of those three survived as a
 * bare import specifier, which in a page with no import map would be a blank
 * screen and a module-resolution error.
 *
 * `base: './'` makes every asset URL the CSS emits relative to the stylesheet, so
 * the webfont requests resolve under `/api/channels/web/assets/` — the one place
 * the asset door serves from. A second assertion fails the build on any
 * stylesheet reference to a FOREIGN origin: the page's CSP is `default-src
 * 'none'` plus same-origin allowances, so such a reference could only ever be a
 * blocked request.
 *
 * CSS is emitted as ONE content-hashed asset (`cssCodeSplit: false`). Selectors
 * are NOT scoped or prefixed: this bundle owns the whole document, so the design
 * system's own `html` / `body` / `:root` rules are exactly what should apply.
 *
 * The manifest carries the plugin name, version (read from pyproject so it can
 * never drift from the Python package), the module entry, the stylesheets the
 * shell links, and a sha384 integrity entry for every emitted file. That
 * integrity map is ALSO the asset door's serving allowlist, so a file missing
 * from it is a file the browser can never fetch.
 */
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { readdirSync, readFileSync, writeFileSync, existsSync } from 'node:fs';
import { build } from 'vite';
import react from '@vitejs/plugin-react';

const scriptDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(scriptDir, '..');
const srcDir = resolve(repoRoot, 'public-src');
const entryFile = resolve(srcDir, 'index.tsx');
const outDir = resolve(repoRoot, 'src', 'tai42_channel_web', 'public');
const MANIFEST_FILENAME = 'public-manifest.json';

/** MUST equal the Python package name — the page loader reads the manifest from
 * inside that package, and the freshness gate pins the two together. */
const PACKAGE_NAME = 'tai42_channel_web';

/** The modules that MUST end up inside the bundle. A standalone page has no
 * import map, so any one of these left as a bare specifier is an unresolvable
 * import at load time — a blank page. */
const MUST_BE_BUNDLED = ['react', 'react-dom', 'react/jsx-runtime', '@tai42/studio-sdk'];

/** A stylesheet reference to anything but this origin — the page's CSP forbids
 * every foreign origin, so such a URL could only ever be a blocked request. */
const FOREIGN_URL_RE = /url\(\s*['"]?(?:[a-z][a-z0-9+.-]*:)?\/\//i;
const REMOTE_IMPORT_RE = /@import\s+(?:url\(\s*)?['"]?(?:[a-z][a-z0-9+.-]*:)?\/\//i;

/** Read the plugin's own version from its pyproject so pyproject stays the single
 * version source and the public manifest can never drift from the Python package. */
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
 * Assert no shared module survived as a bare import specifier in the emitted JS.
 * Both quote styles and both import positions (`import "x"`, `from "x"`) are
 * checked, so a minifier's choice cannot slip one past. Throws on any hit.
 */
function assertSelfContainedJs(files) {
  const violations = [];
  for (const [name, source] of files) {
    for (const specifier of MUST_BE_BUNDLED) {
      for (const quote of ['"', "'"]) {
        for (const keyword of ['from', 'import']) {
          if (source.includes(`${keyword}${quote}${specifier}${quote}`)) {
            violations.push(`${name}: '${specifier}' is still an external import`);
          }
        }
      }
    }
  }
  if (violations.length > 0) {
    throw new Error(
      'the chat page bundle is not self-contained (the page has no import map):\n' +
        violations.map((v) => `  - ${v}`).join('\n'),
    );
  }
}

/**
 * Assert the emitted stylesheet references nothing off this origin — no
 * absolute/protocol-relative `url()` and no remote `@import`. Anything it named
 * would be refused by the page's `default-src 'none'` CSP at runtime, which
 * shows up as a missing font or image rather than as a build failure.
 */
function assertSameOriginCss(cssPath) {
  const css = readFileSync(cssPath, 'utf8');
  const violations = [];
  if (FOREIGN_URL_RE.test(css)) violations.push('a url() pointing at a foreign origin');
  if (REMOTE_IMPORT_RE.test(css)) violations.push('a remote @import');
  if (violations.length > 0) {
    throw new Error(
      `the chat page stylesheet reaches off-origin, which its CSP forbids (${cssPath}):\n` +
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
    // Relative asset URLs: the stylesheet is served from
    // /api/channels/web/assets/<name>, so its webfont references must resolve
    // beside it rather than against the site root.
    base: './',
    plugins: [react()],
    define: { 'process.env.NODE_ENV': JSON.stringify('production') },
    resolve: {
      alias: { '@': srcDir },
      // ONE React in the bundle: the SDK resolves its peer through this repo's
      // install, and a second copy would break every hook at runtime.
      dedupe: ['react', 'react-dom'],
    },
    build: {
      outDir,
      emptyOutDir: true,
      target: 'es2022',
      // The webfonts stay separate FILES rather than base64 in the stylesheet:
      // each `@font-face` carries a `unicode-range`, so a browser fetches only
      // the subsets a page actually renders, and the render-blocking stylesheet
      // stays small. (A `build.lib` build would inline them all regardless of
      // this limit — which is why this is not one.)
      assetsInlineLimit: 0,
      minify: true,
      cssCodeSplit: false,
      modulePreload: { polyfill: false },
      rollupOptions: {
        input: entryFile,
        // EMPTY on purpose — see the module docstring. Everything ships inside.
        external: [],
        output: {
          format: 'es',
          // Content-hashed output: one entry chunk (plus a lazy chunk per dynamic
          // `import()` boundary, named with the `-chunk-` marker so the entry is
          // identifiable below). Non-JS assets (the stylesheet and the webfonts it
          // references) are content-hashed too, so every emitted file is
          // immutable-cache-safe and the integrity map covers each by name.
          entryFileNames: 'tai42-channel-web-[hash].js',
          chunkFileNames: 'tai42-channel-web-chunk-[hash].js',
          assetFileNames: 'tai42-channel-web-[hash][extname]',
        },
      },
    },
  });

  // Exactly one ENTRY; N lazy chunks (each carrying the `-chunk-` marker).
  const emitted = readdirSync(outDir);
  const jsFiles = emitted.filter((name) => name.endsWith('.js'));
  const entries = jsFiles.filter((name) => !name.includes('-chunk-'));
  if (entries.length !== 1) {
    throw new Error(
      `expected exactly one emitted JS entry, got ${entries.length}: ${jsFiles.join(', ')}`,
    );
  }
  const entry = entries[0];

  const styles = emitted.filter((name) => name.endsWith('.css'));
  if (styles.length !== 1) {
    throw new Error(
      `expected exactly one emitted CSS asset, got ${styles.length}: ${styles.join(', ')}`,
    );
  }
  assertSameOriginCss(resolve(outDir, styles[0]));
  assertSelfContainedJs(jsFiles.map((name) => [name, readFileSync(resolve(outDir, name), 'utf8')]));

  const integrity = {};
  for (const name of emitted) {
    if (name === MANIFEST_FILENAME) continue;
    integrity[name] = sha384(readFileSync(resolve(outDir, name)));
  }

  // The page LINKS the entry and every stylesheet, and the door serves ONLY
  // integrity-listed names — so a miss here would 404 in the browser.
  for (const required of [entry, ...styles]) {
    if (!(required in integrity)) {
      throw new Error(`linked file missing from the integrity map: ${required}`);
    }
  }

  const manifest = {
    name: PACKAGE_NAME,
    version: readPluginVersion(),
    entry,
    styles,
    integrity,
  };

  const manifestPath = resolve(outDir, MANIFEST_FILENAME);
  writeFileSync(manifestPath, JSON.stringify(manifest, null, 2) + '\n', 'utf8');

  if (!existsSync(manifestPath)) {
    throw new Error(`${MANIFEST_FILENAME} was not written`);
  }
  console.log(
    `tai42-channel-web public bundle built: ${outDir}\n  entry: ${entry}\n  styles: ${styles.join(', ')}\n  files: ${Object.keys(integrity).join(', ')}`,
  );
}

await main();
