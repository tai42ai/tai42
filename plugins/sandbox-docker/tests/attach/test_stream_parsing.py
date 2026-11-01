"""The attachment's reading of the engine's bytes, fed to it directly in reads of chosen sizes.

No socket carries the bytes: each test hands them to the carrier as the loop's reads would,
so where a read boundary falls is fixed by the test, not by the OS.
"""

from __future__ import annotations

import asyncio
import itertools

import pytest
from tai42_contract.sandbox import SandboxError, SandboxStreamChunk

from tai42_sandbox_docker.attach import ExecAttachment
from tai42_sandbox_docker.attach_carrier import PlainCarrier

_UPGRADED = (
    b"HTTP/1.1 101 UPGRADED\r\nContent-Type: application/vnd.docker.raw-stream\r\n"
    b"Connection: Upgrade\r\nUpgrade: tcp\r\n\r\n"
)


def _frame(stream: int, data: bytes) -> bytes:
    return bytes([stream, 0, 0, 0]) + len(data).to_bytes(4, "big") + data


def _attached() -> tuple[ExecAttachment, PlainCarrier]:
    """An attachment on a carrier with no connection under it: the test is the engine's reads."""
    attachment = ExecAttachment()
    carrier = PlainCarrier(attachment)
    attachment._carrier = carrier
    return attachment, carrier


def _in_reads(data: bytes, *cuts: int) -> list[bytes]:
    """``data`` as the reads that end at each of ``cuts``, and one more for the rest."""
    bounds = [0, *cuts, len(data)]
    return [data[start:end] for start, end in itertools.pairwise(bounds)]


async def _read_all(attachment: ExecAttachment) -> list[SandboxStreamChunk]:
    chunks = []
    while (chunk := await asyncio.wait_for(attachment.read(), 5)) is not None:
        chunks.append(chunk)
    return chunks


async def test_an_ended_stream_reads_what_it_left_then_its_end() -> None:
    attachment, carrier = _attached()
    carrier.data_received(_UPGRADED + _frame(1, b"out"))
    carrier.eof_received()

    assert await attachment.read() == SandboxStreamChunk(stream="stdout", data=b"out")
    assert await attachment.read() is None


async def test_an_upgrade_answer_split_across_several_reads_upgrades_the_stream() -> None:
    attachment, carrier = _attached()
    reads = _in_reads(_UPGRADED, 3, 9, 40, len(_UPGRADED) - 2)

    for data in reads[:-1]:
        carrier.data_received(data)
        assert not attachment._upgraded.done()
    carrier.data_received(reads[-1])

    assert attachment._upgraded.done()
    carrier.data_received(_frame(1, b"out"))
    carrier.eof_received()
    assert await _read_all(attachment) == [SandboxStreamChunk(stream="stdout", data=b"out")]


async def test_an_upgrade_answer_and_the_first_frame_in_one_read_yield_that_frame() -> None:
    attachment, carrier = _attached()

    carrier.data_received(_UPGRADED + _frame(2, b"err") + _frame(1, b"out"))

    assert attachment._upgraded.done()
    carrier.eof_received()
    assert await _read_all(attachment) == [
        SandboxStreamChunk(stream="stderr", data=b"err"),
        SandboxStreamChunk(stream="stdout", data=b"out"),
    ]


@pytest.mark.parametrize("cut", [1, 4, 7])
async def test_a_frame_header_split_across_two_reads_yields_the_whole_frame(cut: int) -> None:
    attachment, carrier = _attached()
    carrier.data_received(_UPGRADED)
    first, rest = _in_reads(_frame(1, b"payload"), cut)

    carrier.data_received(first)
    assert not attachment._chunks
    carrier.data_received(rest)

    carrier.eof_received()
    assert await _read_all(attachment) == [SandboxStreamChunk(stream="stdout", data=b"payload")]


async def test_an_answer_that_is_not_http_ends_the_stream_with_its_error() -> None:
    attachment, carrier = _attached()

    carrier.data_received(b"garbage\r\n\r\n")

    assert carrier.ended
    with pytest.raises(SandboxError, match="answered the exec attach with 'garbage'"):
        await attachment._upgraded


async def test_a_close_here_inside_an_output_frame_ends_the_output_after_the_whole_frames() -> None:
    attachment, carrier = _attached()
    carrier.data_received(_UPGRADED + _frame(1, b"whole") + _frame(1, b"cut short")[:12])

    attachment.close()

    assert await _read_all(attachment) == [SandboxStreamChunk(stream="stdout", data=b"whole")]


async def test_the_engine_ending_the_stream_inside_an_output_frame_raises_after_the_whole_frames() -> None:
    attachment, carrier = _attached()
    carrier.data_received(_UPGRADED + _frame(1, b"whole") + _frame(1, b"cut short")[:12])

    carrier.eof_received()

    assert await asyncio.wait_for(attachment.read(), 5) == SandboxStreamChunk(stream="stdout", data=b"whole")
    with pytest.raises(SandboxError, match=r"ended inside an output frame \(12 bytes left over\)"):
        await asyncio.wait_for(attachment.read(), 5)
