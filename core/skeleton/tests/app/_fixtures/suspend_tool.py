"""A fixture tool that returns the async-park sentinel, for the dispatch-path
preservation of a ``SuspendedInteraction`` through a preset (``TransformedTool``)."""

from tai42_contract.app import tai42_app
from tai42_contract.interactions import SuspendedInteraction
from tai42_contract.presets import PresetInputSchemaSupport


@tai42_app.tools.tool
def make_suspend() -> SuspendedInteraction:
    """Return an async-park sentinel."""
    return SuspendedInteraction(interaction_id="i-preset")


@tai42_app.tools.tool
def make_suspend_payload(payload: dict) -> SuspendedInteraction:
    """Accept a routed input-schema payload, then return an async-park sentinel."""
    return SuspendedInteraction(interaction_id="i-preset")


@tai42_app.tools.tool
def echo_payload(payload: dict) -> dict:
    """Accept a routed input-schema payload and return a conforming answer object."""
    return {"answer": "ok"}


# An input_schema preset routes the caller's validated object into ``payload_arg``;
# declare the support so the fixture bases accept one.
tai42_app.presets.register_input_schema_support("make_suspend_payload", PresetInputSchemaSupport(payload_arg="payload"))
tai42_app.presets.register_input_schema_support("echo_payload", PresetInputSchemaSupport(payload_arg="payload"))
