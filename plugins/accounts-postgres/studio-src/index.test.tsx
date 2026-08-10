import { describe, expect, it } from 'vitest';
import type {
  NavEntryContribution,
  PageContribution,
  PluginContext,
  SettingsTabContribution,
  ToolPanelContribution,
} from '@tai42/studio-sdk';
import { register } from './index';

/**
 * A recording `PluginContext`: `registerPage` / `registerNavEntry` capture their
 * arguments; the other members throw, so the test fails loudly if the plugin
 * registers anything beyond the page + nav pair it is meant to.
 */
function makeRecordingContext(): {
  context: PluginContext;
  pages: PageContribution[];
  navEntries: NavEntryContribution[];
} {
  const pages: PageContribution[] = [];
  const navEntries: NavEntryContribution[] = [];
  const context: PluginContext = {
    registerPage(contribution: PageContribution): void {
      pages.push(contribution);
    },
    registerNavEntry(contribution: NavEntryContribution): void {
      navEntries.push(contribution);
    },
    registerToolPanel(_contribution: ToolPanelContribution): void {
      throw new Error('registerToolPanel must not be called');
    },
    registerSettingsTab(_contribution: SettingsTabContribution): void {
      throw new Error('registerSettingsTab must not be called');
    },
  };
  return { context, pages, navEntries };
}

describe('register', () => {
  it('registers exactly the users page and its nav entry, paths matching', () => {
    const { context, pages, navEntries } = makeRecordingContext();

    register(context);

    expect(pages).toHaveLength(1);
    const page = pages[0];
    if (page === undefined) throw new Error('expected a registered page');
    expect(page.path).toBe('users');
    expect(page.title.length).toBeGreaterThan(0);
    expect(page.component).toBeTypeOf('function');

    expect(navEntries).toHaveLength(1);
    const nav = navEntries[0];
    if (nav === undefined) throw new Error('expected a registered nav entry');
    // The nav entry links to a page THIS plugin registers — a path with no
    // matching page fails the whole plugin load.
    expect(nav.path).toBe(page.path);
    expect(nav.title.length).toBeGreaterThan(0);
    expect(nav.icon).toBeTypeOf('function');
  });
});
