"""What a provider's model can carry: the shared check behind the chat and the embedding builders.

The kit's rule: each setting handed to a model builder reaches the provider — a connection setting is
configured on the provider's client, a generation setting is sent in its request — or the build
refuses it with ``ValueError`` naming it. No setting is dropped. What a provider carries is read from
its own client: the model class's declared fields (by name or alias) that it applies, plus, where the
model forwards its ``model_kwargs`` into the SDK call it makes, the parameters that call accepts. The
same setting given twice (under two names, or once directly and once in a client option) is refused
naming both, never collapsed to one value.
"""

import inspect
from collections.abc import Callable, Collection, Mapping
from typing import Any

from pydantic import AliasChoices, BaseModel

# What a model forwards through ``model_kwargs``: a fixed set of names, ``None`` for any name, or a
# function of the class, the declared kwargs and the forwarded ``model_kwargs`` that names the set for
# the SDK call that model will make.
type Forwardable = frozenset[str] | Callable[[type[BaseModel], dict[str, Any], dict[str, Any]], frozenset[str]] | None


def declared_names(model_cls: type[BaseModel]) -> frozenset[str]:
    """Every name ``model_cls`` accepts at construction: its fields and their aliases."""
    names: set[str] = set()
    for name, info in model_cls.model_fields.items():
        names.add(name)
        if isinstance(info.alias, str):
            names.add(info.alias)
        if isinstance(info.validation_alias, str):
            names.add(info.validation_alias)
        elif isinstance(info.validation_alias, AliasChoices):
            names.update(choice for choice in info.validation_alias.choices if isinstance(choice, str))
    return frozenset(names)


def parameters(*calls: Callable[..., Any]) -> frozenset[str]:
    """The keyword parameters the SDK ``calls`` accept (their signatures are the authority)."""
    return frozenset(
        name
        for call in calls
        for name, parameter in inspect.signature(call).parameters.items()
        if name != "self" and parameter.kind in (parameter.KEYWORD_ONLY, parameter.POSITIONAL_OR_KEYWORD)
    )


def refuse(provider: str, names: Collection[str], *, model: str = "chat model") -> None:
    """Refuse the settings ``names`` that ``provider``'s ``model`` can carry neither way."""
    if names:
        listed = ", ".join(repr(name) for name in sorted(names))
        raise ValueError(
            f"{provider}: {listed} cannot reach the provider: its {model} neither applies nor sends such a setting"
        )


def refuse_twice(provider: str, first: str, second: str) -> None:
    """Refuse one setting given twice, as ``first`` and as ``second``."""
    raise ValueError(f"{provider}: {first!r} and {second!r} are the same setting given twice; give only one")


def carried(
    provider: str,
    kwargs: Mapping[str, Any],
    model_cls: type[BaseModel],
    forwardable: Forwardable,
    *,
    unsent: frozenset[str] = frozenset(),
    model: str = "chat model",
) -> dict[str, Any]:
    """The constructor kwargs for ``model_cls``: declared names as given, the rest as ``model_kwargs``.

    ``forwardable`` names what the model's SDK call accepts through ``model_kwargs`` (``None``: any
    name, written into the request body; empty: the model forwards none). ``unsent`` names declared
    fields the model never applies; they are refused even where any other name is forwarded. A name
    given both directly and inside ``model_kwargs`` is refused as given twice, and any ``model_kwargs``
    entry the SDK call does not accept is refused with the rest.
    """
    declared = declared_names(model_cls) - unsent
    built = {key: value for key, value in kwargs.items() if key in declared}
    given = dict(built.pop("model_kwargs", None) or {}) if "model_kwargs" in declared else {}
    undeclared = {key: value for key, value in kwargs.items() if key not in declared and key not in unsent}
    for key in undeclared:
        if key in given:
            refuse_twice(provider, key, f"model_kwargs[{key!r}]")
    forwarded = {**given, **undeclared}
    allowed = forwardable(model_cls, built, forwarded) if callable(forwardable) else forwardable
    # An unsent name is refused given flat or inside the caller's ``model_kwargs`` (from which the
    # model would move it into the declared field it never reads).
    rejected = {key for key in kwargs if key in unsent} | {key for key in given if key in unsent}
    if allowed is not None:
        rejected |= {key for key in forwarded if key not in allowed}
    refuse(provider, rejected, model=model)
    if forwarded:
        built["model_kwargs"] = forwarded
    return built


def _option_path(path: tuple[str, ...]) -> str:
    return "".join([path[0], *(f"[{part!r}]" for part in path[1:])])


def set_client_option(
    provider: str,
    kwargs: dict[str, Any],
    path: tuple[str, ...],
    value: Any,
    *,
    flat: str,
    header: bool = False,
    also: tuple[str, ...] = (),
) -> None:
    """Set the client option ``kwargs[path[0]]…[path[-1]] = value`` for the flat setting ``flat``.

    ``header`` marks ``path[-1]`` as an HTTP header name in a header mapping, compared without letter
    case (HTTP header names are case-insensitive). ``also`` names other containers whose entries reach
    the same client (a per-client override of ``path[0]``). A value the caller already set at that place
    in any of them is refused as the same setting given twice, never overwritten.
    """
    last = path[-1]
    for container in (path[0], *also):
        node: Any = kwargs.get(container) or {}
        for part in path[1:-1]:
            node = (node or {}).get(part) or {}
        taken = [key for key in node if (key.lower() == last.lower() if header else key == last)]
        if taken:
            refuse_twice(provider, flat, _option_path((container, *path[1:-1], taken[0])))
    target = kwargs
    for part in path[:-1]:
        target[part] = dict(target.get(part) or {})
        target = target[part]
    target[last] = value
