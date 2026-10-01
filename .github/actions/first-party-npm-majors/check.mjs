#!/usr/bin/env node
// Fail when a first-party scoped npm dependency is a full major version behind
// its latest published release. Smaller steps (minors, patches) stay quiet: the
// update bot owns those. This closes the hole the bot leaves open — a major
// bump it does not raise that then sits ignored.
//
// Scanned: every package.json under the root (recursively, excluding
// node_modules/dist/.git), across dependencies/devDependencies/
// peerDependencies, for a dependency whose name matches the configured scope.
// A dependency pinned to an internal source (workspace:/link:/file:/portal:)
// is not a registry pin and is skipped. For a registry range the HIGHEST major
// line the range can install is compared to the latest published major line: a
// range that cannot reach the latest major fails.
//
// 0.x versions follow npm caret semantics — the "major" line is 0.MINOR, so a
// 0.1 -> 0.2 jump is a major step. The major line is therefore a [a, b] key:
// a version with major >= 1 keys as [major, 0]; a 0.x version keys as
// [0, minor]. Keys compare lexicographically, so any 0.x sorts below any >= 1.
//
// Errors raise loudly (non-zero exit, a clear message); a registry/network
// failure or an unreadable range is never a silent pass.
//
// Exit codes: 0 = every matching pin is current (or none found: skip);
// 1 = at least one pin is a full major behind; 2 = an operational error.

import { readFile, readdir } from "node:fs/promises";
import { join, relative } from "node:path";
import { pathToFileURL } from "node:url";

const DEFAULT_SCOPE = "@tai42";
const DEFAULT_REGISTRY = "https://registry.npmjs.org";
const DEP_FIELDS = ["dependencies", "devDependencies", "peerDependencies"];
const INTERNAL_PREFIXES = ["workspace:", "link:", "file:", "portal:"];
const SKIP_DIRS = new Set(["node_modules", "dist", ".git"]);

// The major-line key of a published version: [major, 0] for major >= 1, else
// [0, minor] so npm's 0.x caret semantics (0.MINOR is the major line) hold and
// every 0.x sorts below every >= 1 release.
export function versionKey(version) {
  const core = String(version).trim().split("+")[0].split("-")[0];
  const parts = core.split(".");
  const major = Number(parts[0]);
  const minor = Number(parts[1] ?? "0");
  if (
    !Number.isInteger(major) ||
    major < 0 ||
    !Number.isInteger(minor) ||
    minor < 0
  ) {
    throw new Error(`cannot parse published version "${version}"`);
  }
  return major >= 1 ? [major, 0] : [0, minor];
}

// The HIGHEST major-line key a range can install. A caret/tilde/exact/x-range
// stays on one major line (for 0.x, one 0.MINOR line), so its ceiling is that
// line's key; an unbounded range (*/x/empty/latest) returns [Infinity, Infinity]
// so it is never judged behind. An unreadable or multi-comparator range raises
// loudly rather than guessing a verdict.
export function rangeCeilingKey(range) {
  let r = String(range).trim();
  if (r === "" || r === "*" || r === "x" || r === "X" || r === "latest") {
    return [Infinity, Infinity];
  }
  if (r.includes("||") || r.includes(" - ") || /[<>]/.test(r) || /\s/.test(r)) {
    throw new Error(
      `unsupported version range "${range}" — only exact, ^, ~ and x-ranges are evaluated`,
    );
  }
  if (r[0] === "^" || r[0] === "~" || r[0] === "=") {
    r = r.slice(1);
  }
  if (r[0] === "v" || r[0] === "V") {
    r = r.slice(1);
  }
  const core = r.split("+")[0].split("-")[0];
  const parts = core.split(".");
  const majorRaw = parts[0];
  if (majorRaw === "x" || majorRaw === "X" || majorRaw === "*" || majorRaw === "") {
    return [Infinity, Infinity];
  }
  const major = Number(majorRaw);
  if (!Number.isInteger(major) || major < 0) {
    throw new Error(
      `unsupported version range "${range}" — cannot read a major version`,
    );
  }
  // A range on major >= 1 (^, ~, exact, or an x-range such as 19 / 19.x) stays
  // within that major, so the minor never changes its major line.
  if (major >= 1) {
    return [major, 0];
  }
  const minorRaw = parts[1];
  if (
    minorRaw === undefined ||
    minorRaw === "x" ||
    minorRaw === "X" ||
    minorRaw === "*"
  ) {
    // 0.x / 0 — any 0.MINOR line up to < 1.0.0, so the ceiling is unbounded
    // within major 0.
    return [0, Infinity];
  }
  const minor = Number(minorRaw);
  if (!Number.isInteger(minor) || minor < 0) {
    throw new Error(
      `unsupported version range "${range}" — cannot read a minor version`,
    );
  }
  return [0, minor];
}

