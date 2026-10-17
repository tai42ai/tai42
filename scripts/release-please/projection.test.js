// Tests for projection.js. Each test builds a throw-away git repository with a
// release-please config, a manifest and release tags, then runs projection.js
// as a subprocess with its JSON request on stdin. GitHub's GraphQL API is a
// local fake that answers each commit's associated pull requests.

const assert = require('node:assert/strict');
const {execFile, execFileSync} = require('node:child_process');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const {after, before, beforeEach, test} = require('node:test');

const PROJECTION = path.join(__dirname, 'projection.js');
const REPOSITORY = 'acme/widgets';
const TOKEN = 'test-token';

// The fake GitHub: `pullRequests[sha]` is the list of pull requests GraphQL
// associates with that commit (none when absent, as for a direct push).
const github = {pullRequests: {}, requests: [], fail: null, url: ''};
let server;

before(async () => {
  server = http.createServer((req, res) => {
    let text = '';
    req.on('data', chunk => (text += chunk));
    req.on('end', () => {
      const {query, variables} = JSON.parse(text);
      github.requests.push({path: req.url, authorization: req.headers.authorization, query, variables});
      res.setHeader('content-type', 'application/json');
      if (github.fail) {
        res.statusCode = github.fail.status;
        res.end(JSON.stringify(github.fail.body));
        return;
      }
      const repository = {};
      for (const [alias, sha] of Object.entries(variables)) {
        if (!/^c\d+$/.test(alias)) continue;
        repository[alias] = {associatedPullRequests: {nodes: github.pullRequests[sha] || []}};
      }
      res.end(JSON.stringify({data: {repository}}));
    });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  github.url = `http://127.0.0.1:${server.address().port}/graphql`;
});

after(() => server.close());

beforeEach(() => {
  github.pullRequests = {};
  github.requests = [];
  github.fail = null;
});

// A pull request as GraphQL's associatedPullRequests returns it.
function mergedPullRequest(number, sha, {title = `pull request ${number}`, body = ''} = {}) {
  return {
    number,
    title,
    baseRefName: 'main',
    headRefName: `topic-${number}`,
    body,
    mergeCommit: {oid: sha},
    labels: {nodes: []},
  };
}

function run(request, env = {}) {
  return new Promise(resolve => {
    const child = execFile(
      'node',
      [PROJECTION],
      {
        encoding: 'utf8',
        env: {...process.env, GITHUB_TOKEN: TOKEN, GITHUB_GRAPHQL_URL: github.url, ...env},
      },
      (error, stdout, stderr) => resolve({code: error ? error.code : 0, stdout, stderr}),
    );
    child.stdin.end(typeof request === 'string' ? request : JSON.stringify(request));
  });
}

const GIT_IDENTITY = [
  '-c', 'user.name=Alice',
  '-c', 'user.email=alice@example.com',
  '-c', 'commit.gpgsign=false',
  '-c', 'tag.gpgsign=false',
];

// Each git call gets a later timestamp, so `git log` lists commits in the order
// they were made, as on a real main.
let clock = 1700000000;

function git(repo, ...args) {
  clock += 60;
  const date = `${clock} +0000`;
  const env = {...process.env, GIT_AUTHOR_DATE: date, GIT_COMMITTER_DATE: date};
  return execFileSync('git', [...GIT_IDENTITY, ...args], {cwd: repo, encoding: 'utf8', env}).trim();
}

function write(repo, files) {
  for (const [file, content] of Object.entries(files)) {
    const target = path.join(repo, file);
    fs.mkdirSync(path.dirname(target), {recursive: true});
    fs.writeFileSync(target, content);
  }
}

function commit(repo, message, files) {
  write(repo, files);
  git(repo, 'add', '--all');
  git(repo, 'commit', '--quiet', '--allow-empty', '-m', message);
  return git(repo, 'rev-parse', 'HEAD');
}

const PACKAGES = {
  pkg: {'package-name': 'pkg', component: 'pkg', 'exclude-paths': ['pkg/tests']},
  other: {'package-name': 'other', component: 'other', 'exclude-paths': ['other/tests']},
};

function config(packages = PACKAGES) {
  return JSON.stringify(
    {'release-type': 'python', 'include-component-in-tag': true, packages},
    null,
    2,
  );
}

// A repository whose main carries one released commit (every manifest version
// tagged unless listed in `untagged`), then the `pending` commits that are on
// main but not released yet. Returns the repo and the base sha (main's tip).
function repository({
  packages = PACKAGES,
  manifest = {pkg: '1.2.3', other: '2.0.0'},
  extraFiles = {},
  untagged = [],
  pending = [],
} = {}) {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), 'projection-test-'));
  git(repo, 'init', '--quiet', '--initial-branch=main');
  const files = {
    'release-please-config.json': config(packages),
    '.release-please-manifest.json': JSON.stringify(manifest, null, 2),
    ...extraFiles,
  };
  for (const dir of Object.keys(packages)) files[`${dir}/src/__init__.py`] = '';
  commit(repo, 'chore: release main (#1)', files);
  for (const [dir, version] of Object.entries(manifest)) {
    if (!untagged.includes(dir)) git(repo, 'tag', `${packages[dir].component}-v${version}`);
  }
  const pendingShas = pending.map(([message, changed]) => commit(repo, message, changed));
  return {repo, base: git(repo, 'rev-parse', 'HEAD'), pendingShas};
}

