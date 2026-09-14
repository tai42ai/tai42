"""Storage variant adapters (local, fixture, s3, github)."""

from __future__ import annotations

import abc
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tai42_e2e.settings import REAL_SERVICES
from tai42_e2e.topology import InfraUnavailable, StackResources


class StorageVariant(abc.ABC):
    """One Storage plugin: the manifest ``storage_module`` string, its feature env
    pointing at the stack's isolated store, and a store-agnostic read-back —
    :meth:`assert_stored` / :meth:`assert_absent`, which each variant implements
    against ITS OWN store (a filesystem tree, an S3 bucket, a fake GitHub repo)
    through an independent client, never the plugin under test. A store-shaped
    (``Path``-returning) contract cannot span object stores, so the seam is the two
    assertion methods, not a path accessor."""

    name: str
    module: str
    # The provider class + defining module the ``/api/storage`` identity door
    # reports for this backend (the door names the REGISTERED class, which lives in
    # the plugin's implementation module, not necessarily its package root).
    provider_class: str
    provider_module: str

    @abc.abstractmethod
    def feature_env(self, res: StackResources) -> dict[str, str]:
        """The storage plugin's env group, pointed at this stack's resources."""

    @abc.abstractmethod
    def assert_stored(self, res: StackResources, rel_path: str, content: str) -> None:
        """Assert the plugin really stored ``content`` at ``rel_path`` in this
        backend's own store — read back through an independent client (never the
        plugin under test), the proof the plugin wrote the real bytes. Raises
        :class:`AssertionError` naming the store location when it did not."""

    @abc.abstractmethod
    def assert_absent(self, res: StackResources, rel_path: str) -> None:
        """Assert no object exists at ``rel_path`` in this backend's store, read
        through the same independent client — the delete-really-removed-it proof.
        Raises :class:`AssertionError` when the object is still present."""


class LocalStorage(StorageVariant):
    name = "local"
    module = "tai42_storage_local"
    provider_class = "LocalStorage"
    provider_module = "tai42_storage_local.storage"

    def feature_env(self, res: StackResources) -> dict[str, str]:
        return {"STORAGE_LOCAL_ROOT_PATH": res.storage_root}

    def _stored_object_path(self, storage_root: str, rel_path: str) -> Path:
        # tai42-storage-local writes each object as raw bytes at ``<root>/<path>``.
        # A filesystem-layout detail distinct from the fixture backend's subtree, so
        # the two variants store the same object at different paths. Private: a
        # filesystem-only helper, NOT part of the store-agnostic StorageVariant seam.
        return Path(storage_root) / rel_path

    def assert_stored(self, res: StackResources, rel_path: str, content: str) -> None:
        path = self._stored_object_path(res.storage_root, rel_path)
        if not path.exists():
            raise AssertionError(f"local storage plugin did not write {path}")
        actual = path.read_text(encoding="utf-8")
        if actual != content:
            raise AssertionError(f"stored bytes at {path} decode to {actual!r}, not {content!r}")

    def assert_absent(self, res: StackResources, rel_path: str) -> None:
        path = self._stored_object_path(res.storage_root, rel_path)
        if path.exists():
            raise AssertionError(f"object still present on disk: {path}")


# The fixture storage backend's on-disk format: every object lives under an
# ``objects/`` subtree with a byte header stamped ahead of the content, so both the
# directory shape and the leading bytes differ from the local backend's raw layout.
# The read-back mirrors what ``tai42_e2e_fixtures.storage`` writes.
_FIXTURE_STORAGE_SUBDIR = "objects"


_FIXTURE_STORAGE_HEADER = b"E2E-FIXTURE-STORAGE-V1\n"


