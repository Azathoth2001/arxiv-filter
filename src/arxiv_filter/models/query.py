"""查询侧领域模型：关键词 AST、搜索请求、已构造查询与 Atom 分页结果。

对应方案 §4.1 与 §4.3。AST 节点定义放在这里（而不是 lexer 里），
这样 :mod:`arxiv_filter.arxiv.query_lexer`、``query_builder`` 与缓存键计算
共享同一套类型，不会各自造一份。
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from arxiv_filter.models.base import DomainModel, UtcDatetime
from arxiv_filter.models.paper import Paper

#: 结果上限的硬边界（方案 §4.1：``result_cap`` 1..2000）。
MIN_RESULT_CAP = 1
MAX_RESULT_CAP = 2000

#: 用户结果列表的过滤视图（方案 §4.5）。**不参与** cache key。
ReadFilter = Literal["all", "new", "unread"]


class SearchScope(StrEnum):
    """关键词搜索的字段范围；映射到 arXiv 的 ``all/ti/abs/au`` 前缀。"""

    ALL = "all"
    TITLE = "title"
    ABSTRACT = "abstract"
    AUTHOR = "author"


#: scope → search_query 字段前缀（方案 §5.2）。
SCOPE_PREFIX: dict[SearchScope, str] = {
    SearchScope.ALL: "all",
    SearchScope.TITLE: "ti",
    SearchScope.ABSTRACT: "abs",
    SearchScope.AUTHOR: "au",
}


class SortMode(StrEnum):
    """排序方式；映射到 ``sortBy`` / ``sortOrder``（方案 §5.2）。"""

    SUBMITTED_DESC = "submitted_desc"
    RELEVANCE = "relevance"
    UPDATED_DESC = "updated_desc"


#: sort → (sortBy, sortOrder)。空元组表示相关度不需要显式 sortOrder。
SORT_PARAMS: dict[SortMode, tuple[str, str]] = {
    SortMode.SUBMITTED_DESC: ("submittedDate", "descending"),
    SortMode.RELEVANCE: ("relevance", "descending"),
    SortMode.UPDATED_DESC: ("lastUpdatedDate", "descending"),
}


# —— 关键词 AST ——


class KeywordNode(DomainModel):
    """关键词 AST 的基类。

    用户输入只会变成**叶子节点**；字段前缀由 :class:`SearchScope` 决定，
    绝不把原始字符串直接拼进 ``search_query``（方案 §4.3 第 5 条）。
    """

    def leaves(self) -> list[Word | Phrase]:
        """按出现顺序返回全部叶子节点。"""
        raise NotImplementedError

    def canonical(self) -> object:
        """返回用于 cache key 的稳定结构。"""
        raise NotImplementedError

    def has_negation(self) -> bool:
        """是否包含 ``NOT``——决定能否与 ``OR`` 混用。"""
        raise NotImplementedError


class Word(KeywordNode):
    """一个词。``position`` 是它在原始输入里的 0 基字符偏移，用于错误定位。"""

    text: str = Field(min_length=1)
    position: int = Field(ge=0)

    def leaves(self) -> list[Word | Phrase]:
        return [self]

    def canonical(self) -> object:
        return {"kind": "word", "text": self.text}

    def has_negation(self) -> bool:
        return False


class Phrase(KeywordNode):
    """一个双引号短语。引号内的空白属于短语本身。"""

    text: str = Field(min_length=1)
    position: int = Field(ge=0)

    def leaves(self) -> list[Word | Phrase]:
        return [self]

    def canonical(self) -> object:
        return {"kind": "phrase", "text": self.text}

    def has_negation(self) -> bool:
        return False


class Not(KeywordNode):
    """一元取反。渲染为二元 ``ANDNOT``，因此左侧必须有条件（方案 §4.3 第 3 条）。"""

    operand: KeywordNode

    def leaves(self) -> list[Word | Phrase]:
        return self.operand.leaves()

    def canonical(self) -> object:
        return {"kind": "not", "operand": self.operand.canonical()}

    def has_negation(self) -> bool:
        return True


class And(KeywordNode):
    """合取（含用户写的 ``AND`` 与隐式空白）。"""

    operands: list[KeywordNode] = Field(min_length=2)

    def leaves(self) -> list[Word | Phrase]:
        return [leaf for operand in self.operands for leaf in operand.leaves()]

    def canonical(self) -> object:
        return {"kind": "and", "operands": [op.canonical() for op in self.operands]}

    def has_negation(self) -> bool:
        return any(operand.has_negation() for operand in self.operands)


class Or(KeywordNode):
    """析取。"""

    operands: list[KeywordNode] = Field(min_length=2)

    def leaves(self) -> list[Word | Phrase]:
        return [leaf for operand in self.operands for leaf in operand.leaves()]

    def canonical(self) -> object:
        return {"kind": "or", "operands": [op.canonical() for op in self.operands]}

    def has_negation(self) -> bool:
        return any(operand.has_negation() for operand in self.operands)


KeywordExpression = Word | Phrase | Not | And | Or


# —— 搜索请求 ——


class SearchRequest(DomainModel):
    """一次搜索的用户意图（方案 §4.1、§4.6 的请求体）。

    这里只做**形状与自洽性**校验；分类白名单校验需要 taxonomy，
    由 :func:`arxiv_filter.taxonomy.resolve_categories` 在构造查询时完成。
    """

    group_id: str = Field(min_length=1)
    category_id: str | None = None

    start_date: date
    end_date: date

    keywords: str = Field(default="", max_length=500)
    scope: SearchScope = SearchScope.ALL
    sort: SortMode = SortMode.SUBMITTED_DESC
    read_filter: ReadFilter = "all"
    result_cap: Annotated[int, Field(ge=MIN_RESULT_CAP, le=MAX_RESULT_CAP)] = MAX_RESULT_CAP
    allow_stale_cache: bool = False

    @model_validator(mode="after")
    def _dates_in_order(self) -> SearchRequest:
        if self.start_date > self.end_date:
            raise ValueError("起始日期不能晚于截止日期")
        return self

    @property
    def has_keywords(self) -> bool:
        return bool(self.keywords.strip())


# —— 已构造查询 ——


class BuiltQuery(DomainModel):
    """UI 选择 → 实际发给 arXiv 的参数（方案 §4.1、§5.2）。

    ``expression`` 是**尚未 URL 编码**的 ``search_query``；编码只由
    ``httpx(params=...)`` 做一次，禁止预编码后再编码（方案 §4.3 第 6 条）。
    """

    category_ids: list[str] = Field(min_length=1)
    expression: str = Field(min_length=1)
    sort_by: Literal["submittedDate", "relevance", "lastUpdatedDate"]
    sort_order: Literal["ascending", "descending"]
    canonical_json: str
    cache_key: str = Field(min_length=64, max_length=64)

    #: 表达式编码后超过 1800 字符时的顺序分片（方案 §4.3）。
    #: 为空表示单次 GET 即可。
    split_expressions: list[str] = Field(default_factory=list)

    @property
    def is_split(self) -> bool:
        return bool(self.split_expressions)

    def as_params(self, start: int, max_results: int) -> dict[str, object]:
        """构造单次 GET 的查询参数；不做任何手工编码。"""
        return {
            "search_query": self.expression,
            "start": start,
            "max_results": max_results,
            "sortBy": self.sort_by,
            "sortOrder": self.sort_order,
        }


class FeedPage(DomainModel):
    """一次 Atom 响应的解析结果（方案 §4.1）。

    ``total_results`` 可以为 ``None``：``opensearch:totalResults`` 偶尔缺失，
    此时界面必须显示**不定进度**而不是编造一个数（方案 §4.1 字段映射表末行、
    ``SearchProgress.indeterminate``）。
    """

    total_results: int | None = Field(default=None, ge=0)
    start_index: int = Field(ge=0)
    items_per_page: int = Field(ge=0)
    papers: list[Paper] = Field(default_factory=list)
    feed_updated_at: UtcDatetime | None = None

    #: 解析期发现的问题（缺 primary_category 等），由 JobManager 提升到
    #: ``SearchJob.warnings`` 展示给用户。
    warnings: list[str] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.papers


__all__ = [
    "MAX_RESULT_CAP",
    "MIN_RESULT_CAP",
    "SCOPE_PREFIX",
    "SORT_PARAMS",
    "And",
    "BuiltQuery",
    "FeedPage",
    "KeywordExpression",
    "KeywordNode",
    "Not",
    "Or",
    "Phrase",
    "ReadFilter",
    "SearchRequest",
    "SearchScope",
    "SortMode",
    "Word",
]
