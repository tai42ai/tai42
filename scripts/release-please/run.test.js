// Tests for run.js: the release-please calls it makes, their order, and the
// JSON it prints, with a fake manifest factory in place of GitHub.

const assert = require('node:assert/strict');
const {test} = require('node:test');

const {main} = require('./run.js');

const ARGS = ['--repo-url', 'acme/widgets', '--target-branch', 'main', '--token', 'secret'];

function recorder() {
  const calls = [];
  const options = [];
  const factory = opts => {
    options.push(opts);
    return async () => ({
      createReleases: async () => {
        calls.push('createReleases');
        return [
          {tagName: 'pkg-v1.2.4', sha: 'abc123', path: 'pkg', notes: 'notes', url: 'https://example.com'},
          undefined,
        ];
      },
      createPullRequests: async () => {
        calls.push('createPullRequests');
        return [{number: 12, headBranchName: 'release-please--branches--main', title: 'chore: release main'}];
      },
      buildReleases: async () => {
        calls.push('buildReleases');
        return [{tag: {toString: () => 'pkg-v1.2.4'}, sha: 'abc123', path: 'pkg'}];
      },
      buildPullRequests: async () => {
        calls.push('buildPullRequests');
        return [
          {
            title: {toString: () => 'chore: release main'},
            headRefName: 'release-please--branches--main',
            body: {releaseData: [{component: 'pkg', version: {toString: () => '1.2.4'}}]},
          },
        ];
      },
    });
  };
  return {calls, options, factory};
}

function sink() {
  const chunks = [];
  return {write: chunk => chunks.push(chunk), text: () => chunks.join('')};
}

test('creates releases before pull requests and prints what it created', async () => {
  const {calls, options, factory} = recorder();
  const out = sink();
  await main(ARGS, factory, out);
  assert.deepEqual(calls, ['createReleases', 'createPullRequests']);
  assert.deepEqual(JSON.parse(out.text()), {
    releases: [{tagName: 'pkg-v1.2.4', sha: 'abc123', path: 'pkg'}],
    prs: [{number: 12, headBranchName: 'release-please--branches--main'}],
  });
  assert.equal(options[0].owner, 'acme');
  assert.equal(options[0].repo, 'widgets');
  assert.equal(options[0].targetBranch, 'main');
  assert.equal(options[0].token, 'secret');
  assert.equal(options[0].configFile, 'release-please-config.json');
  assert.equal(options[0].manifestFile, '.release-please-manifest.json');
});

test('a dry run builds and never creates', async () => {
  const {calls, factory} = recorder();
  const out = sink();
  await main([...ARGS, '--dry-run'], factory, out);
  assert.deepEqual(calls, ['buildReleases', 'buildPullRequests']);
  assert.deepEqual(JSON.parse(out.text()), {
    releases: [{tagName: 'pkg-v1.2.4', sha: 'abc123', path: 'pkg'}],
    prs: [
      {
        title: 'chore: release main',
        headBranchName: 'release-please--branches--main',
        releaseData: [{component: 'pkg', version: '1.2.4'}],
      },
    ],
  });
});

test('no pull request prints an empty list', async () => {
  const {factory} = recorder();
  const empty = opts => {
    const load = factory(opts);
    return async () => ({...(await load()), createPullRequests: async () => [undefined]});
  };
  const out = sink();
  await main(ARGS, empty, out);
  assert.deepEqual(JSON.parse(out.text()).prs, []);
});

test('the token falls back to RELEASE_PLEASE_TOKEN', async () => {
  const {options, factory} = recorder();
  const previous = process.env.RELEASE_PLEASE_TOKEN;
  process.env.RELEASE_PLEASE_TOKEN = 'from-env';
  try {
    await main(['--repo-url', 'acme/widgets', '--target-branch', 'main'], factory, sink());
  } finally {
    if (previous === undefined) delete process.env.RELEASE_PLEASE_TOKEN;
    else process.env.RELEASE_PLEASE_TOKEN = previous;
  }
  assert.equal(options[0].token, 'from-env');
});

test('a missing token, repository or target branch is refused', async () => {
  const {factory} = recorder();
  const previous = process.env.RELEASE_PLEASE_TOKEN;
  delete process.env.RELEASE_PLEASE_TOKEN;
  try {
    await assert.rejects(main(['--repo-url', 'acme/widgets', '--target-branch', 'main'], factory, sink()), /token/);
    await assert.rejects(main(['--target-branch', 'main', '--token', 't'], factory, sink()), /--repo-url/);
    await assert.rejects(main(['--repo-url', 'acme', '--target-branch', 'main', '--token', 't'], factory, sink()), /owner\/repo/);
    await assert.rejects(main(['--repo-url', 'acme/widgets', '--token', 't'], factory, sink()), /--target-branch/);
  } finally {
    if (previous !== undefined) process.env.RELEASE_PLEASE_TOKEN = previous;
  }
});

test('an error from release-please propagates', async () => {
  const failing = () => async () => ({
    createReleases: async () => {
      throw new Error('boom');
    },
  });
  await assert.rejects(main(ARGS, failing, sink()), /boom/);
});
