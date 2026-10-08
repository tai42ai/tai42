"""The rendered-template cache: a template's bodies rendered to jq text, keyed on what the render read.

A rendered body depends on the template row and on the resource manager that rendered it (a by-id
body or fragment is fetched and rendered through it). The key is ``(template name, template
version, manager epoch, manager eviction generation)``: a row write takes a new version, a stored
resource eviction bumps the generation, and a rebuilt manager carries a new epoch, so each causes a
re-render. An entry also expires after the manager's own cache TTL, and nothing is cached while
the manager's cache is disabled. Only a successful render is stored.

A re-render can read a stored resource that changed with no eviction reaching this process (an
expired entry, or every read while the manager's cache is off), so the served version token also
carries :func:`rendered_digest` of what the render produced: new text always gives a new token.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tai42_contract.states.rendered import RenderedStateTemplate

    from tai42_skeleton.states.templates import StateTemplate

#: The bound on rendered templates held per process.
RENDERED_TEMPLATES_MAX = 256

#: Hex digits of the content digest a rendered version token carries.
_DIGEST_HEX = 16

RenderedKey = tuple[str, int, int, int]


def rendered_digest(served: RenderedStateTemplate) -> str:
    """A digest of everything a render produced: the served template without its ``version``."""
    content = served.model_dump(mode="json", by_alias=True, exclude={"version"})
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()[:_DIGEST_HEX]


@dataclass(frozen=True, slots=True)
class RenderedEntry:
    """One template rendered: every program body, the declarations check, the input order and sibling prelude.

    ``served`` is the facet's :class:`~tai42_contract.states.models.RenderedStateTemplate`, built once
    and shared by every caller as a read-only value.
    """

    template: StateTemplate
    fragment: dict[str, Any]
    bodies: dict[str, str]
    declarations_check: str | None
    input_order: tuple[str, ...]
    sibling_prelude: str
    served: RenderedStateTemplate
    cached_at: float


class RenderedTemplates:
    """A process-scope LRU of :class:`RenderedEntry`, bounded at :data:`RENDERED_TEMPLATES_MAX`.

    Mutations run under a ``threading.Lock`` that is never held across an ``await``; two concurrent
    misses both render and the last insert wins.
    """

    def __init__(self, maxsize: int = RENDERED_TEMPLATES_MAX) -> None:
        """Start empty with the given bound."""
        self._maxsize = maxsize
        self._lock = threading.Lock()
        self._entries: OrderedDict[RenderedKey, RenderedEntry] = OrderedDict()

    def get(self, key: RenderedKey, ttl: int | None) -> RenderedEntry | None:
        """The entry for ``key`` while it is fresh (``ttl`` ``None`` never expires by age), else ``None``."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if ttl is not None and time.monotonic() - entry.cached_at >= ttl:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return entry

    def put(self, key: RenderedKey, entry: RenderedEntry) -> None:
        """Store ``entry``, evicting the least recently used entry past the bound."""
        with self._lock:
            self._entries[key] = entry
            self._entries.move_to_end(key)
            while len(self._entries) > self._maxsize:
                self._entries.popitem(last=False)

    def __len__(self) -> int:
        """The number of entries held."""
        with self._lock:
            return len(self._entries)
