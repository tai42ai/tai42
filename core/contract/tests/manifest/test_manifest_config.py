"""Validator tests for the manifest config models — the extensions mixin, ``ApiToolsConfig``,
``TaiMCPConfig``/``AgentsConfig``, and the ``Manifest`` module-list fields."""

from __future__ import annotations

from typing import Any, cast

import pytest
from pydantic import ValidationError

from tai42_contract.manifest import AgentsConfig, ApiToolsConfig, Manifest, MCPConfig, TaiMCPConfig, ToolsConfig

# === manifest — ExtensionsConfigMixin.normalize_extensions ==================


def _tools_cfg(**overrides: Any) -> ToolsConfig:
    base: dict[str, Any] = {"title": "t", "module": "m"}
    base.update(overrides)
    return ToolsConfig(**base)


def test_extensions_flat_value_wraps_as_single_combo():
    # {weather: [chain]} normalizes to a single combo {"weather": [["chain"]]}.
    cfg = _tools_cfg(extensions={"weather": ["chain"]})
    assert cfg.extensions == {"weather": [["chain"]]}


def test_extensions_list_of_combos_kept_unchanged():
    # {report: [[chain], [chain, batch]]} stays two combos.
    cfg = _tools_cfg(extensions={"report": [["chain"], ["chain", "batch"]]})
    assert cfg.extensions == {"report": [["chain"], ["chain", "batch"]]}


def test_extensions_default_empty():
    assert _tools_cfg().extensions == {}


def test_extensions_mixed_value_raises_naming_key():
    with pytest.raises(ValueError, match="'x'"):
        _tools_cfg(extensions={"x": ["chain", ["batch"]]})


def test_extensions_non_list_value_raises_naming_key():
    with pytest.raises(ValueError, match=r"'x'.*must be a list"):
        _tools_cfg(extensions={"x": "chain"})


def test_extensions_empty_list_value_raises_naming_key():
    with pytest.raises(ValueError, match=r"'x'.*must not be empty"):
        _tools_cfg(extensions={"x": []})


def test_extensions_empty_inner_combo_raises_naming_key():
    with pytest.raises(ValueError, match=r"'x'.*combo must not be empty"):
        _tools_cfg(extensions={"x": [[]]})


def test_extensions_non_string_member_raises_naming_key():
    # A combo member that is neither a name nor a {"name", "config"} mapping (here
    # a bare int) is rejected loudly, naming the key.
    with pytest.raises(ValueError, match=r"'x'.*extension name or a"):
        _tools_cfg(extensions={"x": [["chain", 1]]})


def test_extensions_non_dict_raises():
    with pytest.raises(ValueError, match="extensions must be a mapping"):
        _tools_cfg(extensions="not-a-dict")


# -- {"name", "config"} combo elements (author-bound config) ------------------


def test_extensions_dict_element_in_flat_combo_binds_config():
    # A flat combo may mix a bare name and a {"name", "config"} mapping; the
    # mapping is wrapped into the single combo unchanged.
    cfg = _tools_cfg(extensions={"sign": [{"name": "ask_external", "config": {"verifier": {"name": "gh"}}}, "monitor"]})
    assert cfg.extensions == {"sign": [[{"name": "ask_external", "config": {"verifier": {"name": "gh"}}}, "monitor"]]}


def test_extensions_dict_element_in_list_of_combos():
    combos = [[{"name": "ask_external", "config": {"verifier": {"name": "gh"}}}], ["monitor"]]
    cfg = _tools_cfg(extensions={"sign": combos})
    assert cfg.extensions == {"sign": combos}


def test_extensions_dict_element_missing_name_raises():
    with pytest.raises(ValueError, match="non-empty string 'name'"):
        _tools_cfg(extensions={"x": [{"config": {}}]})


def test_extensions_dict_element_non_dict_config_raises():
    with pytest.raises(ValueError, match="must carry a 'config' mapping"):
        _tools_cfg(extensions={"x": [{"name": "ask_external", "config": "nope"}]})


def test_extensions_dict_element_missing_config_raises():
    with pytest.raises(ValueError, match="must carry a 'config' mapping"):
        _tools_cfg(extensions={"x": [{"name": "ask_external"}]})


def test_extensions_dict_element_extra_key_raises():
    with pytest.raises(ValueError, match="unexpected keys"):
        _tools_cfg(extensions={"x": [{"name": "ask_external", "config": {}, "bogus": 1}]})


def test_extensions_independent_of_include():
    # include (selection) and extensions (attachment) are independent: a config
    # with BOTH a non-empty include and an extensions map validates, and each
    # keeps its own shape (include stays a plain name list).
    cfg = _tools_cfg(include=["weather", "report"], extensions={"weather": ["chain"]})
    assert cfg.include == ["weather", "report"]
    assert cfg.exclude == []
    assert cfg.extensions == {"weather": [["chain"]]}


