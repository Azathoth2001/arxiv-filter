"""T1.1 数据模型：JSON 往返、UTC 时间、非法输入。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from arxiv_filter.models import (
    And,
    Author,
    BuiltQuery,
    FeedPage,
    Not,
    Or,
    Paper,
    PaperView,
    Phrase,
    SearchJob,
    SearchPhase,
    SearchProgress,
    SearchRequest,
    SearchResultPage,
    SearchScope,
    SortMode,
    Word,
    can_transition,
    format_utc,
    parse_utc,
)
from arxiv_filter.models.base import ensure_aware_utc

UTC = timezone.utc


def make_paper(**overrides: object) -> Paper:
    payload: dict[str, object] = {
        "canonical_id": "2501.01234",
        "versioned_id": "2501.01234v2",
        "version": 2,
        "title": "Example title",
        "authors": [Author(name="A. Author", affiliation="Somewhere")],
        "summary_raw": "Abstract with $x^2$ and\nnewlines.",
        "published_at": datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC),
        "updated_at": datetime(2025, 1, 3, 3, 4, 5, tzinfo=UTC),
        "primary_category": "cs.CL",
        "categories": ["cs.CL", "cs.LG"],
        "abs_url": "https://arxiv.org/abs/2501.01234",
        "pdf_url": "https://arxiv.org/pdf/2501.01234v2",
        "retrieved_at": datetime(2026, 9, 24, 0, 0, 0, tzinfo=UTC),
    }
    payload.update(overrides)
    return Paper(**payload)  # type: ignore[arg-type]


# —— 时间 ——


def test_rfc3339_round_trip_helpers() -> None:
    moment = datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert format_utc(moment) == "2025-01-02T03:04:05Z"
    assert parse_utc("2025-01-02T03:04:05Z") == moment
    assert parse_utc("2025-01-02T03:04:05+00:00") == moment


def test_parse_utc_converts_other_offsets() -> None:
    assert parse_utc("2025-01-02T11:04:05+08:00") == datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)


def test_parse_utc_treats_naive_string_as_utc() -> None:
    """数据库里的历史值都按 UTC 写入，naive 字符串按 UTC 解释。"""
    assert parse_utc("2025-01-02T03:04:05") == datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)


def test_naive_datetime_object_is_rejected() -> None:
    """naive datetime 对象必须报错——否则本机时区会被悄悄当成 UTC。"""
    with pytest.raises(ValueError, match="必须带时区"):
        ensure_aware_utc(datetime(2025, 1, 2, 3, 4, 5))


def test_paper_rejects_naive_datetime() -> None:
    with pytest.raises(ValidationError):
        make_paper(published_at=datetime(2025, 1, 2, 3, 4, 5))


def test_paper_normalises_offset_to_utc() -> None:
    paper = make_paper(published_at=datetime(2025, 1, 2, 11, 0, 0, tzinfo=timezone(timedelta(hours=8))))
    assert paper.published_at == datetime(2025, 1, 2, 3, 0, 0, tzinfo=UTC)
    assert paper.published_at.tzinfo is UTC


def test_paper_accepts_iso_string_from_database() -> None:
    paper = make_paper(updated_at="2025-01-03T03:04:05Z")
    assert paper.updated_at == datetime(2025, 1, 3, 3, 4, 5, tzinfo=UTC)


# —— JSON 往返 ——


def test_paper_json_round_trip_is_lossless() -> None:
    paper = make_paper()
    restored = Paper.model_validate_json(paper.model_dump_json())
    assert restored == paper


def test_paper_json_uses_rfc3339_z_suffix() -> None:
    payload = make_paper().model_dump_json()
    assert '"2025-01-02T03:04:05Z"' in payload
    assert "+00:00" not in payload


def test_paper_python_dump_keeps_datetime_objects() -> None:
    """python 模式保留 datetime 对象；只有 JSON 模式才渲染成字符串。"""
    assert isinstance(make_paper().model_dump()["published_at"], datetime)


def test_paper_view_json_round_trip() -> None:
    view = PaperView.from_paper(
        make_paper(),
        first_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
        is_new=True,
    )
    assert view.is_new is True
    assert view.is_read is False
    assert view.first_seen_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert PaperView.model_validate_json(view.model_dump_json()) == view


def test_paper_view_carries_every_paper_field() -> None:
    paper = make_paper()
    view = PaperView.from_paper(paper)
    for field in Paper.model_fields:
        assert getattr(view, field) == getattr(paper, field), field


# —— Paper 不变式 ——


def test_blank_and_duplicate_categories_are_removed_stably() -> None:
    paper = make_paper(categories=["cs.LG", "  ", "cs.CL", "cs.LG", "cs.AI"])
    assert paper.categories == ["cs.LG", "cs.CL", "cs.AI"]


def test_author_name_whitespace_is_collapsed() -> None:
    author = Author(name="  A.\n  Author  ", affiliation=" Some \t Place ")
    assert author.name == "A. Author"
    assert author.affiliation == "Some Place"


def test_summary_keeps_internal_latex_and_newlines() -> None:
    paper = make_paper(summary_raw="  $x^2$\n\nmore  ")
    assert paper.summary_raw == "$x^2$\n\nmore"


@pytest.mark.parametrize("bad_url", ["javascript:alert(1)", "file:///etc/passwd", "ftp://x/y"])
def test_non_http_urls_are_rejected(bad_url: str) -> None:
    with pytest.raises(ValidationError):
        make_paper(abs_url=bad_url)


def test_blank_pdf_url_becomes_none() -> None:
    assert make_paper(pdf_url="   ").pdf_url is None


def test_extra_field_is_rejected() -> None:
    """字段拼写错误必须立刻报错（metadata_json 往返不允许静默丢字段）。"""
    with pytest.raises(ValidationError):
        make_paper(primary_catgory="cs.CL")


def test_display_title_falls_back_to_id() -> None:
    assert make_paper(title="").display_title == "（无标题）2501.01234"
    assert make_paper(title="Real").display_title == "Real"


def test_source_label_is_chinese() -> None:
    assert make_paper().source_label_zh == "arXiv 官方 API"
    assert make_paper(source="openalex").source_label_zh == "OpenAlex（实验性）"


# —— SearchRequest ——


def test_search_request_defaults_match_frozen_decisions() -> None:
    request = SearchRequest(group_id="cs", start_date=date(2025, 1, 1), end_date=date(2025, 1, 7))
    assert request.category_id is None          # D7：None = 全部子类
    assert request.scope is SearchScope.ALL
    assert request.sort is SortMode.SUBMITTED_DESC
    assert request.read_filter == "all"
    assert request.result_cap == 2000           # D3
    assert request.allow_stale_cache is False   # D5
    assert request.has_keywords is False


def test_search_request_rejects_reversed_dates() -> None:
    with pytest.raises(ValidationError, match="起始日期不能晚于截止日期"):
        SearchRequest(group_id="cs", start_date=date(2025, 1, 8), end_date=date(2025, 1, 7))


def test_search_request_allows_single_day_range() -> None:
    request = SearchRequest(group_id="cs", start_date=date(2025, 1, 1), end_date=date(2025, 1, 1))
    assert request.start_date == request.end_date


@pytest.mark.parametrize("cap", [0, -1, 2001, 99999])
def test_search_request_rejects_out_of_range_cap(cap: int) -> None:
    with pytest.raises(ValidationError):
        SearchRequest(
            group_id="cs", start_date=date(2025, 1, 1), end_date=date(2025, 1, 7), result_cap=cap
        )


@pytest.mark.parametrize("cap", [1, 2000])
def test_search_request_accepts_cap_boundaries(cap: int) -> None:
    request = SearchRequest(
        group_id="cs", start_date=date(2025, 1, 1), end_date=date(2025, 1, 7), result_cap=cap
    )
    assert request.result_cap == cap


def test_search_request_rejects_overlong_keywords() -> None:
    with pytest.raises(ValidationError):
        SearchRequest(
            group_id="cs",
            start_date=date(2025, 1, 1),
            end_date=date(2025, 1, 7),
            keywords="a" * 501,
        )


def test_search_request_accepts_exactly_500_characters() -> None:
    request = SearchRequest(
        group_id="cs",
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 7),
        keywords="a" * 500,
    )
    assert len(request.keywords) == 500


def test_search_request_rejects_unknown_read_filter() -> None:
    with pytest.raises(ValidationError):
        SearchRequest(
            group_id="cs",
            start_date=date(2025, 1, 1),
            end_date=date(2025, 1, 7),
            read_filter="everything",  # type: ignore[arg-type]
        )


def test_search_request_json_round_trip() -> None:
    request = SearchRequest(
        group_id="cs",
        category_id="cs.CL",
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 7),
        keywords='"large language model" AND retrieval NOT survey',
        read_filter="new",
    )
    assert SearchRequest.model_validate_json(request.model_dump_json()) == request


# —— AST 节点 ——


def test_ast_leaves_are_collected_in_order() -> None:
    tree = And(operands=[Word(text="a", position=0), Or(operands=[Word(text="b", position=4), Phrase(text="c d", position=6)])])
    assert [leaf.text for leaf in tree.leaves()] == ["a", "b", "c d"]


def test_ast_canonical_structure_is_stable() -> None:
    tree = And(operands=[Word(text="a", position=0), Not(operand=Word(text="b", position=8))])
    assert tree.canonical() == {
        "kind": "and",
        "operands": [
            {"kind": "word", "text": "a"},
            {"kind": "not", "operand": {"kind": "word", "text": "b"}},
        ],
    }


def test_ast_has_negation_propagates() -> None:
    assert Not(operand=Word(text="x", position=0)).has_negation() is True
    assert Word(text="x", position=0).has_negation() is False
    assert (
        And(operands=[Word(text="a", position=0), Not(operand=Word(text="b", position=2))]).has_negation()
        is True
    )
    assert Or(operands=[Word(text="a", position=0), Word(text="b", position=2)]).has_negation() is False


def test_ast_rejects_single_operand_conjunction() -> None:
    with pytest.raises(ValidationError):
        And(operands=[Word(text="a", position=0)])


def test_ast_requires_non_negative_position() -> None:
    with pytest.raises(ValidationError):
        Word(text="a", position=-1)


# —— BuiltQuery ——


def make_built_query(**overrides: object) -> BuiltQuery:
    payload: dict[str, object] = {
        "category_ids": ["cs.CL"],
        "expression": "(cat:cs.CL) AND submittedDate:[202501010000 TO 202501072359]",
        "sort_by": "submittedDate",
        "sort_order": "descending",
        "canonical_json": '{"a":1}',
        "cache_key": "0" * 64,
    }
    payload.update(overrides)
    return BuiltQuery(**payload)  # type: ignore[arg-type]


def test_built_query_params_are_not_pre_encoded() -> None:
    params = make_built_query().as_params(start=0, max_results=200)
    assert params["search_query"] == "(cat:cs.CL) AND submittedDate:[202501010000 TO 202501072359]"
    assert "%3A" not in str(params["search_query"])
    assert params["start"] == 0
    assert params["max_results"] == 200
    assert params["sortBy"] == "submittedDate"


def test_built_query_single_expression_is_not_split() -> None:
    assert make_built_query().is_split is False


def test_built_query_split_expressions_are_detected() -> None:
    query = make_built_query(split_expressions=["(cat:a)", "(cat:b)"])
    assert query.is_split is True


def test_built_query_rejects_bad_cache_key_length() -> None:
    with pytest.raises(ValidationError):
        make_built_query(cache_key="abc")


# —— FeedPage ——


def test_feed_page_round_trip() -> None:
    page = FeedPage(
        total_results=173,
        start_index=0,
        items_per_page=2,
        papers=[make_paper()],
        feed_updated_at=datetime(2026, 9, 24, tzinfo=UTC),
    )
    assert FeedPage.model_validate_json(page.model_dump_json()) == page
    assert page.is_empty is False


def test_feed_page_allows_zero_results() -> None:
    page = FeedPage(total_results=0, start_index=0, items_per_page=0)
    assert page.is_empty is True
    assert page.feed_updated_at is None


# —— 任务状态 ——


def test_allowed_transitions_follow_the_state_machine() -> None:
    assert can_transition(SearchPhase.QUEUED, SearchPhase.FETCHING)
    assert can_transition(SearchPhase.FETCHING, SearchPhase.POST_PROCESSING)
    assert can_transition(SearchPhase.POST_PROCESSING, SearchPhase.COMPLETED)
    assert can_transition(SearchPhase.FETCHING, SearchPhase.CANCELLING)
    assert can_transition(SearchPhase.CANCELLING, SearchPhase.CANCELLED)
    assert can_transition(SearchPhase.FETCHING, SearchPhase.FAILED)


def test_terminal_phases_do_not_transition() -> None:
    for terminal in (SearchPhase.COMPLETED, SearchPhase.CANCELLED, SearchPhase.FAILED):
        for target in SearchPhase:
            if target is terminal:
                continue
            assert not can_transition(terminal, target), f"{terminal} → {target}"


def test_same_phase_transition_is_allowed() -> None:
    for phase in SearchPhase:
        assert can_transition(phase, phase)


def test_running_and_terminal_partition_every_phase() -> None:
    from arxiv_filter.models.job import RUNNING_PHASES, TERMINAL_PHASES

    assert RUNNING_PHASES | TERMINAL_PHASES == set(SearchPhase)
    assert RUNNING_PHASES & TERMINAL_PHASES == set()


def test_progress_percent_is_none_before_target_is_known() -> None:
    """首包到达前不编造 0%，而是返回 None 让前端显示不定进度。"""
    progress = SearchProgress()
    assert progress.percent is None
    assert progress.indeterminate is False


def test_progress_percent_is_clamped_to_100() -> None:
    progress = SearchProgress(fetched=250, target=200)
    assert progress.percent == 100.0


def test_progress_percent_computes_normally() -> None:
    assert SearchProgress(fetched=50, target=200).percent == 25.0


def test_job_export_rules() -> None:
    job_id = uuid4()
    assert SearchJob(job_id=job_id, status=SearchProgress(phase=SearchPhase.COMPLETED)).can_export
    assert SearchJob(job_id=job_id, status=SearchProgress(phase=SearchPhase.CANCELLED)).can_export
    assert not SearchJob(job_id=job_id, status=SearchProgress(phase=SearchPhase.FAILED)).can_export
    assert not SearchJob(job_id=job_id, status=SearchProgress(phase=SearchPhase.FETCHING)).can_export


def test_job_running_flag() -> None:
    job_id = uuid4()
    running = SearchJob(job_id=job_id, status=SearchProgress(phase=SearchPhase.FETCHING))
    assert running.is_running and not running.is_terminal
    done = SearchJob(job_id=job_id, status=SearchProgress(phase=SearchPhase.COMPLETED))
    assert done.is_terminal and not done.is_running


def test_job_json_round_trip_preserves_uuid() -> None:
    job = SearchJob(job_id=UUID("018f2b9c-0000-7000-8000-000000000000"))
    restored = SearchJob.model_validate_json(job.model_dump_json())
    assert restored.job_id == job.job_id
    assert isinstance(restored.job_id, UUID)


def test_result_page_rejects_total_smaller_than_page() -> None:
    with pytest.raises(ValidationError, match="total 不能小于本页条数"):
        SearchResultPage(items=[PaperView.from_paper(make_paper())], total=0)


def test_result_page_accepts_paged_subset() -> None:
    page = SearchResultPage(items=[PaperView.from_paper(make_paper())], total=500, partial=True)
    assert page.total == 500
    assert len(page.items) == 1
