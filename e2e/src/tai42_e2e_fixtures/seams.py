"""SUT-side registrations for the three platform seams the harness observes:

* a declared **preset seed** (``register_seed``) so a boot makes it a live callable
  preset tool — with an env-selected UPGRADED body so a reload drives the
  content-change upgrade of the tagged version;
* a **rename referee** (``register_rename_referee``) that reports a holder for a
  marked target (proving a plugin-declared reference blocks a rename and joins the
  ``/referees`` union) plus a RAISING variant (proving a referee exception fails the
  rename loudly);
* an **invocation-seam probe** tool that reports the in-flight tool name
  ``current_tool_invocation()`` deposits for its own call, and the ``None`` a reader
  sees where no deposit is in scope.

Each registration runs at IMPORT, so the manifest tool loader re-runs it on every
boot/reload (the registries are reset each ``start()``). Generic vocab only."""

from __future__ import annotations

import os

from tai42_contract.app import tai42_app
from tai42_contract.presets import PresetSeed, PresetSeedToolMeta
from tai42_contract.tools import current_tool_invocation

# -- preset seed -------------------------------------------------------------

# The seed's declared name; a boot binds it as a live callable preset over ``e2e_echo``.
_SEED_NAME = "e2e_seed_probe"
# The env toggle a stack flips (through the env-write door) to ship an UPGRADED body:
# a reload re-imports this module, re-declares the drifted seed, and the applier
# upgrades the tagged version in place.
_SEED_VARIANT_ENV = "E2E_SEED_VARIANT"
_SEED_UPGRADED = "upgraded"


def _seed() -> PresetSeed:
    """The declared seed for the CURRENT env variant: a distinct description +
    baked ``payload`` for the upgraded body so its content genuinely drifts."""
    upgraded = os.environ.get(_SEED_VARIANT_ENV) == _SEED_UPGRADED
    payload = "seeded-upgraded" if upgraded else "seeded-base"
    description = "the shipped seed probe (upgraded)" if upgraded else "the shipped seed probe"
    return PresetSeed(
        name=_SEED_NAME,
        description=description,
        base_tool="e2e_echo",
        fixed_kwargs={"payload": payload},
        tool_meta=PresetSeedToolMeta(display_name="Seed Probe", tags=["seeded"], folder_path="e2e/seeds"),
    )


tai42_app.presets.register_seed(_seed())


# -- rename referee ----------------------------------------------------------

# A rename of a tool whose name carries this marker draws the fixture holder; a name
# carrying the RAISE marker makes the referee raise, failing the rename loudly. Both
# markers are name substrings so a test names its preset to drive either arm.
_REFEREE_HOLD_MARKER = "e2e_ref_hold"
_REFEREE_RAISE_MARKER = "e2e_ref_raise"


def _fixture_holder_text(old_name: str) -> str:
    """The generic holder description the fixture referee returns for a marked
    target — asserted verbatim on the blocked rename and in the referees union."""
    return f"e2e fixture reference to {old_name!r}"


async def _fixture_rename_referee(old_name: str) -> list[str]:
    """Report a holder for a HOLD-marked target and RAISE for a RAISE-marked one; any
    other name draws no objection. A raising referee must fail the rename loudly, never
    a silent bypass."""
    if _REFEREE_RAISE_MARKER in old_name:
        raise RuntimeError(f"e2e fixture referee cannot read holders for {old_name!r}")
    if _REFEREE_HOLD_MARKER in old_name:
        return [_fixture_holder_text(old_name)]
    return []


tai42_app.tools.register_rename_referee(_fixture_rename_referee)


# -- invocation-seam probe ---------------------------------------------------


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_invocation_probe() -> dict:
    """Report the invocation seam INSIDE this call and where no deposit is in scope.

    ``inside`` is the in-flight tool name ``current_tool_invocation()`` deposits for
    this call — the invoked tool's own name, populated over the ``run_tool`` seam AND
    the MCP call edge. ``outside`` reads the seam on a bare thread that inherits no
    ContextVar deposit, so it observes the ``None`` a reader sees when no tool is
    executing."""
    import os as _os
    import threading

    inside = current_tool_invocation()
    inside_name = inside.tool_name if inside is not None else None

    # A bare OS thread starts every ContextVar at its default, so the seam there is the
    # out-of-call ``None`` — never the deposit this call's context carries.
    outside: dict[str, str | None] = {}

    def _read_outside() -> None:
        seen = current_tool_invocation()
        outside["value"] = seen.tool_name if seen is not None else None

    reader = threading.Thread(target=_read_outside)
    reader.start()
    reader.join()

    return {"inside": inside_name, "outside": outside["value"], "pid": _os.getpid()}
