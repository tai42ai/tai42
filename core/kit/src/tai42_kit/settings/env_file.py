"""The env file parsed once per file identity, and the dotenv source that serves it.

A settings construction reads its env file through :class:`TaiDotEnvSettingsSource`,
which asks :func:`read_env_file` for the file's values. The parse is cached per
absolute path and served while the file keeps its identity (device, inode, mtime,
size) and the variables its values interpolate (``${NAME}``) keep their
``os.environ`` values; any other case parses or resolves again, so a caller sees
exactly what a fresh parse returns. A path that is not a regular file (a FIFO) is
read on every call.
"""

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

from dotenv import dotenv_values
from dotenv.main import DotEnv, resolve_variables
from dotenv.variables import Variable, parse_variables
from pydantic_settings import DotEnvSettingsSource
from pydantic_settings.sources.utils import parse_env_vars

__all__ = [
    "DEFAULT_ENV_FILE",
    "EnvFileIdentity",
    "TaiDotEnvSettingsSource",
    "env_file_identity",
    "read_env_file",
]

DEFAULT_ENV_FILE: Final = ".env"

# ``(st_dev, st_ino, st_mtime_ns, st_size)``: every rewrite through ``os.replace``
# is a new inode, and an in-place edit changes the mtime or the size.
EnvFileIdentity = tuple[int, int, int, int]

_DEFAULT_ENCODING: Final = "utf8"


@dataclass(frozen=True)
class _EnvFileEntry:
    identity: EnvFileIdentity
    encoding: str
    raw_pairs: tuple[tuple[str, str | None], ...]
    # Sorted names of every ``${NAME}`` a value reads, and their ``os.environ``
    # values when ``values`` was resolved.
    referenced: tuple[str, ...]
    env_snapshot: tuple[str | None, ...]
    values: Mapping[str, str | None]


# One entry per absolute path: a rewrite replaces the entry, so the map never grows
# past the number of distinct env files. Plain get/set are atomic under the GIL; two
# threads missing together both parse and store identical values.
_ENV_FILE_CACHE: dict[str, _EnvFileEntry] = {}


def _identity_of(st: os.stat_result) -> EnvFileIdentity:
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)


def env_file_identity(path: str | os.PathLike[str]) -> EnvFileIdentity | None:
    """The identity of the regular file at ``path``, or ``None`` when there is none.

    A missing path, a directory, a FIFO, or a path ``stat`` cannot reach answers
    ``None``, as ``Path.is_file()`` answers ``False`` for each.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return _identity_of(st)


def _snapshot(referenced: tuple[str, ...]) -> tuple[str | None, ...]:
    return tuple(os.environ.get(name) for name in referenced)


def _resolve(raw_pairs: tuple[tuple[str, str | None], ...]) -> Mapping[str, str | None]:
    # The interpolation ``dotenv_values`` applies (``DotEnv.dict`` with override).
    return MappingProxyType(dict(resolve_variables(raw_pairs, override=True)))


def _referenced_names(raw_pairs: tuple[tuple[str, str | None], ...]) -> tuple[str, ...]:
    names = {
        atom.name
        for _, value in raw_pairs
        if value is not None
        for atom in parse_variables(value)
        if isinstance(atom, Variable)
    }
    return tuple(sorted(names))


def _parse(path: Path, encoding: str) -> _EnvFileEntry:
    with open(path, encoding=encoding) as fh:
        identity = _identity_of(os.fstat(fh.fileno()))
        raw_pairs = tuple(DotEnv(dotenv_path=None, stream=fh, interpolate=False).parse())
    referenced = _referenced_names(raw_pairs)
    snapshot = _snapshot(referenced)
    return _EnvFileEntry(
        identity=identity,
        encoding=encoding,
        raw_pairs=raw_pairs,
        referenced=referenced,
        env_snapshot=snapshot,
        values=_resolve(raw_pairs),
    )


def read_env_file(path: Path, *, encoding: str | None) -> Mapping[str, str | None]:
    """The values of the env file at ``path``, as ``dotenv_values`` returns them.

    Served from the cache while the file's identity, the encoding, and the values
    of the variables the file interpolates are unchanged. A failed read or parse
    stores nothing and raises.
    """
    resolved_encoding = encoding or _DEFAULT_ENCODING
    try:
        st = os.stat(path)
    except OSError:
        # ``dotenv_values`` reads an unreachable path as an empty file.
        return {}
    if not stat.S_ISREG(st.st_mode):
        return dict(dotenv_values(path, encoding=resolved_encoding))

    key = os.path.abspath(path)
    entry = _ENV_FILE_CACHE.get(key)
    if entry is not None and entry.identity == _identity_of(st) and entry.encoding == resolved_encoding:
        snapshot = _snapshot(entry.referenced)
        if snapshot == entry.env_snapshot:
            return entry.values
        entry = _EnvFileEntry(
            identity=entry.identity,
            encoding=entry.encoding,
            raw_pairs=entry.raw_pairs,
            referenced=entry.referenced,
            env_snapshot=snapshot,
            values=_resolve(entry.raw_pairs),
        )
    else:
        entry = _parse(path, resolved_encoding)
    _ENV_FILE_CACHE[key] = entry
    return entry.values


class TaiDotEnvSettingsSource(DotEnvSettingsSource):
    """The framework's dotenv source, reading each env file through :func:`read_env_file`.

    The multi-file order, the FIFO handling and ``dotenv_filtering`` are the
    framework's own; only the per-file read is served from the cache.
    """

    def _read_env_file(self, file_path: Path) -> Mapping[str, str | None]:
        return parse_env_vars(
            read_env_file(file_path, encoding=self.env_file_encoding),
            self.case_sensitive,
            self.env_ignore_empty,
            self.env_parse_none_str,
        )
