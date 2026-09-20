"""Unit tests for ``AsyncLocalDriver`` against a real temp directory."""

from __future__ import annotations

import os

import pytest

import tai42_storage_local.driver as driver_module
from tai42_storage_local.driver import AsyncLocalDriver


def _driver(tmp_path, create_dirs: bool = True) -> AsyncLocalDriver:
    return AsyncLocalDriver(root_path=str(tmp_path), create_dirs=create_dirs)


# --- text I/O ---------------------------------------------------------------


async def test_write_then_read_file(tmp_path):
    driver = _driver(tmp_path)
    await driver.write_file("a.txt", "hello")
    assert await driver.read_file("a.txt") == "hello"


async def test_write_file_creates_parent_dirs(tmp_path):
    driver = _driver(tmp_path)
    await driver.write_file("nested/deep/a.txt", "x")
    assert (tmp_path / "nested" / "deep" / "a.txt").read_text() == "x"


async def test_write_file_no_create_dirs_missing_parent_raises(tmp_path):
    driver = _driver(tmp_path, create_dirs=False)
    with pytest.raises(FileNotFoundError):
        await driver.write_file("nested/a.txt", "x")


async def test_read_file_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        await _driver(tmp_path).read_file("nope.txt")


# --- binary I/O -------------------------------------------------------------


async def test_write_then_read_bytes_identical(tmp_path):
    driver = _driver(tmp_path)
    payload = bytes(range(256)) * 4  # not valid UTF-8
    await driver.write_bytes("blob.bin", payload)
    assert await driver.read_bytes("blob.bin") == payload


async def test_write_bytes_creates_parent_dirs(tmp_path):
    driver = _driver(tmp_path)
    await driver.write_bytes("sub/blob.bin", b"\x00\x01")
    assert (tmp_path / "sub" / "blob.bin").read_bytes() == b"\x00\x01"


async def test_read_bytes_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        await _driver(tmp_path).read_bytes("nope.bin")


# --- path guard -------------------------------------------------------------


async def test_read_escape_raises(tmp_path):
    with pytest.raises(ValueError, match="outside the storage root"):
        await _driver(tmp_path).read_file("../escape.txt")


async def test_write_escape_raises(tmp_path):
    with pytest.raises(ValueError, match="outside the storage root"):
        await _driver(tmp_path).write_bytes("../escape.bin", b"x")


async def test_symlink_escape_raises(tmp_path):
    # A symlink inside the root pointing outside it must not be followable.
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s")
    (root / "link").symlink_to(outside)
    driver = AsyncLocalDriver(root_path=str(root), create_dirs=True)

    with pytest.raises(ValueError, match="outside the storage root"):
        await driver.read_file("link/secret.txt")
    with pytest.raises(ValueError, match="outside the storage root"):
        await driver.write_bytes("link/new.bin", b"x")
    with pytest.raises(ValueError, match="outside the storage root"):
        await driver.delete_file("link/secret.txt")
    with pytest.raises(ValueError, match="outside the storage root"):
        await driver.delete_dir("link")
    # The outside tree is untouched.
    assert (outside / "secret.txt").read_text() == "s"
    assert not (outside / "new.bin").exists()


async def test_sibling_prefix_escape_raises(tmp_path):
    # A sibling dir whose name merely starts with the root's name must NOT be reachable.
    root = tmp_path / "templates"
    root.mkdir()
    sibling = tmp_path / "templates_evil"
    sibling.mkdir()
    (sibling / "secret").write_text("x")
    driver = AsyncLocalDriver(root_path=str(root), create_dirs=True)

    with pytest.raises(ValueError, match="outside the storage root"):
        await driver.read_file("../templates_evil/secret")
    assert (sibling / "secret").exists()


# --- delete_file ------------------------------------------------------------


async def test_delete_file_removes_file(tmp_path):
    (tmp_path / "a.txt").write_text("hi")
    await _driver(tmp_path).delete_file("a.txt")
    assert not (tmp_path / "a.txt").exists()


async def test_delete_file_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        await _driver(tmp_path).delete_file("nope.txt")


# --- delete_dir -------------------------------------------------------------


async def test_delete_dir_removes_tree(tmp_path):
    sub = tmp_path / "d"
    sub.mkdir()
    (sub / "x.txt").write_text("x")
    (sub / "y.txt").write_text("y")
    await _driver(tmp_path).delete_dir("d")
    assert not sub.exists()


async def test_delete_dir_root_refused(tmp_path):
    # A path resolving to the root itself is refused by the driver's own guard.
    with pytest.raises(ValueError, match="storage root"):
        await _driver(tmp_path).delete_dir("")
    assert tmp_path.exists()


async def test_delete_dir_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        await _driver(tmp_path).delete_dir("no_such_dir")


async def test_delete_dir_escape_raises(tmp_path):
    with pytest.raises(ValueError, match="outside the storage root"):
        await _driver(tmp_path).delete_dir("../outside")


async def test_delete_dir_tolerates_vanished_file(tmp_path, monkeypatch):
    # A file vanishing mid-rmtree (concurrent delete) is tolerated; the delete still completes.
    sub = tmp_path / "d"
    sub.mkdir()
    (sub / "x.txt").write_text("x")
    driver = _driver(tmp_path)

    def fake_rmtree(target, onexc):
        onexc(os.unlink, str(target / "x.txt"), FileNotFoundError("vanished"))

    monkeypatch.setattr(driver_module.shutil, "rmtree", fake_rmtree)
    await driver.delete_dir("d")  # no raise


