"""yaml_util: comment-preserving round-trip load/dump + the merge helper."""

from pathlib import Path

import pytest

from tai42_kit.utils.data.yaml_util import (
    dump_manifest,
    load_manifest,
    merge_and_dump_manifest,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "manifest.yml"


def test_load_manifest_preserves_env_marker():
    doc = load_manifest("db:\n  host: !ENV DB_HOST\n  port: 5432\n")
    assert doc["db"]["host"] == "!ENV DB_HOST"
    assert doc["db"]["port"] == 5432


def test_load_manifest_empty_is_empty_mapping():
    assert load_manifest("") == {}
    assert dict(load_manifest("# only a comment\n")) == {}


def test_load_manifest_non_mapping_raises():
    with pytest.raises(TypeError):
        load_manifest("- a\n- b\n")


def test_commented_fixture_is_byte_stable_round_trip():
    src = _FIXTURE.read_text()
    assert dump_manifest(load_manifest(src)) == src


def test_env_tag_survives_round_trip_as_marker():
    doc = load_manifest("host: !ENV DB_HOST\n")
    out = dump_manifest(doc)
    # Emitted as a real !ENV tag, never the whole marker wrapped as a quoted
    # scalar (which would reload as literal text rather than a tag).
    assert "'!ENV DB_HOST'" not in out
    assert '"!ENV DB_HOST"' not in out
    assert "!ENV" in out
    assert load_manifest(out)["host"] == "!ENV DB_HOST"


def test_env_marker_with_special_chars_round_trips():
    # A marker value carrying YAML-significant characters (a colon, a default
    # expansion, a quote) must still emit as an !ENV tag and reload exactly.
    doc = load_manifest('host: !ENV "DB_HOST:-it\'s:default"\n')
    assert doc["host"] == "!ENV DB_HOST:-it's:default"
    out = dump_manifest(doc)
    assert "!ENV" in out
    assert load_manifest(out)["host"] == "!ENV DB_HOST:-it's:default"


def test_caller_authored_marker_string_emits_as_tag():
    # A marker string assigned directly onto the document (not produced by
    # load_manifest) must still dump back as a genuine !ENV tag.
    doc = load_manifest("host: placeholder\n")
    doc["host"] = "!ENV NEW_VAR"
    out = dump_manifest(doc)
    assert "host: !ENV NEW_VAR" in out
    assert load_manifest(out)["host"] == "!ENV NEW_VAR"


def test_sequences_indented_under_parent_key():
    out = merge_and_dump_manifest(defaults={"items": ["x", "y"]}, current=load_manifest(""), manifest={})
    lines = out.splitlines()
    assert lines[0] == "items:"
    assert lines[1] == "  - x"
    assert lines[2] == "  - y"


def test_merge_values_identical_to_shallow_dict_merge():
    defaults = {"a": 1, "b": 1}
    current = load_manifest("b: 2\nc: 2\n")
    manifest = {"c": 3}
    out = merge_and_dump_manifest(defaults, current, manifest)
    # defaults | current | manifest -> later sources win on conflict.
    assert dict(load_manifest(out)) == {"a": 1, "b": 2, "c": 3}


def test_merge_backfills_missing_defaults_only():
    current = load_manifest("kept: existing\n")
    out = merge_and_dump_manifest(defaults={"kept": "default", "added": "default"}, current=current, manifest={})
    parsed = load_manifest(out)
    assert parsed["kept"] == "existing"
    assert parsed["added"] == "default"


def test_merge_preserves_untouched_comments():
    src = _FIXTURE.read_text()
    current = load_manifest(src)
    # Edit one top-level key; every untouched key's comment must survive.
    out = merge_and_dump_manifest(defaults={}, current=current, manifest={"backend_module": "myapp.backend"})
    assert "myapp.backend" in out
    # A comment attached to an untouched, unrelated section is intact.
    assert "modules with on_startup / on_reload handlers" in out
    assert "registers the Storage provider" in out
    # The edited key kept its own trailing inline comment.
    assert "backend_module:" in out
    assert "# registers the agent backend" in out


def test_dump_manifest_preserves_env_marker_export_convention():
    # A dump/load cycle must keep marker strings satisfying the "!ENV <expr>"
    # convention.
    doc = load_manifest("a: !ENV ONE\nb:\n  c: !ENV TWO\n")
    exported = load_manifest(dump_manifest(doc))
    assert exported["a"] == "!ENV ONE"
    assert exported["b"]["c"] == "!ENV TWO"
