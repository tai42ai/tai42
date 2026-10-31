/**
 * Driving the Studio's searchable pickers — the select used wherever a tool, an agent,
 * a preset or a channel is chosen.
 *
 * The picker's trigger is a `combobox` named by its field label. Opening it moves focus
 * into a search box (a second `combobox`, named by the picker's search label, e.g.
 * "Search tools") over a `listbox` that carries the field's name; typing narrows the
 * options to those whose label or description contains the text, ignoring case, and a
 * click on an option chooses it and closes the popup. The popup is portaled to the page
 * body, so the search box and the options are found on the page, not inside the dialog
 * the trigger sits in.
 */
import { expect, type Locator, type Page } from '@playwright/test';

/** Escape `text` for use inside a regular expression. */
function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * The accessible name of a tool option: the bare tool name, or `Display (name)` when an
 * overlay display name maps it; either way an agent run tool adds ` (agent)`.
 */
export function toolOptionName(tool: string): RegExp {
  return new RegExp(`(^|\\()${escapeRegExp(tool)}\\)?( \\(agent\\))?$`);
}

/**
 * Open the picker behind `trigger`, type `query` into its search box (named
 * `searchLabel`), and choose the option named `option`. Waits for the trigger to be
 * enabled first: a picker is disabled while its list is still loading.
 */
export async function searchAndPick(
  page: Page,
  trigger: Locator,
  searchLabel: string,
  query: string,
  option: string | RegExp,
): Promise<void> {
  await expect(trigger).toBeEnabled();
  await trigger.click();
  await page.getByRole('combobox', { name: searchLabel, exact: true }).fill(query);
  const choice =
    typeof option === 'string'
      ? page.getByRole('option', { name: option, exact: true })
      : page.getByRole('option', { name: option });
  await choice.click();
  await expect(page.getByRole('combobox', { name: searchLabel, exact: true })).toHaveCount(0);
}

/** Choose `tool` in the tool picker behind `trigger` by searching for its name. */
export async function pickTool(page: Page, trigger: Locator, tool: string): Promise<void> {
  await searchAndPick(page, trigger, 'Search tools', tool, toolOptionName(tool));
}

/**
 * Choose `tool` in the tool picker behind `trigger` by searching for `words` of its
 * declared description.
 */
export async function pickToolByDescription(
  page: Page,
  trigger: Locator,
  tool: string,
  words: string,
): Promise<void> {
  await searchAndPick(page, trigger, 'Search tools', words, toolOptionName(tool));
}