class FixtureStorage(StorageVariant):
    """The fixture filesystem storage backend — storage provider #2, with a
    deliberately distinct on-disk layout so the storage-axis switch is proven."""

    name = "fixture"
    module = "tai42_e2e_fixtures.storage"
    provider_class = "FixtureStorage"
    provider_module = "tai42_e2e_fixtures.storage"

    def feature_env(self, res: StackResources) -> dict[str, str]:
        return {"E2E_FIXTURE_STORAGE_ROOT_PATH": res.storage_root}

    def _stored_object_path(self, storage_root: str, rel_path: str) -> Path:
        # Private: a filesystem-only helper, NOT part of the store-agnostic seam.
        return Path(storage_root) / _FIXTURE_STORAGE_SUBDIR / rel_path

    def assert_stored(self, res: StackResources, rel_path: str, content: str) -> None:
        path = self._stored_object_path(res.storage_root, rel_path)
        if not path.exists():
            raise AssertionError(f"fixture storage plugin did not write {path}")
        data = path.read_bytes()
        if not data.startswith(_FIXTURE_STORAGE_HEADER):
            raise AssertionError(f"stored object {path} is not in the fixture-storage format")
        actual = data[len(_FIXTURE_STORAGE_HEADER) :].decode("utf-8")
        if actual != content:
            raise AssertionError(f"stored bytes at {path} decode to {actual!r}, not {content!r}")

    def assert_absent(self, res: StackResources, rel_path: str) -> None:
        path = self._stored_object_path(res.storage_root, rel_path)
        if path.exists():
            raise AssertionError(f"object still present on disk: {path}")


# One shared bucket for the whole s3 leg; the fixture creates it, every stack in
# the leg stores under it, and object keys are per-test unique (``uniq``).
S3_AXIS_BUCKET = "tai42-e2e-storage"


_S3_ENDPOINT_ENV = "TAI_E2E_S3_ENDPOINT"


_S3_ACCESS_KEY_ENV = "TAI_E2E_S3_ACCESS_KEY"


_S3_SECRET_KEY_ENV = "TAI_E2E_S3_SECRET_KEY"


# Defaults match the storage-profile MinIO service in ``compose.yml``.
_S3_DEFAULT_ENDPOINT = "http://127.0.0.1:9002"


_S3_DEFAULT_ACCESS_KEY = "minio"


_S3_DEFAULT_SECRET_KEY = "miniosecret"


# The fake GitHub REST server's origin, shared by the SUT env and the read-back.
# ``storage_axis_backing`` publishes an allocated free port on ``GITHUB_STUB_ENV``
# before the stacks render, which ``feature_env`` then reads; an explicit pin wins,
# and this default is only the last-resort fallback when neither is set.
GITHUB_STUB_ENV = "TAI_E2E_GITHUB_STUB_BASE"


GITHUB_STUB_DEFAULT = "http://127.0.0.1:9099"


# The single repo the fake GitHub server keys objects under; constants both ends share.
_GITHUB_USERNAME = "tai42-e2e"


_GITHUB_REPO = "storage"


_GITHUB_BRANCH = "main"


@dataclass(frozen=True)
class S3Coordinates:
    """The MinIO endpoint + credentials + axis bucket the s3 leg shares between the
    SUT env and the independent read-back client."""

    endpoint: str
    access_key: str
    secret_key: str
    bucket: str


def s3_coordinates() -> S3Coordinates:
    """The s3 leg's MinIO coordinates, read from env with ``compose.yml`` defaults."""
    return S3Coordinates(
        endpoint=os.environ.get(_S3_ENDPOINT_ENV, _S3_DEFAULT_ENDPOINT),
        access_key=os.environ.get(_S3_ACCESS_KEY_ENV, _S3_DEFAULT_ACCESS_KEY),
        secret_key=os.environ.get(_S3_SECRET_KEY_ENV, _S3_DEFAULT_SECRET_KEY),
        bucket=S3_AXIS_BUCKET,
    )


