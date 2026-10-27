"""A neutral chain-declaring plugin distribution, laid out on disk the way ``pip`` leaves one.

The schema-gate suites install it into a scratch site directory: an import package with a
``tools`` module, a packaged ``tai-plugin.yml`` declaring a ``migrations`` chain, one SQL
file, and a ``.dist-info`` whose ``RECORD`` lists those files, so installed metadata maps
the top-level package to the distribution and its spec without importing anything.
"""

from __future__ import annotations

from pathlib import Path

DIST = "tai42-chainprobe"
TOP_LEVEL = "tai42_chainprobe"
TOOLS_MODULE = f"{TOP_LEVEL}.tools"
# The ``migrations_component`` override the default spec declares, and its binding env.
COMPONENT = "chainprobe-store"
BINDING_ENV = "TAI_DB_BINDING_CHAINPROBE_STORE"
CHAIN_FILE = "0001_items.sql"

_TOOLS_BODY = '''\
from tai42_contract.app import tai42_app


@tai42_app.tools.tool
def chainprobe_ping() -> str:
    """Answer a fixed reply."""
    return "pong"
'''


def spec_yaml(*, migrations_component: str | None = COMPONENT) -> str:
    """The packaged ``tai-plugin.yml``: a tool provider declaring a ``migrations`` chain."""
    override = f"migrations_component: {migrations_component}\n" if migrations_component else ""
    return (
        "spec_version: 1\n"
        "namespace: tai42\n"
        "name: chainprobe\n"
        f"package: {DIST}\n"
        "version: 1.0.0\n"
        "description: A neutral chain-declaring test plugin\n"
        "license: Apache-2.0\n"
        "contract: '>=0.1,<1000'\n"
        "categories: [dev]\n"
        "provides:\n"
        "  - kind: tool\n"
        "    name: chainprobe_ping\n"
        f"    module: {TOOLS_MODULE}\n"
        "    description: d\n"
        "migrations: migrations\n"
        f"{override}"
    )


def lay_out(
    site: Path,
    *,
    spec_text: str,
    init_body: str = "",
    second_top_level: str | None = None,
    requires_contract: str | None = None,
) -> Path:
    """Write the distribution into ``site`` and return its import-package directory.

    ``init_body`` is the import package's ``__init__`` source. ``second_top_level`` names a
    second top-level package the distribution also installs, one that ships no spec.
    ``requires_contract`` is the ``tai42-contract`` range its metadata declares, if any.
    """
    site.mkdir(parents=True, exist_ok=True)
    package = site / TOP_LEVEL
    (package / "migrations").mkdir(parents=True)
    files = {
        package / "__init__.py": init_body,
        package / "tools.py": _TOOLS_BODY,
        package / "tai-plugin.yml": spec_text,
        package / "migrations" / CHAIN_FILE: "CREATE TABLE chainprobe_items (id integer PRIMARY KEY);\n",
    }
    if second_top_level is not None:
        (site / second_top_level).mkdir()
        files[site / second_top_level / "__init__.py"] = ""
    for path, body in files.items():
        path.write_text(body, encoding="utf-8")
    dist_info = site / f"{DIST.replace('-', '_')}-1.0.0.dist-info"
    dist_info.mkdir()
    metadata = f"Metadata-Version: 2.1\nName: {DIST}\nVersion: 1.0.0\n"
    if requires_contract is not None:
        metadata += f"Requires-Dist: tai42-contract{requires_contract}\n"
    (dist_info / "METADATA").write_text(metadata, encoding="utf-8")
    rows = [f"{path.relative_to(site).as_posix()},," for path in files]
    rows += [f"{dist_info.name}/METADATA,,", f"{dist_info.name}/RECORD,,"]
    (dist_info / "RECORD").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return package
