"""The name a bound tool is served under.

A leaf module with no app import, so a consumer that only needs the naming rule (a client
looking up a connected server's tool on the served surface) imports it without loading the
tool-binding engine.
"""


def normalized_name(prefix: str, name: str) -> str:
    """The served name of tool ``name`` bound under ``prefix``.

    Both parts are lower-cased with ``-`` turned into ``_``; the prefix is prepended unless the
    name already carries it.
    """
    name = name.lower().replace("-", "_")
    prefix = prefix.lower().replace("-", "_")
    # Prefix unless already prefixed. The check must respect the ``_``
    # separator: ``slacker`` under prefix ``slack`` is NOT already prefixed
    # (a bare ``startswith(prefix)`` would wrongly treat it as such), so it
    # becomes ``slack_slacker``.
    already_prefixed = name == prefix or name.startswith(prefix + "_")
    return name if not prefix or already_prefixed else f"{prefix}_{name}"
