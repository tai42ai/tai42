"""Assemble the boot venv and classify a dependency-resolution conflict by the governing bump.

The venv holds the editable candidate core + identity provider + consumers.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from tai42_cli import api_gate

from _consumer_boot_gate.consumers import Consumer
from _consumer_boot_gate.process import (
    GIT_TIMEOUT_S,
    INSTALL_TIMEOUT_S,
    RERESOLVE_TIMEOUT_S,
    VENV_TIMEOUT_S,
    run_gate_step,
)
from _consumer_boot_gate.versioning import _fail, break_is_accepted

# Kit's optional-dependency groups a served app with a consumer needs present: the
# jq/llm engines, the Postgres and Redis clients, the ASGI server, curl transport,
# and the LangGraph Postgres checkpoint saver.
_KIT_EXTRAS = "jq,llm,postgres,redis,langgraph-checkpoint-postgres,uvicorn,curl"


# uv prints this exact line when a consumer's declared dependency range excludes the
# candidate core, so the boot venv cannot be assembled — the most explicit form of a
# consumer break, classified by the governing bump like a boot failure.
_NO_SOLUTION_MARKER = "No solution found when resolving dependencies"


class _ResolutionConflictError(Exception):
    """The boot venv install failed because uv found no dependency solution.

    A consumer's requirement range excludes the candidate core. Carries the resolver output so the caller
    classifies it by the governing bump.
    """

    def __init__(self, resolver_stderr: str) -> None:
        super().__init__(resolver_stderr)
        self.resolver_stderr = resolver_stderr


def _is_resolution_conflict(install_stderr: str) -> bool:
    """True when an install failure is uv reporting no dependency solution.

    A consumer range excluding the candidate, not a network, build or bad-wheel failure.
    """
    return _NO_SOLUTION_MARKER in install_stderr


def _resolver_conclusion(resolver_stderr: str) -> str:
    """The resolver's own conclusion, from the ``No solution found`` line onward, quoted in the notice."""
    idx = resolver_stderr.find(_NO_SOLUTION_MARKER)
    return resolver_stderr[idx:].strip() if idx != -1 else resolver_stderr.strip()


