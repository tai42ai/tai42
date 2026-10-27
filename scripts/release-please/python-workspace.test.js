// Tests for python-workspace.js. Each release test builds a throw-away git
// repository with a release-please config naming the plugin, a manifest, release
// tags and pending commits, then builds the release pull request with
// release-please's own Manifest over the local clone. The range query is a stub
// that records what it is asked; the last test asks the real range_sync.py.

const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {after, test} = require('node:test');
const {Manifest, setLogger} = require('release-please');
const {LocalGitHub} = require('release-please/build/src/local-github');

const {PythonWorkspace, registerPythonWorkspace} = require('./python-workspace');

const WORKSPACE_ROOT = path.resolve(__dirname, '..', '..');

const silent = {error: () => {}, warn: () => {}, info: () => {}, debug: () => {}, trace: () => {}};
setLogger(silent);

// The local clone, with its release tags standing in for GitHub releases and no
// release pull requests merged before.
class TagReleases extends LocalGitHub {
  async *releaseIterator() {
    const refs = execFileSync('git', ['for-each-ref', 'refs/tags', '--format=%(refname:short)|%(objectname)'], {
      cwd: this.cloneDir,
      encoding: 'utf8',
    });
    for (const line of refs.split('\n')) {
      if (!line) continue;
      const [name, sha] = line.split('|');
      yield {name, tagName: name, sha, notes: ''};
    }
  }

  async *pullRequestIterator() {}
}

const VERSIONS = {core: '1.0.0', 'dep-a': '2.0.0', 'dep-b': '3.1.0', leaf: '0.5.0'};

const repositories = [];
after(() => {
  for (const repo of repositories) fs.rmSync(repo, {recursive: true, force: true});
});

// A repository released at VERSIONS (each tagged), then the `pending` commits.
function repository(pending) {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), 'python-workspace-test-'));
  repositories.push(repo);
  const git = (...args) =>
    execFileSync('git', ['-c', 'user.name=Alice', '-c', 'user.email=alice@example.com', '-c', 'commit.gpgsign=false', ...args], {
      cwd: repo,
      encoding: 'utf8',
    }).trim();
  const write = files => {
    for (const [file, content] of Object.entries(files)) {
      fs.mkdirSync(path.dirname(path.join(repo, file)), {recursive: true});
      fs.writeFileSync(path.join(repo, file), content);
    }
  };
  const packages = Object.fromEntries(Object.keys(VERSIONS).map(name => [name, {'package-name': name, component: name}]));
  write({
    'release-please-config.json': JSON.stringify({
      'release-type': 'python',
      'include-component-in-tag': true,
      'draft-pull-request': true,
      plugins: [{type: 'python-workspace'}],
      packages,
    }),
    '.release-please-manifest.json': JSON.stringify(VERSIONS),
    ...Object.fromEntries(
      Object.entries(VERSIONS).map(([name, version]) => [
        `${name}/pyproject.toml`,
        `[project]\nname = "${name}"\nversion = "${version}"\n`,
      ]),
    ),
  });
  git('init', '--quiet', '--initial-branch=main');
  git('add', '--all');
  git('commit', '--quiet', '-m', 'chore: initial');
  for (const [name, version] of Object.entries(VERSIONS)) git('tag', `${name}-v${version}`);
  for (const {message, files} of pending) {
    write(files);
    git('add', '--all');
    git('commit', '--quiet', '-m', message);
  }
  return repo;
}

// The stub query: `answer(versions)` is the members list; every call is recorded.
function stubQuery(answer) {
  const calls = [];
  const query = async versions => {
    calls.push(versions);
    return answer(versions);
  };
  return {calls, query};
}

const CORE_RANGE = {file: 'dep-a/pyproject.toml', subject: 'core', from: 'core>=1.0,<2', to: 'core>=2.0,<3'};

async function releasePullRequest(repo, query) {
  registerPythonWorkspace({root: repo, query});
  const scm = new TagReleases({owner: 'local', repo: 'test', defaultBranch: 'main'}, {}, repo, {logger: silent});
  const manifest = await Manifest.fromManifest(scm, 'main', undefined, undefined, {logger: silent});
  const prs = await manifest.buildPullRequests();
  assert.equal(prs.length, 1);
  return prs[0];
}

function released(pr) {
  return Object.fromEntries(pr.body.releaseData.map(data => [data.component, data.version.toString()]));
}

