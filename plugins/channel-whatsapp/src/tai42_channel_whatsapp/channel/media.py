"""Media-item sends: each image/document/video/audio as its own native message.

Any ``link`` items are rendered as appended body lines.
"""

from __future__ import annotations

from tai42_contract.channels import ChannelDeliveryError
from tai42_contract.interactions.models import MediaItem, MediaKind

from tai42_channel_whatsapp.client import (
    send_audio,
    send_document,
    send_image,
    send_message,
    send_video,
)


def _link_line(item: MediaItem) -> str:
    """A ``link`` media item rendered as one appended body line."""
    return f"{item.caption}: {item.url}" if item.caption else item.url


def _body_with_links(message: str, links: list[MediaItem]) -> str:
    """The message body with each ``link`` media item appended as its own line.

    A blank ``message`` (a media-only send) contributes no leading blank line — the body is
    then the link lines alone, or ``""`` when there are no links (an images-only send whose
    body is skipped entirely).
    """
    link_lines = [_link_line(item) for item in links]
    if not message.strip():
        return "\n".join(link_lines)
    if not link_lines:
        return message
    return "\n".join([message, *link_lines])


async def _send_one_file(phone_number_id: str, target: str, item: MediaItem) -> str:
    """Send one file-media item (image/document/video/audio) as its own native message and return its ``wamid``.

    A ``link`` never reaches here (it renders as a body line).
    """
    if item.kind is MediaKind.IMAGE:
        return await send_image(phone_number_id=phone_number_id, to=target, link=item.url, caption=item.caption)
    if item.kind is MediaKind.DOCUMENT:
        return await send_document(
            phone_number_id=phone_number_id, to=target, link=item.url, caption=item.caption, filename=item.filename
        )
    if item.kind is MediaKind.VIDEO:
        return await send_video(phone_number_id=phone_number_id, to=target, link=item.url, caption=item.caption)
    # AUDIO: the Cloud API audio object carries no caption/filename (dropped at the channel).
    return await send_audio(phone_number_id=phone_number_id, to=target, link=item.url)


async def _send_file_media(phone_number_id: str, target: str, files: list[MediaItem], sent: list[str]) -> list[str]:
    """Send each file-media item (image/document/video/audio) as its own native message.

    Extends ``sent`` with the minted ``wamid`` in order and returns it.

    Each item is its own message, so there is no native per-send cap here — the platform
    guard bounds the item count. A part that fails mid-send raises naming the wamids already
    delivered (partial delivery stays visible).
    """
    for item in files:
        try:
            sent.append(await _send_one_file(phone_number_id, target, item))
        except ChannelDeliveryError as exc:
            raise ChannelDeliveryError(f"WhatsApp multi-part send failed after delivering {sent}: {exc}") from exc
    return sent


async def _send_media_prelude(phone_number_id: str, target: str, media: list[MediaItem]) -> list[str]:
    """Send a delivered question's accompanying display media as its own messages, BEFORE the question.

    Any ``link`` items go out as one text line-block, then each file item
    (image/document/video/audio) as its own native message (the same per-item send as
    ``notify``).

    Media rides ahead of the question so the actionable prompt (the last message,
    carrying any tappable widget) stays at the foot of the chat. Sent before any
    reservation, so a media failure raises with nothing reserved; a partial send
    raises naming the wamids already delivered.
    """
    files = [item for item in media if item.kind is not MediaKind.LINK]
    links = [item for item in media if item.kind == MediaKind.LINK]
    sent: list[str] = []
    if links:
        sent.append(
            await send_message(
                phone_number_id=phone_number_id, to=target, body="\n".join(_link_line(item) for item in links)
            )
        )
    return await _send_file_media(phone_number_id, target, files, sent)
