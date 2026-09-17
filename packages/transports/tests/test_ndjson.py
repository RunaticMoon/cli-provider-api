import asyncio
import io

import pytest

from cli_provider_transports.ndjson import (
    DEFAULT_MAX_FRAME_BYTES,
    FrameReader,
    FrameTooLarge,
    MalformedFrame,
    decode_frame,
    encode_frame,
)
from cli_provider_transports.stdio import StdioTransport


def test_encode_frame_is_single_newline_terminated_line():
    encoded = encode_frame({"a": 1})
    assert encoded.endswith(b"\n")
    assert encoded.count(b"\n") == 1


def test_decode_frame_round_trips_object():
    assert decode_frame(encode_frame({"a": 1}).rstrip(b"\n")) == {"a": 1}


def test_decode_frame_rejects_malformed_json_without_repair():
    with pytest.raises(MalformedFrame):
        decode_frame(b'{"a": 1')


def test_decode_frame_rejects_non_object_json():
    with pytest.raises(MalformedFrame):
        decode_frame(b"[1, 2, 3]")


def test_decode_frame_rejects_invalid_utf8():
    with pytest.raises(MalformedFrame):
        decode_frame(b"\xff\xfe")


def test_decode_frame_rejects_oversize():
    with pytest.raises(FrameTooLarge):
        decode_frame(b"x" * (DEFAULT_MAX_FRAME_BYTES + 1))


async def _reader_from(data: bytes, limit: int = 1 << 20) -> asyncio.StreamReader:
    reader = asyncio.StreamReader(limit=limit)
    reader.feed_data(data)
    reader.feed_eof()
    return reader


async def test_frame_reader_reads_sequential_frames():
    reader = await _reader_from(b'{"n":1}\n{"n":2}\n')
    frames = FrameReader(reader, max_frame_bytes=1024)
    assert await frames.read_bytes() == b'{"n":1}'
    assert await frames.read_bytes() == b'{"n":2}'
    assert await frames.read_bytes() is None


async def test_frame_reader_handles_split_writes():
    reader = asyncio.StreamReader()
    frames = FrameReader(reader, max_frame_bytes=1024)
    reader.feed_data(b'{"n":')
    reader.feed_data(b"1}\n")
    reader.feed_eof()
    assert await frames.read_bytes() == b'{"n":1}'


async def test_frame_reader_rejects_oversize_before_newline():
    reader = asyncio.StreamReader()
    frames = FrameReader(reader, max_frame_bytes=16)
    reader.feed_data(b"x" * 64)
    with pytest.raises(FrameTooLarge):
        await frames.read_bytes()


async def test_frame_reader_rejects_unterminated_frame_at_eof():
    reader = await _reader_from(b'{"n":1}')
    frames = FrameReader(reader, max_frame_bytes=1024)
    with pytest.raises(MalformedFrame):
        await frames.read_bytes()


class _ByteWriter:
    def __init__(self) -> None:
        self.buf = bytearray()

    def write(self, data: bytes) -> None:
        self.buf.extend(data)

    async def drain(self) -> None:
        return None


def test_stdio_transport_keeps_stderr_separate():
    stderr = io.StringIO()
    writer = _ByteWriter()
    transport = StdioTransport(
        reader=None, writer=writer, stderr_writer=stderr, max_frame_bytes=1024
    )
    asyncio.run(transport.send({"hello": "world"}))
    assert bytes(writer.buf) == encode_frame({"hello": "world"})
    transport.log_stderr("note")
    assert stderr.getvalue() == "note\n"
