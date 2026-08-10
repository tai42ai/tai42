"""File-based configuration manager.

Implements :class:`~tai42_contract.config.manager.ConfigManager` for the ``file``
config mode.  Reads and writes ``.env`` files and ``manifest.yml`` on the
local filesystem.
"""

import copy
import io
import logging
import os
import re
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, cast

from dotenv import dotenv_values
from pyaml_env import parse_config
from ruamel.yaml.comments import CommentedMap
from tai42_contract.config.manager import ConfigManager
from tai42_kit.utils.data import (
    load_manifest,
    merge_and_dump_manifest,
)

logger = logging.getLogger(__name__)


def _acquire_exclusive_lock(fd: int) -> None:
    """Take a blocking exclusive advisory lock on *fd*.

    POSIX uses ``fcntl.flock(LOCK_EX)`` — a genuine, blocking, cross-process
    mutex. ``fcntl`` is POSIX-only and this module is reachable from the
    always-imported ``config`` package, so the import is done here (never at module
    top level) to keep the package importable on Windows.

    On Windows ``fcntl`` is unavailable, so a config write cannot be serialized
    against a concurrent writer. Rather than proceed unlocked and risk a lost
    update, the write is refused with a :class:`RuntimeError` naming the platform
    limitation — a missing lock is a fail-loud condition, not a warning.
    """
    if sys.platform == "win32":
        raise RuntimeError(
            "config file locking requires fcntl, which is POSIX-only and "
            "unavailable on Windows; a serialized config write cannot be "
            "guaranteed here, so the write is refused (run on a POSIX host)."
        )
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_EX)


