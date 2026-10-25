"""The extension registry: registration, lookup, and kind/cardinality validation for tool extensions."""

import inspect
from collections.abc import Callable, Iterator, Sequence
from copy import deepcopy
from typing import Any

from tai42_contract.extensions import ExtensionKind
from tai42_contract.manifest import ExtensionElement
from tai42_kit.utils.data.json_schema_util import (
    InvalidJsonSchemaError,
    JsonSchemaValidationError,
    check_json_schema,
    validate_against_json_schema,
)

from tai42_skeleton.core.registry.base_registry import BaseRegistry
from tai42_skeleton.exceptions.exceptions import TaiValidationError


def extension_name(element: ExtensionElement) -> str:
    """The extension NAME of a combo element — the bare string itself, or the ``name`` of a mapping."""
    if isinstance(element, str):
        return element
    return element["name"]


def extension_config(element: ExtensionElement) -> dict[str, Any]:
    """The author-bound config of a combo element — empty for a bare name, else its ``config`` mapping."""
    if isinstance(element, str):
        return {}
    return element["config"]


def factory_accepts_config(func: Callable) -> bool:
    """Whether an extension factory accepts the author-config argument.

    A factory that closes over author config declares a ``config`` parameter (after
    ``func``/``name``/``description``); the apply site passes it by keyword, so the
    parameter is found whether it is positional-or-keyword or keyword-only. A
    config-agnostic factory omits ``config`` and is called with three arguments, so
    an extension that takes no config needs no change to opt out.
    """
    try:
        params = inspect.signature(func).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == "config" and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY) for p in params)


def _check_config_schema(func: Callable, name: str, config_schema: dict[str, Any] | None) -> None:
    """Refuse a settings schema that does not match the factory's ``config`` parameter or the JSON Schema rules."""
    takes_config = factory_accepts_config(func)
    if config_schema is None:
        if takes_config:
            raise TaiValidationError(f"Extension '{name}' takes a config parameter but declares no config_schema.")
        return
    if not takes_config:
        raise TaiValidationError(
            f"Extension '{name}' declares a config_schema but its factory takes no config parameter."
        )
    try:
        check_json_schema(config_schema)
    except InvalidJsonSchemaError as exc:
        raise TaiValidationError(f"Extension '{name}' config_schema: {exc}") from exc
    if config_schema.get("type") != "object":
        raise TaiValidationError(f"Extension '{name}' config_schema must describe an object.")


