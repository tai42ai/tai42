// Tests for the first-party-npm-majors check. Synthetic package.json fixtures
// in a temp tree, a mock registry fetch — no network. Run with: node --test.

import assert from "node:assert/strict";
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, beforeEach, test } from "node:test";

import {
  compareKeys,
  rangeCeilingKey,
  run,
  versionKey,
} from "./check.mjs";

const tempRoots = [];

async function makeTree(manifests) {
  const root = await mkdtemp(join(tmpdir(), "fp-npm-majors-"));
  tempRoots.push(root);
  for (const [relpath, manifest] of Object.entries(manifests)) {
    const full = join(root, relpath);
    await mkdir(join(full, ".."), { recursive: true });
    const body = typeof manifest === "string" ? manifest : JSON.stringify(manifest);
    await writeFile(full, body);
  }
  return root;
}

// A mock registry: dist-tags.latest per package name, read back from the URL.
function mockRegistry(latestByName) {
  const calls = [];
  const fetchImpl = async (url) => {
    const name = decodeURIComponent(new URL(url).pathname.slice(1));
    calls.push(name);
    if (!(name in latestByName)) {
      throw new Error(`unexpected registry call for ${name}`);
    }
    return {
      ok: true,
      status: 200,
      json: async () => ({ "dist-tags": { latest: latestByName[name] } }),
    };
  };
  return { fetchImpl, calls };
}

function recorder() {
  const out = [];
  const err = [];
  return {
    log: (m) => out.push(m),
    errorLog: (m) => err.push(m),
    out,
    err,
  };
}

after(async () => {
  await Promise.all(tempRoots.map((r) => rm(r, { recursive: true, force: true })));
});

let rec;
beforeEach(() => {
  rec = recorder();
});

test("a dependency a full major behind fails", async () => {
  const root = await makeTree({
    "package.json": { dependencies: { "@tai42/studio-sdk": "^19.0.0" } },
  });
  const { fetchImpl } = mockRegistry({ "@tai42/studio-sdk": "20.1.0" });
  const code = await run({ root, fetchImpl, ...rec });
  assert.equal(code, 1);
  assert.ok(rec.err.some((m) => m.includes("@tai42/studio-sdk")));
  assert.ok(rec.err.some((m) => m.includes("20.1.0")));
});

test("the same major passes quietly", async () => {
  const root = await makeTree({
    "package.json": { dependencies: { "@tai42/studio-sdk": "^19.3.0" } },
  });
  const { fetchImpl } = mockRegistry({ "@tai42/studio-sdk": "19.5.0" });
  const code = await run({ root, fetchImpl, ...rec });
  assert.equal(code, 0);
  assert.equal(rec.err.length, 0);
});

test("a pin ahead of the latest major passes quietly", async () => {
  const root = await makeTree({
    "package.json": { dependencies: { "@tai42/studio-sdk": "^20.0.0" } },
  });
  const { fetchImpl } = mockRegistry({ "@tai42/studio-sdk": "19.3.0" });
  const code = await run({ root, fetchImpl, ...rec });
  assert.equal(code, 0);
  assert.equal(rec.err.length, 0);
});

test("a minor/patch behind passes quietly (the bot's job)", async () => {
  const root = await makeTree({
    "package.json": { devDependencies: { "@tai42/studio-sdk": "^19.3.0" } },
  });
  const { fetchImpl } = mockRegistry({ "@tai42/studio-sdk": "19.4.2" });
  const code = await run({ root, fetchImpl, ...rec });
  assert.equal(code, 0);
  assert.equal(rec.err.length, 0);
});

test("internal-source deps (workspace:/link:) are ignored", async () => {
  const root = await makeTree({
    "package.json": {
      dependencies: {
        "@tai42/studio-sdk": "workspace:*",
        "@tai42/widgets": "link:../widgets",
      },
    },
  });
  // No registry call must happen for an internal pin.
  const { fetchImpl, calls } = mockRegistry({});
  const code = await run({ root, fetchImpl, ...rec });
  assert.equal(code, 0);
  assert.deepEqual(calls, []);
  assert.ok(rec.out.some((m) => m.includes("skipping")));
});

test("an internal pin is ignored while a registry pin beside it is still gated", async () => {
  const root = await makeTree({
    "package.json": {
      dependencies: { "@tai42/studio-sdk": "workspace:*" },
      devDependencies: { "@tai42/widgets": "^1.0.0" },
    },
  });
  const { fetchImpl, calls } = mockRegistry({ "@tai42/widgets": "2.0.0" });
  const code = await run({ root, fetchImpl, ...rec });
  assert.equal(code, 1);
  assert.deepEqual(calls, ["@tai42/widgets"]);
});

test("no scoped dependency skips", async () => {
  const root = await makeTree({
    "package.json": { dependencies: { react: "^19.0.0" } },
  });
  const { fetchImpl, calls } = mockRegistry({});
  const code = await run({ root, fetchImpl, ...rec });
  assert.equal(code, 0);
  assert.deepEqual(calls, []);
  assert.ok(rec.out.some((m) => m.includes("skipping")));
});

test("no package.json at all skips", async () => {
  const root = await makeTree({});
  const { fetchImpl, calls } = mockRegistry({});
  const code = await run({ root, fetchImpl, ...rec });
  assert.equal(code, 0);
  assert.deepEqual(calls, []);
  assert.ok(rec.out.some((m) => m.includes("skipping")));
});

