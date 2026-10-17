#!/usr/bin/env node
// The head branch names release-please opens its release pull request from,
// for the release-please config at a commit.
//
// Reads one JSON request on stdin: {"repo": <checkout path>, "base": <sha>,
// "baseBranch": <the release pull request's target branch>}. Prints
// {"branches": [<name>]}.
//
// Release-please merges every package into one pull request on its default
// branch unless it opens separate ones (`separate-pull-requests`, globally or on
// a package, or a config with a single package); separate pull requests are
// refused, since the release-label check gates the combined one.

const {Manifest, setLogger} = require('release-please');
const {LocalGitHub} = require('release-please/build/src/local-github');
const {BranchName} = require('release-please/build/src/util/branch-name');

const logger = {
  error: (...args) => console.error('release-please:', ...args),
  warn: (...args) => console.error('release-please:', ...args),
  info: () => {},
  debug: () => {},
  trace: () => {},
};

async function trainBranches({repo, base, baseBranch}) {
  if (!baseBranch) throw new Error('release-label-check: baseBranch is required');
  setLogger(logger);
  const scm = new LocalGitHub({owner: 'local', repo: 'projection', defaultBranch: base}, {}, repo, {logger});
  const manifest = await Manifest.fromManifest(scm, base, undefined, undefined, {logger});
  const separate = Object.keys(manifest.repositoryConfig).filter(
    path => manifest.repositoryConfig[path].separatePullRequests,
  );
  if (manifest.separatePullRequests || separate.length) {
    const which = separate.length ? ` (${separate.join(', ')})` : '';
    throw new Error(
      `release-label-check: the release-please config opens separate release pull requests${which}; ` +
        'this check gates the one combined release pull request only',
    );
  }
  return {branches: [BranchName.ofTargetBranch(baseBranch).toString()]};
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
    .then(text => trainBranches(JSON.parse(text)))
    .then(result => process.stdout.write(`${JSON.stringify(result)}\n`))
    .catch(error => {
      console.error(error.stack || String(error));
      process.exitCode = 1;
    });
}

module.exports = {trainBranches};
