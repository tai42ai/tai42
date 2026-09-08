"""The ``templates`` backup section: a restored template id colliding with a
directory that still holds templates is a per-path rejection recorded in the
report, never a silently dropped or whole-restore-aborting failure."""

from __future__ import annotations

from types import SimpleNamespace

from tai42_contract.app import tai42_app
from tai42_contract.storage import StoragePathConflictError

from tai42_skeleton.backup import sections
from tai42_skeleton.backup.registry import import_mode


class _ConflictingRenderer:
    """A resource manager whose upload of the ``conflict`` id raises the store's
    typed conflict; every other id is stored."""

    def __init__(self, conflict_id: str) -> None:
        self.conflict_id = conflict_id
        self.stored: dict[str, str] = {}

    async def list_resources(self) -> list[str]:
        return []

    async def upload_template(self, path: str, content: str) -> None:
        if path == self.conflict_id:
            raise StoragePathConflictError(path, [f"{path}/child.j2"])
        self.stored[path] = content


async def test_restore_conflict_is_recorded_and_restore_continues() -> None:
    renderer = _ConflictingRenderer("a/b")
    payload = {"a/b": "would clobber a directory", "clean.j2": "fine"}

    with (
        tai42_app.bound(SimpleNamespace(storage=SimpleNamespace(resource_manager=renderer))),
        import_mode("overwrite"),
    ):
        report = await sections._import_templates(payload)

    # The colliding id is a per-path skip with a loud error; the clean id still restores.
    assert report["skipped"] == 1
    assert report["created"] == 1
    assert any("a/b" in e and "a/b/child.j2" in e for e in report["errors"])
    assert renderer.stored == {"clean.j2": "fine"}
