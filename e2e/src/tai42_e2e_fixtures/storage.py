"""A fixture :class:`~tai42_contract.storage.Storage` backend with a deliberately
distinct on-disk layout — the e2e "storage provider #2".

It exists to prove the storage-axis switch actually switches: its bytes land in a
layout unlike tai42-storage-local's, so a read-back through the wrong variant's
layout finds nothing. A manifest activates it by naming this module in
``storage_module`` (an import side-effect registers the class); the storage root
comes from ``E2E_FIXTURE_STORAGE_ROOT_PATH``.

Layout: every object ``<path>`` is stored at ``<root>/objects/<path>`` with the
:data:`_HEADER` bytes prepended, so BOTH the directory shape and the leading file
bytes differ from tai42-storage-local's raw ``<root>/<path>``.

Binary-native, like any filesystem backend: ``load_bytes`` / ``upload_bytes`` store
the payload bytes verbatim after the header, and the text surface is UTF-8 on top
of them. (The contract's text-bridge defaults would STRICT-decode UTF-8 and so
could not hold arbitrary bytes.) ``stat`` inherits the contract's ``mimetypes``
path inference — the filesystem stores no content-type metadata.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from pydantic_settings import SettingsConfigDict
from tai42_contract.app import tai42_app
from tai42_contract.storage import Storage, assert_not_root
from tai42_kit.settings import TaiBaseSettings, settings_cache

logger = logging.getLogger(__name__)

# The subtree every object lives under, and the byte header stamped on each stored file —
# together a layout distinct from the local backend's raw ``<root>/<path>``, so the
# storage-variant read-back helper (which reads the file directly) must know THIS format.
_OBJECTS_SUBDIR = "objects"
_HEADER = b"E2E-FIXTURE-STORAGE-V1\n"


class FixtureStorageSettings(TaiBaseSettings):
    model_config = SettingsConfigDict(env_prefix="E2E_FIXTURE_STORAGE_")

    root_path: str = "./e2e-fixture-storage"


@settings_cache
def fixture_storage_settings() -> FixtureStorageSettings:
    return FixtureStorageSettings()


def _root() -> Path:
    return Path(fixture_storage_settings().root_path)


def _resolve(path: str) -> Path:
    """Resolve ``path`` under ``<root>/objects``, refusing an escape with a
    path-boundary check (not a string prefix)."""
    base = (_root() / _OBJECTS_SUBDIR).resolve()
    target = (base / path).resolve()
    if not target.is_relative_to(base):
        raise ValueError(f"Path {path} is outside the storage root")
    return target


@tai42_app.storage.register_storage
class FixtureStorage(Storage):
    async def load_bytes(self, path: str) -> bytes:
        def _read() -> bytes:
            target = _resolve(path)
            try:
                data = target.read_bytes()
            except FileNotFoundError as e:
                raise FileNotFoundError(f"Object not found: {path}") from e
            if not data.startswith(_HEADER):
                raise ValueError(f"Object {path} is not in the fixture-storage format")
            return data[len(_HEADER) :]

        return await asyncio.to_thread(_read)

    async def upload_bytes(self, path: str, data: bytes, content_type: str | None = None) -> None:
        # The filesystem keeps no MIME metadata, so ``content_type`` is accepted for
        # contract parity but not stored; ``stat`` re-infers it from the path suffix.
        def _write() -> None:
            target = _resolve(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_HEADER + data)

        await asyncio.to_thread(_write)
        logger.info("Wrote bytes to fixture storage: %s", path)

    async def load(self, path: str) -> str:
        return (await self.load_bytes(path)).decode("utf-8")

    async def upload(self, path: str, content: str) -> None:
        await self.upload_bytes(path, content.encode("utf-8"))

    async def list(self) -> list[str]:
        def _walk() -> list[str]:
            base = (_root() / _OBJECTS_SUBDIR).resolve()
            if not base.exists():
                return []
            return [str(p.relative_to(base)) for p in base.rglob("*") if p.is_file()]

        return await asyncio.to_thread(_walk)

    async def delete(self, path: str) -> None:
        def _delete() -> None:
            target = _resolve(path)
            try:
                target.unlink()
            except FileNotFoundError as e:
                raise FileNotFoundError(f"Object not found: {path}") from e

        await asyncio.to_thread(_delete)
        logger.info("Deleted object from fixture storage: %s", path)

    async def delete_dir(self, path: str) -> None:
        assert_not_root(path)

        def _delete_dir() -> None:
            target = _resolve(path)
            if not target.is_dir():
                raise FileNotFoundError(f"Storage directory not found: {path}")
            shutil.rmtree(target)

        await asyncio.to_thread(_delete_dir)
        logger.info("Deleted directory from fixture storage: %s", path)
