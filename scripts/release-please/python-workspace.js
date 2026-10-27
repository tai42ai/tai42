// The `python-workspace` release-please plugin: a release that moves a
// workspace member's version also releases every member whose derived
// first-party ranges or `contract:` pins that version moves.
//
// The range rule lives in scripts/range_sync.py alone; this plugin asks it,
// through `range_sync.py --query`, which release units' ranges the candidate
// versions move, and adds each one not already in the release as a forced release (a
// patch bump with no commits, release-please's default for a release without
// commits). A patch bump never moves a derived range (the floor is
// major.minor), so the query settles after one round; it is asked again with
// the forced versions until it names no new unit, so a versioning strategy
// that bumps further is carried too. The plugin writes no range: the release
// pull request's follow-up commit runs `range_sync.py`, which writes the same
// ranges the query reported. Each such release's notes list the ranges it
// carries.
//
// The query reads the workspace checkout at `root` (Python 3.11+ as `python3`
// on PATH), which is the tree release-please builds the release from: the
// target branch's checkout for a release run, the projected tree for the
// release-label check.

const {spawn} = require('node:child_process');
const path = require('node:path');
const {registerPlugin} = require('release-please');
const {WorkspacePlugin, appendDependenciesSectionToChangelog} = require('release-please/build/src/plugins/workspace');
const {Changelog} = require('release-please/build/src/updaters/changelog');
const {Version} = require('release-please/build/src/version');

const PLUGIN_TYPE = 'python-workspace';

// Runs `range_sync.py --query` over the checkout at `root` with the candidate
// versions ({<unit path>: <version>}) on stdin; resolves to its `members` list.
function rangeSyncQuery(root) {
  const script = path.join(root, 'scripts', 'range_sync.py');
  return versions =>
    new Promise((resolve, reject) => {
      const child = spawn('python3', [script, '--query', '--root', root], {stdio: ['pipe', 'pipe', 'pipe']});
      const stdout = [];
      const stderr = [];
      child.stdout.on('data', chunk => stdout.push(chunk));
      child.stderr.on('data', chunk => stderr.push(chunk));
      child.on('error', reject);
      child.on('close', code => {
        if (code !== 0) {
          reject(new Error(`${PLUGIN_TYPE}: range_sync.py --query exited ${code}:\n${Buffer.concat(stderr)}`));
          return;
        }
        try {
          resolve(JSON.parse(Buffer.concat(stdout).toString('utf8')).members);
        } catch (error) {
          reject(new Error(`${PLUGIN_TYPE}: range_sync.py --query printed no JSON result: ${error.message}`));
        }
      });
      child.stdin.end(JSON.stringify(versions));
    });
}

// The release-notes lines for one unit's range changes.
function rangeNotes(changes) {
  return changes.map(change => `* ${change.file}: \`${change.from}\` → \`${change.to}\``).join('\n');
}

class PythonWorkspace extends WorkspacePlugin {
  // `options.query` maps candidate versions to the units whose ranges they move; it defaults
  // to `range_sync.py --query` over `options.root`.
  constructor(github, targetBranch, repositoryConfig, options = {}) {
    super(github, targetBranch, repositoryConfig, options);
    if (!options.query && !options.root) throw new Error(`${PLUGIN_TYPE}: a workspace root is required`);
    this.query = options.query ?? rangeSyncQuery(options.root);
    this.strategiesByPath = {};
    this.releasesByPath = {};
    this.rangeChanges = new Map();
  }

  async preconfigure(strategiesByPath, _commitsByPath, releasesByPath) {
    this.strategiesByPath = strategiesByPath;
    this.releasesByPath = releasesByPath;
    return strategiesByPath;
  }

  // Every package of the manifest is a workspace release unit.
  inScope(_candidate) {
    return true;
  }

  packageNameFromPackage(pkg) {
    return pkg.name;
  }

  pathFromPackage(pkg) {
    return pkg.path;
  }

  // The package at `unitPath`, at `version`.
  async packageAt(unitPath, version) {
    const strategy = this.strategiesByPath[unitPath];
    if (!strategy) throw new Error(`${PLUGIN_TYPE}: ${unitPath} is not a package of the release-please config`);
    return {name: await strategy.getComponent(), path: unitPath, version: version.toString()};
  }

  // A unit the release carries for its ranges alone, at its latest release.
  async releasedPackageAt(unitPath) {
    if (!this.strategiesByPath[unitPath]) {
      throw new Error(`${PLUGIN_TYPE}: ${unitPath} is not a package of the release-please config`);
    }
    const latestRelease = this.releasesByPath[unitPath];
    if (!latestRelease) throw new Error(`${PLUGIN_TYPE}: ${unitPath} has moved ranges but no release to bump from`);
    return this.packageAt(unitPath, latestRelease.tag.version);
  }

