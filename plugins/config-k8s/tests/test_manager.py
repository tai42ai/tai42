"""Tests for ``K8sConfigManager`` Secret / ConfigMap read-write + merge logic.

Real ``kubernetes.client`` model classes and ``ApiException`` are used, but
``CoreV1Api`` is replaced by an in-memory recorder injected over the manager's
``_core_api`` cached property — no cluster, no network.
"""

from __future__ import annotations

import base64

import pytest
import yaml
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from tai42_config_k8s import settings as settings_mod
from tai42_config_k8s.manager import K8sConfigError, K8sConfigManager

NAMESPACE = "default"
SECRET_NAME = "tai-env"
CONFIGMAP_NAME = "tai-manifest"
MANIFEST_KEY = "manifest.yml"
DEFAULTS_KEY = "defaults.manifest.yml"


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("utf-8")


def _secret(data: dict[str, str] | None, rv: str = "rv-1") -> client.V1Secret:
    """A read-shaped Secret carrying the ``resourceVersion`` the write precondition needs."""
    return client.V1Secret(data=data, metadata=client.V1ObjectMeta(name=SECRET_NAME, resource_version=rv))


class FakeCoreV1Api:
    """In-memory stand-in for ``kubernetes.client.CoreV1Api``.

    Each ``*_result`` is returned by the matching read; setting a ``*_exc`` makes
    that call raise instead. Patches are recorded for assertions.
    """

    def __init__(self) -> None:
        self.secret_result: client.V1Secret | None = None
        self.secret_exc: Exception | None = None
        self.configmap_result: client.V1ConfigMap | None = None
        self.configmap_exc: Exception | None = None
        self.replace_secret_exc: Exception | None = None
        self.patch_configmap_exc: Exception | None = None

        self.read_secret_calls: list[tuple[str, str]] = []
        self.read_configmap_calls: list[tuple[str, str]] = []
        self.replaced_secret: client.V1Secret | None = None
        self.replaced_secret_call: tuple[str, str] | None = None
        self.patched_configmap: client.V1ConfigMap | None = None
        self.patched_configmap_call: tuple[str, str] | None = None

    def read_namespaced_secret(self, name: str, namespace: str) -> client.V1Secret:
        self.read_secret_calls.append((name, namespace))
        if self.secret_exc is not None:
            raise self.secret_exc
        assert self.secret_result is not None
        return self.secret_result

    def replace_namespaced_secret(self, name: str, namespace: str, body: client.V1Secret) -> client.V1Secret:
        self.replaced_secret_call = (name, namespace)
        if self.replace_secret_exc is not None:
            raise self.replace_secret_exc
        self.replaced_secret = body
        # A whole-object replace: the resulting Secret holds EXACTLY body.string_data
        # (base64-encoded into data), so a key absent from string_data is gone. The
        # stored result is exposed so a test asserts the RESULTING Secret state.
        self.secret_result = client.V1Secret(
            data={k: _b64(v) for k, v in (body.string_data or {}).items()},
            metadata=client.V1ObjectMeta(name=name, resource_version="rv-next"),
        )
        return body

    def resulting_env(self) -> dict[str, str]:
        """Decode the current stored Secret's data — the RESULTING state after a write."""
        assert self.secret_result is not None
        data = self.secret_result.data or {}
        return {k: base64.b64decode(v, validate=True).decode("utf-8") for k, v in data.items()}

    def read_namespaced_config_map(self, name: str, namespace: str) -> client.V1ConfigMap:
        self.read_configmap_calls.append((name, namespace))
        if self.configmap_exc is not None:
            raise self.configmap_exc
        assert self.configmap_result is not None
        return self.configmap_result

    def patch_namespaced_config_map(self, name: str, namespace: str, body: client.V1ConfigMap) -> client.V1ConfigMap:
        self.patched_configmap_call = (name, namespace)
        if self.patch_configmap_exc is not None:
            raise self.patch_configmap_exc
        self.patched_configmap = body
        return body


@pytest.fixture
def manager_and_api(monkeypatch: pytest.MonkeyPatch, tmp_path) -> tuple[K8sConfigManager, FakeCoreV1Api]:
    # No service-account file -> namespace "default".
    monkeypatch.setattr(settings_mod, "_SA_NAMESPACE_PATH", tmp_path / "missing")

    manager = K8sConfigManager()
    api = FakeCoreV1Api()
    # Inject over the per-instance cached_property so no real client is built.
    manager._core_api = api  # type: ignore[attr-defined]
    return manager, api


