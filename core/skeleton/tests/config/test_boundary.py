"""Unit oracles for the shared env-write boundary validator
(:mod:`tai42_skeleton.config.boundary`) and its wiring into
:class:`~tai42_skeleton.config.service.ConfigService`.

Three refusals guard every env / manifest writer:

* X-band refusal — no profile or editor may CARRY a deployment / boot-identity
  key. The refusal reads the writer's PAYLOAD keys, never the post-carry
  effective env (a profile apply legitimately carries the whole X band across
  ``replace_env``). Asserted per writer path (``apply_env_change`` and the
  ``_validate_replace`` entry) and spanning both halves of the union plus the two
  deployment keys.
* Key-material refusal (CHANGE-aware) — no profile or editor may SET a ``key_material``
  field (a KEK / signing key) to a NEW value; these are ``hot``-class (not X-band) yet must
  rotate through their own path. But an UNCHANGED carry is allowed (key material CAN sit in
  the editable store, so a read-modify-write round-trip / a profile snapshotted from the
  stored env re-sends it unchanged), so the refusal compares the payload value to the stored
  value and fires only on a change.
* Dangling ``!ENV`` refusal — a manifest ``!ENV ${VAR}`` marker with no default,
  referencing a var absent from the effective env, resolves silently to ``"N/A"``
  and is refused pre-persist, naming the var and its json-pointer.

The union-covers-inventory oracle imports the core settings groups so half (a)
is populated, then asserts ``x_band_env_keys()`` covers every design-listed X
row. The K8s provider's ``TAI_K8S_*`` group registers only where the (separately
installed) ``tai42-config-k8s`` plugin is imported, so its coverage is pinned in
that plugin's own suite (``plugins/config-k8s/tests/test_settings.py``); the
registry-driven mechanism test here proves any ``excluded`` field is folded in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest
from tai42_kit.settings import registered_settings, reset_all_settings

# Import each core settings module so its classes self-register in the kit registry
# (half (a) of the X band is registry-driven and reflects only IMPORTED classes).
import tai42_skeleton.app.bus_settings
import tai42_skeleton.backend.settings
import tai42_skeleton.config.config_mode
import tai42_skeleton.connectors.settings
import tai42_skeleton.marketplace.prefix
import tai42_skeleton.routers.metrics_settings
import tai42_skeleton.settings.settings  # noqa: F401
from tai42_skeleton.config.boundary import (
    X_BAND_EXTRA,
    excluded_env_var_names,
    key_material_env_keys,
    refuse_dangling_env_markers,
    refuse_incomplete_admin_pair,
    refuse_key_material,
    refuse_x_band,
    registered_env_var_names,
    x_band_env_keys,
)
from tai42_skeleton.config.recycle_policy import X_CLASSIFIED_DEPLOYMENT_BARE_READS
from tai42_skeleton.config.service import ConfigService
from tests.config.test_service import FakeConfigStore, FakeReloadAdmin, RecordingBus

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _reset_settings_after() -> Iterator[None]:
    yield
    reset_all_settings()


def _service(store: FakeConfigStore) -> tuple[ConfigService, FakeReloadAdmin, RecordingBus]:
    admin = FakeReloadAdmin()
    bus = RecordingBus()
    service = ConfigService(config_manager=store, admin=admin, bus=cast("Any", bus))
    return service, admin, bus


# ---------------------------------------------------------------------------
# X-band refusal — per writer path, spanning both union halves + the deployment keys
# ---------------------------------------------------------------------------

# TAI_CONFIG_MODE  = half (a), a registry ``excluded`` field.
# TAI_MANIFEST_PATH = redundant: registry-excluded AND listed in X_BAND_EXTRA.
# TAI_RUN_MODE     = half (b), a boot-identity bare read no settings class declares.
# TAI_SUPERVISED / TAI_READY_SENTINEL_PATH = the two deployment keys.
_X_KEYS = ["TAI_CONFIG_MODE", "TAI_MANIFEST_PATH", "TAI_RUN_MODE", "TAI_SUPERVISED", "TAI_READY_SENTINEL_PATH"]


@pytest.mark.parametrize("x_key", _X_KEYS)
async def test_apply_env_change_refuses_x_band_key(monkeypatch: pytest.MonkeyPatch, x_key: str) -> None:
    monkeypatch.setenv("TAI_BUS_REDIS_URL", "redis://localhost:6379/0")
    reset_all_settings()
    store = FakeConfigStore(manifest={"mcp": []}, env={"EXISTING": "1"})
    service, admin, bus = _service(store)

    with pytest.raises(ValueError, match=x_key):
        await service.apply_env_change({x_key: "carried-value"})

    # Refused before any write / reload / broadcast.
    assert store.env_writes == []
    assert store.env == {"EXISTING": "1"}
    assert admin.calls == 0
    assert bus.publish_calls == []


@pytest.mark.parametrize("x_key", _X_KEYS)
def test_validate_replace_refuses_x_band_key(x_key: str) -> None:
    store = FakeConfigStore(manifest={}, env={"EXISTING": "1"})
    service, _admin, _bus = _service(store)

    with pytest.raises(ValueError, match=x_key):
        service._validate_replace({x_key: "carried-value"})


def test_refuse_x_band_names_every_offender() -> None:
    with pytest.raises(ValueError, match="TAI_SUPERVISED") as exc:
        refuse_x_band(["TAI_SUPERVISED", "OK_KEY", "TAI_RUN_MODE"])
    message = str(exc.value)
    assert "TAI_SUPERVISED" in message
    assert "TAI_RUN_MODE" in message
    assert "OK_KEY" not in message


def test_refuse_x_band_allows_a_plain_payload() -> None:
    refuse_x_band(["OK_KEY", "ANOTHER"])  # no raise


def test_validate_replace_allows_a_plain_profile_env() -> None:
    # A profile carrying only ordinary keys (no X band, no manifest markers) and no
    # backend registered passes without a bus configured.
    store = FakeConfigStore(manifest={}, env={"OLD": "1"})
    service, _admin, _bus = _service(store)
    service._validate_replace({"NEW_KEY": "v"})  # no raise


# ---------------------------------------------------------------------------
# Key-material refusal — per writer path (a hot-class key_material field, NOT X-band)
# ---------------------------------------------------------------------------

# Both are ``KeyMaterial`` fields on ``ConnectorsSettings`` (imported above) and
# resolve to reload_class ``hot`` — so ONLY the key-material refusal guards them, never
# the X band. A profile SETTING either to a new value must be refused and pointed at
# rotation; carrying one UNCHANGED must be allowed.
_KEY_MATERIAL_KEYS = ["CONNECTORS_KEK", "CONNECTORS_STATE_HMAC_KEY"]


def test_key_material_keys_are_registered_and_not_x_band() -> None:
    # The premise the refusal exists for: these ARE key material and are NOT caught by
    # the X band (they are hot-class), so without the dedicated refusal a profile could
    # bulk-set a KEK.
    km = key_material_env_keys()
    assert set(_KEY_MATERIAL_KEYS) <= km
    assert not (km & x_band_env_keys())


@pytest.mark.parametrize("km_key", _KEY_MATERIAL_KEYS)
async def test_apply_env_change_refuses_changed_key_material_key(monkeypatch: pytest.MonkeyPatch, km_key: str) -> None:
    # SETTING key material to a value different from the current store (here: introducing it
    # into a store that lacks it) is a rotation-via-editor and is refused.
    monkeypatch.setenv("TAI_BUS_REDIS_URL", "redis://localhost:6379/0")
    reset_all_settings()
    store = FakeConfigStore(manifest={"mcp": []}, env={"EXISTING": "1"})
    service, admin, bus = _service(store)

    with pytest.raises(ValueError, match=km_key):
        await service.apply_env_change({km_key: "new-secret"})

    # Refused before any write / reload / broadcast — the KEK never reaches the store.
    assert store.env_writes == []
    assert store.env == {"EXISTING": "1"}
    assert admin.calls == 0
    assert bus.publish_calls == []


@pytest.mark.parametrize("km_key", _KEY_MATERIAL_KEYS)
async def test_apply_env_change_allows_unchanged_key_material_carry(
    monkeypatch: pytest.MonkeyPatch, km_key: str
) -> None:
    # The load-bearing fix (studio ``config.spec.ts:62``): the store already HOLDS a KEK; a
    # read-modify-write save re-sends it UNCHANGED (real value from ``GET /api/config/env``)
    # while editing an unrelated key. The change-aware refusal allows the carry, so the save
    # lands — the KEK reaches the store unchanged and the edit applies.
    monkeypatch.setenv("TAI_BUS_REDIS_URL", "redis://localhost:6379/0")
    reset_all_settings()
    store = FakeConfigStore(manifest={"mcp": []}, env={km_key: "provisioned", "EDITME": "old"})
    service, _admin, _bus = _service(store)

    await service.apply_env_change({km_key: "provisioned", "EDITME": "new"})

    assert store.env_writes, "the unchanged-KEK carry was refused — the env save did not land"
    assert store.env[km_key] == "provisioned"
    assert store.env["EDITME"] == "new"


@pytest.mark.parametrize("km_key", _KEY_MATERIAL_KEYS)
def test_validate_replace_refuses_changed_key_material_key(km_key: str) -> None:
    store = FakeConfigStore(manifest={}, env={km_key: "old-kek", "EXISTING": "1"})
    service, _admin, _bus = _service(store)

    with pytest.raises(ValueError, match=km_key):
        service._validate_replace({km_key: "rotated-value", "EXISTING": "1"})


@pytest.mark.parametrize("km_key", _KEY_MATERIAL_KEYS)
def test_validate_replace_allows_unchanged_key_material_carry(km_key: str) -> None:
    # A profile snapshotted from the stored env carries the KEK unchanged (studio
    # ``config-profiles.spec.ts:147``) — allowed.
    store = FakeConfigStore(manifest={}, env={km_key: "provisioned", "EXISTING": "1"})
    service, _admin, _bus = _service(store)

    service._validate_replace({km_key: "provisioned", "EXISTING": "1"})  # no raise


def test_refuse_key_material_names_offenders_and_the_rotation_path() -> None:
    # A CHANGE (new value vs. empty current) names the offender + rotation path.
    with pytest.raises(ValueError, match="CONNECTORS_KEK") as exc:
        refuse_key_material({"CONNECTORS_KEK": "new-kek", "OK_KEY": "v"}, {})
    message = str(exc.value)
    assert "CONNECTORS_KEK" in message
    assert "OK_KEY" not in message
    # Names the rotation path, per the plan's "names the rotation path" refusal rule.
    assert "rotat" in message.lower()


def test_refuse_key_material_allows_unchanged_and_non_km_payload() -> None:
    # A non-key-material payload never fires; a key-material key carried UNCHANGED (equal to
    # its current value) is allowed — only a CHANGE is refused.
    refuse_key_material({"OK_KEY": "1", "ANOTHER": "2"}, {})  # no key material at all
    refuse_key_material({"CONNECTORS_KEK": "same"}, {"CONNECTORS_KEK": "same"})  # unchanged carry


# ---------------------------------------------------------------------------
# Admin-pair (both-or-neither, per database name)
# ---------------------------------------------------------------------------


def test_refuse_incomplete_admin_pair_refuses_user_only_naming_both_vars() -> None:
    with pytest.raises(ValueError, match="TAI_DATABASE_DEFAULT_PG_ADMIN_USER") as exc:
        refuse_incomplete_admin_pair({"TAI_DATABASE_DEFAULT_PG_ADMIN_USER": "migrator"})
    message = str(exc.value)
    assert "TAI_DATABASE_DEFAULT_PG_ADMIN_USER" in message
    assert "TAI_DATABASE_DEFAULT_PG_ADMIN_PASSWORD" in message  # names BOTH halves of the pair


def test_refuse_incomplete_admin_pair_refuses_password_only() -> None:
    with pytest.raises(ValueError, match="TAI_DATABASE_DEFAULT_PG_ADMIN_PASSWORD"):
        refuse_incomplete_admin_pair({"TAI_DATABASE_DEFAULT_PG_ADMIN_PASSWORD": "pw"})


def test_refuse_incomplete_admin_pair_allows_both_and_neither() -> None:
    refuse_incomplete_admin_pair({})  # neither → allowed (runtime identity migrates)
    refuse_incomplete_admin_pair(
        {"TAI_DATABASE_DEFAULT_PG_ADMIN_USER": "m", "TAI_DATABASE_DEFAULT_PG_ADMIN_PASSWORD": "pw"}
    )  # both → allowed
    # An empty-string value does not count as set (the store never persists empties).
    refuse_incomplete_admin_pair({"TAI_DATABASE_DEFAULT_PG_ADMIN_USER": ""})


def test_refuse_incomplete_admin_pair_is_per_name() -> None:
    # Two databases: WAREHOUSE complete, DEFAULT half-set — only DEFAULT is named.
    with pytest.raises(ValueError, match="'DEFAULT'") as exc:
        refuse_incomplete_admin_pair(
            {
                "TAI_DATABASE_WAREHOUSE_PG_ADMIN_USER": "m",
                "TAI_DATABASE_WAREHOUSE_PG_ADMIN_PASSWORD": "pw",
                "TAI_DATABASE_DEFAULT_PG_ADMIN_USER": "m",
            }
        )
    assert "WAREHOUSE" not in str(exc.value)


async def test_apply_env_change_refuses_half_set_admin_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    # Wired into apply_env_change (the plain editor / backup-import path): a change resulting in
    # a half-set admin identity is refused at the validate step before any write.
    monkeypatch.setenv("TAI_BUS_REDIS_URL", "redis://localhost:6379/0")
    reset_all_settings()
    store = FakeConfigStore(manifest={"mcp": []}, env={"EXISTING": "1"})
    service, admin, bus = _service(store)

    with pytest.raises(ValueError, match="TAI_DATABASE_DEFAULT_PG_ADMIN_USER"):
        await service.apply_env_change({"TAI_DATABASE_DEFAULT_PG_ADMIN_USER": "migrator"})

    assert store.env_writes == []
    assert admin.calls == 0
    assert bus.publish_calls == []


def test_validate_replace_refuses_half_set_admin_pair() -> None:
    # Wired into the profile-apply validate entry: a profile carrying only the admin USER
    # (no PASSWORD) is refused, naming the incomplete pair.
    store = FakeConfigStore(manifest={}, env={"EXISTING": "1"})
    service, _admin, _bus = _service(store)

    with pytest.raises(ValueError, match="TAI_DATABASE_DEFAULT_PG_ADMIN_PASSWORD"):
        service._validate_replace({"TAI_DATABASE_DEFAULT_PG_ADMIN_USER": "migrator"})


def test_validate_replace_allows_complete_admin_pair() -> None:
    store = FakeConfigStore(manifest={}, env={"EXISTING": "1"})
    service, _admin, _bus = _service(store)

    service._validate_replace(
        {"TAI_DATABASE_DEFAULT_PG_ADMIN_USER": "m", "TAI_DATABASE_DEFAULT_PG_ADMIN_PASSWORD": "pw"}
    )  # no raise


# ---------------------------------------------------------------------------
# registered_env_var_names — the generated-key shadow-avoidance source
# ---------------------------------------------------------------------------


def test_registered_env_var_names_covers_x_band_and_a_known_hot_var() -> None:
    # The set the generated secret key is made unique against: it must be a SUPERSET of the X
    # band (the excluded fields) AND carry non-excluded (hot/recycle) vars the X band omits —
    # this is exactly what the X band alone would miss for shadow-avoidance.
    names = registered_env_var_names()
    assert excluded_env_var_names() <= names  # every excluded field's env_var is present
    assert "CONNECTORS_KEK" in names  # a registered, NON-excluded var (hot key-material field)
    assert "CONNECTORS_KEK" not in x_band_env_keys()  # which the X band does not carry


# ---------------------------------------------------------------------------
# Union covers every design-listed X inventory row
# ---------------------------------------------------------------------------

# Half (a) on the booted core (registry ``excluded`` fields), verified by symbol
# against the current checkout. TAI_K8S_* is intentionally NOT here — its group
# registers only where the config-k8s plugin is imported (pinned in that suite).
_CORE_EXCLUDED_INVENTORY = frozenset(
    {
        "TAI_CONFIG_MODE",
        "TAI_CONFIG_DIR_PATH",
        "TAI_MANIFEST_PATH",
        "APP_ARGS_TRANSPORT",
        "APP_ARGS_HOST",
        "APP_ARGS_PORT",
        "APP_ARGS_UDS",
        "BACKEND_METRICS_HOST",
        "BACKEND_METRICS_PORT",
        "PROMETHEUS_MULTIPROC_DIR",
        "TAI_PLUGINS_PREFIX",
    }
)


def test_union_covers_the_design_listed_x_inventory() -> None:
    union = x_band_env_keys()
    inventory = _CORE_EXCLUDED_INVENTORY | X_BAND_EXTRA | X_CLASSIFIED_DEPLOYMENT_BARE_READS
    missing = inventory - union
    assert not missing, f"X-band union is missing inventory rows: {sorted(missing)}"


def test_core_excluded_fields_are_registered_after_boot() -> None:
    # The booted core registers exactly the design's half (a) core fields — a drift
    # (a field losing its ``excluded`` reload_class) drops it here.
    assert excluded_env_var_names() >= _CORE_EXCLUDED_INVENTORY


def test_every_registered_excluded_field_is_in_the_union() -> None:
    # The registry-driven guarantee: any field whose reload_class resolves to
    # ``excluded`` (with a non-empty env_var) is folded into the X band. This is the
    # mechanism that picks up a provider group (e.g. the k8s TAI_K8S_* fields) the
    # moment its settings module is imported.
    for info in registered_settings():
        for field in info.fields:
            if field.reload_class == "excluded" and field.env_var:
                assert field.env_var in x_band_env_keys()


# ---------------------------------------------------------------------------
# Dangling !ENV refusal — grammar cases (pure function)
# ---------------------------------------------------------------------------


def test_dangling_marker_without_default_is_refused_naming_var_and_pointer() -> None:
    manifest = {"mcp": [{"title": "s", "config": {"env": {"AUTH": "!ENV ${SECRET_TOKEN}"}}}]}
    with pytest.raises(ValueError, match="SECRET_TOKEN") as exc:
        refuse_dangling_env_markers(manifest, {})
    message = str(exc.value)
    assert "SECRET_TOKEN" in message
    assert "/mcp/0/config/env/AUTH" in message


def test_marker_with_default_never_dangles() -> None:
    refuse_dangling_env_markers({"k": "!ENV ${SECRET_TOKEN:fallback}"}, {})  # no raise


def test_bare_marker_without_braces_never_dangles() -> None:
    refuse_dangling_env_markers({"k": "!ENV SECRET_TOKEN"}, {})  # no raise


def test_present_var_does_not_dangle() -> None:
    refuse_dangling_env_markers({"k": "!ENV ${SECRET_TOKEN}"}, {"SECRET_TOKEN": "x"})  # no raise


def test_non_marker_scalar_is_ignored() -> None:
    # A plain string that merely contains a ${...} sequence is not an !ENV marker.
    refuse_dangling_env_markers({"k": "literal ${SECRET_TOKEN} text"}, {})  # no raise


def test_refusal_reports_only_the_dangling_ref() -> None:
    # Only the defaultless, absent ref is named; a sibling present ref is not.
    manifest = {"a": "!ENV ${MISSING_ONE}", "b": "!ENV ${PRESENT}"}
    with pytest.raises(ValueError, match="MISSING_ONE") as exc:
        refuse_dangling_env_markers(manifest, {"PRESENT": "1"})
    message = str(exc.value)
    assert "MISSING_ONE" in message
    assert "PRESENT" not in message


# ---------------------------------------------------------------------------
# Dangling !ENV refusal — wired into the writer paths
# ---------------------------------------------------------------------------


async def test_apply_env_change_dropping_a_referenced_key_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_BUS_REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.delenv("SECRET_TOKEN", raising=False)
    reset_all_settings()
    # The manifest references SECRET_TOKEN through a defaultless marker; the store
    # supplies it. Emptying it in the change removes it from the effective env, so the
    # marker would silently resolve to "N/A" — refused, naming the key and the path.
    store = FakeConfigStore(
        manifest={"mcp": [{"title": "s", "config": {"env": {"AUTH": "!ENV ${SECRET_TOKEN}"}}}]},
        env={"SECRET_TOKEN": "live"},
    )
    service, admin, bus = _service(store)

    with pytest.raises(ValueError, match="SECRET_TOKEN"):
        await service.apply_env_change({"SECRET_TOKEN": ""})

    assert store.env_writes == []
    assert admin.calls == 0
    assert bus.publish_calls == []


def test_validate_replace_dropping_a_referenced_key_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SECRET_TOKEN", raising=False)
    # A profile that omits SECRET_TOKEN (replace DELETES omitted keys) leaves the
    # manifest marker dangling — refused.
    store = FakeConfigStore(
        manifest={"mcp": [{"title": "s", "config": {"env": {"AUTH": "!ENV ${SECRET_TOKEN}"}}}]},
        env={"SECRET_TOKEN": "live"},
    )
    service, _admin, _bus = _service(store)

    with pytest.raises(ValueError, match="SECRET_TOKEN"):
        service._validate_replace({"OTHER": "v"})


# ---------------------------------------------------------------------------
# Backup-import refusal fixture — an X key AND a dangling !ENV in one payload
# ---------------------------------------------------------------------------


async def test_backup_import_carrying_x_key_and_dangling_marker_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # Backup restore drives apply_env_change directly, so a crafted backup carrying an
    # X-band key (here the half-(a) TAI_CONFIG_MODE) AND dropping a manifest-referenced
    # key is refused by the shared validator BY CONSTRUCTION — no write lands.
    monkeypatch.setenv("TAI_BUS_REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.delenv("SECRET_TOKEN", raising=False)
    reset_all_settings()
    store = FakeConfigStore(
        manifest={"mcp": [{"title": "s", "config": {"env": {"AUTH": "!ENV ${SECRET_TOKEN}"}}}]},
        env={"SECRET_TOKEN": "live"},
    )
    service, admin, bus = _service(store)

    with pytest.raises(ValueError, match="TAI_CONFIG_MODE"):
        await service.apply_env_change({"TAI_CONFIG_MODE": "file", "SECRET_TOKEN": ""})

    assert store.env_writes == []
    assert store.env == {"SECRET_TOKEN": "live"}
    assert admin.calls == 0
    assert bus.publish_calls == []


async def test_backup_import_setting_key_material_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # Backup restore drives apply_env_change directly, so a crafted backup that SETS a
    # key-material key (a KEK) to a value differing from the current store — here, introducing
    # one the store lacks — is refused by the shared validator BY CONSTRUCTION; the KEK never
    # reaches the store, exactly as the env editor and profile paths refuse a key-material
    # change (an unchanged carry would be allowed).
    monkeypatch.setenv("TAI_BUS_REDIS_URL", "redis://localhost:6379/0")
    reset_all_settings()
    store = FakeConfigStore(manifest={"mcp": []}, env={"EXISTING": "1"})
    service, admin, bus = _service(store)

    with pytest.raises(ValueError, match="CONNECTORS_KEK"):
        await service.apply_env_change({"CONNECTORS_KEK": "leaked-key"})

    assert store.env_writes == []
    assert store.env == {"EXISTING": "1"}
    assert admin.calls == 0
    assert bus.publish_calls == []
