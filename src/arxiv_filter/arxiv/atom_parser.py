"""Atom / OpenSearch → :class:`FeedPage`（方案 §4.1 T1.4、§5.1）。

要点：

* 命名空间**必须显式**——不靠字符串匹配 XML；
* ``defusedxml`` 解析，禁止实体扩展（方案 §2「XML」行）；
* 上游的 Atom ``error`` entry 与正常论文 entry **分开识别**（方案 §4.8）；
* ``abs_url`` / ``pdf_url`` 只从 ``entry/link`` 的 ``rel``/``type``/``title`` 取，
  不用字符串替换盲造 URL；
* 只保留本地 taxonomy 里的 arXiv 分类，ACM/MSC 等进 ``external_categories``。
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Sequence

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

from arxiv_filter.arxiv.ids import InvalidArxivId, canonicalize_arxiv_id, versioned_id_for
from arxiv_filter.errors import ArxivFilterError, ErrorCode
from arxiv_filter.models.paper import Author, Paper
from arxiv_filter.models.query import FeedPage
from arxiv_filter.taxonomy import Taxonomy, load_taxonomy

#: 显式命名空间映射（方案 §4.1：命名空间必须显式）。
NS = {
    "a": "http://www.w3.org/2005/Atom",
    "o": "http://a9.com/-/spec/opensearch/1.1/",
    "arxiv": "http://arxiv.org/schemas/atom",
}

#: Atom error entry 的标题恰好是 ``Error``（方案 §5.7 的探针判定同款规则）。
_ERROR_TITLE = "Error"

#: 诊断摘要的最大长度（方案 §5.4：只保存 ≤ 1 KiB 脱敏摘要）。
MAX_DIAGNOSTIC_CHARS = 1024


def _text(element: object, path: str) -> str | None:
    """取子元素的文字；不存在或为空返回 ``None``。"""
    found = element.find(path, NS)  # type: ignore[attr-defined]
    if found is None or found.text is None:
        return None
    text = found.text.strip()
    return text or None


def _all_text(element: object, path: str) -> list[str]:
    values = []
    for found in element.findall(path, NS):  # type: ignore[attr-defined]
        if found.text and found.text.strip():
            values.append(found.text.strip())
    return values


def _parse_timestamp(value: str | None, *, label: str) -> datetime:
    """解析 Atom 时间戳；缺失或非法即报错，不猜一个「现在」。"""
    if not value:
        raise ArxivFilterError(
            ErrorCode.ARXIV_BAD_RESPONSE,
            details={"reason": "missing_timestamp", "field": label},
        )
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ArxivFilterError(
            ErrorCode.ARXIV_BAD_RESPONSE,
            details={"reason": "invalid_timestamp", "field": label, "value": value[:64]},
        ) from exc


def _pick_links(entry: object) -> tuple[str | None, str | None]:
    """从 ``entry/link`` 派生 ``(abs_url, pdf_url)``。

    只按 ``rel``/``type``/``title`` 判断，**不**用字符串替换拼 URL：

    * ``abs_url`` ← ``rel="alternate"``（``type="text/html"``）；
    * ``pdf_url`` ← ``type="application/pdf"`` 或 ``title="pdf"``。

    DOI 链接同样是 ``rel="related"``，靠 ``title="doi"`` 与 PDF 区分开。
    """
    abs_url: str | None = None
    pdf_url: str | None = None

    for link in entry.findall("a:link", NS):  # type: ignore[attr-defined]
        href = (link.get("href") or "").strip()
        if not href:
            continue
        rel = (link.get("rel") or "").strip().lower()
        link_type = (link.get("type") or "").strip().lower()
        title = (link.get("title") or "").strip().lower()

        if link_type == "application/pdf" or title == "pdf":
            pdf_url = pdf_url or href
        elif rel == "alternate" and abs_url is None:
            abs_url = href

    return abs_url, pdf_url


def _parse_authors(entry: object) -> list[Author]:
    """保持 Atom 里的作者顺序；``arxiv:affiliation`` 可选。"""
    authors: list[Author] = []
    for node in entry.findall("a:author", NS):  # type: ignore[attr-defined]
        name = _text(node, "a:name")
        if not name:
            continue
        authors.append(Author(name=name, affiliation=_text(node, "arxiv:affiliation")))
    return authors


def _known_category_ids(taxonomy: Taxonomy) -> frozenset[str]:
    """白名单：规范 ID **与**别名都算已知，避免把别名误判成外部分类。"""
    return frozenset(category.id for category in taxonomy.categories)


def parse_paper(
    entry: object,
    retrieved_at: datetime,
    *,
    known_categories: frozenset[str] | None = None,
    warnings: list[str] | None = None,
) -> Paper | None:
    """解析一个 ``<entry>``；无法使用时返回 ``None`` 并追加警告。

    ``known_categories`` 为 ``None`` 表示不区分 arXiv / 外部分类（测试用）。
    """
    warnings = warnings if warnings is not None else []

    raw_id = _text(entry, "a:id")
    if not raw_id:
        warnings.append("有一条记录没有 id，已跳过。")
        return None

    try:
        canonical_id, version = canonicalize_arxiv_id(raw_id)
    except InvalidArxivId:
        warnings.append(f"跳过无法识别的 ID：{raw_id[:64]}")
        return None

    abs_url, pdf_url = _pick_links(entry)
    if abs_url is None:
        # 回退：Atom 的 <id> 本身就是规范 URL，这不是「盲造」。
        if raw_id.startswith(("http://", "https://")):
            abs_url = raw_id
        else:
            warnings.append(f"{canonical_id} 没有可用的摘要页链接，已跳过。")
            return None

    title = _text(entry, "a:title") or ""
    if not title:
        warnings.append(f"{canonical_id} 没有标题。")

    primary_category = None
    primary_node = entry.find("arxiv:primary_category", NS)  # type: ignore[attr-defined]
    if primary_node is not None:
        primary_category = (primary_node.get("term") or "").strip() or None
    if primary_category is None:
        # 方案 §4.1：缺失则记录解析警告，**不凭第一个 category 猜测**
        warnings.append(f"{canonical_id} 缺少 primary_category。")

    terms = [term for term in _all_text(entry, "a:category") if term]
    # 某些 feed 用 <category term="..."/> 属性而不是文本
    for node in entry.findall("a:category", NS):  # type: ignore[attr-defined]
        term = (node.get("term") or "").strip()
        if term and term not in terms:
            terms.append(term)

    if known_categories is None:
        categories, external = terms, []
    else:
        categories = [term for term in terms if term in known_categories]
        external = [term for term in terms if term not in known_categories]

    return Paper(
        canonical_id=canonical_id,
        versioned_id=versioned_id_for(canonical_id, version),
        version=version,
        title=title,
        authors=_parse_authors(entry),
        summary_raw=_text(entry, "a:summary") or "",
        published_at=_parse_timestamp(_text(entry, "a:published"), label="published"),
        updated_at=_parse_timestamp(_text(entry, "a:updated"), label="updated"),
        primary_category=primary_category,
        categories=categories,
        external_categories=external,
        comment=_text(entry, "arxiv:comment"),
        journal_ref=_text(entry, "arxiv:journal_ref"),
        doi=_text(entry, "arxiv:doi"),
        abs_url=abs_url,
        pdf_url=pdf_url,
        retrieved_at=retrieved_at,
        source="arxiv_api",
    )


def _find_error_entry(root: object) -> tuple[str, str] | None:
    """识别 Atom ``error`` entry，返回 ``(标题, 安全纯文本说明)``。"""
    for entry in root.findall("a:entry", NS):  # type: ignore[attr-defined]
        title = _text(entry, "a:title")
        if title == _ERROR_TITLE:
            summary = _text(entry, "a:summary") or "arXiv 没有提供说明。"
            return title, summary[:MAX_DIAGNOSTIC_CHARS]
    return None


def _int_text(root: object, path: str) -> int | None:
    raw = _text(root, path)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def parse_atom(
    payload: bytes | str,
    retrieved_at: datetime,
    *,
    taxonomy: Taxonomy | None = None,
    known_categories: frozenset[str] | None = None,
) -> FeedPage:
    """把一次 arXiv Atom 响应解析成 :class:`FeedPage`。

    抛出 :class:`~arxiv_filter.errors.ArxivFilterError`：

    * ``ARXIV_ATOM_ERROR`` —— 上游返回了 error entry（**不是**论文）；
    * ``ARXIV_BAD_RESPONSE`` —— XML 损坏、非 Atom、或含被禁止的实体。
    """
    if isinstance(payload, str):
        payload = payload.encode("utf-8")

    try:
        root = SafeET.fromstring(payload)
    except DefusedXmlException as exc:
        raise ArxivFilterError(
            ErrorCode.ARXIV_BAD_RESPONSE,
            details={"reason": "forbidden_xml_construct", "detail": type(exc).__name__},
        ) from exc
    except Exception as exc:  # noqa: BLE001 - ElementTree 的异常层次很杂
        raise ArxivFilterError(
            ErrorCode.ARXIV_BAD_RESPONSE,
            details={
                "reason": "xml_parse_error",
                "detail": type(exc).__name__,
                "body_prefix": _safe_prefix(payload),
            },
        ) from exc

    if root.tag != f"{{{NS['a']}}}feed":
        raise ArxivFilterError(
            ErrorCode.ARXIV_BAD_RESPONSE,
            details={"reason": "not_an_atom_feed", "root_tag": str(root.tag)[:64]},
        )

    error_entry = _find_error_entry(root)
    if error_entry is not None:
        _, summary = error_entry
        raise ArxivFilterError(
            ErrorCode.ARXIV_ATOM_ERROR,
            details={"upstream_summary": summary},
            message_zh=f"arXiv 拒绝了这条查询：{summary}",
        )

    if known_categories is None:
        table = taxonomy if taxonomy is not None else load_taxonomy()
        known_categories = _known_category_ids(table)

    warnings: list[str] = []
    papers: list[Paper] = []
    for entry in root.findall("a:entry", NS):
        paper = parse_paper(
            entry, retrieved_at, known_categories=known_categories, warnings=warnings
        )
        if paper is not None:
            papers.append(paper)

    total = _int_text(root, f"{{{NS['o']}}}totalResults")
    if total is None:
        # 方案 §4.1：缺失时显示不定进度，而不是假装是 0
        total = _int_text(root, "o:totalResults")

    start_index = _int_text(root, f"{{{NS['o']}}}startIndex")
    if start_index is None:
        start_index = _int_text(root, "o:startIndex")

    items_per_page = _int_text(root, f"{{{NS['o']}}}itemsPerPage")
    if items_per_page is None:
        items_per_page = _int_text(root, "o:itemsPerPage")

    feed_updated_raw = _text(root, "a:updated")

    return FeedPage(
        total_results=total,
        start_index=start_index if start_index is not None else 0,
        items_per_page=items_per_page if items_per_page is not None else len(papers),
        papers=papers,
        feed_updated_at=(
            _parse_timestamp(feed_updated_raw, label="feed/updated")
            if feed_updated_raw
            else None
        ),
        warnings=warnings,
    )


def _safe_prefix(payload: bytes) -> str:
    """脱敏、限长、去控制字符的响应前缀，用于诊断（方案 §5.4）。"""
    text = payload[:MAX_DIAGNOSTIC_CHARS].decode("utf-8", errors="replace")
    cleaned = "".join(char if char.isprintable() or char.isspace() else " " for char in text)
    return " ".join(cleaned.split())[:200]


def detect_atom_error(payload: bytes | str) -> str | None:
    """如果 ``payload`` 是一条 Atom ``error`` entry，返回它的说明文本。

    用于「非 200 但 Content-Type 是 XML」的场景：**必须**先确认它到底是不是
    上游的参数错误说明，才能决定重试与否——500 也可能是 Atom error entry，
    而 503 更可能是 HTML 错误页。

    解析失败或不是 error entry 时返回 ``None``（调用方按状态码分类）。
    """
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    try:
        root = SafeET.fromstring(payload)
    except Exception:  # noqa: BLE001 - 解析不了就交给调用方按状态码处理
        return None
    if root.tag != f"{{{NS['a']}}}feed":
        return None
    found = _find_error_entry(root)
    return found[1] if found is not None else None


def known_categories_from(taxonomy: Taxonomy) -> frozenset[str]:
    """便捷函数：从 taxonomy 取分类白名单。"""
    return _known_category_ids(taxonomy)


__all__ = [
    "MAX_DIAGNOSTIC_CHARS",
    "NS",
    "detect_atom_error",
    "known_categories_from",
    "parse_atom",
    "parse_paper",
]
