"""Plugin-spec I/O: loading ``tai-plugin.yml``, its docs tree, and its env requirements.

Every installable TAI plugin ships a ``tai-plugin.yml`` — at its repo root and
as package-data inside the built wheel. The schema is
:class:`tai42_contract.plugins.PluginSpec`; this package does the YAML reading the
pure contract cannot (the contract has no YAML dependency) and validates loudly:

- :mod:`tai42_kit.plugins.spec` — the hardened spec loader (:func:`parse_plugin_spec`
  and the file/wheel loaders routed through it).
- :mod:`tai42_kit.plugins.docs` — reading and validating the ``docs/`` tree.
- :mod:`tai42_kit.plugins.env_requirements` — the env vars a spec requires at install.
"""

from __future__ import annotations

from tai42_kit.plugins.docs import (
    MAX_DOCS_FILE_BYTES,
    MAX_DOCS_TOTAL_BYTES,
    MDX_SAFE_COMPONENTS,
    PLUGIN_DOCS_DIRNAME,
    PLUGIN_DOCS_INDEX,
    PluginDocsError,
    read_dir_docs,
    read_wheel_docs,
    validate_docs,
)
from tai42_kit.plugins.env_requirements import (
    EnvRequirement,
    accepts_env,
    required_env,
    required_env_for_spec,
)
from tai42_kit.plugins.spec import (
    MAX_PLUGIN_SPEC_BYTES,
    PLUGIN_SPEC_FILENAME,
    PluginSpecLoadError,
    load_plugin_spec,
    parse_plugin_spec,
    read_wheel_plugin_spec,
)

__all__ = [
    "MAX_DOCS_FILE_BYTES",
    "MAX_DOCS_TOTAL_BYTES",
    "MAX_PLUGIN_SPEC_BYTES",
    "MDX_SAFE_COMPONENTS",
    "PLUGIN_DOCS_DIRNAME",
    "PLUGIN_DOCS_INDEX",
    "PLUGIN_SPEC_FILENAME",
    "EnvRequirement",
    "PluginDocsError",
    "PluginSpecLoadError",
    "accepts_env",
    "load_plugin_spec",
    "parse_plugin_spec",
    "read_dir_docs",
    "read_wheel_docs",
    "read_wheel_plugin_spec",
    "required_env",
    "required_env_for_spec",
    "validate_docs",
]
