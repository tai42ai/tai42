"""Kubernetes-based configuration manager.

Implements :class:`~tai42_contract.config.manager.ConfigManager` for the ``k8s``
config mode: env config via K8s Secrets, manifest config via K8s ConfigMaps.
Requires the ``kubernetes`` package (``pip install tai42-config-k8s[k8s]``).
Exposes the :func:`build_config_manager` factory used to select this provider.
"""

from __future__ import annotations

import base64
import copy
import logging
from functools import cached_property
from typing import TYPE_CHECKING, Any, cast

import yaml
from pyaml_env import parse_config
from ruamel.yaml.comments import CommentedMap
from tai42_contract.config.manager import ConfigManager
from tai42_kit.utils.data import load_manifest, merge_and_dump_manifest

from tai42_config_k8s.settings import K8sConfigSettings, k8s_config_settings

if TYPE_CHECKING:
    from collections.abc import Callable

    from kubernetes.client import CoreV1Api, V1ConfigMap, V1Secret

logger = logging.getLogger(__name__)

# Bounds resourceVersion-conflict retries so a conflict storm fails loudly, not forever.
_MAX_CONFLICT_ATTEMPTS = 5


class K8sConfigError(Exception):
    """Raised when a Kubernetes API operation fails."""


class _ConfigMapConflict(Exception):
    """Internal signal for a 409 resourceVersion conflict; never escapes the manager."""


