"""任务状态模型（方案 §4.1、§4.6、§4.7）。

状态机（方案 §4.7）::

    queued → fetching → post_processing → completed
       └──────────────→ cancelling → cancelled
    任意运行态 ─────────────────────→ failed

本模块只描述**状态与不变式**；迁移与并发合并由
:mod:`arxiv_filter.services.job_manager`（T4.1）负责。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from arxiv_filter.models.base import DomainModel
from arxiv_filter.models.paper import PaperView


class SearchPhase(StrEnum):
    """任务阶段。字符串值与方案 §4.1 的 ``Literal`` 完全一致。"""

    QUEUED = "queued"
    FETCHING = "fetching"
    POST_PROCESSING = "post_processing"
    COMPLETED = "completed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    FAILED = "failed"


#: 终态：不再发生迁移。
TERMINAL_PHASES: frozenset[SearchPhase] = frozenset(
    {SearchPhase.COMPLETED, SearchPhase.CANCELLED, SearchPhase.FAILED}
)

#: 运行态：会占用唯一的那个上游任务名额。
RUNNING_PHASES: frozenset[SearchPhase] = frozenset(
    {
        SearchPhase.QUEUED,
        SearchPhase.FETCHING,
        SearchPhase.POST_PROCESSING,
        SearchPhase.CANCELLING,
    }
)

#: 允许的迁移。任何不在表里的迁移都应当被拒绝（T4.1 的 409）。
ALLOWED_TRANSITIONS: dict[SearchPhase, frozenset[SearchPhase]] = {
    SearchPhase.QUEUED: frozenset(
        {SearchPhase.FETCHING, SearchPhase.CANCELLING, SearchPhase.FAILED}
    ),
    SearchPhase.FETCHING: frozenset(
        {
            SearchPhase.POST_PROCESSING,
            SearchPhase.CANCELLING,
            SearchPhase.FAILED,
        }
    ),
    SearchPhase.POST_PROCESSING: frozenset(
        {SearchPhase.COMPLETED, SearchPhase.CANCELLING, SearchPhase.FAILED}
    ),
    SearchPhase.CANCELLING: frozenset({SearchPhase.CANCELLED, SearchPhase.FAILED}),
    SearchPhase.COMPLETED: frozenset(),
    SearchPhase.CANCELLED: frozenset(),
    SearchPhase.FAILED: frozenset(),
}

#: 缓存命中情况（方案 §4.1 ``SearchJob.cache_status``）。
CacheStatus = Literal["hit", "miss", "stale"]


def can_transition(current: SearchPhase, target: SearchPhase) -> bool:
    """判断状态迁移是否合法。"""
    if current is target:
        return True
    return target in ALLOWED_TRANSITIONS[current]


class SearchProgress(DomainModel):
    """进度快照；前端每 500 ms 读一次（方案 §0.2）。"""

    phase: SearchPhase = SearchPhase.QUEUED

    fetched: int = Field(default=0, ge=0)
    target: int | None = Field(default=None, ge=0)
    total_reported: int | None = Field(default=None, ge=0)
    pages: int = Field(default=0, ge=0)
    retries: int = Field(default=0, ge=0)
    elapsed_ms: int = Field(default=0, ge=0)

    message_zh: str = ""

    #: totalResults 尚未到达时为 ``True``，前端显示不定进度（方案 §4.7）。
    indeterminate: bool = False

    @property
    def is_terminal(self) -> bool:
        return self.phase in TERMINAL_PHASES

    @property
    def percent(self) -> float | None:
        """完成百分比；无法确定时返回 ``None`` 而不是编造 0%。"""
        if self.target is None or self.target <= 0:
            return None
        return min(100.0, self.fetched / self.target * 100.0)


class SearchJob(DomainModel):
    """一个抓取任务的状态（方案 §4.1、§4.6）。"""

    job_id: UUID
    status: SearchProgress = Field(default_factory=SearchProgress)

    #: 只有真正命中缓存时才是 ``hit``；取消/失败的任务不得标成 ``hit``。
    cache_status: CacheStatus = "miss"

    #: 结果不完整（被取消或分片中途中止）；``partial`` 的结果不写完整缓存。
    partial: bool = False

    #: ``totalResults > result_cap``。
    truncated: bool = False

    warnings: list[str] = Field(default_factory=list)

    @property
    def is_running(self) -> bool:
        return self.status.phase in RUNNING_PHASES

    @property
    def is_terminal(self) -> bool:
        return self.status.phase in TERMINAL_PHASES

    @property
    def can_export(self) -> bool:
        """能否导出。

        方案 §4.7：``cancelled`` 默认禁用，用户显式确认「导出部分结果」后允许；
        ``failed`` 不可导出；``running`` 时可导出**旧结果**（由前端处理）。
        """
        return self.status.phase in {
            SearchPhase.COMPLETED,
            SearchPhase.CANCELLED,
        }


class SearchResultPage(DomainModel):
    """``GET /search-jobs/{id}/results`` 的响应体（方案 §4.6）。"""

    items: list[PaperView] = Field(default_factory=list)
    total: int = Field(ge=0)
    partial: bool = False
    truncated: bool = False
    query: str | None = None

    @model_validator(mode="after")
    def _total_matches_items_when_complete(self) -> SearchResultPage:
        """完整结果集里 ``total`` 必须等于 ``items`` 长度。

        分页响应会只带一页 ``items``，因此仅在 ``total == len(items)``
        的整页语义下做一致性检查的**下界**校验：``total`` 不能小于本页条数。
        """
        if self.total < len(self.items):
            raise ValueError("total 不能小于本页条数")
        return self


__all__ = [
    "ALLOWED_TRANSITIONS",
    "RUNNING_PHASES",
    "TERMINAL_PHASES",
    "CacheStatus",
    "SearchJob",
    "SearchPhase",
    "SearchProgress",
    "SearchResultPage",
    "can_transition",
]
