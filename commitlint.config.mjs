// Release notes are generated from commit headers and BREAKING CHANGE footers,
// stored as HTML and read back through an HTML parser. A '<' that opens markup
// there (a letter, '/', '!' or '?' after it) starts an element that swallows the
// release sections after it. Entities count too: the notes are decoded before
// they are parsed again, so '&lt;', '&#60;' and '&#x3C;' (any case, leading
// zeros, optional ';') become a '<'.

const OPENER = "(?:<|&[lL][tT];?|&#0*60;?|&#[xX]0*3[cC];?)[A-Za-z/!?]";

const MARKUP_OPEN = new RegExp(OPENER);

export const CONSTRAINT =
  "the release notes are stored as HTML and read back through an HTML parser, " +
  "so a '<' that opens markup (a '<' or its HTML entity followed by a letter, " +
  "'/', '!' or '?') swallows the sections after it; write the placeholder " +
  "without angle brackets (TAI_PROVIDER_KEY, {field})";

// Whole-subject pattern for the PR-title lint, which takes a regex the subject
// must match rather than a rule.
export const TITLE_SUBJECT_PATTERN = `^(?!.*${OPENER}).*$`;

function noMarkupAngle(parsed, when = "always") {
  if (when !== "always") {
    throw new Error(`no-markup-angle supports only "always", got "${when}"`);
  }
  const header = parsed.header ?? "";
  const headerHit = header.match(MARKUP_OPEN);
  if (headerHit) {
    return [false, `the header contains "${headerHit[0]}": ${CONSTRAINT}`];
  }
  for (const note of parsed.notes ?? []) {
    const hit = (note.text ?? "").match(MARKUP_OPEN);
    if (hit) {
      return [false, `the ${note.title} footer contains "${hit[0]}": ${CONSTRAINT}`];
    }
  }
  return [true, ""];
}

export default {
  extends: ["@commitlint/config-conventional"],
  plugins: [{ rules: { "no-markup-angle": noMarkupAngle } }],
  rules: { "no-markup-angle": [2, "always"] },
};
