"""Plugins out of process: the core listens, and each plugin connects.

On a Unix socket, the socket sits in a directory only the service user (and optionally a
plugin group) can enter, and every connection's peer credentials are checked. Where
there's no Unix socket, the core listens on TCP 127.0.0.1 and a plugin proves itself
with a token from a file only the service user can read.
"""

import asyncio
import contextlib
import hmac
import json
import logging
import os
import secrets
import socket
import stat
import struct
from collections.abc import Awaitable, Callable, Collection
from pathlib import Path

from .carrier import LINE_LIMIT, BadLine, Closed, Endpoint, StreamEndpoint
from .messages import Auth

log = logging.getLogger(__name__)

OnPlugin = Callable[[Endpoint], Awaitable[None]]
"""Runs one plugin's connection; it is closed when this returns."""

AUTH_TIMEOUT_S = 5.0


class SocketError(Exception):
    pass


class AlreadyRunning(SocketError):
    """Another instance is listening on the socket."""


class UnsafePath(SocketError):
    """The directory or the path isn't safe to listen in."""


def unix_available() -> bool:
    """Whether this platform has Unix sockets with peer credentials."""
    return hasattr(socket, "AF_UNIX") and hasattr(socket, "SO_PEERCRED")


async def listen_unix(
    path: Path,
    on_plugin: OnPlugin,
    *,
    uids: Collection[int] | None = None,
    gids: Collection[int] = (),
) -> asyncio.Server:
    """Listen for plugins on a Unix socket. A connection is taken only from a process
    running as one of `uids` (by default this process's own user) or in one of `gids`."""
    if not unix_available():
        raise SocketError("no Unix sockets with peer credentials here; use TCP")
    allowed_uids = frozenset(uids if uids is not None else (os.getuid(),))
    allowed_gids = frozenset(gids)
    private_directory(path.parent)
    await _refuse_live(path)

    async def connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        _, uid, gid = peer_credentials(writer.get_extra_info("socket"))
        endpoint = StreamEndpoint(reader, writer)
        if uid not in allowed_uids and gid not in allowed_gids:
            log.warning("refused a plugin connection from uid %d, gid %d", uid, gid)
            await endpoint.close()
            return
        await _run(on_plugin, endpoint)

    # The socket file takes its mode from the umask; nobody else may even connect.
    old = os.umask(0o077)
    try:
        return await asyncio.start_unix_server(connected, path=path, limit=LINE_LIMIT)
    finally:
        os.umask(old)


async def listen_tcp(
    connection_file: Path, on_plugin: OnPlugin, *, port: int = 0
) -> asyncio.Server:
    """Listen for plugins on TCP 127.0.0.1. Writes the address and a new token to
    `connection_file`, readable by this user only; a plugin's first line must carry it."""
    token = secrets.token_hex(32)

    async def connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        endpoint = StreamEndpoint(reader, writer)
        try:
            first = await asyncio.wait_for(endpoint.receive(), AUTH_TIMEOUT_S)
        except (TimeoutError, Closed, BadLine):
            first = None
        if not (isinstance(first, Auth) and hmac.compare_digest(first.token, token)):
            log.warning("refused a plugin connection from %s", writer.get_extra_info("peername"))
            await endpoint.close()
            return
        await _run(on_plugin, endpoint)

    private_directory(connection_file.parent)
    server = await asyncio.start_server(connected, host="127.0.0.1", port=port, limit=LINE_LIMIT)
    bound = server.sockets[0].getsockname()[1]
    contents = {"host": "127.0.0.1", "port": bound, "token": token}
    write_private(connection_file, json.dumps(contents).encode())
    return server


async def connect_unix(path: Path) -> Endpoint:
    reader, writer = await asyncio.open_unix_connection(path, limit=LINE_LIMIT)
    return StreamEndpoint(reader, writer)


async def connect_tcp(connection_file: Path) -> Endpoint:
    try:
        info = json.loads(await asyncio.to_thread(connection_file.read_bytes))
        host, port, token = str(info["host"]), int(info["port"]), str(info["token"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise SocketError(f"can't use {connection_file}: {e}") from e
    reader, writer = await asyncio.open_connection(host, port, limit=LINE_LIMIT)
    endpoint = StreamEndpoint(reader, writer)
    await endpoint.send(Auth(token=token))
    return endpoint


def peer_credentials(sock: socket.socket) -> tuple[int, int, int]:
    """The connecting process's pid, uid and gid, as the kernel saw them at connect."""
    size = struct.calcsize("3i")
    pid, uid, gid = struct.unpack(
        "3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, size)
    )
    return pid, uid, gid


def private_directory(directory: Path) -> None:
    """Make `directory` 0700 if it doesn't exist. If it does, it must be this user's and
    closed to others: 0700, or 0750 for a plugin group."""
    try:
        directory.mkdir(mode=0o700, parents=True)
    except FileExistsError:
        pass
    else:
        directory.chmod(0o700)  # mkdir's mode passes through the umask
        return
    st = directory.lstat()
    if not stat.S_ISDIR(st.st_mode):
        raise UnsafePath(f"{directory} isn't a directory")
    if st.st_uid != os.getuid():
        raise UnsafePath(f"{directory} belongs to uid {st.st_uid}, not this user")
    if st.st_mode & 0o027:
        raise UnsafePath(f"{directory} is open to others ({stat.filemode(st.st_mode)})")


def write_private(path: Path, data: bytes) -> None:
    """Write a file only this user can read, so that it's either all old or all new:
    a temporary file, fsync, rename, then fsync the directory."""
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        temporary.replace(path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
        raise
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


async def _refuse_live(path: Path) -> None:
    # Binding would silently take over a running instance's socket, so look first. A
    # socket nobody listens on is left from a crash and goes.
    try:
        st = await asyncio.to_thread(path.lstat)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(st.st_mode):
        raise UnsafePath(f"{path} exists and isn't a socket")
    try:
        _, writer = await asyncio.open_unix_connection(path)
    except ConnectionRefusedError:
        await asyncio.to_thread(path.unlink)
        return
    writer.close()
    with contextlib.suppress(ConnectionError):
        await writer.wait_closed()
    raise AlreadyRunning(f"another instance is listening on {path}")


async def _run(on_plugin: OnPlugin, endpoint: Endpoint) -> None:
    # A plugin's connection failing never reaches the server, or the other plugins.
    try:
        await on_plugin(endpoint)
    except Closed:
        pass
    except Exception:
        log.exception("a plugin connection failed")
    finally:
        await endpoint.close()
