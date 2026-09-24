"""arXiv ID 归一化（方案 §4.4 T1.5）。

``canonicalize_arxiv_id`` 的步骤严格按方案 §4.4：

1. 去掉 ``http(s)://arxiv.org/abs/``、``/pdf/`` 前缀、末尾 ``.pdf``、query 和 fragment；
2. **只从末尾**识别 ``v([1-9][0-9]*)``；剥离后得到 ``canonical_id``；
3. 接受现代 ID（四位年月 + 4/5 位流水号）和旧式 ``archive/YYMMNNN``；
   **旧式斜杠必须保留**；
4. 同一 canonical ID 的版本取舍由 :mod:`arxiv_filter.arxiv.dedupe` 负责。
"""

from __future__ import annotations

import re

#: 现代 ID：``YYMM.NNNN`` 或 ``YYMM.NNNNN``。
_MODERN_RE = re.compile(r"^(?P<year>\d{2})(?P<month>\d{2})\.(?P<sequence>\d{4,5})$")

#: 旧式 ID：``archive/YYMMNNN``，例如 ``hep-th/9901001``、``math.GT/0309136``。
_LEGACY_RE = re.compile(r"^(?P<archive>[a-z][a-z0-9-]*(?:\.[A-Za-z]{2})?)/(?P<sequence>\d{7})$")

#: 末尾版本号。**只**匹配末尾，且不接受 ``v0``。
_VERSION_RE = re.compile(r"v(?P<version>[1-9][0-9]*)$", re.IGNORECASE)

#: 需要剥掉的外层包装。
_PREFIX_RE = re.compile(
    r"^(?:arxiv:|https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/|/?(?:abs|pdf)/)",
    re.IGNORECASE,
)
_SUFFIX_NOISE_RE = re.compile(r"\.pdf$", re.IGNORECASE)


class InvalidArxivId(ValueError):
    """无法识别的 arXiv ID。"""


def strip_wrapper(value: str) -> str:
    """去掉 URL / ``arXiv:`` 前缀、``.pdf`` 后缀、query 与 fragment。

    可能被反复调用直到稳定，因为形如 ``https://arxiv.org/pdf/2501.01234v2.pdf``
    同时带有前缀与后缀。
    """
    text = value.strip()

    # query 与 fragment 先切掉，避免它们里面的斜杠干扰
    for separator in ("#", "?"):
        index = text.find(separator)
        if index != -1:
            text = text[:index]

    previous = None
    while previous != text:
        previous = text
        text = _PREFIX_RE.sub("", text)
        text = _SUFFIX_NOISE_RE.sub("", text)
        text = text.strip().strip("/")
    return text


def canonicalize_arxiv_id(value: str) -> tuple[str, int | None]:
    """把任意形式的 arXiv ID 归一化成 ``(canonical_id, version)``。

    ``canonical_id`` **不含**末尾版本号；旧式 ID 的斜杠保留。
    ``version`` 在输入没有版本号时为 ``None``。

    抛出 :class:`InvalidArxivId`。**不做猜测**：形状不合法就报错，
    而不是返回一个看似可用的字符串。
    """
    text = strip_wrapper(value)
    if not text:
        raise InvalidArxivId("空的 arXiv ID")

    version: int | None = None
    match = _VERSION_RE.search(text)
    if match is not None:
        version = int(match.group("version"))
        text = text[: match.start()]

    if not is_valid_canonical_id(text):
        raise InvalidArxivId(f"无法识别的 arXiv ID：{value!r}")

    return text, version


def is_valid_canonical_id(value: str) -> bool:
    """``value`` 是否是**不含版本号**的合法 canonical ID。"""
    modern = _MODERN_RE.match(value)
    if modern is not None:
        month = int(modern.group("month"))
        return 1 <= month <= 12

    legacy = _LEGACY_RE.match(value)
    if legacy is not None:
        month = int(legacy.group("sequence")[2:4])
        return 1 <= month <= 12

    return False


def versioned_id_for(canonical_id: str, version: int | None) -> str:
    """把 canonical ID 与版本号拼回带版本的 ID。"""
    if version is None:
        return canonical_id
    return f"{canonical_id}v{version}"


def split_versioned_id(value: str) -> tuple[str, int | None]:
    """``canonicalize_arxiv_id`` 的别名，强调输入可以是带版本的 ID。"""
    return canonicalize_arxiv_id(value)


__all__ = [
    "InvalidArxivId",
    "canonicalize_arxiv_id",
    "is_valid_canonical_id",
    "split_versioned_id",
    "strip_wrapper",
    "versioned_id_for",
]