  // The candidates' packages plus every unit whose ranges the query says their
  // versions move, asked again with each forced version until no new unit appears.
  async buildAllPackages(candidates) {
    const candidatesByPackage = {};
    const packagesByPath = new Map();
    const versions = new Map();
    for (const candidate of candidates) {
      const pkg = await this.packageAt(candidate.path, candidate.pullRequest.version);
      packagesByPath.set(candidate.path, pkg);
      candidatesByPackage[pkg.name] = candidate;
      versions.set(candidate.path, candidate.pullRequest.version);
    }
    for (;;) {
      const members = await this.query(Object.fromEntries([...versions].map(([unit, v]) => [unit, v.toString()])));
      this.rangeChanges = new Map(members.map(member => [member.path, member.changes]));
      const added = members.filter(member => !versions.has(member.path));
      if (!added.length) break;
      for (const member of added) {
        const pkg = await this.releasedPackageAt(member.path);
        this.logger.info(`${PLUGIN_TYPE}: ${member.path} has ranges this release moves; releasing it too`);
        packagesByPath.set(member.path, pkg);
        versions.set(member.path, this.bumpVersion(pkg));
      }
    }
    return {allPackages: [...packagesByPath.values()], candidatesByPackage};
  }

  bumpVersion(pkg) {
    return this.strategiesByPath[pkg.path].versioningStrategy.bump(Version.parse(pkg.version), []);
  }

  // The release set is the candidates plus the query's units, so the graph
  // carries no edges and every package in it is released.
  async buildGraph(allPackages) {
    return new Map(allPackages.map(pkg => [pkg.name, {deps: [], value: pkg}]));
  }

  packageNamesToUpdate(graph, _candidatesByPackage) {
    return [...graph.keys()];
  }

  updateCandidate(existingCandidate, pkg, _updatedVersions) {
    const changes = this.rangeChanges.get(pkg.path);
    if (!changes) return existingCandidate;
    const notes = rangeNotes(changes);
    const {pullRequest} = existingCandidate;
    for (const update of pullRequest.updates) {
      if (update.updater instanceof Changelog) {
        update.updater.changelogEntry = appendDependenciesSectionToChangelog(update.updater.changelogEntry, notes, this.logger);
      }
    }
    if (pullRequest.body.releaseData.length > 0) {
      pullRequest.body.releaseData[0].notes = appendDependenciesSectionToChangelog(
        pullRequest.body.releaseData[0].notes,
        notes,
        this.logger,
      );
    } else {
      pullRequest.body.releaseData.push({
        component: pkg.name,
        version: pullRequest.version,
        notes: appendDependenciesSectionToChangelog('', notes, this.logger),
      });
    }
    return existingCandidate;
  }

  async newCandidate(pkg, updatedVersions) {
    const newVersion = updatedVersions.get(pkg.name);
    if (!newVersion) throw new Error(`${PLUGIN_TYPE}: no forced version for ${pkg.name}`);
    const strategy = this.strategiesByPath[pkg.path];
    const config = this.repositoryConfig[pkg.path];
    // The package's own draft setting, so a forced release keeps the merged
    // release pull request a draft when the config asks for one.
    const draft = config.draftPullRequest ?? false;
    const pullRequest = await strategy.buildReleasePullRequest([], this.releasesByPath[pkg.path], draft, [], {newVersion});
    if (!pullRequest) throw new Error(`${PLUGIN_TYPE}: release-please built no release for ${pkg.path}`);
    return this.updateCandidate({path: pkg.path, pullRequest, config}, pkg, updatedVersions);
  }

  postProcessCandidates(candidates, _updatedVersions) {
    return candidates;
  }
}

// Registers the plugin with release-please for the workspace checkout at
// `root`; called before a Manifest reads a config that names it. `query`
// replaces `range_sync.py --query` (the tests' stub).
function registerPythonWorkspace({root, query}) {
  registerPlugin(PLUGIN_TYPE, options => {
    const config = typeof options.type === 'object' ? options.type : {};
    return new PythonWorkspace(options.github, options.targetBranch, options.repositoryConfig, {
      ...options,
      ...config,
      root,
      query,
      merge: config.merge ?? !options.separatePullRequests,
    });
  });
}

module.exports = {PLUGIN_TYPE, PythonWorkspace, registerPythonWorkspace};
