// Tests for train-branches.js: the head branch names release-please opens its
// release pull request from, for the config at a commit.

const assert = require('node:assert/strict');
const {execFile, execFileSync} = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {test} = require('node:test');

const SCRIPT = path.join(__dirname, 'train-branches.js');

function repository(config, manifest) {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), 'train-branches-test-'));
  const git = (...args) =>
    execFileSync('git', ['-c', 'user.name=Alice', '-c', 'user.email=alice@example.com', ...args], {
      cwd: repo,
      encoding: 'utf8',
    }).trim();
  git('init', '--quiet', '--initial-branch=main');
  fs.writeFileSync(path.join(repo, 'release-please-config.json'), JSON.stringify(config));
  fs.writeFileSync(path.join(repo, '.release-please-manifest.json'), JSON.stringify(manifest));
  git('add', '--all');
  git('commit', '--quiet', '-m', 'chore: initial');
  return {repo, base: git('rev-parse', 'HEAD')};
}

function run(request) {
  return new Promise(resolve => {
    const child = execFile('node', [SCRIPT], {encoding: 'utf8'}, (error, stdout, stderr) =>
      resolve({code: error ? error.code : 0, stdout, stderr}),
    );
    child.stdin.end(JSON.stringify(request));
  });
}

const TWO = {
  'release-type': 'python',
  packages: {pkg: {'package-name': 'pkg'}, other: {'package-name': 'other'}},
};
const VERSIONS = {pkg: '1.0.0', other: '2.0.0'};

test('a combined release pull request has the one default branch', async () => {
  const {repo, base} = repository(TWO, VERSIONS);
  const outcome = await run({repo, base, baseBranch: 'main'});
  assert.equal(outcome.code, 0, outcome.stderr);
  assert.deepEqual(JSON.parse(outcome.stdout), {branches: ['release-please--branches--main']});
});

test('the branch follows the base branch', async () => {
  const {repo, base} = repository(TWO, VERSIONS);
  const outcome = await run({repo, base, baseBranch: 'develop'});
  assert.deepEqual(JSON.parse(outcome.stdout), {branches: ['release-please--branches--develop']});
});

test('separate pull requests are refused loudly', async () => {
  const {repo, base} = repository({...TWO, 'separate-pull-requests': true}, VERSIONS);
  const outcome = await run({repo, base, baseBranch: 'main'});
  assert.equal(outcome.code, 1);
  assert.match(outcome.stderr, /separate release pull requests/);
});

test('a package with its own separate pull request is refused loudly', async () => {
  const config = {...TWO, packages: {...TWO.packages, other: {'package-name': 'other', 'separate-pull-requests': true}}};
  const {repo, base} = repository(config, VERSIONS);
  const outcome = await run({repo, base, baseBranch: 'main'});
  assert.equal(outcome.code, 1);
  assert.match(outcome.stderr, /other/);
});

test("a single-package config, which release-please releases on its own branch, is refused loudly", async () => {
  const {repo, base} = repository({'release-type': 'python', packages: {pkg: {'package-name': 'pkg'}}}, {pkg: '1.0.0'});
  const outcome = await run({repo, base, baseBranch: 'main'});
  assert.equal(outcome.code, 1);
  assert.match(outcome.stderr, /separate release pull requests/);
});
