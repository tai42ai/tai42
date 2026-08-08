import { fileURLToPath } from 'node:url';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

/**
 * Vitest config for the public chat page source: a jsdom DOM environment + React
 * Testing Library, so the page's components, stream reducer, and fetch seam are
 * exercised as real rendered DOM.
 *
 * `resolve.dedupe` binds ONE React across this repo's tests and the
 * registry-installed SDK's component code — the test-time mirror of what the
 * self-contained build guarantees in the browser (one bundled React). The `@`
 * alias mirrors the tsconfig path so source and tests share one import scheme.
 *
 * Coverage (v8) runs on every `pnpm test`, so these thresholds are a real gate.
 * Each one sits a couple of points under what the suite achieves, so one newly
 * uncovered branch does not break the run while losing a module's tests does.
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
      '@': fileURLToPath(new URL('./public-src', import.meta.url)),
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    css: false,
    setupFiles: ['./public-src/test-setup.ts'],
    server: {
      deps: {
        // The SDK installs from the npm registry (`@tai42/studio-sdk`); left
        // externalized, it (and its Radix internals, including the
        // react-remove-scroll sidecar family the Dialog pulls in) would be
        // loaded by node outside vite's graph and bind a second React copy,
        // crashing every hook. Inlining routes them through vite, where
        // `resolve.dedupe` pins the single React above.
        inline: [
          // Testing Library must be ONE graph: React Testing Library configures
          // the DOM library's `eventWrapper` (which is what wraps a dispatched
          // event in `act`), and user-event reads that same configuration. Loaded
          // outside vite the two bind different copies of it, user-event's events
          // arrive unwrapped, and every keystroke prints an act warning.
          /@testing-library\//,
          /@tai42\/studio-sdk/,
          /@radix-ui\//,
          /@floating-ui\//,
          /react-remove-scroll/,
          /react-style-singleton/,
          /use-callback-ref/,
          /use-sidecar/,
          /aria-hidden/,
        ],
      },
    },
    coverage: {
      provider: 'v8',
      include: ['public-src/**/*.{ts,tsx}'],
      exclude: [
        'public-src/**/*.test.{ts,tsx}',
        'public-src/test-setup.ts',
        'public-src/**/*.d.ts',
      ],
      reporter: ['text'],
      thresholds: {
        statements: 97,
        branches: 90,
        functions: 96,
        lines: 97,
      },
    },
  },
});