// Lays the pull request's files onto main as one merge commit — the tree a
// `pull_request` run checks out — and projects it.
function project({repo, base}, {headline, body = '', prBody = '', files, number = 7}) {
  const changed = Object.fromEntries(files.map(file => [file, `${file} changed\n`]));
  return projectTree({repo, base}, {headline, body, prBody, number, changed});
}

function projectTree({repo, base}, {headline, body = '', prBody = '', number = 7, changed}) {
  commit(repo, 'Merge pull request into main', changed);
  return run({
    repo,
    repository: REPOSITORY,
    tree: 'HEAD',
    base,
    pullRequest: {number, headline, body, prBody, files: Object.keys(changed)},
  });
}

function paths(outcome) {
  assert.equal(outcome.code, 0, outcome.stderr);
  return JSON.parse(outcome.stdout).paths;
}

test('a fix on a clean main projects a patch for the touched package', async () => {
  const result = paths(await project(repository(), {headline: 'fix(pkg): x (#7)', files: ['pkg/src/a.py']}));
  assert.deepEqual(result, {pkg: {component: 'pkg', version: '1.2.4', touched: true}});
});

test('the output names the release-please version that projected it', async () => {
  const outcome = await project(repository(), {headline: 'fix(pkg): x (#7)', files: ['pkg/src/a.py']});
  assert.equal(outcome.code, 0, outcome.stderr);
  assert.equal(JSON.parse(outcome.stdout).releasePlease, '17.6.0');
});

test('an unreleased breaking fix on main plus a feat pull request projects the major', async () => {
  const repo = repository({pending: [['fix(pkg)!: drop a method (#5)', {'pkg/src/a.py': 'gone\n'}]]});
  const result = paths(await project(repo, {headline: 'feat(pkg): add (#7)', files: ['pkg/src/b.py']}));
  assert.equal(result.pkg.version, '2.0.0');
  assert.equal(result.pkg.touched, true);
});

test('an unreleased feat on main plus a fix pull request projects the minor', async () => {
  const repo = repository({pending: [['feat(pkg): add (#5)', {'pkg/src/a.py': 'new\n'}]]});
  const result = paths(await project(repo, {headline: 'fix(pkg): x (#7)', files: ['pkg/src/b.py']}));
  assert.equal(result.pkg.version, '1.3.0');
});