test("a 0.x minor behind fails (0.MINOR is the major line)", async () => {
  const root = await makeTree({
    "package.json": { dependencies: { "@tai42/studio-sdk": "^0.1.0" } },
  });
  const { fetchImpl } = mockRegistry({ "@tai42/studio-sdk": "0.2.0" });
  const code = await run({ root, fetchImpl, ...rec });
  assert.equal(code, 1);
  assert.ok(rec.err.some((m) => m.includes("0.2.0")));
});

test("a 0.x patch behind passes quietly", async () => {
  const root = await makeTree({
    "package.json": { dependencies: { "@tai42/studio-sdk": "^0.2.0" } },
  });
  const { fetchImpl } = mockRegistry({ "@tai42/studio-sdk": "0.2.5" });
  const code = await run({ root, fetchImpl, ...rec });
  assert.equal(code, 0);
  assert.equal(rec.err.length, 0);
});

test("nested package.json files are scanned; node_modules/dist are not", async () => {
  const root = await makeTree({
    "package.json": { dependencies: { react: "^19.0.0" } },
    "plugins/web/package.json": {
      dependencies: { "@tai42/studio-sdk": "^18.0.0" },
    },
    "node_modules/pkg/package.json": {
      dependencies: { "@tai42/studio-sdk": "^1.0.0" },
    },
    "plugins/web/dist/package.json": {
      dependencies: { "@tai42/studio-sdk": "^1.0.0" },
    },
  });
  const { fetchImpl, calls } = mockRegistry({ "@tai42/studio-sdk": "19.0.0" });
  const code = await run({ root, fetchImpl, ...rec });
  assert.equal(code, 1);
  // Queried once (cached), never for the excluded trees' behind pins.
  assert.deepEqual(calls, ["@tai42/studio-sdk"]);
  assert.ok(rec.err.some((m) => m.includes("plugins/web/package.json")));
});

test("a registry HTTP error raises loudly, never a silent pass", async () => {
  const root = await makeTree({
    "package.json": { dependencies: { "@tai42/studio-sdk": "^19.0.0" } },
  });
  const fetchImpl = async () => ({ ok: false, status: 503 });
  await assert.rejects(
    run({ root, fetchImpl, ...rec }),
    /returned HTTP 503/,
  );
});

test("a network failure raises loudly, never a silent pass", async () => {
  const root = await makeTree({
    "package.json": { dependencies: { "@tai42/studio-sdk": "^19.0.0" } },
  });
  const fetchImpl = async () => {
    throw new Error("ENOTFOUND");
  };
  await assert.rejects(run({ root, fetchImpl, ...rec }), /failed: ENOTFOUND/);
});

test("an unreadable range raises loudly rather than guessing", async () => {
  const root = await makeTree({
    "package.json": { dependencies: { "@tai42/studio-sdk": ">=18 <20" } },
  });
  const { fetchImpl } = mockRegistry({ "@tai42/studio-sdk": "19.0.0" });
  await assert.rejects(run({ root, fetchImpl, ...rec }), /unsupported version range/);
});

test("the scope is configurable", async () => {
  const root = await makeTree({
    "package.json": { dependencies: { "@acme/kit": "^1.0.0" } },
  });
  const { fetchImpl } = mockRegistry({ "@acme/kit": "2.0.0" });
  const code = await run({ root, scope: "@acme", fetchImpl, ...rec });
  assert.equal(code, 1);
});

test("rangeCeilingKey covers the pin forms", () => {
  assert.deepEqual(rangeCeilingKey("^19.3.0"), [19, 0]);
  assert.deepEqual(rangeCeilingKey("~19.3.0"), [19, 0]);
  assert.deepEqual(rangeCeilingKey("19.3.0"), [19, 0]);
  assert.deepEqual(rangeCeilingKey("=19.3.0"), [19, 0]);
  assert.deepEqual(rangeCeilingKey("v19.3.0"), [19, 0]);
  assert.deepEqual(rangeCeilingKey("19"), [19, 0]);
  assert.deepEqual(rangeCeilingKey("19.x"), [19, 0]);
  assert.deepEqual(rangeCeilingKey("^0.1.2"), [0, 1]);
  assert.deepEqual(rangeCeilingKey("^0.0.3"), [0, 0]);
  assert.deepEqual(rangeCeilingKey("0.x"), [0, Infinity]);
  assert.deepEqual(rangeCeilingKey("*"), [Infinity, Infinity]);
  assert.deepEqual(rangeCeilingKey(""), [Infinity, Infinity]);
  assert.throws(() => rangeCeilingKey(">=1.0.0 <2.0.0"), /unsupported/);
  assert.throws(() => rangeCeilingKey("1 || 2"), /unsupported/);
});

test("versionKey and compareKeys order majors and the 0.x line", () => {
  assert.deepEqual(versionKey("19.3.0"), [19, 0]);
  assert.deepEqual(versionKey("0.2.5"), [0, 2]);
  assert.equal(compareKeys([19, 0], [20, 0]) < 0, true);
  assert.equal(compareKeys([19, 0], [19, 0]), 0);
  assert.equal(compareKeys([0, 9], [1, 0]) < 0, true);
  assert.equal(compareKeys([Infinity, Infinity], [19, 0]) > 0, true);
});