class K8sConfigManager(ConfigManager):
    """Config backend that reads/writes K8s Secrets (env) and ConfigMaps (manifest).

    Raises:
        ImportError: If the ``kubernetes`` package is not installed.
    """

    def __init__(self) -> None:
        from tai42_config_k8s._kubernetes_optional import require_kubernetes

        require_kubernetes()

        self._settings: K8sConfigSettings = k8s_config_settings()

    # -- Internal helpers ----------------------------------------------------

    @cached_property
    def _core_api(self) -> CoreV1Api:
        """Return the :class:`kubernetes.client.CoreV1Api`, built and cached on first access."""
        from kubernetes import client
        from kubernetes import config as k8s_config

        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException as exc:
            logger.info("in-cluster config unavailable (%s); falling back to kubeconfig", exc)
            k8s_config.load_kube_config()
        return client.CoreV1Api()

    # -- Environment configuration (Secret) ----------------------------------

    def read_env(self) -> dict[str, str]:
        """Read env config key-value pairs from a K8s Secret."""
        from kubernetes.client.exceptions import ApiException

        api = self._core_api
        try:
            secret = cast(
                "V1Secret",
                api.read_namespaced_secret(self._settings.secret_name, self._settings.namespace),
            )
        except ApiException as exc:
            if exc.status == 404:
                raise FileNotFoundError(
                    f"Secret '{self._settings.secret_name}' not found in namespace '{self._settings.namespace}'"
                ) from exc
            raise K8sConfigError(f"Failed to read Secret: {exc.reason}") from exc

        if not secret.data:
            return {}
        # validate=True: a corrupt (non-base64) value raises rather than being dropped.
        return {k: base64.b64decode(v, validate=True).decode("utf-8") for k, v in secret.data.items()}

    def write_env(self, config: dict[str, str]) -> None:
        """Patch a K8s Secret with env config key-value pairs.

        Uses ``string_data`` so K8s handles base64 encoding. Merges with existing
        keys and filters out empty values. Each entry is an independent Secret key,
        so the strategic-merge patch is per-key (same key is last-writer-wins).
        """
        from kubernetes import client
        from kubernetes.client.exceptions import ApiException

        api = self._core_api

        # Read existing secret to merge
        try:
            existing = cast(
                "V1Secret",
                api.read_namespaced_secret(self._settings.secret_name, self._settings.namespace),
            )
        except ApiException as exc:
            if exc.status == 404:
                raise K8sConfigError(
                    f"Secret '{self._settings.secret_name}' not found "
                    f"in namespace '{self._settings.namespace}'. "
                    "Create it before writing configuration."
                ) from exc
            raise K8sConfigError(f"Failed to read Secret: {exc.reason}") from exc

        existing_data: dict[str, str] = {}
        if existing.data:
            # validate=True: a corrupt existing value aborts the write, never mangled.
            existing_data = {k: base64.b64decode(v, validate=True).decode("utf-8") for k, v in existing.data.items()}

        preserved = {k: v for k, v in existing_data.items() if k not in config}
        merged = {**config, **preserved}
        filtered = {k: v for k, v in merged.items() if v != ""}

        body = client.V1Secret(
            string_data=filtered,
            metadata=client.V1ObjectMeta(name=self._settings.secret_name),
        )
        try:
            api.patch_namespaced_secret(
                self._settings.secret_name,
                self._settings.namespace,
                body,
            )
        except ApiException as exc:
            if exc.status == 403:
                raise K8sConfigError(
                    f"Permission denied updating Secret '{self._settings.secret_name}': {exc.reason}"
                ) from exc
            raise K8sConfigError(f"Failed to update Secret: {exc.reason}") from exc

        logger.info(
            "Updated K8s Secret '%s' in namespace '%s'",
            self._settings.secret_name,
            self._settings.namespace,
        )

    # -- Manifest configuration (ConfigMap) ----------------------------------

    def read_manifest(self) -> dict[str, Any]:
        """Read manifest YAML from a K8s ConfigMap key."""
        from kubernetes.client.exceptions import ApiException

        api = self._core_api
        try:
            cm = cast(
                "V1ConfigMap",
                api.read_namespaced_config_map(self._settings.configmap_name, self._settings.namespace),
            )
        except ApiException as exc:
            if exc.status == 404:
                raise FileNotFoundError(
                    f"ConfigMap '{self._settings.configmap_name}' not found in namespace '{self._settings.namespace}'"
                ) from exc
            raise K8sConfigError(f"Failed to read ConfigMap: {exc.reason}") from exc

        if not cm.data or self._settings.manifest_key not in cm.data:
            raise FileNotFoundError(
                f"Manifest key '{self._settings.manifest_key}' not found in ConfigMap '{self._settings.configmap_name}'"
            )

        parsed = parse_config(data=cm.data[self._settings.manifest_key]) or {}
        if not isinstance(parsed, dict):
            # Non-mapping YAML can't satisfy the dict[str, Any] contract; fail loudly.
            raise K8sConfigError(
                f"Manifest key '{self._settings.manifest_key}' in ConfigMap "
                f"'{self._settings.configmap_name}' parsed to a {type(parsed).__name__}, "
                "expected a mapping"
            )
        return parsed

    def read_manifest_preserved(self) -> dict[str, Any]:
        """Read manifest YAML from a K8s ConfigMap key with ``!ENV`` tags preserved.

        Like :meth:`read_manifest`, but each ``!ENV <expr>`` node is kept as its
        literal ``"!ENV <expr>"`` marker string rather than resolved.
        """
        from kubernetes.client.exceptions import ApiException

        api = self._core_api
        try:
            cm = cast(
                "V1ConfigMap",
                api.read_namespaced_config_map(self._settings.configmap_name, self._settings.namespace),
            )
        except ApiException as exc:
            if exc.status == 404:
                raise FileNotFoundError(
                    f"ConfigMap '{self._settings.configmap_name}' not found in namespace '{self._settings.namespace}'"
                ) from exc
            raise K8sConfigError(f"Failed to read ConfigMap: {exc.reason}") from exc

        if not cm.data or self._settings.manifest_key not in cm.data:
            raise FileNotFoundError(
                f"Manifest key '{self._settings.manifest_key}' not found in ConfigMap '{self._settings.configmap_name}'"
            )

        return self._load_manifest_document(cm.data[self._settings.manifest_key])

    def read_defaults_manifest(self) -> dict[str, Any]:
        """Read defaults manifest YAML from a K8s ConfigMap key."""
        from kubernetes.client.exceptions import ApiException

        api = self._core_api
        try:
            cm = cast(
                "V1ConfigMap",
                api.read_namespaced_config_map(self._settings.configmap_name, self._settings.namespace),
            )
        except ApiException as exc:
            if exc.status == 404:
                return {}
            raise K8sConfigError(f"Failed to read ConfigMap: {exc.reason}") from exc

        return self._parse_defaults(cm.data)

    def _parse_defaults(self, data: dict[str, str] | None) -> dict[str, Any]:
        """Parse the defaults-manifest key out of ConfigMap ``data`` (empty if absent).

        A malformed defaults manifest is a loud error, never a silent empty config.
        """
        if not data or self._settings.defaults_manifest_key not in data:
            return {}
        try:
            parsed = parse_config(data=data[self._settings.defaults_manifest_key]) or {}
        except yaml.YAMLError:
            logger.error(
                "Error parsing defaults manifest YAML from ConfigMap '%s' key '%s'",
                self._settings.configmap_name,
                self._settings.defaults_manifest_key,
                exc_info=True,
            )
            raise
        if not isinstance(parsed, dict):
            # Non-mapping YAML can't satisfy the dict[str, Any] contract; fail loudly.
            raise K8sConfigError(
                f"Defaults manifest key '{self._settings.defaults_manifest_key}' in ConfigMap "
                f"'{self._settings.configmap_name}' parsed to a {type(parsed).__name__}, "
                "expected a mapping"
            )
        return parsed

    def _load_defaults_preserved(self, data: dict[str, str] | None) -> CommentedMap:
        """Load the defaults-manifest key as the PRESERVED view for the write merge.

        Keeps ``!ENV`` tags as ``"!ENV <expr>"`` marker strings so a backfilled
        default never bakes a resolved secret into the ConfigMap as plaintext.
        Absent key yields an empty document; a non-mapping one raises loudly.
        """
        if not data or self._settings.defaults_manifest_key not in data:
            return CommentedMap()
        try:
            return load_manifest(data[self._settings.defaults_manifest_key])
        except TypeError as exc:
            raise K8sConfigError(
                f"Defaults manifest key '{self._settings.defaults_manifest_key}' in ConfigMap "
                f"'{self._settings.configmap_name}' parsed to a non-mapping document, "
                "expected a mapping"
            ) from exc

    def _load_manifest_document(self, text: str) -> CommentedMap:
        """Round-trip-load a stored manifest string, keeping ``!ENV`` markers and comments.

        A non-mapping top-level document fails loudly, naming the key.
        """
        try:
            return load_manifest(text)
        except TypeError as exc:
            raise K8sConfigError(
                f"Manifest key '{self._settings.manifest_key}' in ConfigMap "
                f"'{self._settings.configmap_name}' parsed to a non-mapping document, "
                "expected a mapping"
            ) from exc

    def _read_configmap_for_write(self) -> V1ConfigMap:
        """Read the ConfigMap a manifest write patches; an absent one (404) fails loudly."""
        from kubernetes.client.exceptions import ApiException

        try:
            return cast(
                "V1ConfigMap",
                self._core_api.read_namespaced_config_map(self._settings.configmap_name, self._settings.namespace),
            )
        except ApiException as exc:
            if exc.status == 404:
                raise K8sConfigError(
                    f"ConfigMap '{self._settings.configmap_name}' not found "
                    f"in namespace '{self._settings.namespace}'. "
                    "Create it before writing configuration."
                ) from exc
            raise K8sConfigError(f"Failed to read ConfigMap: {exc.reason}") from exc

    def _require_resource_version(self, existing: V1ConfigMap) -> str:
        """Return the ConfigMap ``resourceVersion``, refusing the write if it is absent.

        Without it the patch would serialize without the optimistic-concurrency
        precondition, silently allowing a lost update.
        """
        if existing.metadata is None:
            raise K8sConfigError(
                f"ConfigMap '{self._settings.configmap_name}' was returned without metadata; "
                "cannot apply the optimistic-concurrency precondition"
            )
        if not existing.metadata.resource_version:
            raise K8sConfigError(
                f"ConfigMap '{self._settings.configmap_name}' was returned without a resourceVersion; "
                "cannot apply the optimistic-concurrency precondition"
            )
        return existing.metadata.resource_version

    def _manifest_patch_body(self, existing: V1ConfigMap, content: str, resource_version: str) -> V1ConfigMap:
        """Build the ConfigMap patch body carrying the manifest content and precondition.

        Other ConfigMap keys ride along unchanged; the metadata ``resourceVersion``
        is the optimistic-concurrency precondition.
        """
        from kubernetes import client

        cm_data = dict(existing.data or {})
        cm_data[self._settings.manifest_key] = content
        return client.V1ConfigMap(
            data=cm_data,
            metadata=client.V1ObjectMeta(
                name=self._settings.configmap_name,
                resource_version=resource_version,
            ),
        )

    def _patch_manifest_precondition(self, existing: V1ConfigMap, content: str) -> None:
        """Patch the manifest key under the ``resourceVersion`` precondition.

        A 409 conflict is surfaced as :class:`_ConfigMapConflict` so the retry
        loop can re-read and re-run; a 403 and any other API error fail loudly.
        """
        from kubernetes.client.exceptions import ApiException

        resource_version = self._require_resource_version(existing)
        body = self._manifest_patch_body(existing, content, resource_version)
        try:
            self._core_api.patch_namespaced_config_map(
                self._settings.configmap_name,
                self._settings.namespace,
                body,
            )
        except ApiException as exc:
            if exc.status == 409:
                raise _ConfigMapConflict from exc
            if exc.status == 403:
                raise K8sConfigError(
                    f"Permission denied updating ConfigMap '{self._settings.configmap_name}': {exc.reason}"
                ) from exc
            raise K8sConfigError(f"Failed to update ConfigMap: {exc.reason}") from exc

    def _commit_with_retry(
        self, render: Callable[[V1ConfigMap, dict[str, Any]], tuple[str, dict[str, Any]]]
    ) -> dict[str, Any]:
        """Run the optimistic-concurrency retry loop for a manifest write.

        Each attempt freshly reads the ConfigMap, calls ``render`` with it and its
        preserved-view defaults to build the YAML content and persisted document,
        then patches under the ``resourceVersion`` precondition. A 409 re-reads and
        re-invokes ``render`` on the fresh state, so ``render`` must be re-runnable.
        Attempts are bounded; exhaustion fails loudly. A ``render`` exception aborts
        with nothing patched and propagates.
        """
        for attempt in range(1, _MAX_CONFLICT_ATTEMPTS + 1):
            existing = self._read_configmap_for_write()
            defaults = self._load_defaults_preserved(existing.data)
            content, document = render(existing, defaults)
            try:
                self._patch_manifest_precondition(existing, content)
            except _ConfigMapConflict:
                logger.info(
                    "resourceVersion conflict updating ConfigMap '%s' (attempt %d/%d); re-reading",
                    self._settings.configmap_name,
                    attempt,
                    _MAX_CONFLICT_ATTEMPTS,
                )
                continue
            logger.info(
                "Updated K8s ConfigMap '%s' key '%s' in namespace '%s'",
                self._settings.configmap_name,
                self._settings.manifest_key,
                self._settings.namespace,
            )
            return document

        raise K8sConfigError(
            f"Failed to update ConfigMap '{self._settings.configmap_name}' after "
            f"{_MAX_CONFLICT_ATTEMPTS} attempts due to repeated resourceVersion conflicts"
        )

    def write_manifest(self, manifest: dict[str, Any]) -> None:
        """Write manifest YAML to a K8s ConfigMap as a single key.

        Three-way merges (defaults + current + new) and patches under the read
        ``resourceVersion`` precondition, so a concurrent modification fails loudly
        with a 409 rather than being clobbered.
        """
        from kubernetes.client.exceptions import ApiException

        existing = self._read_configmap_for_write()

        # Preserved view so an `!ENV` default backfill round-trips as its marker,
        # never a resolved secret baked to disk as plaintext.
        defaults = self._load_defaults_preserved(existing.data)
        current: CommentedMap | dict[str, Any] = {}
        if existing.data and self._settings.manifest_key in existing.data:
            # A malformed existing manifest aborts the write, never discarded.
            current = self._load_manifest_document(existing.data[self._settings.manifest_key])

        content = merge_and_dump_manifest(defaults, cast("CommentedMap", current), manifest)
        resource_version = self._require_resource_version(existing)
        body = self._manifest_patch_body(existing, content, resource_version)
        try:
            self._core_api.patch_namespaced_config_map(
                self._settings.configmap_name,
                self._settings.namespace,
                body,
            )
        except ApiException as exc:
            if exc.status == 403:
                raise K8sConfigError(
                    f"Permission denied updating ConfigMap '{self._settings.configmap_name}': {exc.reason}"
                ) from exc
            raise K8sConfigError(f"Failed to update ConfigMap: {exc.reason}") from exc

        logger.info(
            "Updated K8s ConfigMap '%s' key '%s' in namespace '%s'",
            self._settings.configmap_name,
            self._settings.manifest_key,
            self._settings.namespace,
        )

    def mutate_manifest(self, mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        """Atomically read-modify-write the manifest under a resourceVersion precondition.

        Reads the manifest as the preserved view, runs ``mutator`` to edit it in
        place, and patches under the precondition. A 409 re-reads and re-runs
        ``mutator``, so ``mutator`` MUST be re-runnable / pure. Attempts are bounded;
        exhaustion fails loudly; a ``mutator`` exception aborts with nothing patched.
        Returns the persisted document.
        """

        def render(existing: V1ConfigMap, defaults: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            text = (existing.data or {}).get(self._settings.manifest_key, "")
            document = self._load_manifest_document(text)
            mutator(document)
            content = merge_and_dump_manifest(defaults, document, {})
            return content, document

        return self._commit_with_retry(render)

    def replace_manifest(self, document: dict[str, Any]) -> dict[str, Any]:
        """Atomically replace the whole stored manifest under a resourceVersion precondition.

        ``document`` becomes the entire stored manifest — a key absent from it is
        deleted (defaults still backfill missing keys). Shares the precondition/retry
        machinery of :meth:`mutate_manifest`. The caller owns the preserved view:
        ``document`` must carry ``!ENV`` marker strings, never resolved secrets, since
        it is persisted verbatim. Returns the persisted document.
        """

        def render(existing: V1ConfigMap, defaults: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            # Copy so retries and the backfill never mutate the caller's document.
            working = cast("CommentedMap", copy.deepcopy(document))
            content = merge_and_dump_manifest(defaults, working, {})
            return content, working

        return self._commit_with_retry(render)


def build_config_manager() -> ConfigManager:
    """Provider entry point for the ``k8s`` config mode (the factory convention)."""
    return K8sConfigManager()