# -- read_env ----------------------------------------------------------------


def test_read_env_decodes_secret_data(manager_and_api) -> None:
    manager, api = manager_and_api
    api.secret_result = client.V1Secret(data={"API_KEY": _b64("s3cret"), "HOST": _b64("db")})
    assert manager.read_env() == {"API_KEY": "s3cret", "HOST": "db"}
    assert api.read_secret_calls == [(SECRET_NAME, NAMESPACE)]


def test_read_env_empty_when_no_data(manager_and_api) -> None:
    manager, api = manager_and_api
    api.secret_result = client.V1Secret(data=None)
    assert manager.read_env() == {}


def test_read_env_missing_secret_raises_filenotfound(manager_and_api) -> None:
    # Absent Secret (404) -> FileNotFoundError, per the ConfigManager contract.
    manager, api = manager_and_api
    api.secret_exc = ApiException(status=404, reason="Not Found")
    with pytest.raises(FileNotFoundError, match="not found"):
        manager.read_env()


def test_read_env_other_api_error_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    api.secret_exc = ApiException(status=500, reason="Server Error")
    with pytest.raises(K8sConfigError, match="Failed to read Secret"):
        manager.read_env()


def test_read_env_invalid_base64_raises(manager_and_api) -> None:
    import binascii

    manager, api = manager_and_api
    # A non-base64 Secret value is corrupt — decode raises, never mangled.
    api.secret_result = client.V1Secret(data={"BAD": "a"})
    with pytest.raises(binascii.Error):
        manager.read_env()


def test_read_env_non_alphabet_base64_raises(manager_and_api) -> None:
    import binascii

    manager, api = manager_and_api
    # A non-alphabet char in otherwise-decodable base64: validate=True raises, not drops.
    api.secret_result = client.V1Secret(data={"BAD": "aGVs!bG8="})
    with pytest.raises(binascii.Error):
        manager.read_env()


# -- write_env ---------------------------------------------------------------


def test_write_env_merges_and_filters(manager_and_api) -> None:
    manager, api = manager_and_api
    api.secret_result = _secret({"API_KEY": _b64("old"), "KEEP": _b64("preserved")})
    manager.write_env({"API_KEY": "new", "BLANK": "", "EXTRA": "added"})

    # Assert the RESULTING Secret state (not the request body): API_KEY overwritten,
    # KEEP preserved (not in new config), BLANK filtered out.
    assert api.resulting_env() == {"API_KEY": "new", "EXTRA": "added", "KEEP": "preserved"}
    assert api.replaced_secret is not None
    assert api.replaced_secret.metadata.name == SECRET_NAME
    assert api.replaced_secret_call == (SECRET_NAME, NAMESPACE)


def test_write_env_empty_value_removes_key(manager_and_api) -> None:
    manager, api = manager_and_api
    api.secret_result = _secret({"KEEP": _b64("old"), "OTHER": _b64("keep-me")})
    # An explicit empty value DELETES the key. A merge-PATCH could never remove it —
    # the whole-object replace does. Assert the resulting Secret state, not the body.
    manager.write_env({"KEEP": ""})
    assert api.resulting_env() == {"OTHER": "keep-me"}


def test_write_env_handles_empty_existing(manager_and_api) -> None:
    manager, api = manager_and_api
    api.secret_result = _secret(None)
    manager.write_env({"A": "1"})
    assert api.resulting_env() == {"A": "1"}


def test_write_env_corrupt_existing_base64_raises(manager_and_api) -> None:
    import binascii

    manager, api = manager_and_api
    # A corrupt existing Secret value aborts the write; nothing is replaced.
    api.secret_result = _secret({"KEEP": "aGVs!bG8="})
    with pytest.raises(binascii.Error):
        manager.write_env({"A": "1"})
    assert api.replaced_secret is None


def test_write_env_missing_secret_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    api.secret_exc = ApiException(status=404, reason="Not Found")
    with pytest.raises(K8sConfigError, match="Create it before writing"):
        manager.write_env({"A": "1"})


def test_write_env_read_other_error_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    api.secret_exc = ApiException(status=500, reason="Server Error")
    with pytest.raises(K8sConfigError, match="Failed to read Secret"):
        manager.write_env({"A": "1"})


