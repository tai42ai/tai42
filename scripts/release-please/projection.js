#!/usr/bin/env node
// What release-please would mint if a pull request were squash-merged.
//
// Reads one JSON request on stdin:
//   {"repo": <checkout path>, "repository": <owner/name>,
//    "tree": <ref of main with the pull request applied>, "base": <base sha>,
//    "pullRequest": {"number", "headline", "body", "prBody", "files"}}
// `headline` and `body` are the squash commit GitHub will write; `prBody` is the
// pull request's own body, where release-please reads BEGIN_COMMIT_OVERRIDE.
//
// release-please runs over the local clone: its commit history is the squash
// commit followed by `base` and its ancestors, every file it reads (config,
// manifest, version files) comes from `tree`, and its releases are the local
// release tags. Each commit of that history carries the pull request GitHub
// associates with it, read with GITHUB_TOKEN from GITHUB_GRAPHQL_URL (default
// github.com) and chosen the way release-please's GitHub client chooses it.
// Prints, for every package that has a version or that the pull request touches:
//   {"releasePlease": <version>, "paths": {<path>: {"component", "version"|null, "touched"}}}

const {execFile} = require('node:child_process');
const {promisify} = require('node:util');
const {GitHub, Manifest, VERSION, setLogger} = require('release-please');
const {LocalGitHub} = require('release-please/build/src/local-github');
const {CommitSplit} = require('release-please/build/src/util/commit-split');
const {CommitExclude} = require('release-please/build/src/util/commit-exclude');
const {TagName} = require('release-please/build/src/util/tag-name');
const {registerPythonWorkspace} = require('./python-workspace');

const run = promisify(execFile);

// release-please logs progress at info and debug; warnings go to stderr so they
// show in the job log, the rest is dropped to keep stdout the JSON result.
const logger = {
  error: (...args) => console.error('release-please:', ...args),
  warn: (...args) => console.error('release-please:', ...args),
  info: () => {},
  debug: () => {},
  trace: () => {},
};

// The pull request fields release-please's GitHub client reads per commit.
const PULL_REQUEST_FIELDS = `associatedPullRequests(first: 10) {
  nodes { number title baseRefName headRefName body mergeCommit { oid } labels(first: 10) { nodes { name } } }
}`;

// Returns a lookup from a page of commit shas to each one's associated pull requests.
async function gitHubPullRequests(repository) {
  const token = process.env.GITHUB_TOKEN;
  if (!token) throw new Error('release-label-check: GITHUB_TOKEN is required to read the pending pull requests');
  const [owner, repo, ...rest] = (repository || '').split('/');
  if (!owner || !repo || rest.length) throw new Error(`release-label-check: repository must be owner/name, got ${repository}`);
  const graphqlUrl = process.env.GITHUB_GRAPHQL_URL;
  const github = await GitHub.create({
    owner,
    repo,
    token,
    defaultBranch: 'main',
    graphqlUrl: graphqlUrl ? graphqlUrl.replace(/\/graphql$/, '') : undefined,
    logger,
  });
  return async shas => {
    const declared = shas.map((_, n) => `$c${n}: GitObjectID!`).join(', ');
    const fields = shas.map((_, n) => `c${n}: object(oid: $c${n}) { ... on Commit { ${PULL_REQUEST_FIELDS} } }`).join('\n');
    const query = `query($owner: String!, $repo: String!, ${declared}) { repository(owner: $owner, name: $repo) { ${fields} } }`;
    const variables = Object.fromEntries(shas.map((sha, n) => [`c${n}`, sha]));
    const response = await github.graphqlRequest({query, owner, repo, ...variables});
    if (!response) throw new Error('release-label-check: GitHub returned no pull requests for the pending commits');
    return shas.map((sha, n) => {
      const object = response.repository[`c${n}`];
      if (!object) throw new Error(`release-label-check: commit ${sha} is not on GitHub`);
      return object.associatedPullRequests.nodes;
    });
  };
}

// The pull request release-please attaches to a commit: the one that merged it
// as a single merge commit within the page, else the first associated one.
function attachPullRequests(page, pullRequestsPerCommit) {
  const mergeCommitCount = {};
  for (const nodes of pullRequestsPerCommit) {
    for (const pr of nodes) {
      if (pr.mergeCommit?.oid) mergeCommitCount[pr.mergeCommit.oid] = (mergeCommitCount[pr.mergeCommit.oid] || 0) + 1;
    }
  }
  return page.map((commit, n) => {
    const nodes = pullRequestsPerCommit[n];
    const merged = nodes.find(
      pr => pr.mergeCommit && pr.mergeCommit.oid === commit.sha && mergeCommitCount[pr.mergeCommit.oid] === 1,
    );
    const pr = merged || nodes[0];
    const {pullRequest: _localGuess, ...attached} = commit;
    if (!pr) return attached;
    return {
      ...attached,
      pullRequest: {
        sha: commit.sha,
        number: pr.number,
        baseBranchName: pr.baseRefName,
        headBranchName: pr.headRefName,
        mergeCommitOid: pr.mergeCommit?.oid,
        title: pr.title,
        body: pr.body,
        labels: pr.labels.nodes.map(node => node.name),
        files: commit.files,
      },
    };
  });
}

