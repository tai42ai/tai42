# tai42-config-k8s

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

The Kubernetes `ConfigManager` provider for the TAI ecosystem — a separately
installed plugin. It implements the `tai42_contract.config.manager.ConfigManager`
ABC for the `k8s` config mode:

- **env** configuration via a K8s **Secret**
- **manifest** configuration via a K8s **ConfigMap** (with `!ENV`-tag
  round-tripping through the tai42-kit yaml utilities)

Its only tai-* dependencies are `tai42-contract` (the interface it implements) and
`tai42-kit` (settings machinery + manifest yaml helpers). It **never** imports the
skeleton — it is loaded by the skeleton's config seam through the
`build_config_manager()` factory convention by dynamic import (the skeleton names
this module as the string `"tai42_config_k8s.manager"`, so there is no static edge
in either direction).

## The TAI ecosystem

TAI is an open-source runtime for MCP tools, agents, and workflows. A
`ConfigManager` is the runtime's configuration provider: one pluggable object the
server reads its env and manifest configuration through, so where that
configuration lives is a deployment choice rather than a code change. This
package is one such provider (Kubernetes Secrets and ConfigMaps); any package can
back the same contract, so this repo is this provider's own full doc home, and
the documentation site covers the platform-level story:

- Config & secrets concept: https://tai42.ai/concepts/config-and-secrets
- Build a config provider (author guide): https://tai42.ai/guides/authors/config-provider
- Ecosystem catalog: https://tai42.ai/reference/catalog

## Install

Requires **Python 3.13+**. Install from PyPI into the environment that runs the
server, with the optional `[k8s]` extra that pulls the kubernetes client:

```bash
uv add "tai42-config-k8s[k8s]"
```

Or from source — clone this repo and add it as an editable dependency. Clone
`tai-contract` and `tai-kit` beside this repo first — `[tool.uv.sources]`
resolves them from sibling paths.

```bash
git clone https://github.com/tai42ai/tai42   # next to your app checkout
cd /path/to/your/app
uv add --editable "../tai-config-k8s[k8s]"
```

The `kubernetes` client is an optional `[k8s]` extra, imported lazily inside
`K8sConfigManager`; if it is absent the manager raises a copy-pasteable install
hint rather than a bare `ImportError`.

Select it at runtime with `TAI_CONFIG_MODE=k8s`.

## Settings

`K8sConfigSettings` (prefix `TAI_K8S_`):

- `TAI_K8S_NAMESPACE` (overrides the auto-detected namespace)
- `TAI_K8S_SECRET_NAME` (default `tai-env`)
- `TAI_K8S_CONFIGMAP_NAME` (default `tai-manifest`)
- `TAI_K8S_MANIFEST_KEY` (default `manifest.yml`)
- `TAI_K8S_DEFAULTS_MANIFEST_KEY` (default `defaults.manifest.yml`)

When `TAI_K8S_NAMESPACE` is unset (or blank), the namespace is auto-detected
from the pod service account, falling back to `default` for local development.

Typed package (`py.typed`).

## Development

```bash
uv venv --python 3.13
uv pip install --no-sources --group dev --editable .
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pyright
uv run --no-sync pytest --cov --cov-report=term-missing
```

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
