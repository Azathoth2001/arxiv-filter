"""Cross-platform localhost binding and single-instance runtime metadata."""

from __future__ import annotations

import json
import os
import re
import socket
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_CHALLENGE_RE = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True, slots=True)
class RuntimeRecord:
    pid: int
    port: int
    challenge: str


class SingleInstanceLock:
    """Hold an OS-managed byte/file lock for the life of the process."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                if self.path.stat().st_size == 0:
                    handle.write(b"\0")
                    handle.flush()
                    handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            handle.close()
            return False
        self._file = handle
        return True

    def release(self) -> None:
        handle = self._file
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._file = None

    def __enter__(self) -> SingleInstanceLock:
        if not self.acquire():
            raise RuntimeError("application instance lock is already held")
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.release()


def bind_loopback(port: int) -> socket.socket:
    """Bind before starting uvicorn so port selection has no check/use race."""
    if not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if os.name == "nt" and exclusive is not None:
            listener.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen(socket.SOMAXCONN)
        listener.setblocking(False)
        return listener
    except Exception:
        listener.close()
        raise


def read_runtime(path: Path) -> RuntimeRecord | None:
    try:
        raw = path.read_text(encoding="utf-8")
        if len(raw) > 4096:
            return None
        value: Any = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    pid = value.get("pid")
    port = value.get("port")
    challenge = value.get("challenge")
    if (
        type(pid) is not int
        or pid <= 0
        or type(port) is not int
        or not 1 <= port <= 65535
        or not isinstance(challenge, str)
        or not _CHALLENGE_RE.fullmatch(challenge)
    ):
        return None
    return RuntimeRecord(pid=pid, port=port, challenge=challenge)


def write_runtime(path: Path, record: RuntimeRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"pid": record.pid, "port": record.port, "challenge": record.challenge},
        separators=(",", ":"),
    )
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".runtime-",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temp_path = Path(temporary.name)
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def remove_runtime(path: Path, *, expected_challenge: str) -> bool:
    """Remove only this process's record; never erase a newer instance's file."""
    record = read_runtime(path)
    if record is None or record.challenge != expected_challenge:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def process_is_alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


__all__ = [
    "RuntimeRecord",
    "SingleInstanceLock",
    "bind_loopback",
    "process_is_alive",
    "read_runtime",
    "remove_runtime",
    "write_runtime",
]