class ProjectionScm extends LocalGitHub {
  constructor(repoDir, tree, base, squashCommit, pullRequests) {
    super({owner: 'local', repo: 'projection', defaultBranch: tree}, {}, repoDir, {logger});
    this.base = base;
    this.squashCommit = squashCommit;
    this.pullRequests = pullRequests;
  }

  // The release tags stand in for GitHub releases, newest first as GitHub lists them.
  async *releaseIterator(options) {
    const {stdout} = await run(
      'git',
      ['for-each-ref', '--sort=-creatordate', 'refs/tags', '--format=%(refname:short)|%(objectname)|%(*objectname)'],
      {cwd: this.cloneDir, maxBuffer: 100 * 1024 * 1024},
    );
    const limit = options?.maxResults || Number.MAX_SAFE_INTEGER;
    let count = 0;
    for (const line of stdout.split('\n')) {
      if (!line || count >= limit) continue;
      const [name, objectSha, commitSha] = line.split('|');
      count += 1;
      yield {name, tagName: name, sha: commitSha || objectSha, notes: ''};
    }
  }

  // `tree` is the merge commit, whose history also holds the pull request's own
  // unsquashed commits; release-please must see the squash commit on top of base.
  // Base's commits are read in release-please's pages, each with its pull requests.
  async *mergeCommitIterator(_target, options) {
    yield this.squashCommit;
    const size = options?.batchSize || 10;
    let page = [];
    for await (const commit of super.mergeCommitIterator(this.base, options)) {
      page.push(commit);
      if (page.length === size) {
        yield* attachPullRequests(page, await this.pullRequests(page.map(c => c.sha)));
        page = [];
      }
    }
    if (page.length) yield* attachPullRequests(page, await this.pullRequests(page.map(c => c.sha)));
  }
}

function squashCommit({number, headline, body, prBody, files}, base) {
  return {
    sha: 'pull-request',
    message: body ? `${headline}\n\n${body}` : headline,
    files,
    pullRequest: {
      sha: 'pull-request',
      number,
      title: headline,
      body: prBody,
      labels: [],
      files,
      baseBranchName: base,
      headBranchName: '',
    },
  };
}

// A version released before this pull request without its tag would put
// release-please in bootstrap mode, counting every commit in its search window.
// A package the pull request adds to the manifest has no release yet.
async function requireReleaseTags(manifest, strategiesByPath, baseVersions) {
  const released = Object.keys(strategiesByPath).filter(path => {
    const version = manifest.releasedVersions[path];
    return version && version.toString() !== '0.0.0' && path in baseVersions;
  });
  const found = await manifest.backfillReleasesFromTags(released, strategiesByPath);
  for (const path of released) {
    if (found[path]) continue;
    const version = manifest.releasedVersions[path];
    const config = manifest.repositoryConfig[path];
    const component = await strategiesByPath[path].getComponent();
    const tag = new TagName(version, component, config.tagSeparator, config.includeVInTag);
    throw new Error(
      `release-label-check: ${path} is at ${version} in the manifest but no tag ${tag} exists; ` +
        'the projection has no baseline',
    );
  }
}

async function project({repo, repository, tree, base, pullRequest}) {
  setLogger(logger);
  // `tree` is the checkout at `repo`, so the config's `python-workspace` plugin
  // queries the ranges of the tree being projected.
  registerPythonWorkspace({root: repo});
  const commit = squashCommit(pullRequest, base);
  const scm = new ProjectionScm(repo, tree, base, commit, await gitHubPullRequests(repository));
  const manifest = await Manifest.fromManifest(scm, tree, undefined, undefined, {logger});
  const strategiesByPath = await manifest.getStrategiesByPath();
  const baseVersions = await scm.getFileJson(manifest.manifestPath, base);
  await requireReleaseTags(manifest, strategiesByPath, baseVersions);

  const versions = {};
  for (const pr of await manifest.buildPullRequests()) {
    for (const release of pr.body.releaseData) {
      versions[release.component] = release.version.toString();
    }
  }

  const config = manifest.repositoryConfig;
  const packagePaths = Object.keys(config);
  const split = new CommitSplit({includeEmpty: true, packagePaths}).split([commit]);
  const perPath = Object.fromEntries(packagePaths.map(path => [path, split[path] || []]));
  const kept = new CommitExclude(config).excludeCommits(perPath);

  const paths = {};
  for (const path of packagePaths) {
    const component = await strategiesByPath[path].getComponent();
    const version = versions[component] || null;
    const touched = kept[path].length > 0;
    if (version || touched) paths[path] = {component, version, touched};
  }
  return {releasePlease: VERSION, paths};
}

function readStdin() {
  return new Promise((resolve, reject) => {
    const chunks = [];
    process.stdin.on('data', chunk => chunks.push(chunk));
    process.stdin.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    process.stdin.on('error', reject);
  });
}

if (require.main === module) {
  readStdin()
    .then(text => project(JSON.parse(text)))
    .then(result => process.stdout.write(`${JSON.stringify(result)}\n`))
    .catch(error => {
      console.error(error.stack || String(error));
      process.exitCode = 1;
    });
}

module.exports = {project};
