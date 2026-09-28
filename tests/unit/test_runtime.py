"""T4.3 loopback binding, process lock, and runtime metadata tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from arxiv_filter.runtime import (
    RuntimeRecord,
    SingleInstanceLock,
    bind_loopback,
    process_is_alive,
    read_runtime,
    remove_runtime,
    write_runtime,
)


def test_single_instance_lock_is_exclusive_and_releasable(tmp_path: Path) -> None:
    path = tmp_path / "runtime.lock"
    first = SingleInstanceLock(path)
    second = SingleInstanceLock(path)

    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


def test_runtime_record_atomic_round_trip_and_owner_guard(tmp_path: Path) -> None:
    path = tmp_path / "runtime.json"
    record = RuntimeRecord(pid=os.getpid(), port=43210, challenge="a" * 32)

    write_runtime(path, record)
    assert read_runtime(path) == record
    assert not remove_runtime(path, expected_challenge="b" * 32)
    assert read_runtime(path) == record
    assert remove_runtime(path, expected_challenge=record.challenge)
    assert read_runtime(path) is None


@pytest.mark.parametrize(
    "payload",
    [
        "{broken",
        json.dumps({"pid": True, "port": 1234, "challenge": "a" * 32}),
        json.dumps({"pid": 1, "port": 0, "challenge": "a" * 32}),
        json.dumps({"pid": 1, "port": 1234, "challenge": "short"}),
    ],
)
def test_runtime_reader_rejects_malformed_or_unsafe_records(
    tmp_path: Path, payload: str
) -> None:
    path = tmp_path / "runtime.json"
    path.write_text(payload, encoding="utf-8")
    assert read_runtime(path) is None


def test_bind_loopback_uses_ephemeral_port_and_rejects_collision() -> None:
    listener = bind_loopback(0)
    try:
        host, port = listener.getsockname()
        assert host == "127.0.0.1"
        assert port > 0
        with pytest.raises(OSError):
            bind_loopback(port)
    finally:
        listener.close()


def test_bind_loopback_rejects_invalid_port() -> None:
    with pytest.raises(ValueError):
        bind_loopback(65536)


def test_process_liveness_handles_current_and_missing_pid() -> None:
    assert process_is_alive(os.getpid())
    assert not process_is_alive(2**30)
