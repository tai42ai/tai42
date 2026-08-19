"""Storage-axis backing for the hermetic s3 + github legs.

The s3 and github storage variants run against harness-owned stand-ins that must
exist BEFORE any stack boots and writes: the s3 leg's shared axis bucket on the
storage-profile MinIO, and the github leg's in-process fake GitHub REST server.
This session-scoped autouse fixture stands the right one up for the selected axis
(a no-op on the filesystem legs), set up ahead of the module-scoped stack fixtures.

The s3 endpoint/credentials and the github stub origin are the SAME coordinates the
storage variant's ``feature_env`` + read-back read, so the SUT, the read-back, and
this backing all agree without threading a value through per-stack resources."""

from __future__ import annotations

import os
from collections.abc import Iterator
from urllib.parse import urlparse

import pytest

from tai42_e2e.ports import allocate_port
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.variants import GITHUB_STUB_ENV, S3_AXIS_BUCKET, github_stub_base, open_s3_client, s3_coordinates
from tai42_e2e.waiting import wait_for

# Resolved via the tests/storage path insertion (pytest prepend import mode).
from ._fake_github import FakeGithub  # pyright: ignore[reportMissingImports]


def _ensure_s3_bucket() -> None:
    """Create the shared axis bucket on MinIO through the independent client,
    tolerating an already-existing bucket and waiting out MinIO warm-up."""
    from botocore.exceptions import ClientError, EndpointConnectionError

    client = open_s3_client()

    def _create() -> bool:
        try:
            client.create_bucket(Bucket=S3_AXIS_BUCKET)
        except EndpointConnectionError:
            return False
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                raise
        return True

    endpoint = s3_coordinates().endpoint
    try:
        wait_for(
            _create,
            deadline=30.0,
            message=f"MinIO at {endpoint} never reachable to create bucket {S3_AXIS_BUCKET}",
        )
    finally:
        client.close()


@pytest.fixture(scope="session", autouse=True)
def storage_axis_backing() -> Iterator[None]:
    storage = HarnessSettings().storage
    if storage == "s3":
        _ensure_s3_bucket()
        yield
    elif storage == "github":
        # The stub origin defaults to a FIXED port, collision-prone on a shared host. When
        # it is not pinned via env, allocate a free loopback port and publish it on the env
        # seam BEFORE the stacks render, so both the fake below and every stack's
        # feature_env read the same origin off ``github_stub_base()``.
        if GITHUB_STUB_ENV not in os.environ:
            os.environ[GITHUB_STUB_ENV] = f"http://127.0.0.1:{allocate_port()}"
        base = urlparse(github_stub_base())
        if base.hostname is None or base.port is None:
            raise ValueError(f"github stub base must carry host and port: {github_stub_base()!r}")
        fake = FakeGithub(base.hostname, base.port)
        fake.start()
        try:
            yield
        finally:
            fake.stop()
    else:
        yield