// The manifest the release pull request writes.
function manifestVersions(pr) {
  const update = pr.updates.find(u => u.path === '.release-please-manifest.json');
  return JSON.parse(update.updater.updateContent(JSON.stringify(VERSIONS)));
}

const CORE_MAJOR = [{message: 'feat!: break core', files: {'core/api.py': 'x = 1\n'}}];

test('a core major releases exactly the members the query names, each as a patch', async () => {
  const repo = repository(CORE_MAJOR);
  const {calls, query} = stubQuery(() => [{path: 'dep-a', changes: [CORE_RANGE]}]);
  const pr = await releasePullRequest(repo, query);
  assert.deepEqual(released(pr), {core: '2.0.0', 'dep-a': '2.0.1'});
  assert.deepEqual(calls, [{core: '2.0.0'}, {core: '2.0.0', 'dep-a': '2.0.1'}]);
  assert.equal(manifestVersions(pr)['dep-a'], '2.0.1');
  const notes = pr.body.releaseData.find(data => data.component === 'dep-a').notes;
  assert.match(notes, /### Dependencies/);
  assert.match(notes, /dep-a\/pyproject\.toml: `core>=1\.0,<2` → `core>=2\.0,<3`/);
  const versionUpdate = pr.updates.find(u => u.path === 'dep-a/pyproject.toml');
  assert.ok(versionUpdate, 'the forced release bumps its pyproject version');
  assert.equal(pr.draft, true, 'the release pull request stays a draft as configured');
});

test('a release that moves no range adds nothing', async () => {
  const repo = repository([{message: 'fix: patch core', files: {'core/api.py': 'x = 2\n'}}]);
  const {calls, query} = stubQuery(() => []);
  const pr = await releasePullRequest(repo, query);
  assert.deepEqual(released(pr), {core: '1.0.1'});
  assert.deepEqual(calls, [{core: '1.0.1'}]);
});

test('a member already in the release is not released twice and carries the range notes', async () => {
  const repo = repository([...CORE_MAJOR, {message: 'feat: grow dep-b', files: {'dep-b/api.py': 'y = 1\n'}}]);
  const depB = {file: 'dep-b/pyproject.toml', subject: 'core', from: 'core>=1.0,<2', to: 'core>=2.0,<3'};
  const {calls, query} = stubQuery(() => [
    {path: 'dep-a', changes: [CORE_RANGE]},
    {path: 'dep-b', changes: [depB]},
  ]);
  const pr = await releasePullRequest(repo, query);
  assert.deepEqual(
    pr.body.releaseData.map(data => data.component).sort(),
    ['core', 'dep-a', 'dep-b'],
  );
  assert.deepEqual(released(pr), {core: '2.0.0', 'dep-a': '2.0.1', 'dep-b': '3.2.0'});
  assert.deepEqual(calls[0], {core: '2.0.0', 'dep-b': '3.2.0'});
  assert.match(pr.body.releaseData.find(data => data.component === 'dep-b').notes, /dep-b\/pyproject\.toml/);
});

test('a unit with moved ranges outside the release-please config is refused loudly', async () => {
  const repo = repository(CORE_MAJOR);
  const {query} = stubQuery(() => [{path: 'stray', changes: [CORE_RANGE]}]);
  await assert.rejects(releasePullRequest(repo, query), /stray is not a package of the release-please config/);
});

test('a failing query fails the release', async () => {
  const repo = repository(CORE_MAJOR);
  const query = async () => {
    throw new Error('query refused');
  };
  await assert.rejects(releasePullRequest(repo, query), /query refused/);
});

test('the default query asks range_sync.py over the workspace root', async () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(WORKSPACE_ROOT, '.release-please-manifest.json'), 'utf8'));
  const plugin = new PythonWorkspace(undefined, 'main', {}, {root: WORKSPACE_ROOT, logger: silent});
  assert.deepEqual(await plugin.query({'core/contract': manifest['core/contract']}), []);
  const major = `${Number(manifest['core/contract'].split('.')[0]) + 1}.0.0`;
  const members = await plugin.query({'core/contract': major});
  assert.ok(members.some(member => member.path === 'core/kit'));
  await assert.rejects(plugin.query({'core/nonesuch': '1.0.0'}), /range_sync\.py --query exited 1/);
});

test('a workspace root or a query is required', () => {
  assert.throws(() => new PythonWorkspace(undefined, 'main', {}, {logger: silent}), /a workspace root is required/);
});
