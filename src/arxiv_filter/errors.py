"""稳定错误码与中英消息。

对应方案 §4.6「统一错误」与 §4.8「异常与用户可见行为」。规则：

* 每个面向用户的失败都必须带一个 :class:`ErrorCode`；
* ``code`` 一经发布不得改义，只允许新增；
* ``message_zh`` 面向非技术用户，必须给出「发生了什么 + 可以做什么」；
* 任何消息都不得包含代理凭据、完整请求 URL 的用户名/密码或响应正文原文。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping


class ErrorCode(StrEnum):
    """稳定的机器可读错误码。"""

    # —— 上游网络 ——
    ARXIV_TIMEOUT = "ARXIV_TIMEOUT"
    ARXIV_DNS_FAILURE = "ARXIV_DNS_FAILURE"
    ARXIV_CONNECT_FAILURE = "ARXIV_CONNECT_FAILURE"
    ARXIV_RATE_LIMITED = "ARXIV_RATE_LIMITED"
    ARXIV_NOT_ACCEPTABLE = "ARXIV_NOT_ACCEPTABLE"
    ARXIV_UNAVAILABLE = "ARXIV_UNAVAILABLE"
    ARXIV_BAD_RESPONSE = "ARXIV_BAD_RESPONSE"
    ARXIV_QUERY_CONTRACT = "ARXIV_QUERY_CONTRACT"
    ARXIV_ATOM_ERROR = "ARXIV_ATOM_ERROR"
    PROXY_AUTH_FAILED = "PROXY_AUTH_FAILED"
    TLS_VERIFY_FAILED = "TLS_VERIFY_FAILED"

    # —— 本地输入 ——
    INVALID_SEARCH_REQUEST = "INVALID_SEARCH_REQUEST"
    INVALID_KEYWORD_SYNTAX = "INVALID_KEYWORD_SYNTAX"
    UNKNOWN_CATEGORY = "UNKNOWN_CATEGORY"
    RESULT_CAP_INVALID = "RESULT_CAP_INVALID"

    # —— taxonomy / 配置 ——
    TAXONOMY_CORRUPT = "TAXONOMY_CORRUPT"
    CONFIG_CORRUPT = "CONFIG_CORRUPT"

    # —— 任务 ——
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JOB_NOT_FINISHED = "JOB_NOT_FINISHED"
    JOB_ALREADY_FINISHED = "JOB_ALREADY_FINISHED"
    JOB_LIMIT_REACHED = "JOB_LIMIT_REACHED"
    PAGINATION_STALLED = "PAGINATION_STALLED"
    SEARCH_TIMEOUT = "SEARCH_TIMEOUT"

    # —— 存储 ——
    CACHE_UNAVAILABLE = "CACHE_UNAVAILABLE"
    DATABASE_MIGRATION_FAILED = "DATABASE_MIGRATION_FAILED"
    DATABASE_TOO_NEW = "DATABASE_TOO_NEW"

    # —— 导出 ——
    EXPORT_FAILED = "EXPORT_FAILED"
    EXPORT_TARGET_LOCKED = "EXPORT_TARGET_LOCKED"
    EXPORT_INVALID_SUBSET = "EXPORT_INVALID_SUBSET"

    # —— 系统 / 启动 ——
    INVALID_TOKEN = "INVALID_TOKEN"
    ORIGIN_REJECTED = "ORIGIN_REJECTED"
    SHUTDOWN_BLOCKED = "SHUTDOWN_BLOCKED"
    LAUNCH_FAILED = "LAUNCH_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclass(slots=True)
class ErrorSpec:
    """一个错误码的展示属性。"""

    message_zh: str
    message_en: str
    http_status: int
    retryable: bool = False


#: 错误码 → 展示属性。文案严格取自方案 §4.8 表格。
ERROR_SPECS: Mapping[ErrorCode, ErrorSpec] = {
    ErrorCode.ARXIV_TIMEOUT: ErrorSpec(
        "连接 arXiv 超时，已完成重试。可重试或使用上次缓存。",
        "The arXiv request timed out after retries.",
        504,
        retryable=True,
    ),
    ErrorCode.ARXIV_DNS_FAILURE: ErrorSpec(
        "无法解析或连接 arXiv，请检查网络、代理或 DNS。",
        "Could not resolve or connect to arXiv. Check your network, proxy, or DNS.",
        502,
        retryable=True,
    ),
    ErrorCode.ARXIV_CONNECT_FAILURE: ErrorSpec(
        "无法连接 arXiv，请检查网络、代理或防火墙。",
        "Could not connect to arXiv. Check your network, proxy, or firewall.",
        502,
        retryable=True,
    ),
    ErrorCode.ARXIV_RATE_LIMITED: ErrorSpec(
        "请求过于频繁，程序已暂停并按服务端要求等待。",
        "arXiv rate-limited this client; the app waited as requested.",
        429,
        retryable=True,
    ),
    # 2026-09-24 实测：arXiv 以 HTTP 406 + 空响应体表达限流，而不是 429。
    # 见 docs/api-verification.md §3。用户可见文案与 429 一致——对非技术用户
    # 而言两者是同一件事，且都不该看到裸的状态码。
    ErrorCode.ARXIV_NOT_ACCEPTABLE: ErrorSpec(
        "请求过于频繁，程序已暂停并按服务端要求等待。",
        "arXiv refused the request (HTTP 406). Treated as rate limiting; the app backed off.",
        503,
        retryable=True,
    ),
    ErrorCode.ARXIV_UNAVAILABLE: ErrorSpec(
        "arXiv 暂时不可用，程序正在退避重试。",
        "arXiv is temporarily unavailable; the app retried with backoff.",
        503,
        retryable=True,
    ),
    ErrorCode.ARXIV_BAD_RESPONSE: ErrorSpec(
        "服务返回了无法识别的数据；没有覆盖已有缓存。",
        "The service returned unparsable data; existing cache was not overwritten.",
        502,
        retryable=False,
    ),
    ErrorCode.ARXIV_QUERY_CONTRACT: ErrorSpec(
        "arXiv 拒绝了这条查询表达式，请调整关键词后再试。",
        "arXiv rejected this query expression. Adjust the keywords and retry.",
        422,
        retryable=False,
    ),
    ErrorCode.ARXIV_ATOM_ERROR: ErrorSpec(
        "arXiv 返回了一条错误说明，请按提示调整查询条件。",
        "arXiv returned an error entry describing the problem with the query.",
        422,
        retryable=False,
    ),
    ErrorCode.PROXY_AUTH_FAILED: ErrorSpec(
        "代理拒绝连接，请检查地址、账号或系统代理。",
        "The proxy refused the connection. Check its address, credentials, or the system proxy.",
        502,
        retryable=False,
    ),
    ErrorCode.TLS_VERIFY_FAILED: ErrorSpec(
        "证书验证失败。请联系网络管理员配置受信任 CA。",
        "TLS certificate verification failed. Ask your network administrator for a trusted CA.",
        502,
        retryable=False,
    ),
    ErrorCode.INVALID_SEARCH_REQUEST: ErrorSpec(
        "查询条件不合法，请检查日期、分类和结果上限。",
        "The search request is invalid. Check the dates, category, and result cap.",
        422,
        retryable=False,
    ),
    ErrorCode.INVALID_KEYWORD_SYNTAX: ErrorSpec(
        "关键词表达式有语法错误，请按提示的位置修改。",
        "The keyword expression has a syntax error; see the reported position.",
        422,
        retryable=False,
    ),
    ErrorCode.UNKNOWN_CATEGORY: ErrorSpec(
        "该分类不在内置分类表中，请更新应用或改选其它分类。",
        "That category is not in the bundled taxonomy. Update the app or pick another one.",
        422,
        retryable=False,
    ),
    ErrorCode.RESULT_CAP_INVALID: ErrorSpec(
        "结果上限必须在 1 到 2000 之间。",
        "The result cap must be between 1 and 2000.",
        422,
        retryable=False,
    ),
    ErrorCode.TAXONOMY_CORRUPT: ErrorSpec(
        "内置分类表校验失败，请重新安装完整发布包。",
        "The bundled category taxonomy failed validation. Reinstall the full release package.",
        500,
        retryable=False,
    ),
    ErrorCode.CONFIG_CORRUPT: ErrorSpec(
        "配置文件无法解析，已回退到默认设置。",
        "The configuration file could not be parsed; defaults were restored.",
        500,
        retryable=False,
    ),
    ErrorCode.JOB_NOT_FOUND: ErrorSpec(
        "找不到这个任务，可能程序已经重启。",
        "That job was not found; the app may have restarted.",
        404,
        retryable=False,
    ),
    ErrorCode.JOB_NOT_FINISHED: ErrorSpec(
        "任务还没有完成，请稍候再试。",
        "The job has not finished yet; try again shortly.",
        409,
        retryable=True,
    ),
    ErrorCode.JOB_ALREADY_FINISHED: ErrorSpec(
        "任务已经结束，无法再取消。",
        "The job already finished and can no longer be cancelled.",
        409,
        retryable=False,
    ),
    ErrorCode.JOB_LIMIT_REACHED: ErrorSpec(
        "同时只能运行一个抓取任务，请等待当前任务结束。",
        "Only one upstream job may run at a time; wait for the current one to finish.",
        409,
        retryable=True,
    ),
    ErrorCode.PAGINATION_STALLED: ErrorSpec(
        "服务返回的分页数据没有前进，已停止以免重复请求。",
        "Pagination stopped advancing; the app halted to avoid repeated requests.",
        502,
        retryable=False,
    ),
    ErrorCode.SEARCH_TIMEOUT: ErrorSpec(
        "本次抓取超过 10 分钟上限，已停止。可缩小日期范围或分类。",
        "The search exceeded its 10-minute budget and stopped. Narrow the dates or category.",
        504,
        retryable=False,
    ),
    ErrorCode.CACHE_UNAVAILABLE: ErrorSpec(
        "本地缓存暂时不可用，已跳过缓存直接查询。",
        "The local cache is unavailable; the app queried upstream directly.",
        500,
        retryable=True,
    ),
    ErrorCode.DATABASE_MIGRATION_FAILED: ErrorSpec(
        "数据库升级失败，已保留原库并以只读模式启动。备份文件见日志。",
        "Database migration failed. The original file was kept and the app started read-only.",
        500,
        retryable=False,
    ),
    ErrorCode.DATABASE_TOO_NEW: ErrorSpec(
        "数据库版本比当前程序新，已拒绝写入。请使用匹配版本或恢复备份。",
        "The database schema is newer than this build; writes are refused. Use a matching build or restore a backup.",
        500,
        retryable=False,
    ),
    ErrorCode.EXPORT_FAILED: ErrorSpec(
        "无法写入目标文件；请检查文件是否被 Excel 占用。",
        "Could not write the target file; check whether Excel has it open.",
        500,
        retryable=False,
    ),
    ErrorCode.EXPORT_TARGET_LOCKED: ErrorSpec(
        "目标文件正被其它程序占用，请关闭后重试。",
        "The target file is locked by another program; close it and retry.",
        409,
        retryable=True,
    ),
    ErrorCode.EXPORT_INVALID_SUBSET: ErrorSpec(
        "导出请求里包含了不属于本次结果的论文。",
        "The export request contained papers outside the current result set.",
        422,
        retryable=False,
    ),
    ErrorCode.INVALID_TOKEN: ErrorSpec(
        "会话令牌无效或已过期，请重新打开程序。",
        "The session token is missing or invalid; reopen the app.",
        403,
        retryable=False,
    ),
    ErrorCode.ORIGIN_REJECTED: ErrorSpec(
        "请求来源不被信任，已拒绝。",
        "The request origin is not trusted and was rejected.",
        403,
        retryable=False,
    ),
    ErrorCode.SHUTDOWN_BLOCKED: ErrorSpec(
        "仍有任务在运行，请先取消或等待完成再退出。",
        "A job is still running; cancel it or wait before exiting.",
        409,
        retryable=True,
    ),
    ErrorCode.LAUNCH_FAILED: ErrorSpec(
        "程序启动失败，请查看日志文件。",
        "The app failed to start. See the log file for details.",
        500,
        retryable=False,
    ),
    ErrorCode.INTERNAL_ERROR: ErrorSpec(
        "程序内部错误，详情已写入日志。",
        "An internal error occurred; details were written to the log.",
        500,
        retryable=False,
    ),
}


class ArxivFilterError(Exception):
    """所有面向用户的失败的基类。

    参数
    ----
    code:
        稳定错误码，决定 HTTP 状态、中文文案与是否可重试。
    details:
        附加诊断信息，必须已脱敏（不得含凭据或响应正文原文）。
    message_zh:
        可选覆盖文案；默认取 :data:`ERROR_SPECS`。
    """

    def __init__(
        self,
        code: ErrorCode,
        *,
        details: Mapping[str, Any] | None = None,
        message_zh: str | None = None,
    ) -> None:
        self.code = code
        self.spec = ERROR_SPECS[code]
        self.details: dict[str, Any] = dict(details or {})
        self.message_zh = message_zh or self.spec.message_zh
        super().__init__(f"{code}: {self.message_zh}")

    @property
    def http_status(self) -> int:
        return self.spec.http_status

    @property
    def retryable(self) -> bool:
        return self.spec.retryable

    def to_envelope(self, request_id: str) -> dict[str, Any]:
        """渲染为方案 §4.6 定义的统一错误信封。"""
        return {
            "error": {
                "code": str(self.code),
                "message_zh": self.message_zh,
                "message_en": self.spec.message_en,
                "retryable": self.retryable,
                "request_id": request_id,
                "details": self.details,
            }
        }


@dataclass(slots=True)
class Diagnostic:
    """脱敏后的上游诊断摘要（方案 §5.4：最多 1 KiB）。"""

    status: int | None = None
    content_type: str = ""
    body_prefix: str = ""
    retry_after: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "ErrorCode",
    "ErrorSpec",
    "ERROR_SPECS",
    "ArxivFilterError",
    "Diagnostic",
]