async def test_delete_dir_real_error_propagates(tmp_path, monkeypatch):
    # Any non-vanished failure (e.g. a permission error) must surface loudly.
    sub = tmp_path / "d"
    sub.mkdir()
    (sub / "x.txt").write_text("x")
    driver = _driver(tmp_path)

    def fake_rmtree(target, onexc):
        onexc(os.unlink, str(target / "x.txt"), PermissionError("denied"))

    monkeypatch.setattr(driver_module.shutil, "rmtree", fake_rmtree)
    with pytest.raises(PermissionError):
        await driver.delete_dir("d")


# --- list_recursive ---------------------------------------------------------


async def test_list_recursive_returns_relative_paths(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    sub = tmp_path / "d"
    sub.mkdir()
    (sub / "b.txt").write_text("b")
    listed = sorted(await _driver(tmp_path).list_recursive())
    assert listed == ["a.txt", os.path.join("d", "b.txt")]


async def test_list_recursive_missing_root_returns_empty(tmp_path):
    driver = AsyncLocalDriver(root_path=str(tmp_path / "absent"), create_dirs=True)
    assert await driver.list_recursive() == []


# --- file/directory id collisions + empty-parent pruning --------------------


async def test_delete_file_prunes_empty_parents(tmp_path):
    driver = _driver(tmp_path)
    await driver.write_file("a/b/c.txt", "x")
    await driver.delete_file("a/b/c.txt")
    # The file and every now-empty ancestor up to the root are gone; the root stays.
    assert not (tmp_path / "a" / "b").exists()
    assert not (tmp_path / "a").exists()
    assert tmp_path.exists()


async def test_delete_file_keeps_non_empty_parent(tmp_path):
    driver = _driver(tmp_path)
    await driver.write_file("a/b/c.txt", "x")
    await driver.write_file("a/keep.txt", "y")
    await driver.delete_file("a/b/c.txt")
    # "a/b" empties and is pruned, but "a" still holds a sibling and stays.
    assert not (tmp_path / "a" / "b").exists()
    assert (tmp_path / "a" / "keep.txt").read_text() == "y"


async def test_delete_dir_prunes_empty_parents(tmp_path):
    driver = _driver(tmp_path)
    await driver.write_file("x/y/z.txt", "x")
    await driver.delete_dir("x/y")
    # The deleted subtree AND the now-empty ancestor "x" are gone.
    assert not (tmp_path / "x" / "y").exists()
    assert not (tmp_path / "x").exists()


async def test_upload_over_empty_leftover_directory_succeeds(tmp_path):
    driver = _driver(tmp_path)
    # An id left as an empty directory tree is free to become a file.
    (tmp_path / "a" / "b").mkdir(parents=True)
    await driver.write_file("a/b", "now a file")
    assert (tmp_path / "a" / "b").read_text() == "now a file"


async def test_upload_over_non_empty_directory_refused_with_ids(tmp_path):
    driver = _driver(tmp_path)
    await driver.write_file("a/b/c.txt", "c")
    await driver.write_file("a/b/d.txt", "d")
    with pytest.raises(driver_module.StoragePathConflictError) as exc:
        await driver.write_file("a/b", "cannot")
    assert exc.value.path == "a/b"
    assert exc.value.conflicts == ["a/b/c.txt", "a/b/d.txt"]


async def test_upload_under_existing_file_id_refused(tmp_path):
    driver = _driver(tmp_path)
    await driver.write_file("a/b", "a file")
    with pytest.raises(driver_module.StoragePathConflictError) as exc:
        await driver.write_file("a/b/c.txt", "nested")
    assert exc.value.conflicts == ["a/b"]


async def test_upload_under_non_regular_file_ancestor_refused(tmp_path):
    driver = _driver(tmp_path)
    # A non-regular file (here a FIFO) is not a directory, so mkdir cannot descend
    # through it; the conflict must name that ancestor id, not guess a parent.
    os.mkfifo(tmp_path / "a")
    with pytest.raises(driver_module.StoragePathConflictError) as exc:
        await driver.write_file("a/b/c", "nested")
    assert exc.value.conflicts == ["a"]


async def test_upload_mkdir_conflict_without_blocker_reraises(tmp_path, monkeypatch):
    driver = _driver(tmp_path)

    # mkdir fails but no ancestor under the root blocks (a race): the original OS
    # error propagates, never a fabricated StoragePathConflictError.
    def _raise_exists(self, *args, **kwargs):
        raise FileExistsError("raced")

    monkeypatch.setattr("pathlib.Path.mkdir", _raise_exists)
    with pytest.raises(FileExistsError, match="raced"):
        await driver.write_file("nested/a.txt", "x")


async def test_upload_over_empty_dir_refuses_file_racing_in(tmp_path):
    driver = _driver(tmp_path)
    empty = tmp_path / "a" / "b"
    empty.mkdir(parents=True)

    def _plant_after_scan() -> None:
        # A write lands under the id between the emptiness scan and the removal.
        (empty / "raced.txt").write_text("survivor")

    driver._before_empty_dir_removal = _plant_after_scan

    with pytest.raises(driver_module.StoragePathConflictError) as exc:
        await driver.write_file("a/b", "would clobber the racer")

    # rmdir refuses the now-non-empty directory, so the raced-in file is intact.
    assert exc.value.conflicts == ["a/b/raced.txt"]
    assert (empty / "raced.txt").read_text() == "survivor"
    assert not (empty / "b").exists()  # nothing turned "a/b" into a file


async def test_upload_resolving_to_root_refused(tmp_path):
    driver = _driver(tmp_path)
    # An id resolving to the store root must never trigger a root removal.
    with pytest.raises(ValueError, match="storage root"):
        await driver.write_file(".", "x")
    assert tmp_path.exists()
