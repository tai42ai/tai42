r"""Consumer boot gate: fail a platform release whose candidate core breaks a published consumer's BOOT.

This is a behavioural check the textual API-diff gate cannot see.

The API-diff gate (``tai42_cli.api_gate``) classifies a public-symbol diff. A change
that removes no symbol yet refuses a previously valid route or lifecycle at
startup reads as additive to that gate, while every published consumer built on
the old surface fails to boot. This gate closes that hole: it installs the
candidate tree's core packages (contract/kit/skeleton/cli, editable from the
tree) together with one or more PREVIOUSLY PUBLISHED consumer distributions,
boots the app the real way (the ``tai serve`` entrypoint, access control on,
health awaited), and asserts, for each consumer, that every route registers, no
lifecycle handler raises, and health becomes ready.

Each consumer is mounted the way a deployment layers it: its additive surface
(routers/tools/channels/extensions/identity providers/lifecycle) plus the exclusive
provider slot it selects — ``backend_module`` / ``storage_module`` / ``sandbox_module``
/ ``monitoring_module`` or an ``agents`` entry per agent — so the plugin's module is
imported and registers, and health-ready proves that registration ran. A provider whose
boot lifecycle needs a live external service CI has no stand-in for (a config provider,
selected before the manifest and backed by e.g. the Kubernetes API) is INSTALLED and
reported ``install-only``, never booted — the summary line distinguishes the two, so a
consumer the gate cannot boot is never a silent pass.

A consumer that declares ``permissions.network`` (it reaches an external messaging API
or identity provider at startup) is given the deployment config it needs with every
outbound endpoint pointed at a closed loopback port. Its boot then runs through install,
import, registration and lifecycle and either becomes health-ready (its startup contacts
nothing, e.g. a config-guard-only channel) or fails ONLY with a connection-class error at
that endpoint. The latter is reported ``install-only (external service: <handler>)`` — the
plugin needs a live external service the gate cannot provide, distinct from a candidate
break (a missing symbol, a refused route, a guard). The
install-only-vs-broken decision reads only the declared network permission and the runtime
error class, never a distribution name.

Rule: a boot failure of a previously published consumer against the candidate is
a BREAKING change. The gate fails unless the governing release is a MAJOR bump —
the bump read the same way ``tai42_cli.api_gate`` reads it, the governing package's
version against its previous released tag. Under a major bump the same failure is
reported as an ACCEPTED break (a printed notice) rather than a gate failure.

A consumer whose declared dependency range excludes the candidate cannot even be
installed into the boot venv; the resolver's ``No solution found`` is the most
explicit form of a consumer break. Whether it is a break of THIS candidate is decided
against the same baseline the bump uses — the previous released tag. Under a major
bump the conflict is an accepted break (a notice, the gate passes). Under a non-major
bump the same install set is re-resolved against the previous released tag's tree: if
it resolves there, the candidate introduced the conflict and the gate fails; if it
also finds no solution there, the incompatibility predates the candidate (it is
carried from the previous release) and is not a new break, so a notice names the
consumers and the gate passes. Any other install failure (network, a bad wheel, a
build error), here or in the re-resolve, is always a hard failure, never reclassified.

Consumers come from two sources. Supplied ones are wheels (``--consumer-wheel``)
or requirement specs (``--consumer-req``); the gate names no specific consumer, so
it is agnostic to which distributions a deployment layers on the platform. The
first-party plugins are enumerated with ``--emit-matrix``: each packaged plugin at
its latest published PyPI version that is NOT in this train's bump set (a plugin
being released now boots its unpublished candidate code through its own lanes,
never here), one boot per consumer so mutually exclusive infra plugins never share
a manifest. When no consumer is supplied (a first release, or a run without the
download secret) the gate says so and passes; a plugin with no PyPI release yet is
a skipped notice, never a failure. A supplied wheel that does not exist raises
loudly — a download that failed upstream can never read as a silent pass.

CLI::

    consumer_boot_gate.py --package tai42-skeleton --dir core/skeleton \\
        --version 11.2.0 --consumer-wheel dist/some_consumer-1.4.0-py3-none-any.whl
    consumer_boot_gate.py --package tai42-skeleton --dir core/skeleton \\
        --consumer-req tai42-some-plugin==1.4.0
    consumer_boot_gate.py --emit-matrix   # a CI matrix of the first-party consumers
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from _consumer_boot_gate.boot import (
    _DEFAULT_IDENTITY_PACKAGE,
    Infra,
    _infra_from_args,
    boot_consumer,
)
from _consumer_boot_gate.boot_failure import BootFailure, external_service_handlers, is_external_service_only
from _consumer_boot_gate.consumers import Consumer, collect_consumers, enumerate_first_party
from _consumer_boot_gate.install import (
    _install_venv,
    _report_unresolvable_consumers,
    _ResolutionConflictError,
)
from _consumer_boot_gate.provides import read_provides
from _consumer_boot_gate.versioning import _fail, break_is_accepted, governing_bump, read_project_version

# The core packages installed editable from the candidate tree: the platform the
# consumers boot against. Overridable so a fork with a different layout can point
# the gate at its own core member dirs.
_DEFAULT_CORE_DIRS = ("core/contract", "core/kit", "core/skeleton", "core/cli")


def _load_plugin_yaml(venv_bin: Path, dist_name: str) -> dict:
    """Read the named consumer distribution's ``tai-plugin.yml`` from the boot venv.

    The descriptor the deployment actually ships, not a tree copy. Located through the
    distribution's own file manifest (never by importing its package: a plugin whose
    ``__init__`` registers against ``tai42_app`` at import raises before the app binds,
    which is exactly the plugins this gate must read), so a co-installed plugin's
    descriptor is never mistaken for it. A consumer that ships no descriptor cannot
    declare a boot surface to exercise; the caller treats that as a hard error, never a
    silent skip.
    """
    script = (
        "import importlib.metadata as m, sys\n"
        f"dist = m.distribution({dist_name!r})\n"
        "found = ''\n"
        "for f in dist.files or []:\n"
        "    if f.name == 'tai-plugin.yml':\n"
        "        found = dist.locate_file(f).read_text()\n"
        "        break\n"
        "sys.stdout.write(found)\n"
    )
    result = subprocess.run([str(venv_bin / "python"), "-c", script], capture_output=True, text=True)  # noqa: S603 fixed, trusted argv; no shell and no user input
    if result.returncode != 0:
        _fail(f"could not read the {dist_name} descriptor from the boot venv: {result.stderr.strip()[-400:]}")
    if not result.stdout.strip():
        return {}
    import yaml

    return yaml.safe_load(result.stdout) or {}


def _resolve_version(args: argparse.Namespace, repo_root: Path) -> str:
    if args.version is not None:
        return args.version
    return read_project_version(repo_root / args.dir)


def _emit_matrix(repo_root: Path) -> None:
    """Print a GitHub-Actions matrix of the first-party consumers to boot, plus skip notices.

    One include entry per distribution, each ``{label, req}``, emitted as ``{"include": [...]}`` on
    stdout, with a ``::notice::`` per plugin skipped for having no PyPI release yet. An empty include
    is valid — the matrix job then has no combinations and is skipped.
    """
    consumers, notices = enumerate_first_party(repo_root)
    for notice in notices:
        print(f"::notice::consumer-boot-gate: {notice}", file=sys.stderr)
    include = [{"label": c.label, "req": c.install_arg} for c in consumers]
    print(json.dumps({"include": include}))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", help="governing release-please package-name (the bump owner)")
    parser.add_argument("--dir", help="the governing package's member dir")
    parser.add_argument("--version", help="governing version (default: read from the member's pyproject.toml)")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--core-dir", action="append", dest="core_dirs", help="editable core member dir (repeatable)")
    parser.add_argument("--consumer-wheel", action="append", default=[], help="path to a consumer wheel (repeatable)")
    parser.add_argument("--consumer-req", action="append", default=[], help="a consumer requirement spec (repeatable)")
    parser.add_argument(
        "--emit-matrix",
        action="store_true",
        help="enumerate first-party consumers (minus the bump set) and print a CI matrix, then exit",
    )
    parser.add_argument("--identity-package", default=_DEFAULT_IDENTITY_PACKAGE)
    parser.add_argument("--redis-url", default=os.environ.get("CONSUMER_BOOT_REDIS_URL", "redis://127.0.0.1:6379"))
    parser.add_argument("--pg-host", default=os.environ.get("CONSUMER_BOOT_PG_HOST", "127.0.0.1"))
    parser.add_argument("--pg-port", default=os.environ.get("CONSUMER_BOOT_PG_PORT", "5432"))
    parser.add_argument("--pg-user", default=os.environ.get("CONSUMER_BOOT_PG_USER", "postgres"))
    parser.add_argument("--pg-password", default=os.environ.get("CONSUMER_BOOT_PG_PASSWORD", "postgres"))
    parser.add_argument("--workdir", type=Path, help="scratch dir for the venv, config and logs (default: a tempdir)")
    parser.add_argument("--keep", action="store_true", help="keep the scratch dir after the run")
    return parser


def _boot_all(
    header: str, consumers: list[Consumer], venv_bin: Path, workdir: Path, infra: Infra
) -> tuple[int, int, int, list[tuple[Consumer, BootFailure]]]:
    """Boot every supplied consumer against the candidate core.

    Returns the counts of booted / install-only / install-only-external plus the list of real boot
    failures.
    """
    print(f"{header}: booting {len(consumers)} consumer(s) against the candidate core.")
    booted = 0
    install_only = 0
    external_only = 0
    failures: list[tuple[Consumer, BootFailure]] = []
    for index, consumer in enumerate(consumers):
        plugin_yaml = _load_plugin_yaml(venv_bin, consumer.dist_name)
        if not plugin_yaml:
            _fail(
                f"{consumer.label}: ships no tai-plugin.yml descriptor, so no boot surface can be mounted — "
                f"the gate cannot prove it boots. A supplied consumer must be a plugin distribution."
            )
        provides = read_provides(plugin_yaml)
        for kind, reason in provides.install_only:
            print(f"::notice::{consumer.label}: {kind} provider install-only: {reason}")
        if not provides.has_boot_surface():
            if provides.install_only:
                # Installed against the candidate core (its dependency closure resolves)
                # but not bootable: reported, never a silent pass.
                print(f"  - {consumer.label}: install-only (installs; not booted).")
                install_only += 1
                continue
            _fail(
                f"{consumer.label}: declares no surface the gate can mount and no install-only provider — "
                f"a core-only boot would prove nothing (the hollow pass this gate exists to prevent). "
                f"Its tai-plugin.yml 'provides' names no router/tool/channel/extension/identity/lifecycle "
                f"module, no backend/storage/sandbox/monitoring/agent slot, and no install-only kind."
            )
        boot_dir = workdir / f"boot-{index}"
        boot_dir.mkdir(parents=True, exist_ok=True)
        failure = boot_consumer(consumer, venv_bin, provides, boot_dir, infra)
        if failure is None:
            print(f"  - {consumer.label}: booted, health ready.")
            booted += 1
        elif provides.network and is_external_service_only(failure):
            # The boot ran through install, import, registration and lifecycle and then
            # could not reach the external endpoint the gate black-holed for it — the
            # plugin's startup needs an external service the gate cannot stand in for, not
            # a candidate-core break. Reported, never a silent pass and never a failure.
            reached = ", ".join(external_service_handlers(failure))
            print(f"  - {consumer.label}: install-only (external service: {reached}).")
            external_only += 1
        else:
            print(f"  - {consumer.label}: BOOT FAILED — {failure.summary()}")
            failures.append((consumer, failure))
    return booted, install_only, external_only, failures


def _report_and_exit(
    header: str,
    bump: str,
    booted: int,
    install_only: int,
    external_only: int,
    failures: list[tuple[Consumer, BootFailure]],
) -> None:
    """Print the final tally and decide the gate.

    A clean run or a major-bump-accepted break passes; any other boot failure fails the gate loudly.
    """
    tally = f"{booted} booted, {install_only} install-only, {external_only} install-only (external service)"
    if not failures:
        print(f"{header}: every supplied consumer accounted for ({tally}) — gate passes.")
        return

    lines = [f"{consumer.label}: {failure.summary()}" for consumer, failure in failures]
    if break_is_accepted(bump):
        print(f"::notice::{header}: {len(failures)} consumer(s) fail to boot, ACCEPTED as a major-bump break:")
        for line in lines:
            print(f"  - {line}")
        print(f"{header}: accepted break under a major bump ({tally}, {len(failures)} broken) — gate passes.")
        return
    _fail(
        f"{header}: {len(failures)} previously published consumer(s) fail to boot against the candidate, "
        f"which is a breaking change not carried by a major bump ({tally}). Offending: {'; '.join(lines)}"
    )


def main() -> None:
    args = _build_parser().parse_args()

    repo_root = args.repo_root.resolve()
    if args.emit_matrix:
        _emit_matrix(repo_root)
        return
    if not args.package or not args.dir:
        _fail("--package and --dir are required to boot a consumer (omit them only with --emit-matrix)")
    core_dirs = args.core_dirs or list(_DEFAULT_CORE_DIRS)
    version = _resolve_version(args, repo_root)
    bump = governing_bump(args.package, version, repo_root)

    consumers = collect_consumers(args.consumer_wheel, args.consumer_req)
    header = f"consumer-boot-gate: {args.package} {version} ({bump} bump)"
    if not consumers:
        print(f"{header}: no previously published consumer supplied — nothing to boot, gate passes.")
        return

    workdir = args.workdir.resolve() if args.workdir else Path(tempfile.mkdtemp(prefix="consumer-boot-"))
    workdir.mkdir(parents=True, exist_ok=True)
    infra = _infra_from_args(args)
    venv = workdir / "venv"
    try:
        venv_bin = _install_venv(repo_root, core_dirs, args.identity_package, consumers, venv)
    except _ResolutionConflictError as conflict:
        _report_unresolvable_consumers(
            header,
            bump,
            consumers,
            conflict.resolver_stderr,
            package=args.package,
            version=version,
            repo_root=repo_root,
            core_dirs=core_dirs,
            identity_package=args.identity_package,
            venv=venv,
        )
        return

    booted, install_only, external_only, failures = _boot_all(header, consumers, venv_bin, workdir, infra)

    if not args.keep and args.workdir is None:
        import shutil

        shutil.rmtree(workdir, ignore_errors=True)

    _report_and_exit(header, bump, booted, install_only, external_only, failures)
