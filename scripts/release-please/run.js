#!/usr/bin/env node
// Mint releases with release-please: create the GitHub releases and tags for a
// merged release pull request, then open or update the release pull request.
//
//   node run.js --repo-url <owner/repo> --target-branch <branch>
//               [--token <token>] [--config-file <path>] [--manifest-file <path>]
//               [--api-url <url>] [--graphql-url <url>] [--dry-run]
//
// The token defaults to RELEASE_PLEASE_TOKEN; the API URLs to the runner's
// GITHUB_API_URL / GITHUB_GRAPHQL_URL, else github.com. Prints
//   {"releases": [{"tagName", "sha", "path"}], "prs": [{"number", "headBranchName"}]}
// With --dry-run nothing is written: the would-be releases and release pull
// requests are printed instead, each pull request with its title, branch and
// releaseData. Log lines go to stderr.

const {parseArgs} = require('node:util');
const {GitHub, Manifest, setLogger} = require('release-please');

const logger = {
  error: (...args) => console.error(...args),
  warn: (...args) => console.error(...args),
  info: (...args) => console.error(...args),
  debug: (...args) => console.error(...args),
  trace: () => {},
};

function options(argv) {
  const {values} = parseArgs({
    args: argv,
    options: {
      'repo-url': {type: 'string'},
      'target-branch': {type: 'string'},
      token: {type: 'string'},
      'config-file': {type: 'string', default: 'release-please-config.json'},
      'manifest-file': {type: 'string', default: '.release-please-manifest.json'},
      'api-url': {type: 'string'},
      'graphql-url': {type: 'string'},
      'dry-run': {type: 'boolean', default: false},
    },
    strict: true,
  });
  if (!values['repo-url']) throw new Error('--repo-url <owner/repo> is required');
  const [owner, repo, ...rest] = values['repo-url'].split('/');
  if (!owner || !repo || rest.length) throw new Error(`--repo-url must be owner/repo, got ${values['repo-url']}`);
  if (!values['target-branch']) throw new Error('--target-branch is required');
  const token = values.token || process.env.RELEASE_PLEASE_TOKEN;
  if (!token) throw new Error('a token is required: --token or RELEASE_PLEASE_TOKEN');
  const graphqlUrl = values['graphql-url'] || process.env.GITHUB_GRAPHQL_URL;
  return {
    owner,
    repo,
    token,
    targetBranch: values['target-branch'],
    configFile: values['config-file'],
    manifestFile: values['manifest-file'],
    apiUrl: values['api-url'] || process.env.GITHUB_API_URL || undefined,
    // release-please appends /graphql itself.
    graphqlUrl: graphqlUrl ? graphqlUrl.replace(/\/graphql$/, '') : undefined,
    dryRun: values['dry-run'],
  };
}

// Returns a loader that reads a fresh Manifest from the target branch on each
// call, over one GitHub client.
function gitHubManifests(opts) {
  let github;
  return async () => {
    github ??= await GitHub.create({
      owner: opts.owner,
      repo: opts.repo,
      token: opts.token,
      defaultBranch: opts.targetBranch,
      apiUrl: opts.apiUrl,
      graphqlUrl: opts.graphqlUrl,
      logger,
    });
    return Manifest.fromManifest(github, github.repository.defaultBranch, opts.configFile, opts.manifestFile);
  };
}

async function main(argv, manifests = gitHubManifests, out = process.stdout) {
  const opts = options(argv);
  const load = manifests(opts);
  let result;
  if (opts.dryRun) {
    const releases = await (await load()).buildReleases();
    const prs = await (await load()).buildPullRequests();
    result = {
      releases: releases.map(release => ({tagName: release.tag.toString(), sha: release.sha, path: release.path})),
      prs: prs.map(pr => ({
        title: pr.title.toString(),
        headBranchName: pr.headRefName,
        releaseData: pr.body.releaseData.map(data => ({component: data.component, version: data.version.toString()})),
      })),
    };
  } else {
    // Releases before pull requests: a merged release pull request is tagged
    // before the next one is opened.
    const releases = (await (await load()).createReleases()).filter(Boolean);
    const prs = (await (await load()).createPullRequests()).filter(Boolean);
    result = {
      releases: releases.map(release => ({tagName: release.tagName, sha: release.sha, path: release.path})),
      prs: prs.map(pr => ({number: pr.number, headBranchName: pr.headBranchName})),
    };
  }
  out.write(`${JSON.stringify(result)}\n`);
}

if (require.main === module) {
  setLogger(logger);
  main(process.argv.slice(2)).catch(error => {
    console.error(error.stack || String(error));
    process.exitCode = 1;
  });
}

module.exports = {main};
