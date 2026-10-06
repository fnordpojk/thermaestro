import asyncio
import contextlib
import json
import os
import shutil
import socket
import stat
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from capfake import FakePump

from thermaestro.cap import BadLine, Closed, Endpoint, pair, serve
from thermaestro.cap import sockets as cap_sockets
from thermaestro.cap.carrier import StreamEndpoint, parse
from thermaestro.cap.messages import Auth, Describe, Error, Hello
from thermaestro.cap.sockets import (
    AlreadyRunning,
    UnsafePath,
    connect_tcp,
    connect_unix,
    listen_tcp,
    listen_unix,
    private_directory,
)


@pytest.fixture
def short_dir() -> Iterator[Path]:
    # A Unix socket's path has to fit in about 100 bytes, which pytest's own
    # temporary directories don't always leave room for.
    d = Path(tempfile.mkdtemp(prefix="cap", dir="/tmp"))
    yield d
    shutil.rmtree(d)


async def serve_fake(endpoint: Endpoint) -> None:
    await serve(endpoint, FakePump())


@contextlib.asynccontextmanager
async def running(server: asyncio.Server) -> AsyncIterator[None]:
    """Shut the server down at the end, with any connection a failed test left open."""
    try:
        yield
    finally:
        server.close()
        server.close_clients()
        await asyncio.wait_for(server.wait_closed(), 5)


async def describes(endpoint: Endpoint) -> bool:
    """Whether the plugin on `endpoint` answers describe. Closes the endpoint."""
    try:
        await endpoint.send(Describe(id=1))
        while True:  # the plugin may send its health first
            if (await asyncio.wait_for(endpoint.receive(), 5)).type == "described":
                return True
    except Closed:
        return False
    finally:
        await endpoint.close()


@pytest.fixture
async def unix_server(short_dir: Path) -> AsyncIterator[Path]:
    path = short_dir / "plugins" / "s"
    async with running(await listen_unix(path, serve_fake)):
        yield path


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_the_directory_is_made_private(short_dir: Path) -> None:
    private_directory(short_dir / "a" / "b")
    assert mode(short_dir / "a" / "b") == 0o700


@pytest.mark.parametrize(
    ("bits", "safe"), [(0o700, True), (0o750, True), (0o770, False), (0o755, False)]
)
def test_an_existing_directory_must_be_closed_to_others(
    short_dir: Path, bits: int, safe: bool
) -> None:
    d = short_dir / "d"
    d.mkdir()
    d.chmod(bits)
    if safe:
        private_directory(d)
    else:
        with pytest.raises(UnsafePath, match="open to others"):
            private_directory(d)


async def test_the_socket_is_for_this_user_only(unix_server: Path) -> None:
    assert stat.S_ISSOCK(unix_server.lstat().st_mode)
    assert mode(unix_server) & 0o077 == 0


async def test_a_second_instance_refuses_to_start(unix_server: Path) -> None:
    with pytest.raises(AlreadyRunning):
        await listen_unix(unix_server, serve_fake)


async def test_a_socket_left_from_a_crash_is_replaced(short_dir: Path) -> None:
    path = short_dir / "s"
    left = socket.socket(socket.AF_UNIX)
    left.bind(str(path))  # bound but never listening, as after a crash
    left.close()
    async with running(await listen_unix(path, serve_fake)):
        assert await describes(await connect_unix(path))


async def test_something_else_at_the_path_is_refused(short_dir: Path) -> None:
    path = short_dir / "s"
    path.write_text("not a socket")
    with pytest.raises(UnsafePath, match="isn't a socket"):
        await listen_unix(path, serve_fake)


async def test_a_peer_with_another_uid_is_refused(short_dir: Path) -> None:
    path = short_dir / "s"
    async with running(await listen_unix(path, serve_fake, uids={os.getuid() + 1})):
        assert not await describes(await connect_unix(path))


async def test_a_peer_in_an_allowed_group_is_taken(short_dir: Path) -> None:
    path = short_dir / "s"
    async with running(await listen_unix(path, serve_fake, uids=(), gids={os.getgid()})):
        assert await describes(await connect_unix(path))