def test_write_env_missing_resource_version_refuses(manager_and_api) -> None:
    manager, api = manager_and_api
    # Metadata present but no resourceVersion: the replace would serialize without its
    # optimistic-concurrency guard (a lost-update window), so the write refuses.
    api.secret_result = client.V1Secret(data={}, metadata=client.V1ObjectMeta(name=SECRET_NAME, resource_version=""))
    with pytest.raises(K8sConfigError, match="without a resourceVersion"):
        manager.write_env({"A": "1"})
    assert api.replaced_secret is None


def test_write_env_permission_denied_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    api.secret_result = _secret({})
    api.replace_secret_exc = ApiException(status=403, reason="Forbidden")
    with pytest.raises(K8sConfigError, match="Permission denied updating Secret"):
        manager.write_env({"A": "1"})


def test_write_env_replace_other_error_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    api.secret_result = _secret({})
    api.replace_secret_exc = ApiException(status=500, reason="Server Error")
    with pytest.raises(K8sConfigError, match="Failed to update Secret"):
        manager.write_env({"A": "1"})


# -- replace_env -------------------------------------------------------------


def test_replace_env_deletes_uninvited_keys(manager_and_api) -> None:
    manager, api = manager_and_api
    api.secret_result = _secret({"KEEP": _b64("old"), "DROP": _b64("gone")})
    # Whole-map replace: DROP is absent from the new map, so it is deleted; BLANK is
    # filtered out. Assert the resulting Secret state.
    manager.replace_env({"KEEP": "new", "ADD": "added", "BLANK": ""})
    assert api.resulting_env() == {"KEEP": "new", "ADD": "added"}
    assert api.replaced_secret_call == (SECRET_NAME, NAMESPACE)


def test_replace_env_missing_secret_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    api.secret_exc = ApiException(status=404, reason="Not Found")
    with pytest.raises(K8sConfigError, match="Create it before writing"):
        manager.replace_env({"A": "1"})


def test_replace_env_permission_denied_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    api.secret_result = _secret({})
    api.replace_secret_exc = ApiException(status=403, reason="Forbidden")
    with pytest.raises(K8sConfigError, match="Permission denied updating Secret"):
        manager.replace_env({"A": "1"})


class RaceSecretApi:
    """Fake CoreV1Api modeling a resourceVersion race on the env Secret.

    On the FIRST replace a concurrent writer advances the stored Secret (adds
    ``C`` alongside the existing keys, bumps ``resourceVersion``), so that replace
    409s; the re-read returns the advanced Secret and the second replace matches the
    bumped version.
    """

    def __init__(self, initial_data: dict[str, str], initial_rv: str) -> None:
        self._data = dict(initial_data)
        self._rv = initial_rv
        self._concurrent_applied = False
        self.read_calls = 0
        self.replace_calls = 0

    def read_namespaced_secret(self, name: str, namespace: str) -> client.V1Secret:
        self.read_calls += 1
        return client.V1Secret(
            data=dict(self._data),
            metadata=client.V1ObjectMeta(name=name, resource_version=self._rv),
        )

    def replace_namespaced_secret(self, name: str, namespace: str, body: client.V1Secret) -> client.V1Secret:
        self.replace_calls += 1
        if not self._concurrent_applied:
            # A concurrent writer lands between the caller's read and this replace,
            # ADDING ``C`` while leaving the existing keys in place.
            self._data["C"] = _b64("3")
            self._rv = "rv-2"
            self._concurrent_applied = True
        assert body.metadata is not None
        if body.metadata.resource_version != self._rv:
            raise ApiException(status=409, reason="Conflict")
        self._data = {k: _b64(v) for k, v in (body.string_data or {}).items()}
        self._rv = "rv-final"
        return body

    def stored_env(self) -> dict[str, str]:
        return {k: base64.b64decode(v, validate=True).decode("utf-8") for k, v in self._data.items()}


def test_replace_env_retries_on_conflict(manager_and_api) -> None:
    manager, _ = manager_and_api
    api = RaceSecretApi({"A": _b64("1")}, "rv-1")
    manager._core_api = api  # type: ignore[attr-defined]

    manager.replace_env({"A": "1", "X": "9"})

    # First replace 409s against stale rv-1; the loop re-reads rv-2 and re-applies.
    assert api.replace_calls == 2
    # replace wins wholesale: the concurrent `C` is uninvited and gone.
    assert api.stored_env() == {"A": "1", "X": "9"}


