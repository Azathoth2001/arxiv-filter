"""诊断信息的脱敏与限长（方案 §4.2「日志」行、§4.8、§5.4）。

规则（写在代码里，避免各处实现漂移）：

* 代理凭据 ``http://user:pass@host`` 里的 user:pass 一律换成 ``***``；
* ``Authorization`` / ``api_key`` / ``token`` / ``password`` 等键的值一律遮蔽；
* 只保留**最多 1 KiB** 的正文前缀，并去掉控制字符；
* 磁盘日志默认只记 query hash，不记完整关键词。

这些函数被 ``arxiv/client.py``、``services/log_service.py`` 与诊断导出共用。
"""

from __future__ import annotations

import re
import unicodedata

#: 诊断摘要的字节上限（方案 §5.4）。
MAX_DIAGNOSTIC_BYTES = 1024

#: ``scheme://user:pass@host`` → ``scheme://***@host``
_CREDENTIAL_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s@]+)@")

#: 敏感键值对。整段替换，避免只遮蔽键名而把值留在正文里。
_SECRET_RE = re.compile(
    r"(?i)\b(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|token"
    r"|password|passwd|pwd|secret|client[_-]?secret)"
    r"\b\s*[:=]\s*(?:bearer\s+|basic\s+)?\S+"
)

#: 控制字符（含 DEL）；制表/换行/回车保留为空白，稍后由 split 折叠。
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def redact(text: str) -> str:
    """遮蔽 URL 凭据与常见密钥字段。"""
    text = _CREDENTIAL_RE.sub(r"\1***@", text)
    return _SECRET_RE.sub("***", text)


def redact_url(url: str) -> str:
    """只遮蔽 URL 里的凭据，便于安全地写进日志或展示。"""
    return _CREDENTIAL_RE.sub(r"\1***@", url)


def diagnostic_prefix(payload: bytes | str, *, limit: int = MAX_DIAGNOSTIC_BYTES) -> str:
    """把响应正文压成 ≤ ``limit`` 字节的脱敏单行摘要。

    控制字符会被替换掉，连续空白折叠成单个空格——目的是让摘要能被安全地
    写进日志文件与 UI，而不是还原正文。
    """
    if isinstance(payload, str):
        payload = payload.encode("utf-8", errors="replace")
    text = payload[:limit].decode("utf-8", errors="replace")
    text = _CONTROL_RE.sub(" ", text)
    text = unicodedata.normalize("NFKC", text)
    text = redact(text)
    return " ".join(text.split())


def summarise_proxy(proxy: str | None) -> str | None:
    """把代理 URL 渲染成可安全记录的形式；``None`` 原样返回。"""
    if proxy is None:
        return None
    return redact_url(proxy)


__all__ = [
    "MAX_DIAGNOSTIC_BYTES",
    "diagnostic_prefix",
    "redact",
    "redact_url",
    "summarise_proxy",
]
