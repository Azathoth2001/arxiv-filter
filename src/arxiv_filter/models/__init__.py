"""领域模型包（方案 §4.1）。

分为三个模块：

* :mod:`~arxiv_filter.models.paper` —— 作者与论文（``Paper`` / ``PaperView``）；
* :mod:`~arxiv_filter.models.query` —— 关键词 AST、``SearchRequest``、
  ``BuiltQuery``、``FeedPage``；
* :mod:`~arxiv_filter.models.job` —— 任务状态机与结果页；
* :mod:`~arxiv_filter.models.base` —— 公共基类与 UTC 时间类型。
"""

from __future__ import annotations

from arxiv_filter.models.base import DomainModel, UtcDatetime, format_utc, parse_utc
from arxiv_filter.models.job import (
    ALLOWED_TRANSITIONS,
    RUNNING_PHASES,
    TERMINAL_PHASES,
    CacheStatus,
    SearchJob,
    SearchPhase,
    SearchProgress,
    SearchResultPage,
    can_transition,
)
from arxiv_filter.models.paper import (
    SOURCE_LABELS_ZH,
    Author,
    Paper,
    PaperSource,
    PaperView,
)
from arxiv_filter.models.query import (
    MAX_RESULT_CAP,
    MIN_RESULT_CAP,
    SCOPE_PREFIX,
    SORT_PARAMS,
    And,
    BuiltQuery,
    FeedPage,
    KeywordExpression,
    KeywordNode,
    Not,
    Or,
    Phrase,
    ReadFilter,
    SearchRequest,
    SearchScope,
    SortMode,
    Word,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "MAX_RESULT_CAP",
    "MIN_RESULT_CAP",
    "RUNNING_PHASES",
    "SCOPE_PREFIX",
    "SORT_PARAMS",
    "SOURCE_LABELS_ZH",
    "TERMINAL_PHASES",
    "And",
    "Author",
    "BuiltQuery",
    "CacheStatus",
    "DomainModel",
    "FeedPage",
    "KeywordExpression",
    "KeywordNode",
    "Not",
    "Or",
    "Paper",
    "PaperSource",
    "PaperView",
    "Phrase",
    "ReadFilter",
    "SearchJob",
    "SearchPhase",
    "SearchProgress",
    "SearchRequest",
    "SearchResultPage",
    "SearchScope",
    "SortMode",
    "UtcDatetime",
    "Word",
    "can_transition",
    "format_utc",
    "parse_utc",
]
