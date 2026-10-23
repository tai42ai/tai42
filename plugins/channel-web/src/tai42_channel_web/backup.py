"""The ``channel_web_words`` backup section: every web route's stored words.

The exporter writes ``{"version": 1, "words": {identity: words}}``; the importer validates the
whole document before it writes anything, then stores each identity's words (replacing any already
set). The section is not secret: the words are the texts a visitor reads.
"""

from __future__ import annotations

from typing import Any, Final

from pydantic import ValidationError
from tai42_contract.app import tai42_app
from tai42_contract.backup import BackupSectionReport

from tai42_channel_web.routes.dtos import _clean_identity
from tai42_channel_web.routes.session_access import _store_configured
from tai42_channel_web.store.words import get_words, list_words, set_words
from tai42_channel_web.words import WebWords, stored_payload

_SECTION: Final = "channel_web_words"
_VERSION: Final = 1


class WordsArchiveError(ValueError):
    """A ``channel_web_words`` document that is not a version-1 words archive; nothing is written."""


async def export_words() -> dict[str, Any]:
    """The section exporter: every web route's words.

    An empty map when no store is configured on this deployment (no words can exist).
    """
    if not _store_configured():
        return {"version": _VERSION, "words": {}}
    listed = await list_words()
    return {"version": _VERSION, "words": {identity: stored_payload(words) for identity, words in listed.items()}}


def _archive_words(payload: Any) -> dict[str, WebWords]:
    """The validated words of a version-1 archive, or :class:`WordsArchiveError` naming the first fault."""
    if not isinstance(payload, dict):
        raise WordsArchiveError(f"the {_SECTION} backup payload is not an object")
    version = payload.get("version")
    # bool is an int subclass — refuse it explicitly (True would pass as 1).
    if not isinstance(version, int) or isinstance(version, bool) or version != _VERSION:
        raise WordsArchiveError(
            f"the {_SECTION} backup payload version {version!r} is not one this plugin reads ({_VERSION})"
        )
    entries = payload.get("words")
    if not isinstance(entries, dict):
        raise WordsArchiveError(f"the {_SECTION} backup payload carries no words map")
    words: dict[str, WebWords] = {}
    for identity, entry in entries.items():
        if not isinstance(identity, str) or _clean_identity(identity) != identity:
            raise WordsArchiveError(f"the {_SECTION} backup payload names an unusable web route {identity!r}")
        try:
            words[identity] = WebWords.model_validate(entry)
        except ValidationError as exc:
            raise WordsArchiveError(f"the {_SECTION} words for web route {identity!r} are not valid: {exc}") from exc
    return words


async def import_words(payload: Any) -> BackupSectionReport:
    """The section importer: store every identity's words from ``payload``.

    The whole document is validated first, so a broken entry writes nothing. Words in a document
    imported where no store is configured raise: they would have nowhere to go.
    """
    words = _archive_words(payload)
    if not words:
        return BackupSectionReport()
    if not _store_configured():
        raise RuntimeError(
            f"cannot import the {_SECTION} section: the web channel store is not configured on this deployment"
        )
    report = BackupSectionReport()
    for identity, entry in words.items():
        existed = await get_words(identity) is not None
        await set_words(identity, entry)
        if existed:
            report.updated += 1
        else:
            report.created += 1
    return report


@tai42_app.lifecycle.on_startup
def register_words_backup_section() -> None:
    """Register the ``channel_web_words`` section once per boot.

    Idempotent: the host keeps one backup registry across reloads, so a re-run skips rather than
    tripping the duplicate-name guard.
    """
    if any(section.name == _SECTION for section in tai42_app.backup.sections()):
        return
    tai42_app.backup.register_section(_SECTION, export_words, import_words)
