"""The declared-default-preset seed models (``tai42_contract.presets.models``).

Pin the SHAPE a plugin ships for import-time preset seeding: ``PresetSeed`` and
its optional ``PresetSeedToolMeta`` display block construct from required and
optional fields, and their defaults are the "nothing declared" state the applier
reads (no baked kwargs, no schemas, no display seed). The applier logic lives
skeleton-side; here we pin only the contract-level model construction.
"""

from __future__ import annotations


def test_preset_seed_tool_meta_all_fields_default_none():
    from tai42_contract.presets.models import PresetSeedToolMeta

    # An empty display seed declares nothing: every field defaults absent so the
    # applier overwrites no operator-set display value.
    meta = PresetSeedToolMeta()
    assert meta.display_name is None
    assert meta.tags is None
    assert meta.folder_path is None


def test_preset_seed_tool_meta_constructs_with_all_fields():
    from tai42_contract.presets.models import PresetSeedToolMeta

    meta = PresetSeedToolMeta(display_name="Summarize", tags=["text", "nlp"], folder_path="Text/Tools")
    assert meta.display_name == "Summarize"
    assert meta.tags == ["text", "nlp"]
    assert meta.folder_path == "Text/Tools"


def test_preset_seed_required_fields_and_defaults():
    from tai42_contract.presets.models import PresetSeed

    # Only name/description/base_tool are required; the baked kwargs default empty
    # and both schemas plus the display seed default to the "not declared" None.
    seed = PresetSeed(name="summarize", description="Summarize input text.", base_tool="agent")
    assert seed.name == "summarize"
    assert seed.description == "Summarize input text."
    assert seed.base_tool == "agent"
    assert seed.fixed_kwargs == {}
    assert seed.input_schema is None
    assert seed.output_schema is None
    assert seed.tool_meta is None


def test_preset_seed_constructs_with_all_optional_fields():
    from tai42_contract.presets.models import PresetSeed, PresetSeedToolMeta

    seed = PresetSeed(
        name="summarize",
        description="Summarize input text.",
        base_tool="agent",
        fixed_kwargs={"tone": "concise"},
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        output_schema={"type": "object", "properties": {"summary": {"type": "string"}}},
        tool_meta=PresetSeedToolMeta(display_name="Summarize", tags=["text"], folder_path="Text"),
    )
    assert seed.fixed_kwargs == {"tone": "concise"}
    assert seed.input_schema == {"type": "object", "properties": {"text": {"type": "string"}}}
    assert seed.output_schema == {"type": "object", "properties": {"summary": {"type": "string"}}}
    assert seed.tool_meta is not None
    assert seed.tool_meta.display_name == "Summarize"


def test_preset_seed_round_trips_through_model_dump():
    from tai42_contract.presets.models import PresetSeed, PresetSeedToolMeta

    seed = PresetSeed(
        name="summarize",
        description="Summarize input text.",
        base_tool="agent",
        fixed_kwargs={"tone": "concise"},
        tool_meta=PresetSeedToolMeta(tags=["text"]),
    )
    again = PresetSeed(**seed.model_dump())
    assert again == seed
