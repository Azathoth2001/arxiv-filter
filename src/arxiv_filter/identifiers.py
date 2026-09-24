"""标识符生成（UUIDv7 与请求 ID）。

``docs/data-schema.md`` §3 规定 ``query_history.id`` 是 UUIDv7 文本。
Python 3.12 的标准库还没有 ``uuid7``，这里按 RFC 9562 的最小实现补上——
选 UUIDv7 而不是 UUID4 是因为它**按时间单调递增**，历史列表可以拿主键当有序游标，
不必额外依赖 ``started_at`` 索引。
"""

from __future__ import annotations

import os
import time
import uuid

__all__ = ["REQUEST_ID_HEX_BYTES", "request_id", "uuid7"]

#: 请求 ID 的随机字节数（128-bit 令牌同款强度，见方案 §1.3）。
REQUEST_ID_HEX_BYTES = 16


def uuid7(timestamp_ms: int | None = None) -> uuid.UUID:
    """生成一个 UUIDv7。

    布局（RFC 9562 §5.7）：48 位毫秒时间戳 | 4 位版本(=7) | 12 位随机 |
    2 位变体(=0b10) | 62 位随机。
    """
    millis = int(time.time() * 1000) if timestamp_ms is None else int(timestamp_ms)
    if not 0 <= millis < (1 << 48):
        raise ValueError("毫秒时间戳超出 48 位范围")

    random_bits = int.from_bytes(os.urandom(10), "big")
    rand_a = (random_bits >> 68) & 0xFFF
    rand_b = random_bits & ((1 << 62) - 1)

    value = (millis & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= rand_a << 64
    value |= 0b10 << 62
    value |= rand_b
    return uuid.UUID(int=value)


def uuid7_str(timestamp_ms: int | None = None) -> str:
    return str(uuid7(timestamp_ms))


def uuid7_timestamp(value: uuid.UUID) -> int:
    """从 UUIDv7 里取回毫秒时间戳（用于按 ID 排序时做诊断）。"""
    return value.int >> 80


def request_id() -> str:
    """生成一个请求 ID，用于错误信封里的 ``request_id``。

    与 :func:`uuid7` 同源，保证日志与 UI 里能看到时间顺序。
    """
    return uuid7_str()


def random_token(byte_length: int = REQUEST_ID_HEX_BYTES) -> str:
    """生成启动令牌用的随机十六进制串（方案 §1.3：128-bit）。"""
    return os.urandom(byte_length).hex()
