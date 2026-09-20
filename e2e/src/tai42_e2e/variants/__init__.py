"""The variant adapter layer: one small object per backend, identity and storage axis."""

from __future__ import annotations

from tai42_e2e.variants.backends import BACKENDS, ArqVariant, BackendVariant, CeleryVariant, RqVariant
from tai42_e2e.variants.census import BrokerLease, BusWorker, bus_census, short_presence_ttl_env
from tai42_e2e.variants.identities import IDENTITIES, FixtureIdentity, IdentityVariant, RedisIdentity
from tai42_e2e.variants.resolve import Variants, resolve_variants
from tai42_e2e.variants.storages import (
    GITHUB_STUB_DEFAULT,
    GITHUB_STUB_ENV,
    S3_AXIS_BUCKET,
    STORAGES,
    FixtureStorage,
    GithubRealStorage,
    GithubStorage,
    LocalStorage,
    S3Coordinates,
    S3RealStorage,
    S3Storage,
    StorageVariant,
    github_stub_base,
    open_s3_client,
    s3_coordinates,
)

__all__ = [
    "BACKENDS",
    "GITHUB_STUB_DEFAULT",
    "GITHUB_STUB_ENV",
    "IDENTITIES",
    "S3_AXIS_BUCKET",
    "STORAGES",
    "ArqVariant",
    "BackendVariant",
    "BrokerLease",
    "BusWorker",
    "CeleryVariant",
    "FixtureIdentity",
    "FixtureStorage",
    "GithubRealStorage",
    "GithubStorage",
    "IdentityVariant",
    "LocalStorage",
    "RedisIdentity",
    "RqVariant",
    "S3Coordinates",
    "S3RealStorage",
    "S3Storage",
    "StorageVariant",
    "Variants",
    "bus_census",
    "github_stub_base",
    "open_s3_client",
    "resolve_variants",
    "s3_coordinates",
    "short_presence_ttl_env",
]