test('a conventional line in the last paragraph of the body is read as its own commit', async () => {
  const result = paths(
    await project(repository(), {
      headline: 'chore(pkg): bump (#7)',
      body: 'Follow-up.\nfeat: support was added upstream',
      files: ['pkg/src/a.py'],
    }),
  );
  assert.equal(result.pkg.version, '1.3.0');
});

test('a breaking conventional line in the body lifts a fix to a major', async () => {
  const result = paths(
    await project(repository(), {
      headline: 'fix(pkg): tidy (#7)',
      body: 'refactor(pkg)!: remove a method',
      files: ['pkg/src/a.py'],
    }),
  );
  assert.equal(result.pkg.version, '2.0.0');
});

test('Release-As outside the footer paragraph is ignored', async () => {
  const result = paths(
    await project(repository(), {
      headline: 'fix(pkg): x (#7)',
      body: 'Release-As: 5.0.0\n\nmore text',
      files: ['pkg/src/a.py'],
    }),
  );
  assert.equal(result.pkg.version, '1.2.4');
});

test('Release-As as a footer of the squash body forces that version', async () => {
  const result = paths(
    await project(repository(), {
      headline: 'fix(pkg): x (#7)',
      body: 'Some context.\n\nRelease-As: 5.0.0',
      files: ['pkg/src/a.py'],
    }),
  );
  assert.equal(result.pkg.version, '5.0.0');
});

test('BREAKING-CHANGE in the middle of a line projects a major', async () => {
  const result = paths(
    await project(repository(), {
      headline: 'fix(pkg): x (#7)',
      body: 'some text BREAKING-CHANGE: the shape changed',
      files: ['pkg/src/a.py'],
    }),
  );
  assert.equal(result.pkg.version, '2.0.0');
});

test('a capitalised Feat type is not a feature and projects a patch', async () => {
  const result = paths(await project(repository(), {headline: 'Feat(pkg): x (#7)', files: ['pkg/src/a.py']}));
  assert.equal(result.pkg.version, '1.2.4');
});

test('a node release-type package projects as release-please does', async () => {
  const packages = {
    ...PACKAGES,
    web: {'release-type': 'node', 'package-name': 'web', component: 'web'},
  };
  const repo = repository({
    packages,
    manifest: {pkg: '1.2.3', other: '2.0.0', web: '3.0.0'},
    extraFiles: {'web/package.json': JSON.stringify({name: 'web', version: '3.0.0'}, null, 2)},
  });
  const result = paths(await project(repo, {headline: 'feat(web): add (#7)', files: ['web/src/index.js']}));
  assert.deepEqual(result, {web: {component: 'web', version: '3.1.0', touched: true}});
});

test('a commit override in the pull request body replaces the squash message', async () => {
  const result = paths(
    await project(repository(), {
      headline: 'chore(pkg): x (#7)',
      prBody: 'Context.\nBEGIN_COMMIT_OVERRIDE\nfeat(pkg)!: y\nEND_COMMIT_OVERRIDE',
      files: ['pkg/src/a.py'],
    }),
  );
  assert.equal(result.pkg.version, '2.0.0');
});

test('a commit override in the squash body alone does not replace the message', async () => {
  const result = paths(
    await project(repository(), {
      headline: 'chore(pkg): x (#7)',
      body: 'BEGIN_COMMIT_OVERRIDE\nfeat(pkg)!: y\nEND_COMMIT_OVERRIDE\n\nMore context.',
      files: ['pkg/src/a.py'],
    }),
  );
  assert.deepEqual(result, {pkg: {component: 'pkg', version: null, touched: true}});
});

test('a change under exclude-paths only touches nothing and projects nothing', async () => {
  const result = paths(await project(repository(), {headline: 'fix(pkg): x (#7)', files: ['pkg/tests/test_a.py']}));
  assert.deepEqual(result, {});
});

