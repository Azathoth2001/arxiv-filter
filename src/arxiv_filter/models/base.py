"""领域模型的公共基类与时间类型。

方案 §4.1 与 ``docs/data-schema.md`` §0 的硬规则：

* 内部时间一律是**带时区的 UTC** ``datetime``；
* 数据库与 JSON 一律写 RFC 3339 的 ``Z`` 形式；
* 不接受 naive datetime——本地时区会在跨日筛选上产生静默错误，必须直接报错。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, PlainSerializer

#: RFC 3339 UTC，秒精度，固定以 ``Z`` 结尾。
RFC3339_Z_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def format_utc(value: datetime) -> str:
    """把带时区的 datetime 渲染成 RFC 3339 ``Z`` 形式。"""
    return value.astimezone(timezone.utc).strftime(RFC3339_Z_FORMAT)


def parse_utc(value: str) -> datetime:
    """解析 RFC 3339 字符串；``Z`` 与 ``+00:00`` 都接受。

    naive 字符串会被当作 **UTC**（数据库里的历史值都按 UTC 写入），
    但 :func:`ensure_aware_utc` 对传入的 ``datetime`` 对象仍然拒绝 naive，
    避免把本机时区悄悄当成 UTC。
    """
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def ensure_aware_utc(value: Any) -> Any:
    """校验器：拒绝 naive datetime，其余一律换算到 UTC。"""
    if isinstance(value, str):
        value = parse_utc(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError(
                "时间必须带时区（内部一律使用 UTC）；naive datetime 会被拒绝，"
                "以免把本机时区误当成 UTC。"
            )
        return value.astimezone(timezone.utc)
    return value


#: 带时区的 UTC 时间；JSON 序列化为 RFC 3339 ``Z``。
UtcDatetime = Annotated[
    datetime,
    BeforeValidator(ensure_aware_utc),
    PlainSerializer(format_utc, return_type=str, when_used="json"),
]


class DomainModel(BaseModel):
    """所有领域模型的基类。

    ``extra="forbid"``：字段拼写错误必须立刻报错，而不是被静默丢掉——
    领域模型同时用于 SQLite 的 ``metadata_json`` 往返，多余字段意味着数据不一致。
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


__all__ = [
    "RFC3339_Z_FORMAT",
    "DomainModel",
    "UtcDatetime",
    "ensure_aware_utc",
    "format_utc",
    "parse_utc",
]
