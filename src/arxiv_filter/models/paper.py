"""论文领域模型（方案 §4.1）。

字段来源映射见方案 §4.1 的表格；这里只固化「形状与不变式」，
XML 解析在 :mod:`arxiv_filter.arxiv.atom_parser`。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from arxiv_filter.models.base import DomainModel, UtcDatetime

#: 论文元数据的来源。MVP 只会产生 ``arxiv_api``；第三方源必须显式标注。
PaperSource = Literal[
    "arxiv_api",
    "arxiv_oai",
    "semantic_scholar",
    "openalex",
    "crossref",
]

#: 每个 ``source`` 的中文展示名，用于 UI 来源徽标（方案 §5.8）。
SOURCE_LABELS_ZH: dict[str, str] = {
    "arxiv_api": "arXiv 官方 API",
    "arxiv_oai": "arXiv OAI-PMH",
    "semantic_scholar": "Semantic Scholar（实验性）",
    "openalex": "OpenAlex（实验性）",
    "crossref": "Crossref（仅补充 DOI）",
}


class Author(DomainModel):
    """一位作者；``affiliation`` 来自 ``arxiv:affiliation``，缺失即 ``None``。"""

    name: str = Field(min_length=1)
    affiliation: str | None = None

    @field_validator("name", "affiliation")
    @classmethod
    def _collapse_whitespace(cls, value: str | None) -> str | None:
        """折叠连续空白但保留 Unicode（方案 §4.1 ``title`` 行同样的规则）。"""
        if value is None:
            return None
        return " ".join(value.split())


class Paper(DomainModel):
    """一篇论文的规范元数据。

    不变式：

    * ``canonical_id`` **不含**末尾版本号；旧式 ID 保留斜杠（如 ``hep-th/9901001``）；
    * ``versioned_id`` 在返回值带 ``vN`` 时保留，否则等于 ``canonical_id``；
    * ``summary_raw`` 是原始 LaTeX 文本，**绝不以 HTML 信任**；
    * ``categories`` 只放本地 taxonomy 中的 arXiv 分类 ID；
      ACM/MSC 等非 arXiv 分类进 ``external_categories``。
    """

    schema_version: Literal[1] = 1

    canonical_id: str = Field(min_length=1)
    versioned_id: str = Field(min_length=1)
    version: int | None = Field(default=None, ge=1)

    title: str
    authors: list[Author] = Field(default_factory=list)
    summary_raw: str = ""

    published_at: UtcDatetime
    updated_at: UtcDatetime

    primary_category: str | None = None
    categories: list[str] = Field(default_factory=list)
    external_categories: list[str] = Field(default_factory=list)

    comment: str | None = None
    journal_ref: str | None = None
    doi: str | None = None

    abs_url: str = Field(min_length=1)
    pdf_url: str | None = None

    retrieved_at: UtcDatetime
    source: PaperSource = "arxiv_api"

    @field_validator("title")
    @classmethod
    def _collapse_title_whitespace(cls, value: str) -> str:
        """标题折叠换行与连续空白，但保留 Unicode（方案 §4.1 字段映射表）。"""
        return " ".join(value.split())

    @field_validator("summary_raw")
    @classmethod
    def _strip_summary(cls, value: str) -> str:
        """摘要只去外围空白；**内部 LaTeX 与换行原样保留**（方案 §4.1）。"""
        return value.strip()

    @field_validator("categories", "external_categories")
    @classmethod
    def _stable_unique(cls, value: list[str]) -> list[str]:
        """稳定去重：保留首次出现顺序，去掉空串。"""
        seen: set[str] = set()
        result: list[str] = []
        for item in value:
            text = item.strip()
            if text and text not in seen:
                seen.add(text)
                result.append(text)
        return result

    @field_validator("abs_url", "pdf_url")
    @classmethod
    def _require_http(cls, value: str | None) -> str | None:
        """URL 必须来自 Atom ``link``，不做字符串拼造（方案 §4.1）。

        只允许 http/https，避免把 ``javascript:`` 之类的值带进前端。
        """
        if value is None:
            return None
        text = value.strip()
        if not text:
            return None
        if not text.startswith(("http://", "https://")):
            raise ValueError(f"URL 必须是 http/https，收到的是 {text[:32]!r}")
        return text

    @property
    def source_label_zh(self) -> str:
        return SOURCE_LABELS_ZH.get(self.source, self.source)

    @property
    def display_title(self) -> str:
        """标题缺失时的兜底展示，不让列表出现空白标题。"""
        return self.title or f"（无标题）{self.canonical_id}"


class PaperView(Paper):
    """``Paper`` + 当前用户的阅读状态与本次查询上下文（方案 §4.1、§4.5）。

    ``is_new`` / ``is_read`` 由 :mod:`arxiv_filter.services.search_service`
    按 **job 开始时的 read_state 快照** 计算，不随滚动变化。
    """

    first_seen_at: UtcDatetime | None = None
    read_at: UtcDatetime | None = None

    is_new: bool = False
    is_read: bool = False

    relevance_score: float | None = None

    @classmethod
    def from_paper(
        cls,
        paper: Paper,
        *,
        first_seen_at: object = None,
        read_at: object = None,
        is_new: bool = False,
        is_read: bool = False,
        relevance_score: float | None = None,
    ) -> PaperView:
        """由 ``Paper`` 构造视图，避免调用方逐个复制字段。"""
        return cls(
            **paper.model_dump(),
            first_seen_at=first_seen_at,  # type: ignore[arg-type]
            read_at=read_at,  # type: ignore[arg-type]
            is_new=is_new,
            is_read=is_read,
            relevance_score=relevance_score,
        )


__all__ = ["SOURCE_LABELS_ZH", "Author", "Paper", "PaperSource", "PaperView"]