def open_s3_client() -> Any:
    """An independent boto3 S3 client at the leg's MinIO coordinates — the storage
    read-back path AND the fixture's bucket-create, never the aioboto3 plugin under
    test. boto3 is imported lazily: it is an s3-leg-only dependency, absent on every
    other storage leg."""
    import boto3
    from botocore.config import Config

    coords = s3_coordinates()
    return boto3.client(
        "s3",
        endpoint_url=coords.endpoint,
        aws_access_key_id=coords.access_key,
        aws_secret_access_key=coords.secret_key,
        region_name="us-east-1",
        use_ssl=False,
        verify=False,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def github_stub_base() -> str:
    """The fake GitHub REST server's origin (SUT env + read-back share it)."""
    return os.environ.get(GITHUB_STUB_ENV, GITHUB_STUB_DEFAULT)


def _github_raw_url(rel_path: str) -> str:
    return f"{github_stub_base()}/raw/{_GITHUB_USERNAME}/{_GITHUB_REPO}/refs/heads/{_GITHUB_BRANCH}/{rel_path}"


_S3_NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NotFound"})


class S3Storage(StorageVariant):
    """The S3 storage backend, run hermetically against a storage-profile MinIO
    container (never a real AWS endpoint). Read-back goes through an independent
    boto3 client against the same bucket, never the aioboto3 plugin under test."""

    name = "s3"
    module = "tai42_storage_s3"
    provider_class = "S3Storage"
    provider_module = "tai42_storage_s3.storage"

    def feature_env(self, res: StackResources) -> dict[str, str]:
        coords = s3_coordinates()
        # MinIO has no virtual-host buckets and serves plain HTTP in the leg, so
        # the SUT client must use path-style addressing over an insecure transport.
        return {
            "STORAGE_S3_ENDPOINT": coords.endpoint,
            "STORAGE_S3_BUCKET": coords.bucket,
            "STORAGE_S3_ACCESS_KEY": coords.access_key,
            "STORAGE_S3_SECRET_KEY": coords.secret_key,
            "STORAGE_S3_SECURE": "false",
            "STORAGE_S3_VERIFY_SSL": "false",
            "STORAGE_S3_ADDRESSING_STYLE": "path",
        }

    def assert_stored(self, res: StackResources, rel_path: str, content: str) -> None:
        from botocore.exceptions import ClientError

        client = open_s3_client()
        try:
            try:
                resp = client.get_object(Bucket=S3_AXIS_BUCKET, Key=rel_path)
            except ClientError as exc:
                if _s3_error_code(exc) in _S3_NOT_FOUND_CODES:
                    raise AssertionError(f"S3 storage plugin did not write s3://{S3_AXIS_BUCKET}/{rel_path}") from None
                raise
            actual = resp["Body"].read().decode("utf-8")
        finally:
            client.close()
        if actual != content:
            loc = f"s3://{S3_AXIS_BUCKET}/{rel_path}"
            raise AssertionError(f"stored bytes at {loc} decode to {actual!r}, not {content!r}")

    def assert_absent(self, res: StackResources, rel_path: str) -> None:
        from botocore.exceptions import ClientError

        client = open_s3_client()
        try:
            client.head_object(Bucket=S3_AXIS_BUCKET, Key=rel_path)
        except ClientError as exc:
            if _s3_error_code(exc) in _S3_NOT_FOUND_CODES:
                return
            raise
        finally:
            client.close()
        raise AssertionError(f"object still present: s3://{S3_AXIS_BUCKET}/{rel_path}")


def _s3_error_code(exc: Any) -> str | None:
    return exc.response.get("Error", {}).get("Code")


class GithubStorage(StorageVariant):
    """The GitHub storage backend, run hermetically against an in-process fake
    GitHub REST server (raw + contents + trees). Read-back GETs the fake's raw
    endpoint directly (an independent httpx client), never the plugin under test."""

    name = "github"
    module = "tai42_storage_github"
    provider_class = "GithubStorage"
    provider_module = "tai42_storage_github.storage"

    def feature_env(self, res: StackResources) -> dict[str, str]:
        base = github_stub_base()
        # The base-URL settings hold ``{username}``/``{repo}``/``{branch}`` placeholders
        # the plugin ``.format()``s; only the host is swapped from the real GitHub
        # surfaces to the fake, so the plugin's URL construction is exercised unchanged.
        return {
            "STORAGE_GITHUB_USERNAME": _GITHUB_USERNAME,
            "STORAGE_GITHUB_REPO": _GITHUB_REPO,
            "STORAGE_GITHUB_BRANCH": _GITHUB_BRANCH,
            "STORAGE_GITHUB_RAW_BASE_URL": f"{base}/raw/{{username}}/{{repo}}/refs/heads/{{branch}}",
            "STORAGE_GITHUB_CONTENTS_API_URL": f"{base}/api/repos/{{username}}/{{repo}}/contents",
            "STORAGE_GITHUB_TREES_API_URL": f"{base}/api/repos/{{username}}/{{repo}}/git/trees/{{branch}}",
        }

    def assert_stored(self, res: StackResources, rel_path: str, content: str) -> None:
        import httpx

        url = _github_raw_url(rel_path)
        resp = httpx.get(url)
        if resp.status_code == 404:
            raise AssertionError(f"GitHub storage plugin did not write {rel_path} (404 at {url})")
        resp.raise_for_status()
        if resp.text != content:
            raise AssertionError(f"stored bytes at {url} decode to {resp.text!r}, not {content!r}")

    def assert_absent(self, res: StackResources, rel_path: str) -> None:
        import httpx

        url = _github_raw_url(rel_path)
        resp = httpx.get(url)
        if resp.status_code != 404:
            raise AssertionError(f"object still present at {url}: HTTP {resp.status_code}")


def _real_leg_env(service: str) -> dict[str, str]:
    """The operator-supplied env for a real storage leg, read verbatim from the
    ambient environment. A missing or empty required var (per ``REAL_SERVICES`` — the
    single source of truth) raises loudly naming the exact vars, so selecting a real
    storage variant without its credentials never boots half-configured (the same
    contract the ``TAI_E2E_REAL`` collection gate enforces)."""
    required = REAL_SERVICES[service].required_env
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        raise InfraUnavailable(
            f"real storage leg {service!r} needs env var(s): {', '.join(missing)} "
            f"(also select TAI_E2E_REAL={service} so they are checked at collection)"
        )
    return {key: os.environ[key] for key in required}


def _real_s3_client(env: dict[str, str]) -> Any:
    """An independent boto3 client at the operator's real bucket coordinates — the
    read-back path, never the aioboto3 plugin under test. Transport security follows
    the endpoint scheme; path addressing is the default non-AWS stores require."""
    import boto3
    from botocore.config import Config

    style = os.environ.get("STORAGE_S3_ADDRESSING_STYLE", "path")
    return boto3.client(
        "s3",
        endpoint_url=env["STORAGE_S3_ENDPOINT"],
        aws_access_key_id=env["STORAGE_S3_ACCESS_KEY"],
        aws_secret_access_key=env["STORAGE_S3_SECRET_KEY"],
        region_name=env["STORAGE_S3_REGION"],
        config=Config(signature_version="s3v4", s3={"addressing_style": style}),
    )


class S3RealStorage(StorageVariant):
    """The S3 storage backend against a LIVE bucket (``STORAGE_S3_*`` from the filled
    template). Read-back goes through an independent boto3 client at the same real
    coordinates, never the plugin under test."""

    name = "s3-real"
    module = "tai42_storage_s3"
    provider_class = "S3Storage"
    provider_module = "tai42_storage_s3.storage"

    def feature_env(self, res: StackResources) -> dict[str, str]:
        env = _real_leg_env("storage-s3")
        # Path addressing + the narrow checksum mode default to the values non-AWS
        # S3-compatible stores (e.g. OCI) require; an operator override wins.
        env["STORAGE_S3_ADDRESSING_STYLE"] = os.environ.get("STORAGE_S3_ADDRESSING_STYLE", "path")
        env["STORAGE_S3_REQUEST_CHECKSUM_CALCULATION"] = os.environ.get(
            "STORAGE_S3_REQUEST_CHECKSUM_CALCULATION", "when_required"
        )
        return env

    def assert_stored(self, res: StackResources, rel_path: str, content: str) -> None:
        from botocore.exceptions import ClientError

        env = _real_leg_env("storage-s3")
        bucket = env["STORAGE_S3_BUCKET"]
        client = _real_s3_client(env)
        try:
            try:
                resp = client.get_object(Bucket=bucket, Key=rel_path)
            except ClientError as exc:
                if _s3_error_code(exc) in _S3_NOT_FOUND_CODES:
                    raise AssertionError(f"S3 storage plugin did not write s3://{bucket}/{rel_path}") from None
                raise
            actual = resp["Body"].read().decode("utf-8")
        finally:
            client.close()
        if actual != content:
            raise AssertionError(f"stored bytes at s3://{bucket}/{rel_path} decode to {actual!r}, not {content!r}")

    def assert_absent(self, res: StackResources, rel_path: str) -> None:
        from botocore.exceptions import ClientError

        env = _real_leg_env("storage-s3")
        bucket = env["STORAGE_S3_BUCKET"]
        client = _real_s3_client(env)
        try:
            client.head_object(Bucket=bucket, Key=rel_path)
        except ClientError as exc:
            if _s3_error_code(exc) in _S3_NOT_FOUND_CODES:
                return
            raise
        finally:
            client.close()
        raise AssertionError(f"object still present: s3://{bucket}/{rel_path}")


class GithubRealStorage(StorageVariant):
    """The GitHub storage backend against a LIVE repo (``STORAGE_GITHUB_*`` from the
    filled template). The plugin's base-URL settings already default to real GitHub,
    so the real leg sets only the coordinates + PAT. Read-back GETs the repo's
    Contents API with the same token (an independent httpx client, so private repos
    read back too), never the plugin under test."""

    name = "github-real"
    module = "tai42_storage_github"
    provider_class = "GithubStorage"
    provider_module = "tai42_storage_github.storage"

    def feature_env(self, res: StackResources) -> dict[str, str]:
        env = _real_leg_env("storage-github")
        # BRANCH is not in the required set (defaults ``main`` at the plugin); pass it
        # through when the operator pinned one. No RAW/CONTENTS/TREES overrides — the
        # plugin defaults already address real GitHub.
        branch = os.environ.get("STORAGE_GITHUB_BRANCH")
        if branch:
            env["STORAGE_GITHUB_BRANCH"] = branch
        return env

    def _contents_get(self, rel_path: str) -> Any:
        import httpx

        env = _real_leg_env("storage-github")
        branch = os.environ.get("STORAGE_GITHUB_BRANCH", "main")
        url = f"https://api.github.com/repos/{env['STORAGE_GITHUB_USERNAME']}/{env['STORAGE_GITHUB_REPO']}/contents/{rel_path}"
        return httpx.get(
            url,
            params={"ref": branch},
            headers={
                "Authorization": f"Bearer {env['STORAGE_GITHUB_TOKEN']}",
                "Accept": "application/vnd.github.raw+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def assert_stored(self, res: StackResources, rel_path: str, content: str) -> None:
        resp = self._contents_get(rel_path)
        if resp.status_code == 404:
            raise AssertionError(f"GitHub storage plugin did not write {rel_path} (404 at contents API)")
        resp.raise_for_status()
        if resp.text != content:
            raise AssertionError(f"stored bytes for {rel_path} decode to {resp.text!r}, not {content!r}")

    def assert_absent(self, res: StackResources, rel_path: str) -> None:
        resp = self._contents_get(rel_path)
        if resp.status_code != 404:
            raise AssertionError(f"object still present at {rel_path}: HTTP {resp.status_code}")


STORAGES: dict[str, StorageVariant] = {
    "local": LocalStorage(),
    "fixture": FixtureStorage(),
    "s3": S3Storage(),
    "github": GithubStorage(),
    "s3-real": S3RealStorage(),
    "github-real": GithubRealStorage(),
}
