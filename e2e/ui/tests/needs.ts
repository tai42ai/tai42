/**
 * What a spec needs from the stack it drives.
 *
 * Every spec imports `test` from here and declares its needs once at the top of the
 * file — `needs('kind:storage', 'mutable')` — or inside a `test.describe` for a group
 * that needs more. A spec with nothing beyond the page and the login key declares
 * `needs()`. Against a target (`TAI_E2E_TARGET`) a declaration the target does not meet
 * skips its tests with the unmet need as the reason, before any hook runs; a test with
 * no declaration is skipped too, as built-stack only.
 *
 * The vocabulary has three classes, decided by the word before the first `:`:
 * - reported — `kind:<kind>` (that kind is on) and `kind:<kind>:<name>` (and `<name>`
 *   appears in its plugin or detail), read from the target's `/api/system/kinds`;
 * - declared — `probe-tools`, `mutable`: true only when the target file lists it under
 *   `provides`;
 * - built — `process`, `store`, `files`, `helper`, `setting`, `topology`, `cli`,
 *   `metrics`, `second-stack`, `fixture-page`, `no-stack`: only a stack the run builds
 *   itself has it, so such a test never runs against a target. A qualifier after `:` is
 *   free text for the reader (`helper:llm`, `setting:ACCESS_CONTROL_ENABLE=false`).
 */
import { test as base } from '@playwright/test';

import { BUILT_NEEDS, DECLARED_NEEDS, TARGET, type Target } from './target';

function checkNeeds(list: readonly string[]): void {
  const unknown = list.filter((need) => {
    const [head, ...rest] = need.split(':');
    if (head === 'kind') return rest.join(':') === '';
    return !(need in DECLARED_NEEDS) && !(head in BUILT_NEEDS);
  });
  if (unknown.length > 0) {
    throw new Error(
      `unknown need(s) ${unknown.join(', ')}; a need is kind:<kind>[:<name>], one of ${Object.keys(DECLARED_NEEDS).join(', ')}, or one of ${Object.keys(BUILT_NEEDS).join(', ')} (optionally :<detail>)`,
    );
  }
}

/** Why a test with these needs cannot run against `target`, or `undefined` when it can. */
export function unmet(target: Target, list: readonly string[] | undefined): string | undefined {
  if (list === undefined) return 'no needs declared: built-stack only';
  for (const need of list) {
    const [head, kind = '', ...rest] = need.split(':');
    const name = rest.join(':');
    if (head in BUILT_NEEDS) {
      return `needs ${need} (${BUILT_NEEDS[head]}): only a stack this run builds has it`;
    }
    if (need in DECLARED_NEEDS) {
      if (!target.provides.includes(need)) {
        return `target does not provide ${need} (${DECLARED_NEEDS[need]})`;
      }
      continue;
    }
    // The kinds arrive from the global setup; the runner's own load of a spec file
    // happens before it, and decides nothing.
    if (target.kinds === undefined) continue;
    if (!(kind in target.kinds)) return `target reports kind ${kind} off`;
    if (name && !target.kinds[kind].includes(name.toLowerCase())) {
      return `target's ${kind} does not name ${name}`;
    }
  }
  return undefined;
}

export const test = base.extend<{ needs: readonly string[] | undefined; needsGate: void }>({
  needs: [undefined, { option: true }],
  needsGate: [
    async ({ needs: declared }, use, testInfo) => {
      if (declared !== undefined) {
        testInfo.annotations.push({ type: 'needs', description: declared.join(', ') || 'none' });
      }
      const reason = TARGET ? unmet(TARGET, declared) : undefined;
      testInfo.skip(reason !== undefined, reason);
      await use();
    },
    { auto: true },
  ],
});

/** Declare what the tests of the enclosing file or `describe` need from the stack. */
export function needs(...list: string[]): void {
  checkNeeds(list);
  test.use({ needs: list });
  const reason = TARGET ? unmet(TARGET, list) : undefined;
  test.skip(reason !== undefined, reason ?? '');
}
