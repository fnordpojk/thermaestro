"""Files and directories only the service user may read: plugin sockets, connection
tokens, the secrets file."""

import contextlib
import os
import secrets
import stat
from pathlib import Path


class UnsafePath(Exception):
    """A file or directory that others could read or change, or that isn't what it
    should be."""


def private_directory(directory: Path) -> None:
    """Make `directory` 0700 if it doesn't exist. If it does, it must be this user's and
    closed to others: 0700, or 0750 for a group of plugins."""
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


def check_private_file(path: Path) -> None:
    """`path` must be a regular file of this user's that nobody else can read or write."""
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode):
        raise UnsafePath(f"{path} isn't a regular file")
    if st.st_uid != os.getuid():
        raise UnsafePath(f"{path} belongs to uid {st.st_uid}, not this user")
    if st.st_mode & 0o077:
        raise UnsafePath(f"{path} is open to others ({stat.filemode(st.st_mode)}); make it 0600")


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