class ExtensionRegistry(BaseRegistry):
    """Registry of tool extensions keyed by name, with kind and body-locality metadata."""

    def __init__(self, requested_extensions: frozenset[str]):
        """Record the ``requested_extensions`` set and start with empty registration maps."""
        self._requested_extensions = requested_extensions

        self._kinds: dict[str, ExtensionKind] = {}
        self._extensions: dict[str, Callable] = {}
        self._available_extensions: set[str] = set()
        self._requires_body_locality: dict[str, bool] = {}
        self._pauses: dict[str, bool] = {}
        self._declares_output_schema: dict[str, bool] = {}
        self._config_schemas: dict[str, dict[str, Any] | None] = {}

    def register_extension(
        self,
        func: Callable,
        kind: ExtensionKind,
        name: str,
        *,
        requires_body_locality: bool = False,
        pauses: bool = False,
        declares_output_schema: bool = False,
        config_schema: dict[str, Any] | None = None,
    ):
        """Register extension ``func`` under ``name`` with its ``kind``; a duplicate name raises.

        ``config_schema`` is the JSON Schema of the settings the factory's ``config`` parameter
        receives. A factory taking ``config`` must declare one and a factory without ``config``
        must not; the schema must hold the JSON Schema rules and describe an object. Any breach
        raises and registers nothing.
        """
        # The registry is rebuilt fresh on every start/reload, so a name already
        # present within one build is a genuine collision (two extensions claiming
        # one name), never a legitimate re-register — fail loud rather than let the
        # last one silently win.
        if name in self._extensions:
            raise TaiValidationError(f"Extension '{name}' is already registered.")
        _check_config_schema(func, name, config_schema)
        self._extensions[name] = func
        self._kinds[name] = kind
        self._requires_body_locality[name] = requires_body_locality
        self._pauses[name] = pauses
        self._declares_output_schema[name] = declares_output_schema
        self._config_schemas[name] = deepcopy(config_schema)
        self._available_extensions.add(name)

    def extension(
        self,
        f: Callable | None = None,
        *,
        kind: ExtensionKind,
        name: str | None = None,
        requires_body_locality: bool = False,
        pauses: bool = False,
        declares_output_schema: bool = False,
        config_schema: dict[str, Any] | None = None,
    ):
        """Decorator form of :meth:`register_extension` (the ``app.extensions`` facet body); bare or with arguments.

        ``requires_body_locality`` marks an extension whose wrapper only works in
        the process running the tool body it wraps; the bind engine reads it to
        require the extension to sit INSIDE any execution-relocating
        (``ExtensionKind.relocates_execution``) extension in a stacked combo.
        ``pauses`` marks an extension every branch of which can pause, whatever its base declares.
        ``declares_output_schema`` marks an extension that declares the wrapped tool's output shape.
        ``config_schema`` is the JSON Schema of the settings the factory's ``config`` parameter receives.
        """
        if f and callable(f):
            return self.extension(
                kind=kind,
                name=name,
                requires_body_locality=requires_body_locality,
                pauses=pauses,
                declares_output_schema=declares_output_schema,
                config_schema=config_schema,
            )(f)

        def decorator(func):
            self.register_extension(
                func,
                kind,
                name or func.__name__,
                requires_body_locality=requires_body_locality,
                pauses=pauses,
                declares_output_schema=declares_output_schema,
                config_schema=config_schema,
            )
            return func

        return decorator

    def get_extension(self, element: ExtensionElement) -> Callable:
        """The registered factory for combo ``element``; an unregistered name raises."""
        name = extension_name(element)
        try:
            return self._extensions[name]
        except KeyError:
            raise TaiValidationError(f"Extension '{name}' is not registered.") from None

    def get_kind(self, element: ExtensionElement) -> ExtensionKind:
        """The :class:`ExtensionKind` of combo ``element``; an unregistered name raises."""
        name = extension_name(element)
        try:
            return self._kinds[name]
        except KeyError:
            raise TaiValidationError(f"Extension '{name}' is not registered.") from None

    def requires_body_locality(self, element: ExtensionElement) -> bool:
        """Whether the extension registered with ``requires_body_locality``.

        Its wrapper only works in the process running the tool body it wraps, so the bind engine
        must place it INSIDE any execution-relocating extension.
        """
        name = extension_name(element)
        try:
            return self._requires_body_locality[name]
        except KeyError:
            raise TaiValidationError(f"Extension '{name}' is not registered.") from None

    def declares_output_schema(self, element: ExtensionElement) -> bool:
        """Whether the extension registered with ``declares_output_schema=True`` (it declares the output shape)."""
        name = extension_name(element)
        try:
            return self._declares_output_schema[name]
        except KeyError:
            raise TaiValidationError(f"Extension '{name}' is not registered.") from None

    def pauses(self, element: ExtensionElement) -> bool:
        """Whether the extension registered with ``pauses=True`` (every branch it mints can pause)."""
        name = extension_name(element)
        try:
            return self._pauses[name]
        except KeyError:
            raise TaiValidationError(f"Extension '{name}' is not registered.") from None

    def config_schema(self, element: ExtensionElement) -> dict[str, Any] | None:
        """The declared settings schema of combo ``element``'s extension, ``None`` when it takes none."""
        name = extension_name(element)
        try:
            return self._config_schemas[name]
        except KeyError:
            raise TaiValidationError(f"Extension '{name}' is not registered.") from None

    def validate_config(self, element: ExtensionElement) -> None:
        """Check combo ``element``'s settings against its extension's declared schema.

        An extension without a schema accepts no settings. A mismatch is named by its JSON path and
        the schema rule it breaks, never by the value, since settings can hold secrets.
        """
        name = extension_name(element)
        schema = self.config_schema(element)
        config = extension_config(element)
        if schema is None:
            if config:
                raise TaiValidationError(f"extension '{name}' does not accept config")
            return
        try:
            validate_against_json_schema(config, schema, redact_value=True)
        except JsonSchemaValidationError as exc:
            refusal = str(exc)
        else:
            return
        raise TaiValidationError(f"extension '{name}' config: {refusal}")

    def available_extensions(self) -> list[dict[str, Any]]:
        """Every registered extension as ``{"name", "kind", "multiple", "config_schema"}``, sorted by name.

        ``multiple`` is the kind's own rule: whether one tool may carry more than one extension of it;
        ``config_schema`` is the declared settings schema, ``None`` for an extension that takes none.
        """
        # Serialize the enum VALUE (lowercase, e.g. "backend"), not its member
        # name ("BACKEND"): only the value round-trips back through
        # ``ExtensionKind("backend")``.
        return [
            {
                "name": name,
                "kind": self._kinds[name].value,
                "multiple": self._kinds[name].multiple,
                "config_schema": deepcopy(self._config_schemas[name]),
            }
            for name in sorted(self._available_extensions)
        ]

    def validate(self, elements: Sequence[ExtensionElement]):
        """Validate a combo's ``elements``: every name known and per-kind cardinality respected."""
        # Resolve each combo element to its extension NAME (a bare name or a
        # ``{"name", "config"}`` mapping) before the registry lookups.
        names = [extension_name(element) for element in elements]
        # Membership-check first so an unknown name raises a descriptive error
        # rather than a bare KeyError when the cardinality loop indexes ``_kinds``.
        unknown = [name for name in names if name not in self._kinds]
        if unknown:
            raise TaiValidationError(f"Extension(s) '{', '.join(sorted(unknown))}' not found in server.")
        for kind in self.kind_iterator():
            kind_names = [name for name in names if self._kinds[name] == kind]
            if not kind.multiple and len(kind_names) > 1:
                raise TaiValidationError(f"Only one '{kind}' extension is allowed. Found multiple: {kind_names}")

    def missing_extensions(self) -> set[str]:
        """The requested extension names that were never registered."""
        return set(self._requested_extensions - self._available_extensions)

    @staticmethod
    def kind_iterator() -> Iterator[ExtensionKind]:
        """Iterate every :class:`ExtensionKind`."""
        return iter(ExtensionKind)

    def validation(self):
        """Raise when any requested extension was not registered."""
        missing_extensions = self.missing_extensions()
        if missing_extensions:
            raise TaiValidationError(f"Extension(s) '{', '.join(sorted(missing_extensions))}' not found in server.")