def test_write_env_retries_on_conflict_and_reruns(manager_and_api) -> None:
    manager, _ = manager_and_api
    api = RaceSecretApi({"A": _b64("1")}, "rv-1")
    manager._core_api = api  # type: ignore[attr-defined]

    manager.write_env({"B": "2"})

    # write_env re-reads on the 409 and re-merges against the FRESH state, so the
    # concurrent `C` survives alongside the original `A` and the merged `B`.
    assert api.replace_calls == 2
    assert api.stored_env() == {"A": "1", "C": "3", "B": "2"}


def test_replace_env_conflict_exhaustion_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    api.secret_result = _secret({}, rv="rv-1")
    # Every replace conflicts: the loop exhausts its bounded attempts and fails loudly.
    api.replace_secret_exc = ApiException(status=409, reason="Conflict")
    with pytest.raises(K8sConfigError, match="after 5 attempts"):
        manager.replace_env({"A": "1"})


# -- read_manifest -----------------------------------------------------------


def test_read_manifest_parses_yaml(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_result = client.V1ConfigMap(data={MANIFEST_KEY: "a: 1\nb: two"})
    assert manager.read_manifest() == {"a": 1, "b": "two"}
    assert api.read_configmap_calls == [(CONFIGMAP_NAME, NAMESPACE)]


def test_read_manifest_empty_document_returns_empty(manager_and_api) -> None:
    manager, api = manager_and_api
    # A truthy document that parses to None (e.g. "null") -> {} via `or {}`.
    api.configmap_result = client.V1ConfigMap(data={MANIFEST_KEY: "null"})
    assert manager.read_manifest() == {}


def test_read_manifest_resolves_env_tag(manager_and_api, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, api = manager_and_api
    # An !ENV ${VAR} tag in the ConfigMap is interpolated by parse_config on read.
    monkeypatch.setenv("TAI_MANIFEST_TOKEN", "resolved-secret")
    api.configmap_result = client.V1ConfigMap(data={MANIFEST_KEY: "token: !ENV ${TAI_MANIFEST_TOKEN}\nname: app"})
    assert manager.read_manifest() == {"token": "resolved-secret", "name": "app"}


def test_read_manifest_invalid_yaml_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_result = client.V1ConfigMap(data={MANIFEST_KEY: "a: : :"})
    # Malformed manifest YAML is a loud error, never a silent empty config.
    with pytest.raises(yaml.YAMLError):
        manager.read_manifest()


def test_read_manifest_non_mapping_document_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    # A non-mapping document (a list) fails the mapping guard, naming the key.
    api.configmap_result = client.V1ConfigMap(data={MANIFEST_KEY: "- one\n- two"})
    with pytest.raises(K8sConfigError, match="expected a mapping"):
        manager.read_manifest()


def test_read_manifest_missing_configmap_raises_filenotfound(manager_and_api) -> None:
    # Absent ConfigMap (404) -> FileNotFoundError, per the ConfigManager contract.
    manager, api = manager_and_api
    api.configmap_exc = ApiException(status=404, reason="Not Found")
    with pytest.raises(FileNotFoundError, match="not found"):
        manager.read_manifest()


def test_read_manifest_other_api_error_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_exc = ApiException(status=500, reason="Server Error")
    with pytest.raises(K8sConfigError, match="Failed to read ConfigMap"):
        manager.read_manifest()


def test_read_manifest_missing_key_raises_filenotfound(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_result = client.V1ConfigMap(data={"other.yml": "a: 1"})
    with pytest.raises(FileNotFoundError, match="Manifest key"):
        manager.read_manifest()


def test_read_manifest_no_data_raises_filenotfound(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_result = client.V1ConfigMap(data=None)
    with pytest.raises(FileNotFoundError, match="Manifest key"):
        manager.read_manifest()


# -- read_manifest_preserved -------------------------------------------------


def test_read_manifest_preserved_keeps_env_tag_marker(manager_and_api, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, api = manager_and_api
    # The preserving view keeps `!ENV ${VAR}` as its marker, never reading the env.
    monkeypatch.setenv("TAI_MANIFEST_TOKEN", "resolved-secret")
    api.configmap_result = client.V1ConfigMap(data={MANIFEST_KEY: "token: !ENV ${TAI_MANIFEST_TOKEN}\nname: app"})
    assert manager.read_manifest_preserved() == {"token": "!ENV ${TAI_MANIFEST_TOKEN}", "name": "app"}


def test_read_manifest_preserved_non_mapping_document_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    # A non-mapping document (a list) fails the mapping guard, naming the key.
    api.configmap_result = client.V1ConfigMap(data={MANIFEST_KEY: "- one\n- two"})
    with pytest.raises(K8sConfigError, match="expected a mapping"):
        manager.read_manifest_preserved()


def test_read_manifest_preserved_missing_configmap_raises_filenotfound(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_exc = ApiException(status=404, reason="Not Found")
    with pytest.raises(FileNotFoundError, match="not found"):
        manager.read_manifest_preserved()


def test_read_manifest_preserved_other_api_error_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_exc = ApiException(status=500, reason="Server Error")
    with pytest.raises(K8sConfigError, match="Failed to read ConfigMap"):
        manager.read_manifest_preserved()


def test_read_manifest_preserved_missing_key_raises_filenotfound(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_result = client.V1ConfigMap(data={"other.yml": "a: 1"})
    with pytest.raises(FileNotFoundError, match="Manifest key"):
        manager.read_manifest_preserved()


# -- read_defaults_manifest --------------------------------------------------


def test_read_defaults_manifest_parses_yaml(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_result = client.V1ConfigMap(data={DEFAULTS_KEY: "x: 9"})
    assert manager.read_defaults_manifest() == {"x": 9}


def test_read_defaults_manifest_resolves_env_tag(manager_and_api, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, api = manager_and_api
    # An !ENV ${VAR} tag in the defaults ConfigMap is interpolated on read too.
    monkeypatch.setenv("TAI_DEFAULTS_TOKEN", "default-secret")
    api.configmap_result = client.V1ConfigMap(data={DEFAULTS_KEY: "token: !ENV ${TAI_DEFAULTS_TOKEN}"})
    assert manager.read_defaults_manifest() == {"token": "default-secret"}


def test_read_defaults_manifest_missing_configmap_returns_empty(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_exc = ApiException(status=404, reason="Not Found")
    assert manager.read_defaults_manifest() == {}


def test_read_defaults_manifest_other_api_error_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_exc = ApiException(status=500, reason="Server Error")
    with pytest.raises(K8sConfigError, match="Failed to read ConfigMap"):
        manager.read_defaults_manifest()


def test_read_defaults_manifest_missing_key_returns_empty(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_result = client.V1ConfigMap(data={"other.yml": "x: 1"})
    assert manager.read_defaults_manifest() == {}


def test_read_defaults_manifest_invalid_yaml_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_result = client.V1ConfigMap(data={DEFAULTS_KEY: "a: : :"})
    # A malformed defaults manifest is a loud error, not a silent empty config.
    with pytest.raises(yaml.YAMLError):
        manager.read_defaults_manifest()


def test_read_defaults_manifest_non_mapping_document_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    # A non-mapping defaults document (a list) fails the mapping guard, naming the key.
    api.configmap_result = client.V1ConfigMap(data={DEFAULTS_KEY: "- one\n- two"})
    with pytest.raises(K8sConfigError, match="expected a mapping"):
        manager.read_defaults_manifest()


# -- mutate_manifest / replace_manifest --------------------------------------


class RaceCoreV1Api:
    """Fake CoreV1Api modeling a resourceVersion race on the manifest ConfigMap.

    On the FIRST patch a concurrent writer advances the stored document (adds
    ``c: 3``, bumps ``resourceVersion``), so that patch 409s; the re-read returns
    the advanced document and the second patch matches the bumped version.
    """

    def __init__(self, initial_manifest: str, initial_rv: str) -> None:
        self._data: dict[str, str] = {MANIFEST_KEY: initial_manifest}
        self._rv = initial_rv
        self._concurrent_applied = False
        self.read_calls = 0
        self.patch_calls = 0

    def read_namespaced_config_map(self, name: str, namespace: str) -> client.V1ConfigMap:
        self.read_calls += 1
        return client.V1ConfigMap(
            data=dict(self._data),
            metadata=client.V1ObjectMeta(name=name, resource_version=self._rv),
        )

    def patch_namespaced_config_map(self, name: str, namespace: str, body: client.V1ConfigMap) -> client.V1ConfigMap:
        self.patch_calls += 1
        if not self._concurrent_applied:
            # A concurrent writer lands between the caller's read and this patch.
            self._data[MANIFEST_KEY] = "a: 1\nc: 3\n"
            self._rv = "rv-2"
            self._concurrent_applied = True
        assert body.metadata is not None
        if body.metadata.resource_version != self._rv:
            raise ApiException(status=409, reason="Conflict")
        self._data = dict(body.data or {})
        self._rv = "rv-final"
        return body

    def stored_manifest(self) -> dict:
        return yaml.safe_load(self._data[MANIFEST_KEY])


def test_mutate_manifest_retries_on_conflict_and_reruns(manager_and_api) -> None:
    manager, _ = manager_and_api
    api = RaceCoreV1Api("a: 1\n", "rv-1")
    manager._core_api = api  # type: ignore[attr-defined]

    runs: list[dict] = []

    def mutator(document: dict) -> None:
        runs.append(dict(document))
        document["b"] = 2

    result = manager.mutate_manifest(mutator)

    # Mutator runs once per attempt: stale {a:1}, then re-read {a:1,c:3} after the 409.
    assert len(runs) == 2
    assert runs[0] == {"a": 1}
    assert runs[1] == {"a": 1, "c": 3}
    assert api.patch_calls == 2
    # Persisted document carries both the concurrent `c` and the mutator's `b`.
    assert api.stored_manifest() == {"a": 1, "c": 3, "b": 2}
    assert result == {"a": 1, "c": 3, "b": 2}


def test_mutate_manifest_preserves_comments(manager_and_api) -> None:
    manager, api = manager_and_api
    source = "# top-level note\nname: app  # the app name\ntoken: !ENV ${TAI_MANIFEST_SECRET}\n"
    api.configmap_result = client.V1ConfigMap(
        data={MANIFEST_KEY: source},
        metadata=client.V1ObjectMeta(name=CONFIGMAP_NAME, resource_version="rv-1"),
    )

    def mutator(document: dict) -> None:
        document["name"] = "renamed"

    manager.mutate_manifest(mutator)

    body = api.patched_configmap
    assert body is not None
    written = body.data[MANIFEST_KEY]
    # Comments survive; the edited value changes; the !ENV marker stays a tag.
    assert "# top-level note" in written
    assert "# the app name" in written
    assert "name: renamed" in written
    assert "token: !ENV ${TAI_MANIFEST_SECRET}" in written


def test_mutate_manifest_conflict_exhaustion_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_result = client.V1ConfigMap(
        data={MANIFEST_KEY: "a: 1\n"},
        metadata=client.V1ObjectMeta(name=CONFIGMAP_NAME, resource_version="rv-1"),
    )
    # Every patch conflicts: the loop exhausts its bounded attempts and fails loudly.
    api.patch_configmap_exc = ApiException(status=409, reason="Conflict")
    runs: list[int] = []
    with pytest.raises(K8sConfigError, match="after 5 attempts"):
        manager.mutate_manifest(lambda document: runs.append(1))
    assert len(runs) == 5


def test_mutate_manifest_mutator_exception_aborts(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_result = client.V1ConfigMap(
        data={MANIFEST_KEY: "a: 1\n"},
        metadata=client.V1ObjectMeta(name=CONFIGMAP_NAME, resource_version="rv-1"),
    )

    def boom(document: dict) -> None:
        raise ValueError("bad mutation")

    # A mutator exception aborts the transaction with nothing patched.
    with pytest.raises(ValueError, match="bad mutation"):
        manager.mutate_manifest(boom)
    assert api.patched_configmap is None


def test_mutate_manifest_permission_denied_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_result = client.V1ConfigMap(
        data={MANIFEST_KEY: "a: 1\n"},
        metadata=client.V1ObjectMeta(name=CONFIGMAP_NAME, resource_version="rv-1"),
    )
    # The retry-loop path surfaces a 403 as a loud permission error, not a retried 409.
    api.patch_configmap_exc = ApiException(status=403, reason="Forbidden")
    with pytest.raises(K8sConfigError, match="Permission denied updating ConfigMap"):
        manager.mutate_manifest(lambda document: document.__setitem__("b", 2))


def test_mutate_manifest_patch_other_error_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_result = client.V1ConfigMap(
        data={MANIFEST_KEY: "a: 1\n"},
        metadata=client.V1ObjectMeta(name=CONFIGMAP_NAME, resource_version="rv-1"),
    )
    # A non-409 API error on the precondition patch fails loudly, never retried.
    api.patch_configmap_exc = ApiException(status=500, reason="Server Error")
    with pytest.raises(K8sConfigError, match="Failed to update ConfigMap"):
        manager.mutate_manifest(lambda document: document.__setitem__("b", 2))


def test_mutate_manifest_missing_configmap_raises_create_it_first(manager_and_api) -> None:
    manager, api = manager_and_api
    # The write path reads the ConfigMap it must patch; an absent one (404) is a loud
    # refusal telling the operator to create it, NOT the read path's FileNotFoundError.
    api.configmap_exc = ApiException(status=404, reason="Not Found")
    with pytest.raises(K8sConfigError, match="Create it before writing"):
        manager.mutate_manifest(lambda document: document.__setitem__("b", 2))
    assert api.patched_configmap is None


def test_mutate_manifest_read_other_api_error_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    # A non-404 API error reading the ConfigMap to patch fails loudly.
    api.configmap_exc = ApiException(status=500, reason="Server Error")
    with pytest.raises(K8sConfigError, match="Failed to read ConfigMap"):
        manager.mutate_manifest(lambda document: document.__setitem__("b", 2))


def test_mutate_manifest_configmap_without_metadata_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    # A ConfigMap returned without metadata cannot carry the optimistic-concurrency
    # precondition, so the write refuses rather than patch without it.
    api.configmap_result = client.V1ConfigMap(data={MANIFEST_KEY: "a: 1\n"}, metadata=None)
    with pytest.raises(K8sConfigError, match="without metadata"):
        manager.mutate_manifest(lambda document: document.__setitem__("b", 2))
    assert api.patched_configmap is None


def test_mutate_manifest_configmap_without_resource_version_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    # Metadata present but no resourceVersion: the precondition would serialize without
    # its concurrency guard (a lost-update window), so the write refuses.
    api.configmap_result = client.V1ConfigMap(
        data={MANIFEST_KEY: "a: 1\n"},
        metadata=client.V1ObjectMeta(name=CONFIGMAP_NAME, resource_version=""),
    )
    with pytest.raises(K8sConfigError, match="without a resourceVersion"):
        manager.mutate_manifest(lambda document: document.__setitem__("b", 2))
    assert api.patched_configmap is None


def test_mutate_manifest_non_mapping_defaults_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    # The write merge loads the defaults key as the preserved view; a non-mapping
    # defaults document (a list) fails the mapping guard loudly before any patch.
    api.configmap_result = client.V1ConfigMap(
        data={MANIFEST_KEY: "a: 1\n", DEFAULTS_KEY: "- one\n- two\n"},
        metadata=client.V1ObjectMeta(name=CONFIGMAP_NAME, resource_version="rv-1"),
    )
    with pytest.raises(K8sConfigError, match="non-mapping document"):
        manager.mutate_manifest(lambda document: document.__setitem__("b", 2))
    assert api.patched_configmap is None


def test_replace_manifest_deletes_uninvited_keys(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_result = client.V1ConfigMap(
        data={MANIFEST_KEY: "a: 1\nb: 2\n", DEFAULTS_KEY: "d: 9\n"},
        metadata=client.V1ObjectMeta(name=CONFIGMAP_NAME, resource_version="rv-1"),
    )
    result = manager.replace_manifest({"a": 100})

    body = api.patched_configmap
    assert body is not None
    written = yaml.safe_load(body.data[MANIFEST_KEY])
    # `b` is gone (uninvited), while defaults still backfill the missing `d`.
    assert written == {"a": 100, "d": 9}
    assert result == {"a": 100, "d": 9}
    assert body.metadata.resource_version == "rv-1"


def test_replace_manifest_retries_on_conflict(manager_and_api) -> None:
    manager, _ = manager_and_api
    api = RaceCoreV1Api("a: 1\n", "rv-1")
    manager._core_api = api  # type: ignore[attr-defined]

    result = manager.replace_manifest({"a": 1, "x": 9})

    # First patch 409s against stale rv-1; the loop re-reads rv-2 and re-applies.
    assert api.patch_calls == 2
    # replace wins wholesale: the concurrent `c` is uninvited and gone.
    assert api.stored_manifest() == {"a": 1, "x": 9}
    assert result == {"a": 1, "x": 9}


def test_replace_manifest_does_not_mutate_caller_document(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_result = client.V1ConfigMap(
        data={MANIFEST_KEY: "a: 1\n", DEFAULTS_KEY: "d: 9\n"},
        metadata=client.V1ObjectMeta(name=CONFIGMAP_NAME, resource_version="rv-1"),
    )
    document = {"a": 100}
    result = manager.replace_manifest(document)

    # The backfilled `d` lands in the persisted document but not the caller's dict.
    assert result == {"a": 100, "d": 9}
    assert document == {"a": 100}


def test_replace_manifest_preserves_comments_from_round_trip_document(manager_and_api) -> None:
    manager, api = manager_and_api
    source = "# top-level note\nname: app  # the app name\ntoken: !ENV ${TAI_MANIFEST_SECRET}\n"
    api.configmap_result = client.V1ConfigMap(
        data={MANIFEST_KEY: source},
        metadata=client.V1ObjectMeta(name=CONFIGMAP_NAME, resource_version="rv-1"),
    )
    # Read as the preserved view, then replace with that same round-trip document.
    document = manager.read_manifest_preserved()
    manager.replace_manifest(document)

    body = api.patched_configmap
    assert body is not None
    written = body.data[MANIFEST_KEY]
    # Comments survive the replace round-trip; the !ENV marker stays a tag.
    assert "# top-level note" in written
    assert "# the app name" in written
    assert "token: !ENV ${TAI_MANIFEST_SECRET}" in written


def test_mutate_manifest_defaults_env_marker_never_leaks_plaintext(
    manager_and_api, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, api = manager_and_api
    # An `!ENV` default backfilled for a key absent from the manifest lands as its
    # marker, never the resolved secret as plaintext.
    monkeypatch.setenv("SOME_SECRET", "super-secret-plaintext")
    api.configmap_result = client.V1ConfigMap(
        data={MANIFEST_KEY: "a: 1\n", DEFAULTS_KEY: "foo: !ENV ${SOME_SECRET}\n"},
        metadata=client.V1ObjectMeta(name=CONFIGMAP_NAME, resource_version="rv-1"),
    )

    def mutator(document: dict) -> None:
        document["a"] = 2

    manager.mutate_manifest(mutator)

    body = api.patched_configmap
    assert body is not None
    written = body.data[MANIFEST_KEY]
    # The backfilled default stays an `!ENV` tag; the resolved secret never lands.
    assert "foo: !ENV ${SOME_SECRET}" in written
    assert "super-secret-plaintext" not in written


def test_replace_manifest_defaults_env_marker_never_leaks_plaintext(
    manager_and_api, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, api = manager_and_api
    # `foo`, absent from the replacement, is backfilled from defaults as its `!ENV`
    # marker, not the plaintext secret.
    monkeypatch.setenv("SOME_SECRET", "super-secret-plaintext")
    api.configmap_result = client.V1ConfigMap(
        data={MANIFEST_KEY: "a: 1\n", DEFAULTS_KEY: "foo: !ENV ${SOME_SECRET}\n"},
        metadata=client.V1ObjectMeta(name=CONFIGMAP_NAME, resource_version="rv-1"),
    )

    manager.replace_manifest({"a": 1})

    body = api.patched_configmap
    assert body is not None
    written = body.data[MANIFEST_KEY]
    assert "foo: !ENV ${SOME_SECRET}" in written
    assert "super-secret-plaintext" not in written


# -- lazy CoreV1Api builder --------------------------------------------------


def _build_manager(monkeypatch: pytest.MonkeyPatch, tmp_path) -> K8sConfigManager:
    # The hermetic environment and settings-cache reset come from the shared
    # conftest fixture; only the service-account path needs pointing away.
    monkeypatch.setattr(settings_mod, "_SA_NAMESPACE_PATH", tmp_path / "missing")
    return K8sConfigManager()


def test_core_api_uses_incluster_config(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import kubernetes

    calls: list[str] = []
    sentinel = object()
    monkeypatch.setattr(kubernetes.config, "load_incluster_config", lambda: calls.append("incluster"))
    monkeypatch.setattr(kubernetes.config, "load_kube_config", lambda: calls.append("kube"))
    monkeypatch.setattr(kubernetes.client, "CoreV1Api", lambda: sentinel)

    manager = _build_manager(monkeypatch, tmp_path)
    assert manager._core_api is sentinel  # type: ignore[attr-defined]
    # In-cluster config succeeded, so the kubeconfig fallback is not used.
    assert calls == ["incluster"]


def test_core_api_falls_back_to_kubeconfig(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import kubernetes
    from kubernetes.config import ConfigException

    calls: list[str] = []
    sentinel = object()

    def _raise_incluster() -> None:
        raise ConfigException("no in-cluster config")

    monkeypatch.setattr(kubernetes.config, "load_incluster_config", _raise_incluster)
    monkeypatch.setattr(kubernetes.config, "load_kube_config", lambda: calls.append("kube"))
    monkeypatch.setattr(kubernetes.client, "CoreV1Api", lambda: sentinel)

    manager = _build_manager(monkeypatch, tmp_path)
    assert manager._core_api is sentinel  # type: ignore[attr-defined]
    assert calls == ["kube"]
