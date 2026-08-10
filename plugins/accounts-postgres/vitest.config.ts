import { fileURLToPath } from 'node:url';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

/**
 * Vitest config for the Studio front-end source: a jsdom DOM environment + React
 * Testing Library, so the plugin's users-admin page and its `register` entry are
 * exercised as real rendered DOM.
 *
 * `resolve.dedupe` binds ONE React across this repo's tests and the
 * registry-installed SDK's component code — the test-time mirror of what the
 * host import map guarantees in the browser (react/react-dom are external
 * singletons there). The `@` alias mirrors the tsconfig path so source and
 * tests share one import scheme.
 */
export default defineConfig({
  plugins: [react()],
  resolve: {
    // Prefer each package's ESM (`module`) build: the Radix scroll-lock
    // sidecars ship a CJS `main` whose untransformed `require('react')` would
    // bind a second React copy natively; their ESM builds are ssr-transformed
    // so `dedupe` below pins the single React.
    mainFields: ['module', 'browser', 'jsnext:main', 'jsnext'],
    dedupe: ['react', 'react-dom'],
    alias: {
      '@': fileURLToPath(new URL('./studio-src', import.meta.url)),
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    css: false,
    setupFiles: ['./studio-src/test-setup.ts'],
    server: {
      deps: {
        // The SDK installs from the npm registry (`@tai42/studio-sdk`); left
        // externalized, it (and its Radix internals, including the
        // react-remove-scroll sidecar family the Dialog/Select pull in) would
        // be loaded by node outside vite's graph and bind a second React copy,
        // crashing every hook. Inlining routes them through vite, where
        // `resolve.dedupe` pins the single React above.
        inline: [
          /@tai42\/studio-sdk/,
          /@radix-ui\//,
          /react-remove-scroll/,
          /react-style-singleton/,
          /use-callback-ref/,
          /use-sidecar/,
          /aria-hidden/,
        ],
      },
    },
  },
});
