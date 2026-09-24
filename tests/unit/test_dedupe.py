"""T1.5 去重与版本取舍（方案 §4.4）。"""

from __future__ import annotations

from datetime import datetime, timezone

from arxiv_filter.arxiv.dedupe import (
    deduplicate,
    deduplicate_papers,
    merge_papers,
)
from arxiv_filter.models.paper import Author, Paper

UTC = timezone.utc


def paper(
    canonical_id: str = "2501.01234",
    *,
    version: int | None = 1,
    updated: datetime | None = None,
    categories: list[str] | None = None,
    external: list[str] | None = None,
    primary: str | None = "cs.CL",
    title: str | None = None,
) -> Paper:
    versioned = canonical_id if version is None else f"{canonical_id}v{version}"
    return Paper(
        canonical_id=canonical_id,
        versioned_id=versioned,
        version=version,
        title=title or f"Title {versioned}",
        authors=[Author(name="A. Author")],
        summary_raw="summary",
        published_at=datetime(2025, 1, 2, tzinfo=UTC),
        updated_at=updated or datetime(2025, 1, 3, tzinfo=UTC),
        primary_category=primary,
        categories=categories if categories is not None else ["cs.CL"],
        external_categories=external if external is not None else [],
        abs_url=f"https://arxiv.org/abs/{versioned}",
        retrieved_at=datetime(2026, 9, 24, tzinfo=UTC),
    )


def test_unique_papers_pass_through_unchanged() -> None:
    result = deduplicate([paper("2501.00001"), paper("2501.00002")])
    assert [p.canonical_id for p in result.papers] == ["2501.00001", "2501.00002"]
    assert result.duplicates_removed == 0
    assert result.warnings == []


def test_higher_version_wins() -> None:
    result = deduplicate([paper(version=1), paper(version=3), paper(version=2)])
    assert len(result.papers) == 1
    assert result.papers[0].version == 3
    assert result.papers[0].versioned_id == "2501.01234v3"
    assert result.duplicates_removed == 2


def test_version_wins_over_newer_updated_at() -> None:
    """版本号是第一优先级：v1 即使更新时间更晚也不能盖掉 v2。"""
    older = paper(version=2, updated=datetime(2025, 1, 1, tzinfo=UTC))
    newer = paper(version=1, updated=datetime(2026, 1, 1, tzinfo=UTC))
    assert deduplicate([older, newer]).papers[0].version == 2


def test_newer_updated_at_wins_when_versions_are_equal() -> None:
    old = paper(version=2, updated=datetime(2025, 1, 1, tzinfo=UTC), title="old")
    new = paper(version=2, updated=datetime(2025, 6, 1, tzinfo=UTC), title="new")
    assert deduplicate([old, new]).papers[0].title == "new"


def test_newer_updated_at_wins_when_versions_are_unknown() -> None:
    old = paper(version=None, updated=datetime(2025, 1, 1, tzinfo=UTC), title="old")
    new = paper(version=None, updated=datetime(2025, 6, 1, tzinfo=UTC), title="new")
    result = deduplicate([old, new])
    assert result.papers[0].title == "new"
    assert result.papers[0].version is None


def test_known_version_beats_unknown_version() -> None:
    unknown = paper(version=None, updated=datetime(2026, 1, 1, tzinfo=UTC), title="unknown")
    known = paper(version=1, updated=datetime(2020, 1, 1, tzinfo=UTC), title="known")
    assert deduplicate([unknown, known]).papers[0].title == "known"


def test_categories_are_merged_as_a_stable_union() -> None:
    first = paper(version=1, categories=["cs.CL", "cs.LG"])
    second = paper(version=2, categories=["cs.CL", "cs.AI"])
    merged = deduplicate([first, second]).papers[0]
    # 胜出记录（v2）的列表在前，补上落败记录里独有的项
    assert merged.categories == ["cs.CL", "cs.AI", "cs.LG"]


def test_external_categories_are_merged_separately() -> None:
    first = paper(version=1, external=["I.2.7"])
    second = paper(version=2, external=["68T50"])
    merged = deduplicate([first, second]).papers[0]
    assert merged.external_categories == ["68T50", "I.2.7"]
    assert merged.categories == ["cs.CL"]


def test_primary_category_conflict_produces_a_warning() -> None:
    first = paper(version=1, primary="cs.LG")
    second = paper(version=2, primary="cs.CL")
    result = deduplicate([first, second])
    assert result.papers[0].primary_category == "cs.CL"
    assert any("主分类不一致" in warning for warning in result.warnings)


def test_no_warning_when_primary_categories_agree() -> None:
    result = deduplicate([paper(version=1), paper(version=2)])
    assert result.warnings == []


def test_missing_primary_category_does_not_warn() -> None:
    result = deduplicate([paper(version=1, primary=None), paper(version=2, primary="cs.CL")])
    assert result.warnings == []


def test_cross_list_produces_exactly_one_paper() -> None:
    """cross-list 只产生一个 Paper（方案 §4.4 第 5 条）。"""
    cross_listed = [
        paper("2501.10001", version=1, categories=["cs.LG", "cs.CL"], primary="cs.LG"),
        paper("2501.10001", version=1, categories=["cs.CL", "stat.ML"], primary="cs.LG"),
    ]
    result = deduplicate(cross_listed)
    assert len(result.papers) == 1
    assert result.papers[0].categories == ["cs.LG", "cs.CL", "stat.ML"]


def test_output_preserves_first_appearance_order() -> None:
    """去重不改变上游顺序——排序由调用方单独做。"""
    papers = [paper("2501.00003"), paper("2501.00001"), paper("2501.00003"), paper("2501.00002")]
    assert [p.canonical_id for p in deduplicate(papers).papers] == [
        "2501.00003",
        "2501.00001",
        "2501.00002",
    ]


def test_legacy_ids_deduplicate_by_canonical_form() -> None:
    papers = [
        paper("hep-th/9901001", version=1),
        paper("hep-th/9901001", version=2),
    ]
    result = deduplicate(papers)
    assert len(result.papers) == 1
    assert result.papers[0].canonical_id == "hep-th/9901001"
    assert result.papers[0].version == 2


def test_three_way_merge_keeps_the_best() -> None:
    papers = [
        paper(version=1, categories=["a"], external=["x"]),
        paper(version=2, categories=["b"], external=["y"]),
        paper(version=3, categories=["c"], external=["z"]),
    ]
    merged = deduplicate(papers).papers[0]
    assert merged.version == 3
    assert merged.categories == ["c", "b", "a"]
    assert merged.external_categories == ["z", "y", "x"]


def test_merge_papers_is_symmetric_in_outcome() -> None:
    """``merge(a, b)`` 与 ``merge(b, a)`` 必须得到同一个胜出记录。"""
    low = paper(version=1)
    high = paper(version=2)
    assert merge_papers(low, high).versioned_id == merge_papers(high, low).versioned_id


def test_input_count_accounts_for_everything() -> None:
    result = deduplicate([paper(version=1), paper(version=2), paper("2501.99999")])
    assert result.input_count == 3
    assert len(result.papers) == 2
    assert result.duplicates_removed == 1


def test_empty_input() -> None:
    result = deduplicate([])
    assert result.papers == []
    assert result.duplicates_removed == 0
    assert result.input_count == 0


def test_deduplicate_papers_convenience_wrapper() -> None:
    assert len(deduplicate_papers([paper(version=1), paper(version=2)])) == 1