test('a package with pending commits the pull request does not touch is reported untouched', async () => {
  const repo = repository({pending: [['fix(other): y (#5)', {'other/src/x.py': 'y\n'}]]});
  const result = paths(await project(repo, {headline: 'fix(pkg): x (#7)', files: ['pkg/src/a.py']}));
  assert.deepEqual(result, {
    pkg: {component: 'pkg', version: '1.2.4', touched: true},
    other: {component: 'other', version: '2.0.1', touched: false},
  });
});

test('a release-as in the config forces that version', async () => {
  const packages = {...PACKAGES, pkg: {...PACKAGES.pkg, 'release-as': '7.0.0'}};
  const result = paths(await project(repository({packages}), {headline: 'fix(pkg): x (#7)', files: ['pkg/src/a.py']}));
  assert.equal(result.pkg.version, '7.0.0');
});

test('a manifest version with no release tag fails loudly', async () => {
  const outcome = await project(repository({untagged: ['other']}), {
    headline: 'fix(pkg): x (#7)',
    files: ['pkg/src/a.py'],
  });
  assert.equal(outcome.code, 1);
  assert.match(
    outcome.stderr,
    /release-label-check: other is at 2\.0\.0 in the manifest but no tag other-v2\.0\.0 exists; the projection has no baseline/,
  );
});

test('a pull request that edits the release-please config is projected with its edit', async () => {
  const packages = {...PACKAGES, pkg: {...PACKAGES.pkg, 'exclude-paths': ['pkg/tests', 'pkg/docs']}};
  const result = paths(
    await projectTree(repository(), {
      headline: 'fix(pkg): x (#7)',
      changed: {'release-please-config.json': config(packages), 'pkg/docs/guide.md': 'text\n'},
    }),
  );
  assert.deepEqual(result, {});
});

test("the pull request's own commits under the merge commit are not counted", async () => {
  const {repo, base} = repository();
  git(repo, 'checkout', '--quiet', '-b', 'topic');
  commit(repo, 'feat(pkg)!: an intermediate step', {'pkg/src/a.py': 'step\n'});
  git(repo, 'checkout', '--quiet', 'main');
  git(repo, 'merge', '--quiet', '--no-ff', '-m', 'Merge topic into main', 'topic');
  const outcome = await run({
    repo,
    repository: REPOSITORY,
    tree: 'HEAD',
    base,
    pullRequest: {number: 7, headline: 'fix(pkg): x (#7)', body: '', prBody: '', files: ['pkg/src/a.py']},
  });
  assert.deepEqual(paths(outcome), {pkg: {component: 'pkg', version: '1.2.4', touched: true}});
});

test('a request that is not valid JSON fails loudly', async () => {
  const result = await run('not json');
  assert.equal(result.code, 1);
  assert.match(result.stderr, /JSON/);
});

test("a pending commit's pull request body override counts as release-please counts it", async () => {
  const repo = repository({pending: [['chore(pkg): rework (#5)', {'pkg/src/a.py': 'rework\n'}]]});
  const [sha] = repo.pendingShas;
  github.pullRequests[sha] = [
    mergedPullRequest(5, sha, {body: 'Context.\nBEGIN_COMMIT_OVERRIDE\nfeat(pkg)!: rework the api\nEND_COMMIT_OVERRIDE'}),
  ];
  const result = paths(await project(repo, {headline: 'fix(pkg): tidy (#7)', files: ['pkg/src/b.py']}));
  assert.equal(result.pkg.version, '2.0.0');
});

test('an override block in a pending commit message without a pull request is not applied', async () => {
  const message = 'chore(pkg): rework (#5)\n\nBEGIN_COMMIT_OVERRIDE\nfeat(pkg)!: rework the api\nEND_COMMIT_OVERRIDE\n\nMore.';
  const repo = repository({pending: [[message, {'pkg/src/a.py': 'rework\n'}]]});
  const result = paths(await project(repo, {headline: 'fix(pkg): tidy (#7)', files: ['pkg/src/b.py']}));
  assert.equal(result.pkg.version, '1.2.4');
});

