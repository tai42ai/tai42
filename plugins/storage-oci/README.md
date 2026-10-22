# tai42-storage-oci

OCI Object Storage backend for the TAI ecosystem — text, binary, and media
content over an Object Storage bucket, authenticated as a principal.

## The TAI ecosystem

A storage backend implements the `Storage` contract from `tai42-contract` and
registers itself as the active provider on import. It sits below the skeleton and
never imports it.

## Install

```sh
pip install tai42-storage-oci
```

## Discovery

```yaml manifest.yml
storage_module: tai42_storage_oci
```

## Configuration

| Env var | Default | Purpose |
| --- | --- | --- |
| `STORAGE_OCI_BUCKET` | — | Target bucket (required) |
| `STORAGE_OCI_NAMESPACE` | — | Object Storage namespace; resolved from the principal's tenancy when unset |
| `STORAGE_OCI_REGION` | — | Overrides the region the principal or profile carries |
| `STORAGE_OCI_AUTH` | `workload_identity` | `workload_identity` / `instance_principal` / `config_file` |
| `STORAGE_OCI_SERVICE_ACCOUNT_TOKEN_PATH` | — | Projected service-account token path; unset uses the SDK default |
| `STORAGE_OCI_CONNECT_TIMEOUT` | `5` | Connect timeout (seconds) |
| `STORAGE_OCI_READ_TIMEOUT` | `30` | Read timeout (seconds) |

There is no credential variable: the backend authenticates as a principal, so a
static key has nowhere to be configured.

## Why not the S3 backend

OCI Object Storage also speaks an S3-compatible API, which `tai42-storage-s3`
can use. That API accepts only a customer secret key, so it requires an IAM user
and a long-lived static credential per tenant. This backend talks to the native
API, where a principal authenticates with no key at all.

## Content-type behavior

Object Storage stores bytes and content-types natively. `upload` tags
`text/plain; charset=utf-8`, `upload_bytes` stores the given type, and `stat`
returns the stored type.

## Development

```sh
uv sync --locked
uv run --package tai42-storage-oci ruff check .
uv run --package tai42-storage-oci ruff format --check .
uv run --package tai42-storage-oci pyright
uv run --package tai42-storage-oci pytest --cov --cov-report=term-missing
```

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
