"""覆盖方案里难以用内置 taxonomy 触发的分支。

主要是两类：

* ``query_builder`` 的**超长表达式拆分**（方案 §4.3）——内置 cs 组没超过 1800 字符，
  所以必须用合成的 taxonomy 才能走到；
* ``paths`` 的 frozen / 便携模式分支。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

from arxiv_filter import paths
from arxiv_filter.arxiv import query_builder
from arxiv_filter.arxiv.query_builder import (
    MAX_ENCODED_QUERY_CHARS,
    build_query,
    encoded_length,
)
from arxiv_filter.models.query import SearchRequest, SearchScope
from arxiv_filter.taxonomy import Taxonomy

JAN1 = date(2025, 1, 1)
JAN7 = date(2025, 1, 7)


def synthetic_taxonomy(count: int, *, id_length: int = 24) -> Taxonomy:
    """构造一个分类 ID 很长的 snapshot，用来逼出拆分路径。"""
    suffix = "x" * id_length
    categories = [
        {
            "id": f"syn.{suffix}{index:04d}",
            "group_id": "syn",
            "archive_id": "syn",
            "name_en": f"Synthetic {index}",
            "name_zh": f"合成分类 {index}",
            "active": True,
            "alias_of": None,
        }
        for index in range(count)
    ]
    return Taxonomy(
        {
            "schema_version": 1,
            "taxonomy_version": "2026-09-24",
            "source": {
                "html_url": "https://example.invalid/",
                "html_sha256": "0" * 64,
                "html_checked_at": "2026-09-24T00:00:00Z",
                "canonical_url": "https://example.invalid/",
                "canonical_sha256": "1" * 64,
                "canonical_checked_at": "2026-09-24T00:00:00Z",
            },
            "aliases": {},
            "groups": [{"id": "syn", "name_en": "Synthetic", "name_zh": "合成"}],
            "archives": [
                {"id": "syn", "group_id": "syn", "name_en": "Synthetic", "name_zh": "合成"}
            ],
            "categories": categories,
        }
    )


def request_for(table: Taxonomy, **overrides: object) -> SearchRequest:
    payload: dict[str, object] = {
        "group_id": "syn",
        "category_id": None,
        "start_date": JAN1,
        "end_date": JAN7,
    }
    payload.update(overrides)
    return SearchRequest(**payload)  # type: ignore[arg-type]


# —— 超长表达式拆分（方案 §4.3）——


def test_large_expansion_is_split_into_budgeted_chunks() -> None:
    table = synthetic_taxonomy(200)
    query = build_query(request_for(table), taxonomy=table)

    # 前置条件：单条表达式确实超限，否则这个测试没有意义
    assert encoded_length(query.expression) > MAX_ENCODED_QUERY_CHARS

    assert query.is_split
    assert len(query.split_expressions) > 1
    for chunk in query.split_expressions:
        assert encoded_length(chunk) <= MAX_ENCODED_QUERY_CHARS


def test_split_chunks_together_cover_every_category() -> None:
    table = synthetic_taxonomy(200)
    query = build_query(request_for(table), taxonomy=table)
    joined = " ".join(query.split_expressions or [query.expression])
    for category_id in query.category_ids:
        assert f"cat:{category_id}" in joined, category_id


def test_split_chunks_keep_the_date_and_keyword_clauses() -> None:
    table = synthetic_taxonomy(200)
    query = build_query(request_for(table, keywords="transformer"), taxonomy=table)
    for chunk in query.split_expressions:
        assert "submittedDate:[202501010000 TO 202501072359]" in chunk
        assert "all:transformer" in chunk


def test_each_split_chunk_has_balanced_parentheses() -> None:
    table = synthetic_taxonomy(200)
    query = build_query(request_for(table), taxonomy=table)
    for chunk in query.split_expressions:
        assert chunk.count("(") == chunk.count(")")


def test_cache_key_is_shared_across_split_chunks() -> None:
    """拆分只影响网络往返次数，不改变查询语义——cache key 必须一致。"""
    table = synthetic_taxonomy(200)
    split_query = build_query(request_for(table), taxonomy=table)
    assert split_query.is_split
    # 同一请求重复构造得到同一 key
    assert build_query(request_for(table), taxonomy=table).cache_key == split_query.cache_key


def test_moderate_expansion_is_not_split() -> None:
    table = synthetic_taxonomy(20)
    query = build_query(request_for(table), taxonomy=table)
    assert encoded_length(query.expression) <= MAX_ENCODED_QUERY_CHARS
    assert query.split_expressions == []


def test_single_category_that_is_still_too_long_stays_unsplit() -> None:
    """单分类也超预算时无法再拆——返回原表达式，由上游决定是否拒绝。"""
    table = synthetic_taxonomy(1, id_length=2000)
    query = build_query(request_for(table), taxonomy=table)
    assert query.split_expressions == []
    assert len(query.category_ids) == 1


def test_chunk_helper_honours_the_budget_directly() -> None:
    table = synthetic_taxonomy(40)
    ids = list(table.leaves_of_group("syn"))
    chunks = query_builder._chunk_categories(ids, JAN1, JAN7, None, SearchScope.ALL, 600)
    assert len(chunks) > 1
    for chunk in chunks:
        assert encoded_length(chunk) <= 600


def test_encoded_length_counts_utf8_percent_encoding() -> None:
    assert encoded_length("cat:cs.CL") == len("cat%3Acs.CL")
    assert encoded_length("all:机器学习") == len("all%3A%E6%9C%BA%E5%99%A8%E5%AD%A6%E4%B9%A0")


# —— paths 的 frozen 与便携分支 ——


def test_resource_root_uses_meipass_when_frozen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundled = tmp_path / "arxiv_filter"
    bundled.mkdir()
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert paths.resource_root() == bundled
    assert paths.static_dir() == bundled / "static"


def test_resource_root_falls_back_to_meipass_when_package_dir_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert paths.resource_root() == tmp_path


def test_is_frozen_reflects_sys_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert paths.is_frozen() is True
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert paths.is_frozen() is False


def test_executable_dir_uses_sys_executable_when_frozen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = tmp_path / "arxiv-filter.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert paths.executable_dir() == tmp_path


def test_executable_dir_returns_project_root_in_source_mode() -> None:
    root = paths.executable_dir()
    assert (root / "pyproject.toml").is_file()


def test_portable_mode_is_rejected_when_directory_is_not_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / paths.PORTABLE_FLAG).write_text("", encoding="utf-8")
    monkeypatch.setattr(paths, "executable_dir", lambda: tmp_path)
    monkeypatch.setattr(paths, "_is_writable", lambda _path: False)
    decision = paths.evaluate_portable_mode()
    assert decision.enabled is False
    assert "不可写" in decision.reason_zh


def test_is_writable_detects_a_missing_parent(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deeper"
    assert paths._is_writable(target) is True
    assert target.is_dir()


def test_data_dir_uses_portable_root_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ARXIV_FILTER_DATA_DIR", raising=False)
    portable = tmp_path / "data"
    monkeypatch.setattr(
        paths,
        "evaluate_portable_mode",
        lambda: paths.PortableDecision(True, portable, "便携模式已启用。"),
    )
    assert paths.data_dir() == portable.resolve()