# An env key is a shell-identifier: a letter/underscore then letters/digits/
# underscores. Anything else (a newline, ``=``, a space) could inject a second
# assignment or corrupt the parse, so it is rejected loudly.
_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _dotenv_serialize_value(value: str) -> str:
    """Serialize *value* as a double-quoted ``.env`` literal that ``dotenv_values``
    parses back to the exact string — the write side of :meth:`read_env`.

    Backslash is escaped first (so real backslashes survive), then the double
    quote and newline/carriage-return that would otherwise break out of the quote
    or split the line. Every other character rides through the quotes verbatim.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    return f'"{escaped}"'


class FileConfigManager(ConfigManager):
    """Config backend that reads/writes local ``.env`` and manifest files.

    ``config_dir_path`` resolves to the constructor arg, then
    ``TAI_CONFIG_DIR_PATH``, then ``"/app"``; every anchored path (env, manifest,
    defaults) is rooted there. ``TAI_MANIFEST_PATH`` overrides the manifest
    path.
    """

    def __init__(self, config_dir_path: str | None = None) -> None:
        # ``TAI_CONFIG_DIR_PATH`` is a bootstrap path read here directly; it is
        # classified excluded on ``ConfigModeSettings.config_dir_path`` so the
        # reload boundary refuses any profile that tries to carry it.
        self._config_dir_path = config_dir_path or os.environ.get("TAI_CONFIG_DIR_PATH", "").strip() or "/app"

    @property
    def _env_path(self) -> str:
        return os.path.join(self._config_dir_path, ".env")

    @property
    def _manifest_path(self) -> str:
        return os.getenv("TAI_MANIFEST_PATH") or os.path.join(self._config_dir_path, "manifest.yml")

    @property
    def _defaults_manifest_path(self) -> str:
        return os.path.join(self._config_dir_path, "templates", "manifest.yml")

    @contextmanager
    def _file_lock(self, path: str) -> Iterator[None]:
        """Serialize a read-modify-write on *path* across processes.

        Takes a blocking exclusive POSIX ``flock`` on a SIDECAR lock file
        (``<path>.lock``, created ``0600``) held for the whole read → modify →
        atomic-write span the caller wraps, so a concurrent worker's RMW cannot
        interleave and lose an update. The lock subject is the sidecar, never
        *path* itself: the atomic write replaces *path* with ``os.replace``, which
        swaps the target's inode out from under any lock held on it, making a lock
        on the target useless.

        This is the lock primitive; :meth:`_manifest_transaction` builds the
        manifest read-modify-write span on top of it, and :meth:`write_env` uses it
        for the env file.

        On Windows ``fcntl`` is unavailable, so acquiring the lock raises rather
        than proceeding unserialized. An ``OSError`` from opening the lock file or
        acquiring the lock propagates loudly; there is no timeout or retry loop (the
        critical section is a sub-millisecond atomic write).
        """
        fd = os.open(f"{path}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _acquire_exclusive_lock(fd)
            yield
        finally:
            # Closing the descriptor releases the flock.
            os.close(fd)

    @contextmanager
    def _manifest_transaction(self) -> Iterator[None]:
        """Hold the manifest sidecar lock across a whole read → mutate → write span.

        :meth:`mutate_manifest` and :meth:`replace_manifest` run their ENTIRE
        read-modify-write under this one lock, so a concurrent worker cannot
        interleave between another writer's read and write and lose an update.
        """
        with self._file_lock(self._manifest_path):
            yield

    # -- Environment configuration -------------------------------------------

    def read_env(self) -> dict[str, str]:
        """Parse the ``.env`` file into a ``{key: value}`` dict.

        Delegates to :func:`dotenv.dotenv_values`, which handles ``export``,
        inline comments, quoting, and escapes. Interpolation is disabled, so
        values are returned literally — a ``$`` or ``${VAR}`` in a value is not
        POSIX-expanded, making the double-quoted serializer a true inverse.
        """
        path = self._env_path
        if not os.path.exists(path):
            raise FileNotFoundError(f"Env file not found: {path}")
        try:
            parsed = dotenv_values(path, interpolate=False)
        except OSError:
            logger.error("Failed to read env file: %s", path, exc_info=True)
            raise
        # Drop valueless keys (``KEY=`` with nothing after) — None values.
        return {k: v for k, v in parsed.items() if v is not None}

    def write_env(self, config: dict[str, str]) -> None:
        """Merge *config* into the ``.env`` file.

        Every incoming key is validated against a shell-identifier charset
        (raising :class:`ValueError` otherwise), and every value is serialized as
        a double-quoted literal so a write followed by :meth:`read_env` is an exact
        round-trip — a newline, ``#``, quote, or leading/trailing space survives
        instead of injecting a key or being silently truncated. Existing keys not
        present in *config* are preserved; empty / ``None`` values are dropped.

        Before the file is written, the serialized content is re-parsed with the
        same parser as :meth:`read_env` (``dotenv_values`` with
        ``interpolate=False``, here reading from a ``StringIO`` stream rather than
        the file path) and every written value is compared to its parsed result; a
        value the parser cannot round-trip (which would silently drop that key and
        every key after it on the next reload) raises :class:`ValueError` naming the
        offending key, and no write happens.
        """
        for key in config:
            if not _ENV_KEY_RE.fullmatch(key):
                raise ValueError(f"invalid env key {key!r}: must match [A-Za-z_][A-Za-z0-9_]*")
        path = self._env_path
        # The whole read-modify-write (re-read existing → merge → atomic write) runs
        # under the env sidecar lock so a concurrent worker's write cannot merge
        # against a stale base and drop this write's keys.
        with self._file_lock(path):
            existing: dict[str, str] = {}
            if os.path.exists(path):
                existing = self.read_env()
            merged = {**existing, **config}
            written = {k: v for k, v in merged.items() if v is not None and v != ""}
            lines = [f"{k}={_dotenv_serialize_value(v)}" for k, v in written.items()]
            content = "\n".join(lines) + ("\n" if lines else "")
            # Re-parse the serialized content through the same parser as read_env
            # (``dotenv_values`` with ``interpolate=False``, over a StringIO stream)
            # and confirm every written key round-trips. python-dotenv exposes no
            # double-quoted representation for some values (e.g. a value ending in an
            # odd number of backslashes): its parser pairs the trailing backslash with
            # the closing quote and drops that key AND every key after it. A key
            # serialized last hides the fault until the next write appends a key after
            # it, so the content is probed with a trailing sentinel binding to exercise
            # that position now — any value that cannot round-trip fails loudly here
            # rather than silently corrupting the store on a later reload.
            # The sentinel key is grown with ``_`` until it collides with no written
            # key, so a caller that legitimately writes a key literally named
            # ``_TAI_ENV_ROUNDTRIP_PROBE`` is not shadowed by the probe (dotenv is
            # last-wins) and thereby falsely flagged.
            probe_key = "_TAI_ENV_ROUNDTRIP_PROBE"
            while probe_key in written:
                probe_key += "_"
            probe = f'{content}{probe_key}="0"\n'
            reparsed = dotenv_values(stream=io.StringIO(probe), interpolate=False)
            corrupted = [key for key, value in written.items() if reparsed.get(key) != value]
            if corrupted:
                names = ", ".join(repr(key) for key in corrupted)
                raise ValueError(f"env value for {names} cannot be round-tripped through the .env format")
            try:
                self._atomic_write(path, content)
            except OSError:
                logger.error("Failed to write env file: %s", path, exc_info=True)
                raise

    def replace_env(self, config: dict[str, str]) -> None:
        """Replace the whole ``.env`` file with *config* (whole-map, NOT a merge).

        Identical to :meth:`write_env` — same charset validation, round-trip probe,
        env-file format, sidecar lock, and atomic write — except *config* becomes the
        ENTIRE stored env: a key absent from *config* is DELETED (no existing key is
        preserved), so nothing survives uninvited. Empty / ``None`` values are still
        dropped.
        """
        for key in config:
            if not _ENV_KEY_RE.fullmatch(key):
                raise ValueError(f"invalid env key {key!r}: must match [A-Za-z_][A-Za-z0-9_]*")
        path = self._env_path
        # The whole write runs under the env sidecar lock so a concurrent worker's
        # write cannot interleave with this replace; unlike write_env there is no
        # read-of-existing to serialize against, but the atomic write still must not
        # race another writer's replace.
        with self._file_lock(path):
            written = {k: v for k, v in config.items() if v is not None and v != ""}
            lines = [f"{k}={_dotenv_serialize_value(v)}" for k, v in written.items()]
            content = "\n".join(lines) + ("\n" if lines else "")
            # Same trailing-sentinel round-trip probe as write_env: a value the parser
            # cannot round-trip would silently drop that key and every key after it on
            # the next reload, so it is caught here rather than corrupting the store.
            probe_key = "_TAI_ENV_ROUNDTRIP_PROBE"
            while probe_key in written:
                probe_key += "_"
            probe = f'{content}{probe_key}="0"\n'
            reparsed = dotenv_values(stream=io.StringIO(probe), interpolate=False)
            corrupted = [key for key, value in written.items() if reparsed.get(key) != value]
            if corrupted:
                names = ", ".join(repr(key) for key in corrupted)
                raise ValueError(f"env value for {names} cannot be round-tripped through the .env format")
            try:
                self._atomic_write(path, content)
            except OSError:
                logger.error("Failed to write env file: %s", path, exc_info=True)
                raise

    # -- Manifest configuration ----------------------------------------------

    # Two YAML views of the manifest: the runtime expands ``!ENV`` tags to their
    # env values; the write-merge preserves them as tags so the operator-authored
    # placeholder survives a round-trip (instead of baking the secret to disk).

    def _load_yaml_expanded(self, path: str) -> dict:
        """``!ENV`` tags expanded to their resolved values — runtime view."""
        with open(path) as fh:
            return parse_config(data=fh.read()) or {}

    def _load_yaml_preserved(self, path: str) -> CommentedMap:
        """``!ENV`` tags preserved as ``"!ENV <expr>"`` marker strings — round-trip
        view. Comments, key ordering, and formatting are kept for a later dump."""
        with open(path) as fh:
            return load_manifest(fh.read())

    def read_manifest(self) -> dict:
        """Read ``manifest.yml`` with ``!ENV`` tags EXPANDED (runtime view).

        This is the worker BOOT / runtime read, so it TOLERATES a marker whose var is
        not yet resolved — the fleet backup/import/convergence pattern seeds a manifest
        whose env is supplied AFTER boot, and an unresolved required marker transiently
        materializes to ``pyaml_env``'s literal ``"N/A"`` until the env write lands.
        Refusing it here would abort boot for a legitimate deferred-env deployment. A
        mutation that INTRODUCES a dangling marker is still refused at the write/replace
        validation (:class:`~tai42_skeleton.config.service.ConfigService`) and by the
        offline CLI ``validate`` — closing the silent WRITE path, never the boot read.
        """
        path = self._manifest_path
        if not os.path.exists(path):
            raise FileNotFoundError(f"Manifest not found: {path}")
        return self._load_yaml_expanded(path)

    def read_manifest_preserved(self) -> dict:
        """Read ``manifest.yml`` with ``!ENV`` tags PRESERVED as ``"!ENV <expr>"``
        marker strings (round-trip view) — no secret values are resolved."""
        path = self._manifest_path
        if not os.path.exists(path):
            raise FileNotFoundError(f"Manifest not found: {path}")
        return self._load_yaml_preserved(path)

    def read_defaults_manifest(self) -> dict:
        """Read template defaults with ``!ENV`` tags EXPANDED (runtime view).

        A missing defaults file is optional and yields ``{}``; a malformed one
        raises so the broken template surfaces instead of silently dropping the
        defaults from the write-merge backfill. Like :meth:`read_manifest` this is a
        boot/runtime read, so it TOLERATES a not-yet-resolved marker (the deferred-env
        pattern); a dangling marker is refused only at the write/replace validation and
        the offline CLI ``validate``, never here.
        """
        path = self._defaults_manifest_path
        if not os.path.exists(path):
            return {}
        return self._load_yaml_expanded(path)

    def mutate_manifest(self, mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        """Atomically read-modify-write the manifest under the transaction lock.

        Inside the shared manifest transaction: the current manifest is read in the
        PRESERVED view (every ``!ENV <expr>`` kept as its literal marker string, so
        no secret is resolved), ``mutator`` edits that round-trip document IN PLACE,
        and the edited document is dumped (defaults backfilling only keys it is
        missing) and written atomically. Untouched keys keep their values, comments,
        and ordering; no other writer's change can interleave between the read and
        the write. Returns the persisted preserved-view document.

        Because the mutator only ever sees marker strings, a resolved secret can
        never be present to bake to disk — the ``!ENV`` preservation is structural
        here, not a post-hoc scan. A ``mutator`` exception aborts the transaction
        with nothing written and propagates unchanged. Validation of the mutated
        document against the resolved view is the caller's concern.
        """
        with self._manifest_transaction():
            document: CommentedMap = CommentedMap()
            if os.path.exists(self._manifest_path):
                document = self._load_yaml_preserved(self._manifest_path)
            # A mutator exception propagates here, before any write — nothing lands.
            mutator(document)
            defaults: dict = {}
            if os.path.exists(self._defaults_manifest_path):
                defaults = self._load_yaml_preserved(self._defaults_manifest_path)
            content = merge_and_dump_manifest(defaults, document, {})
            path = self._manifest_path
            try:
                self._atomic_write(path, content)
            except OSError:
                logger.error("Failed to write manifest file: %s", path, exc_info=True)
                raise
        return document

    def replace_manifest(self, document: dict[str, Any]) -> dict[str, Any]:
        """Atomically replace the whole persisted manifest under the transaction lock.

        Inside the shared manifest transaction: *document* becomes the entire stored
        manifest — a key absent from *document* is DELETED, nothing from the old
        manifest survives uninvited (defaults still backfill keys *document* is
        missing). The document is dumped verbatim and written atomically; it is not
        read or re-preserved, so building it from the preserved view (``!ENV`` marker
        strings, never resolved secrets) is the caller's obligation. Returns the
        persisted document (a copy of *document* with defaults backfilled); the
        caller's dict is left untouched.
        """
        with self._manifest_transaction():
            defaults: dict = {}
            if os.path.exists(self._defaults_manifest_path):
                defaults = self._load_yaml_preserved(self._defaults_manifest_path)
            persisted = cast("CommentedMap", copy.deepcopy(document))
            content = merge_and_dump_manifest(defaults, persisted, {})
            path = self._manifest_path
            try:
                self._atomic_write(path, content)
            except OSError:
                logger.error("Failed to write manifest file: %s", path, exc_info=True)
                raise
        return persisted

    @staticmethod
    def _atomic_write(path: str, content: str) -> None:
        """Write *content* to *path* atomically with ``0600`` permissions.

        A uniquely-named temp file is created ``0600`` in the target's own
        directory (so ``os.replace`` stays on one filesystem), flushed + fsynced,
        then renamed over the target — atomic on POSIX, so a crash mid-write never
        leaves a truncated secret store. The unique name avoids colliding with a
        temp left by a previously crashed write. Failures propagate loudly after
        the orphan temp is removed. After the rename, the target directory is
        fsynced so the rename itself is durable across a crash.
        """
        directory = os.path.dirname(path) or "."
        fd, tmp = tempfile.mkstemp(dir=directory)
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                logger.error("Failed to remove temp file after a failed write: %s", tmp, exc_info=True)
            raise
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def build_config_manager() -> ConfigManager:
    """Provider entry point for the ``file`` config mode (the factory convention)."""
    return FileConfigManager()