def test_taimcpconfig_accepts_extensions():
    # The mixin sits on TaiMCPConfig too: an MCP config carries the map.
    cfg = TaiMCPConfig(
        title="t",
        config=MCPConfig(url="https://x"),
        extensions=cast("dict[str, Any]", {"search": ["chain"]}),
    )
    assert cfg.extensions == {"search": [["chain"]]}


def test_agents_config_extensions_key_raises_extra_field():
    # extra="forbid": an extensions key on an agents config is a loud pydantic
    # extra-field error, never silently ignored (the mixin is NOT on AgentsConfig).
    kwargs: dict[str, Any] = {"title": "t", "module": "m", "extensions": {"x": ["chain"]}}
    with pytest.raises(ValueError, match=r"[Ee]xtra"):
        AgentsConfig(**kwargs)


# === manifest — ApiToolsConfig ==============================================


def test_api_tools_defaults_no_args():
    # Default construction (what Manifest's default_factory calls) succeeds and
    # yields the locked default-in shape.
    cfg = ApiToolsConfig()
    assert cfg.enabled is True
    assert cfg.expose_destructive is True
    assert cfg.include == []
    assert cfg.exclude == []
    assert cfg.extensions == {}


def test_api_tools_include_only():
    cfg = ApiToolsConfig(include=["reload_config", "list_hooks"])
    assert cfg.include == ["reload_config", "list_hooks"]
    assert cfg.exclude == []


def test_api_tools_exclude_only():
    cfg = ApiToolsConfig(exclude=["remove_tool"])
    assert cfg.exclude == ["remove_tool"]
    assert cfg.include == []


def test_api_tools_include_exclude_overlap_raises():
    # An op in BOTH lists is a loud validation error — the deliberate deviation
    # from BaseConfig semantics, unique to this config.
    with pytest.raises(ValueError, match=r"both include and exclude"):
        ApiToolsConfig(include=["reload_config"], exclude=["reload_config"])


def test_api_tools_extensions_map_round_trips():
    # The mixin's extensions map is carried and normalized like any other config,
    # and survives a model_dump/model_validate round-trip.
    cfg = ApiToolsConfig(extensions=cast("dict[str, Any]", {"reload_config": [["cache"]]}))
    assert cfg.extensions == {"reload_config": [["cache"]]}
    assert ApiToolsConfig.model_validate(cfg.model_dump()).extensions == {"reload_config": [["cache"]]}


def test_manifest_api_tools_defaults_and_survives_model_dump():
    # api_tools is a NORMAL serialized field: absent input materializes a default
    # ApiToolsConfig, and it survives model_dump so live_manifest carries it.
    m = Manifest()
    assert isinstance(m.api_tools, ApiToolsConfig)
    assert m.api_tools.enabled is True
    dumped = m.model_dump()
    assert dumped["api_tools"] == {
        "enabled": True,
        "expose_destructive": True,
        "include": [],
        "exclude": [],
        "extensions": {},
    }


def test_manifest_api_tools_round_trips_when_present():
    m = Manifest(api_tools=ApiToolsConfig(enabled=False, exclude=["remove_tool"]))
    restored = Manifest.model_validate(m.model_dump())
    assert restored.api_tools.enabled is False
    assert restored.api_tools.exclude == ["remove_tool"]


def test_manifest_unknown_top_level_key_raises_naming_it():
    # extra="forbid": an unknown top-level manifest key is a misconfig and must
    # fail loudly naming the offending key, not be silently dropped.
    with pytest.raises(ValidationError, match="not_a_real_key"):
        Manifest.model_validate({"not_a_real_key": 1})


# === monitoring — MonitoringFilter._check_ranges ============================
# === manifest — Manifest ``*_modules`` fields ===============================


def test_manifest_modules_default_to_empty_lists():
    m = Manifest()
    assert m.middlewares_modules == []
    assert m.routers_modules == []
    assert m.extensions_modules == []
    assert m.lifecycle_modules == []
    assert m.webhook_verifier_modules == []
    assert m.channel_modules == []
    assert m.studio_plugins == []


def test_manifest_studio_plugins_survive_model_dump():
    # studio_plugins is a NORMAL serialized field (no exclude) so the registry
    # can read it back off live_manifest's model_dump output.
    m = Manifest(studio_plugins=["acme_plugin"])
    assert m.model_dump()["studio_plugins"] == ["acme_plugin"]


@pytest.mark.parametrize(
    "field",
    [
        "middlewares_modules",
        "routers_modules",
        "extensions_modules",
        "lifecycle_modules",
        "webhook_verifier_modules",
        "channel_modules",
        "studio_plugins",
    ],
)
def test_manifest_modules_reject_explicit_null(field: str):
    kwargs: dict[str, Any] = {field: None}
    with pytest.raises(ValueError, match="valid list"):
        Manifest(**kwargs)