test('a pull request associated with a commit it did not merge lends its body', async () => {
  // release-please falls back to the first associated pull request when none
  // merged the commit as its single merge commit.
  const repo = repository({pending: [['chore(pkg): rework', {'pkg/src/a.py': 'rework\n'}]]});
  const [sha] = repo.pendingShas;
  github.pullRequests[sha] = [
    mergedPullRequest(5, 'f'.repeat(40), {body: 'BEGIN_COMMIT_OVERRIDE\nfeat(pkg): add\nEND_COMMIT_OVERRIDE'}),
  ];
  const result = paths(await project(repo, {headline: 'fix(pkg): tidy (#7)', files: ['pkg/src/b.py']}));
  assert.equal(result.pkg.version, '1.3.0');
});

test('pending commits are looked up with the token, in pages of ten', async () => {
  const pending = Array.from({length: 12}, (_, n) => [`fix(other): step ${n}`, {'other/src/x.py': `${n}\n`}]);
  const repo = repository({pending});
  paths(await project(repo, {headline: 'fix(pkg): x (#7)', files: ['pkg/src/a.py']}));
  assert.ok(github.requests.length >= 2);
  for (const request of github.requests) {
    assert.equal(request.path, '/graphql');
    assert.equal(request.authorization, `token ${TOKEN}`);
    assert.equal(request.variables.owner, 'acme');
    assert.equal(request.variables.repo, 'widgets');
  }
  const looked = github.requests.flatMap(r => Object.keys(r.variables).filter(k => /^c\d+$/.test(k)).map(k => r.variables[k]));
  for (const sha of repo.pendingShas) assert.ok(looked.includes(sha), sha);
  assert.ok(github.requests.every(r => Object.keys(r.variables).filter(k => /^c\d+$/.test(k)).length <= 10));
});

test('a failed pull request lookup fails loudly', async () => {
  const repo = repository({pending: [['fix(other): y', {'other/src/x.py': 'y\n'}]]});
  github.fail = {status: 200, body: {errors: [{message: 'Resource not accessible by integration'}]}};
  const outcome = await project(repo, {headline: 'fix(pkg): x (#7)', files: ['pkg/src/a.py']});
  assert.equal(outcome.code, 1);
  assert.match(outcome.stderr, /Resource not accessible by integration/);
});

test('a projection without a token fails loudly', async () => {
  const {repo, base} = repository();
  const outcome = await run(
    {repo, repository: REPOSITORY, tree: 'HEAD', base, pullRequest: {number: 7, headline: 'fix(pkg): x', body: '', prBody: '', files: []}},
    {GITHUB_TOKEN: ''},
  );
  assert.equal(outcome.code, 1);
  assert.match(outcome.stderr, /GITHUB_TOKEN/);
});

test('releases are found from their tags without a missing-release warning', async () => {
  const outcome = await project(repository(), {headline: 'fix(pkg): x (#7)', files: ['pkg/src/a.py']});
  assert.equal(outcome.code, 0, outcome.stderr);
  assert.doesNotMatch(outcome.stderr, /Expected \d+ releases/);
  assert.doesNotMatch(outcome.stderr, /Missing \d+ paths/);
  // release-please's own warnings still reach stderr.
  assert.match(outcome.stderr, /release-please: .*pyproject\.toml did not exist/);
});

test('a package the pull request adds is projected as its first release', async () => {
  const packages = {...PACKAGES, fresh: {'package-name': 'fresh', component: 'fresh'}};
  const result = paths(
    await projectTree(repository(), {
      headline: 'feat(fresh): a new package (#7)',
      changed: {
        'release-please-config.json': config(packages),
        '.release-please-manifest.json': JSON.stringify({pkg: '1.2.3', other: '2.0.0', fresh: '0.1.0'}),
        'fresh/src/__init__.py': '',
      },
    }),
  );
  // No release tag yet: release-please bumps from the manifest version.
  assert.deepEqual(result, {fresh: {component: 'fresh', version: '0.2.0', touched: true}});
});
