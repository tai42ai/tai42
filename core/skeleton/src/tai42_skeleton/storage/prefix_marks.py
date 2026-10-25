"""Per-epoch storage prefix marks: "not backed up" and "unlisted".

Both marks share one prefix rule: a canonical store key followed by ``/``. Keys
compared against a mark are canonical (every door runs the shared key guard), so a
plain ``startswith`` is exact.
"""

from __future__ import annotations

from tai42_skeleton.template.path_guard import UnsafeTemplatePathError, safe_template_path


class PrefixSet:
    """The prefixes declared for one mark in the current serving epoch."""

    def __init__(self) -> None:
        """Start with no prefix declared."""
        self._prefixes: set[str] = set()

    def declare(self, prefix: str) -> None:
        """Add ``prefix``; it must be a canonical key followed by ``/``. Declaring it again is a no-op."""
        if not isinstance(prefix, str) or not prefix.endswith("/"):
            raise ValueError(f"storage mark prefix {prefix!r} must be a canonical store key followed by '/'")
        try:
            safe_template_path(prefix[:-1])
        except UnsafeTemplatePathError as exc:
            raise ValueError(f"storage mark prefix {prefix!r} is not canonical: {exc}") from exc
        self._prefixes.add(prefix)

    def prefixes(self) -> frozenset[str]:
        """The declared prefixes."""
        return frozenset(self._prefixes)

    def reset(self) -> None:
        """Drop every declared prefix."""
        self._prefixes.clear()


class PrefixMarkRegistry:
    """The two independent marks; a prefix may carry either or both."""

    def __init__(self) -> None:
        """Start with both marks empty."""
        self.not_backed_up = PrefixSet()
        self.unlisted = PrefixSet()

    def reset(self) -> None:
        """Drop every prefix of both marks."""
        self.not_backed_up.reset()
        self.unlisted.reset()


def covering_prefix(key: str, prefixes: frozenset[str]) -> str | None:
    """Return the declared prefix ``key`` lies under, or ``None``."""
    for prefix in prefixes:
        if key.startswith(prefix):
            return prefix
    return None