def _install_venv(
    repo_root: Path, core_dirs: list[str], identity_package: str, consumers: list[Consumer], venv: Path
) -> Path:
    """Create the boot venv and install the candidate core, identity provider, and consumers; return its ``bin``.

    A no-solution resolution failure raises :class:`_ResolutionConflictError` for the caller to classify by
    the bump; any other install failure is a hard failure here.
    """
    run_gate_step(
        ["uv", "venv", "--python", "3.13", str(venv)],
        what="creating the boot venv",
        timeout=VENV_TIMEOUT_S,
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    venv_bin = venv / "bin"
    install_args = ["uv", "pip", "install", "--python", str(venv_bin / "python")]
    for core_dir in core_dirs:
        extras = f"[{_KIT_EXTRAS}]" if core_dir.endswith("/kit") else ""
        install_args += ["-e", f"{core_dir}{extras}"]
    install_args.append(identity_package)
    install_args += [consumer.install_arg for consumer in consumers]
    result = run_gate_step(
        install_args,
        what="installing the boot venv (candidate core + consumers)",
        timeout=INSTALL_TIMEOUT_S,
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()[-800:]
        if _is_resolution_conflict(stderr):
            raise _ResolutionConflictError(stderr)
        _fail(f"boot venv install failed: {stderr}")
    return venv_bin


def _reresolve_against_previous_tag(
    previous_tag: str,
    repo_root: Path,
    core_dirs: list[str],
    identity_package: str,
    consumers: list[Consumer],
    venv: Path,
) -> str | None:
    """Re-resolve the boot install set against the previous released tag's tree in a throwaway worktree.

    Tells a conflict the candidate INTRODUCED from one that predates it. Returns the resolver output when
    the previous tree ALSO finds no solution (a pre-existing incompatibility); returns ``None`` when it
    resolves (the candidate introduced the conflict). Any other failure of the re-resolve raises loudly —
    a pre-existing verdict is only ever reached through the resolver's own no-solution conclusion, never a
    swallowed error.
    """
    parent = Path(tempfile.mkdtemp(prefix="consumer-boot-prev-"))
    worktree = parent / "tree"
    try:
        run_gate_step(
            ["git", "worktree", "add", "--detach", str(worktree), previous_tag],
            what=f"checking out the previous release tree ({previous_tag})",
            timeout=GIT_TIMEOUT_S,
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
        install_args = ["uv", "pip", "install", "--dry-run", "--python", str(venv / "bin" / "python")]
        for core_dir in core_dirs:
            extras = f"[{_KIT_EXTRAS}]" if core_dir.endswith("/kit") else ""
            install_args += ["-e", f"{worktree / core_dir}{extras}"]
        install_args.append(identity_package)
        install_args += [consumer.install_arg for consumer in consumers]
        result = run_gate_step(
            install_args,
            what=f"re-resolving the boot install set against the previous release ({previous_tag})",
            timeout=RERESOLVE_TIMEOUT_S,
            cwd=worktree,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return None
        stderr = result.stderr.strip()[-800:]
        if _is_resolution_conflict(stderr):
            return stderr
        _fail(
            f"re-resolving the boot install set against the previous release {previous_tag} failed for a reason "
            f"other than a dependency conflict, so the conflict cannot be classified: {stderr}"
        )
    finally:
        run_gate_step(
            ["git", "worktree", "remove", "--force", str(worktree)],
            what="removing the previous-release worktree",
            timeout=GIT_TIMEOUT_S,
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        import shutil

        shutil.rmtree(parent, ignore_errors=True)


def _report_unresolvable_consumers(
    header: str,
    bump: str,
    consumers: list[Consumer],
    resolver_stderr: str,
    *,
    package: str,
    version: str,
    repo_root: Path,
    core_dirs: list[str],
    identity_package: str,
    venv: Path,
) -> None:
    """Classify a boot-venv resolution conflict.

    Under a major bump every supplied consumer is an accepted break (a notice quoting the resolver's
    conclusion, the gate passes). Under a non-major bump the conflict is a break only if THIS candidate
    introduced it: the same install set is re-resolved against the previous released tag's tree — if it
    resolves there the candidate introduced the conflict and the gate fails, and if it also finds no
    solution the incompatibility predates the candidate (carried from the previous release) and a notice
    passes it. This is the single accept/fail decision point for an unresolvable install set.
    """
    if break_is_accepted(bump):
        names = ", ".join(consumer.label for consumer in consumers)
        print(
            f"::notice::{header}: {len(consumers)} consumer(s) cannot resolve against the candidate, "
            f"ACCEPTED as a major-bump break: {names}"
        )
        print(f"  - {_resolver_conclusion(resolver_stderr)}")
        print(f"{header}: accepted break under a major bump ({len(consumers)} unresolvable) — gate passes.")
        return

    previous_tag = api_gate._previous_tag(package, version, repo_root)
    if previous_tag is None:
        _fail(f"boot venv install failed: {resolver_stderr}")
    prior_stderr = _reresolve_against_previous_tag(
        previous_tag, repo_root, core_dirs, identity_package, consumers, venv
    )
    if prior_stderr is None:
        _fail(f"boot venv install failed: {resolver_stderr}")

    names = ", ".join(consumer.label for consumer in consumers)
    print(
        f"::notice::{header}: {len(consumers)} consumer(s) cannot resolve against the candidate, but neither can "
        f"they against the previous release ({previous_tag}) — a pre-existing incompatibility, not a new break: "
        f"{names}"
    )
    print(f"  - candidate: {_resolver_conclusion(resolver_stderr)}")
    print(f"  - previous release {previous_tag}: {_resolver_conclusion(prior_stderr)}")
    print(
        f"{header}: {len(consumers)} unresolvable consumer(s) carry a pre-existing incompatibility from "
        f"{previous_tag}, not introduced by this candidate — gate passes."
    )
