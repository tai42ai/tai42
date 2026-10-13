// Tests for the commitlint config's markup-angle rule and the PR-title subject
// pattern derived from it. Run with: node --test commitlint.config.test.mjs

import assert from "node:assert/strict";
import { test } from "node:test";

import config, { TITLE_SUBJECT_PATTERN } from "./commitlint.config.mjs";

const RULE = "no-markup-angle";

function ruleFn() {
  const plugin = config.plugins.find((p) => p.rules && p.rules[RULE]);
  assert.ok(plugin, `no plugin defines ${RULE}`);
  return plugin.rules[RULE];
}

function lint({ header, notes = [] }) {
  const [ok, message] = ruleFn()({ header, notes }, "always");
  return { ok, message };
}

// The PR-title lint matches the subject with `new RegExp(pattern)` and requires
// the match to cover the whole subject.
function titleSubjectPasses(subject) {
  const match = subject.match(new RegExp(TITLE_SUBJECT_PATTERN));
  return match !== null && match[0].length === subject.length;
}

const breaking = (text) => [{ title: "BREAKING CHANGE", text }];

const MARKUP_SUBJECTS = [
  "TAI_<PROVIDER>_KEY",
  "close </details> early",
  "drop <!-- marker",
  "keep <?xml decl",
  "TAI_&lt;PROVIDER&gt;_KEY",
  "TAI_&LT;PROVIDER_KEY",
  "TAI_&#60;PROVIDER_KEY",
  "TAI_&#060;PROVIDER_KEY",
  "TAI_&#x3C;PROVIDER_KEY",
  "TAI_&#X3c;PROVIDER_KEY",
  "TAI_&ltPROVIDER_KEY",
];

const PLAIN_SUBJECTS = [
  "a < b holds",
  "require age <18 rejected",
  "x<=y comparison",
  "map input -> output",
  "a &lt; b in text",
  "TAI_PROVIDER_KEY and {field}",
  // Dependabot subjects as they land on main.
  "bump the npm group across 1 directory with 4 updates (#440)",
  "bump langgraph from 1.2.10 to 1.2.12 (#441)",
  "bump the github-actions group with 2 updates (#393)",
];

test("the config enables the rule as an error", () => {
  assert.deepEqual(config.rules[RULE], [2, "always"]);
  assert.ok(config.extends.includes("@commitlint/config-conventional"));
});

test("a header whose subject opens markup is refused", () => {
  for (const subject of MARKUP_SUBJECTS) {
    const { ok, message } = lint({ header: `feat!: ${subject}` });
    assert.equal(ok, false, subject);
    assert.match(message, /HTML parser/);
    assert.match(message, /without angle brackets/);
  }
});

test("a BREAKING CHANGE footer that opens markup is refused", () => {
  const { ok, message } = lint({
    header: "feat!: rename the validation error shape",
    notes: breaking("the <field> <rule> <what>"),
  });
  assert.equal(ok, false);
  assert.match(message, /BREAKING CHANGE footer/);
});

test("a BREAKING-CHANGE footer with an entity spelling is refused", () => {
  const { ok } = lint({
    header: "feat!: rename the key",
    notes: [{ title: "BREAKING-CHANGE", text: "set TAI_&#x3C;PROVIDER&gt;_KEY" }],
  });
  assert.equal(ok, false);
});

test("a markup opener on a later footer line is refused", () => {
  const { ok } = lint({
    header: "feat!: rename the key",
    notes: breaking("the key is renamed.\nSet TAI_<PROVIDER>_KEY instead."),
  });
  assert.equal(ok, false);
});

test("comparisons, digits, arrows and plain text pass", () => {
  for (const subject of PLAIN_SUBJECTS) {
    assert.deepEqual(lint({ header: `fix: ${subject}` }).ok, true, subject);
  }
  const { ok } = lint({
    header: "feat!: rename the key",
    notes: breaking("set TAI_PROVIDER_KEY; values <18 are refused, x<=y holds"),
  });
  assert.equal(ok, true);
});

test("the PR-title subject pattern refuses the same openers", () => {
  for (const subject of MARKUP_SUBJECTS) {
    assert.equal(titleSubjectPasses(subject), false, subject);
  }
  for (const subject of PLAIN_SUBJECTS) {
    assert.equal(titleSubjectPasses(subject), true, subject);
  }
});
