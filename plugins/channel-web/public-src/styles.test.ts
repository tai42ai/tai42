// @vitest-environment node
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

/**
 * The chat page owns the whole document and picks its theme from the OS
 * preference alone (it never stamps `data-theme`). These assertions guard the
 * two things that keep native controls legible in the dark theme, neither of
 * which jsdom can compute (the vitest env runs with `css: false`): the page
 * pins `color-scheme` to the dark scheme so native control internals (number
 * spinners, the composer's resize grip, autofill, scrollbars) follow the tokens,
 * and its own native control paints from the `--tai-*` surface tokens rather
 * than a colour of its own.
 */
const css = readFileSync(resolve(process.cwd(), 'public-src/styles.css'), 'utf8');

/** Collapse whitespace so a declaration matches regardless of source wrapping. */
const flat = css.replace(/\s+/g, ' ');

describe('the chat page stylesheet', () => {
  it('pins color-scheme to dark for the OS dark preference', () => {
    // The `data-theme`-less page inherits only the design system's
    // `prefers-color-scheme` token swap, which never touches `color-scheme`; the
    // page must pin it itself so native chrome renders on the dark scheme.
    expect(flat).toMatch(
      /@media \(prefers-color-scheme: dark\) \{ :root:not\(\[data-theme='light'\]\) \{ color-scheme: dark; \}/,
    );
  });

  it('paints its native select from the surface + text + border tokens', () => {
    const select = /\.tcw-select \{[^}]*\}/.exec(css)?.[0] ?? '';
    expect(select).toMatch(/background: var\(--tai-color-surface\)/);
    expect(select).toMatch(/color: var\(--tai-color-text\)/);
    expect(select).toMatch(/border: 1px solid var\(--tai-color-border\)/);
  });
});