async def raw_exchange(path: Path, line: bytes) -> Error:
    reader, writer = await asyncio.open_unix_connection(path)
    try:
        writer.write(line)
        await writer.drain()
        while True:
            message = parse(await asyncio.wait_for(reader.readline(), 5))
            if isinstance(message, Error):
                return message
    finally:
        writer.close()


@pytest.mark.parametrize(
    ("line", "code", "id"),
    [
        (b"not json\n", "invalid", None),
        (b"[1, 2]\n", "invalid", None),
        (b'{"type": "bogus", "id": 4}\n', "unsupported", 4),
        (b'{"type": "read", "id": 5}\n', "invalid", 5),
        (b'{"type": "values", "id": 6, "values": []}\n', "unsupported", 6),
    ],
)
async def test_a_plugin_answers_lines_it_cant_serve(
    unix_server: Path, line: bytes, code: str, id: int | None
) -> None:
    error = await raw_exchange(unix_server, line)
    assert (error.code, error.id) == (code, id)


async def test_the_connection_goes_on_after_a_bad_line(unix_server: Path) -> None:
    reader, writer = await asyncio.open_unix_connection(unix_server)
    writer.write(b"garbage\n")
    assert await describes(StreamEndpoint(reader, writer))


def test_parse_keeps_the_id_where_it_can() -> None:
    with pytest.raises(BadLine) as e:
        parse(b'{"type": "act", "id": 3, "lever": "hp1/dhw/block"}')
    assert (e.value.code, e.value.id) == ("invalid", 3)
    with pytest.raises(BadLine) as e:
        parse(b'{"type": "act", "id": true}')
    assert e.value.id is None


@pytest.fixture
async def tcp_server(short_dir: Path) -> AsyncIterator[Path]:
    connection_file = short_dir / "plugins" / "connection.json"
    async with running(await listen_tcp(connection_file, serve_fake)):
        yield connection_file


async def test_the_connection_file_is_private(tcp_server: Path) -> None:
    assert mode(tcp_server) == 0o600
    info = json.loads(tcp_server.read_text())
    assert info["host"] == "127.0.0.1"
    assert len(info["token"]) == 64


async def test_tcp_with_the_token(tcp_server: Path) -> None:
    assert await describes(await connect_tcp(tcp_server))


DESCRIBE_LINE = b'{"type": "describe", "id": 1}\n'


async def tcp_raw(connection_file: Path, first: bytes) -> bytes:
    info = json.loads(connection_file.read_text())
    reader, writer = await asyncio.open_connection(info["host"], info["port"])
    try:
        writer.write(first + DESCRIBE_LINE)
        await writer.drain()
        return await asyncio.wait_for(reader.read(), 5)
    finally:
        writer.close()


async def test_tcp_with_the_wrong_token_is_closed_unanswered(tcp_server: Path) -> None:
    wrong = Auth(token="0" * 64).model_dump_json().encode() + b"\n"
    assert await tcp_raw(tcp_server, wrong) == b""


async def test_tcp_without_a_token_is_closed_unanswered(tcp_server: Path) -> None:
    assert await tcp_raw(tcp_server, b"") == b""


async def test_tcp_waits_only_so_long_for_the_token(
    tcp_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cap_sockets, "AUTH_TIMEOUT_S", 0.2)
    info = json.loads(tcp_server.read_text())
    reader, writer = await asyncio.open_connection(info["host"], info["port"])
    assert await asyncio.wait_for(reader.read(), 5) == b""
    writer.close()


async def test_in_process_closing_ends_both_sides() -> None:
    core, plugin = pair()
    await core.send(Hello(id=1, protocol="thermaestro-cap", version="0.1", role="core"))
    assert (await plugin.receive()).type == "hello"
    await core.close()
    with pytest.raises(Closed):
        await plugin.receive()
    with pytest.raises(Closed):
        await core.receive()
    with pytest.raises(Closed):
        await core.send(Describe(id=2))
