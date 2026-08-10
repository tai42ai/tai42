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


class _ResourceVersionConflict(Exception):
    """Internal signal for a 409 resourceVersion conflict on a Secret or ConfigMap
    write; never escapes the manager."""


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
        """Merge *config* into the K8s Secret, deleting keys blanked to ``""``.

        Reads the existing Secret, overlays *config* (last-writer-wins per key),
        drops every empty value, and REPLACES the whole Secret with the result under
        a ``resourceVersion`` precondition. Existing keys not named in *config* are
        preserved; a key mapped to ``""`` is genuinely removed (a strategic-merge
        patch cannot delete a Secret key, so the write is a whole-object replace).
        """
        self._commit_secret_with_retry(
            lambda existing_data: {k: v for k, v in {**existing_data, **config}.items() if v != ""}
        )

    def replace_env(self, config: dict[str, str]) -> None:
        """Replace the whole Secret with *config* (whole-map, NOT a merge).

        *config* becomes the entire stored env: a key absent from *config* is
        DELETED, nothing from the old Secret survives uninvited. Empty values are
        filtered out. Shares the ``resourceVersion`` precondition + bounded 409-retry
        machinery of :meth:`write_env`.
        """
        self._commit_secret_with_retry(lambda _existing_data: {k: v for k, v in config.items() if v != ""})

    # -- Environment write machinery (Secret) --------------------------------

    def _read_secret_for_write(self) -> V1Secret:
        """Read the Secret an env write replaces; an absent one (404) fails loudly."""
        from kubernetes.client.exceptions import ApiException

        try:
            return cast(
                "V1Secret",
                self._core_api.read_namespaced_secret(self._settings.secret_name, self._settings.namespace),
            )
        except ApiException as exc:
            if exc.status == 404:
                raise K8sConfigError(
                    f"Secret '{self._settings.secret_name}' not found "
                    f"in namespace '{self._settings.namespace}'. "
                    "Create it before writing configuration."
                ) from exc
            raise K8sConfigError(f"Failed to read Secret: {exc.reason}") from exc

    @staticmethod
    def _decode_secret_data(secret: V1Secret) -> dict[str, str]:
        """Decode a Secret's base64 ``data`` map; a corrupt value aborts loudly.

        ``validate=True`` so a non-base64 value raises rather than being silently
        mangled or dropped.
        """
        if not secret.data:
            return {}
        return {k: base64.b64decode(v, validate=True).decode("utf-8") for k, v in secret.data.items()}

    def _secret_replace_body(self, existing: V1Secret, string_data: dict[str, str], resource_version: str) -> V1Secret:
        """Build the whole-Secret replace body carrying *string_data* and the precondition.

        ``data`` is left unset so the replaced Secret holds EXACTLY *string_data* (a
        key absent from it is deleted). The Secret ``type`` and the operator-relevant
        identity metadata (name, namespace, uid, labels, annotations) are carried from
        *existing* so the replace does not wipe them; the ``resourceVersion`` is the
        optimistic-concurrency precondition.
        """
        from kubernetes import client

        meta = existing.metadata
        return client.V1Secret(
            string_data=string_data,
            type=existing.type,
            metadata=client.V1ObjectMeta(
                name=self._settings.secret_name,
                namespace=meta.namespace if meta else None,
                uid=meta.uid if meta else None,
                resource_version=resource_version,
                labels=meta.labels if meta else None,
                annotations=meta.annotations if meta else None,
            ),
        )

    def _replace_secret_precondition(self, existing: V1Secret, string_data: dict[str, str]) -> None:
        """Replace the Secret under its ``resourceVersion`` precondition.

        A 409 conflict surfaces as :class:`_ResourceVersionConflict` so the retry loop
        can re-read and re-run; a 403 and any other API error fail loudly.
        """
        from kubernetes.client.exceptions import ApiException

        resource_version = self._require_resource_version(existing, f"Secret '{self._settings.secret_name}'")
        body = self._secret_replace_body(existing, string_data, resource_version)
        try:
            self._core_api.replace_namespaced_secret(
                self._settings.secret_name,
                self._settings.namespace,
                body,
            )
        except ApiException as exc:
            if exc.status == 409:
                raise _ResourceVersionConflict from exc
            if exc.status == 403:
                raise K8sConfigError(
                    f"Permission denied updating Secret '{self._settings.secret_name}': {exc.reason}"
                ) from exc
            raise K8sConfigError(f"Failed to update Secret: {exc.reason}") from exc

    def _commit_secret_with_retry(self, build_desired: Callable[[dict[str, str]], dict[str, str]]) -> None:
        """Run the optimistic-concurrency retry loop for a whole-Secret env write.

        Each attempt freshly reads the Secret, decodes its data, calls ``build_desired``
        with that decoded map to produce the full desired env map, then replaces the
        Secret under the ``resourceVersion`` precondition. A 409 re-reads and re-invokes
        ``build_desired`` on the fresh state, so ``build_desired`` must be re-runnable.
        Attempts are bounded; exhaustion fails loudly.
        """
        for attempt in range(1, _MAX_CONFLICT_ATTEMPTS + 1):
            existing = self._read_secret_for_write()
            existing_data = self._decode_secret_data(existing)
            desired = build_desired(existing_data)
            try:
                self._replace_secret_precondition(existing, desired)
            except _ResourceVersionConflict:
                logger.info(
                    "resourceVersion conflict updating Secret '%s' (attempt %d/%d); re-reading",
                    self._settings.secret_name,
                    attempt,
                    _MAX_CONFLICT_ATTEMPTS,
                )
                continue
            logger.info(
                "Updated K8s Secret '%s' in namespace '%s'",
                self._settings.secret_name,
                self._settings.namespace,
            )
            return

        raise K8sConfigError(
            f"Failed to update Secret '{self._settings.secret_name}' after "
            f"{_MAX_CONFLICT_ATTEMPTS} attempts due to repeated resourceVersion conflicts"
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

    def _require_resource_version(self, existing: V1ConfigMap | V1Secret, resource_label: str) -> str:
        """Return the resource ``resourceVersion``, refusing the write if it is absent.

        Without it the write would serialize without the optimistic-concurrency
        precondition, silently allowing a lost update. ``resource_label`` names the
        resource (e.g. ``"ConfigMap 'tai-manifest'"``) in the refusal message.
        """
        if existing.metadata is None:
            raise K8sConfigError(
                f"{resource_label} was returned without metadata; cannot apply the optimistic-concurrency precondition"
            )
        if not existing.metadata.resource_version:
            raise K8sConfigError(
                f"{resource_label} was returned without a resourceVersion; "
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

        A 409 conflict is surfaced as :class:`_ResourceVersionConflict` so the retry
        loop can re-read and re-run; a 403 and any other API error fail loudly.
        """
        from kubernetes.client.exceptions import ApiException

        resource_version = self._require_resource_version(existing, f"ConfigMap '{self._settings.configmap_name}'")
        body = self._manifest_patch_body(existing, content, resource_version)
        try:
            self._core_api.patch_namespaced_config_map(
                self._settings.configmap_name,
                self._settings.namespace,
                body,
            )
        except ApiException as exc:
            if exc.status == 409:
                raise _ResourceVersionConflict from exc
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
            except _ResourceVersionConflict:
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