export function compareKeys(x, y) {
  if (x[0] !== y[0]) {
    return x[0] < y[0] ? -1 : 1;
  }
  if (x[1] !== y[1]) {
    return x[1] < y[1] ? -1 : 1;
  }
  return 0;
}

export async function collectPackageJsonFiles(root) {
  const found = [];
  async function walk(dir) {
    const entries = await readdir(dir, { withFileTypes: true });
    for (const entry of entries) {
      if (entry.isDirectory()) {
        if (SKIP_DIRS.has(entry.name)) {
          continue;
        }
        await walk(join(dir, entry.name));
      } else if (entry.isFile() && entry.name === "package.json") {
        found.push(join(dir, entry.name));
      }
    }
  }
  await walk(root);
  found.sort();
  return found;
}

function scopedPins(manifest, scope) {
  const pins = [];
  for (const field of DEP_FIELDS) {
    const deps = manifest[field];
    if (!deps || typeof deps !== "object") {
      continue;
    }
    for (const [name, spec] of Object.entries(deps)) {
      if (name === scope || name.startsWith(`${scope}/`)) {
        pins.push({ name, spec: String(spec) });
      }
    }
  }
  return pins;
}

async function latestVersion(registry, name, fetchImpl) {
  const base = registry.replace(/\/+$/, "");
  const url = `${base}/${name.replace("/", "%2f")}`;
  let response;
  try {
    response = await fetchImpl(url);
  } catch (err) {
    throw new Error(`registry request for ${name} failed: ${err.message}`);
  }
  if (!response.ok) {
    throw new Error(`registry request for ${name} returned HTTP ${response.status}`);
  }
  let body;
  try {
    body = await response.json();
  } catch (err) {
    throw new Error(`registry response for ${name} was not JSON: ${err.message}`);
  }
  const latest = body?.["dist-tags"]?.latest;
  if (typeof latest !== "string" || latest === "") {
    throw new Error(`registry response for ${name} has no dist-tags.latest`);
  }
  return latest;
}

export async function run({
  root = ".",
  scope = DEFAULT_SCOPE,
  registry = DEFAULT_REGISTRY,
  fetchImpl = globalThis.fetch,
  log = console.log,
  errorLog = console.error,
} = {}) {
  const files = await collectPackageJsonFiles(root);
  const pins = [];
  for (const file of files) {
    let manifest;
    const raw = await readFile(file, "utf8");
    try {
      manifest = JSON.parse(raw);
    } catch (err) {
      throw new Error(`cannot parse ${file}: ${err.message}`);
    }
    for (const pin of scopedPins(manifest, scope)) {
      pins.push({ file: relative(root, file) || "package.json", ...pin });
    }
  }

  const registryPins = pins.filter(
    (pin) => !INTERNAL_PREFIXES.some((prefix) => pin.spec.startsWith(prefix)),
  );
  if (registryPins.length === 0) {
    log(
      `first-party npm majors: no ${scope} registry dependencies found under ${root}, skipping`,
    );
    return 0;
  }

  const latestByName = new Map();
  const behind = [];
  for (const pin of registryPins) {
    const ceiling = rangeCeilingKey(pin.spec);
    if (!latestByName.has(pin.name)) {
      latestByName.set(pin.name, await latestVersion(registry, pin.name, fetchImpl));
    }
    const latest = latestByName.get(pin.name);
    if (compareKeys(ceiling, versionKey(latest)) < 0) {
      behind.push({ ...pin, latest });
    }
  }

  if (behind.length > 0) {
    errorLog(
      `first-party npm majors: ${behind.length} dependency(ies) a full major behind the latest published release:`,
    );
    for (const entry of behind) {
      errorLog(`  ${entry.file}  ${entry.name} ${entry.spec} -> latest ${entry.latest}`);
    }
    errorLog(
      "A major bump the update bot does not raise is sitting ignored; upgrade the pin(s) above.",
    );
    return 1;
  }

  log(
    `first-party npm majors: all ${registryPins.length} ${scope} registry dependency(ies) are current on their major line.`,
  );
  return 0;
}

function parseArgs(argv) {
  const opts = { root: ".", scope: DEFAULT_SCOPE, registry: DEFAULT_REGISTRY };
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i];
    const value = argv[i + 1];
    if (flag === "--root" || flag === "--scope" || flag === "--registry") {
      if (value === undefined) {
        throw new Error(`missing value for ${flag}`);
      }
      opts[flag.slice(2)] = value;
      i++;
    } else {
      throw new Error(`unknown argument: ${flag}`);
    }
  }
  return opts;
}

const isMain = import.meta.url === pathToFileURL(process.argv[1] ?? "").href;
if (isMain) {
  run(parseArgs(process.argv.slice(2)))
    .then((code) => {
      process.exit(code);
    })
    .catch((err) => {
      console.error(`first-party npm majors: ${err.message}`);
      process.exit(2);
    });
}
