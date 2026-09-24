"""去重与版本取舍（方案 §4.4 T1.5）。

规则（方案 §4.4 第 4–5 条）：

* 同一 canonical ID：**版本高者覆盖**；
* 版本相同或未知时：``updated_at`` 较新者覆盖；
* ``categories`` 与 ``external_categories`` **分别取稳定并集**；
* ``primary_category`` 取胜出记录，冲突写 warning；
* cross-list 只产生**一个** ``Paper``。

输出顺序保持输入中 canonical ID 的**首次出现顺序**；排序由调用方单独做
（方案 §4.4「默认排序」一段），这样去重不会偷偷改变上游给出的相关度顺序。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from arxiv_filter.models.paper import Paper


@dataclass(slots=True)
class DedupeResult:
    """去重结果。"""

    papers: list[Paper]
    duplicates_removed: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def input_count(self) -> int:
        return len(self.papers) + self.duplicates_removed


def _stable_union(first: Sequence[str], second: Sequence[str]) -> list[str]:
    """稳定并集：先 first 的顺序，再补 second 里没出现过的。"""
    seen = set(first)
    merged = list(first)
    for item in second:
        if item not in seen:
            seen.add(item)
            merged.append(item)
    return merged


def _is_better(candidate: Paper, current: Paper) -> bool:
    """``candidate`` 是否应当取代 ``current``。

    版本号是第一优先级；两边都没有版本号（或版本相同）时才比较 ``updated_at``。
    """
    candidate_version = candidate.version or 0
    current_version = current.version or 0
    if candidate_version != current_version:
        return candidate_version > current_version
    return candidate.updated_at > current.updated_at


def merge_papers(candidate: Paper, current: Paper, warnings: list[str] | None = None) -> Paper:
    """合并同一 canonical ID 的两条记录。"""
    winner, loser = (candidate, current) if _is_better(candidate, current) else (current, candidate)

    categories = _stable_union(winner.categories, loser.categories)
    external = _stable_union(winner.external_categories, loser.external_categories)

    if (
        winner.primary_category
        and loser.primary_category
        and winner.primary_category != loser.primary_category
        and warnings is not None
    ):
        warnings.append(
            f"{winner.canonical_id} 在不同记录里的主分类不一致"
            f"（{winner.primary_category} / {loser.primary_category}），"
            f"已采用 {winner.primary_category}。"
        )

    # 胜出记录的标量字段优先，仅列表字段取并集
    return winner.model_copy(
        update={
            "categories": categories,
            "external_categories": external,
        }
    )


def deduplicate(papers: Iterable[Paper]) -> DedupeResult:
    """按 canonical ID 去重，cross-list 只保留一条记录。"""
    order: list[str] = []
    merged: dict[str, Paper] = {}
    warnings: list[str] = []
    removed = 0

    for paper in papers:
        key = paper.canonical_id
        existing = merged.get(key)
        if existing is None:
            order.append(key)
            merged[key] = paper
            continue
        removed += 1
        merged[key] = merge_papers(paper, existing, warnings)

    return DedupeResult(
        papers=[merged[key] for key in order],
        duplicates_removed=removed,
        warnings=warnings,
    )


def deduplicate_papers(papers: Iterable[Paper]) -> list[Paper]:
    """只取论文列表的便捷入口。"""
    return deduplicate(papers).papers


__all__ = [
    "DedupeResult",
    "deduplicate",
    "deduplicate_papers",
    "merge_papers",
]
