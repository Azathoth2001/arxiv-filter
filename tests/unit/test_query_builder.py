"""T1.3 查询构造（方案 §4.3、§5.2）。

核心是 **7 个 golden query**——方案 §5.2 的 UI→API 映射表逐行固定，
再加 NOT 相关的边界（这是最容易写错语义的地方）。
"""

from __future__ import annotations

from datetime import date

import pytest

from arxiv_filter.arxiv.query_builder import (
    MAX_ENCODED_QUERY_CHARS,
    build_category_clause,
    build_date_clause,
    build_expression,
    build_query,
    canonical_json,
    encoded_length,
    format_date_bound,
)
from arxiv_filter.arxiv.query_lexer import parse_keywords
from arxiv_filter.errors import ArxivFilterError, ErrorCode
from arxiv_filter.models.query import SearchRequest, SearchScope, SortMode
from arxiv_filter.taxonomy import load_taxonomy

JAN1 = date(2025, 1, 1)
JAN7 = date(2025, 1, 7)


def request(**overrides: object) -> SearchRequest:
    payload: dict[str, object] = {
        "group_id": "cs",
        "category_id": "cs.CL",
        "start_date": JAN1,
        "end_date": JAN7,
    }
    payload.update(overrides)
    return SearchRequest(**payload)  # type: ignore[arg-type]


# —— 日期 ——


def test_date_bounds_are_inclusive_utc_days() -> None:
    assert format_date_bound(JAN1, end_of_day=False) == "202501010000"
    assert format_date_bound(JAN7, end_of_day=True) == "202501072359"


def test_date_clause_matches_plan_format() -> None:
    assert build_date_clause(JAN1, JAN7) == "submittedDate:[202501010000 TO 202501072359]"


def test_single_day_range_starts_and_ends_on_the_same_day() -> None:
    assert build_date_clause(JAN1, JAN1) == "submittedDate:[202501010000 TO 202501012359]"


def test_leap_day_is_formatted() -> None:
    assert build_date_clause(date(2024, 2, 29), date(2024, 2, 29)) == (
        "submittedDate:[202402290000 TO 202402292359]"
    )


# —— 分类 ——


def test_single_category_has_no_parentheses() -> None:
    assert build_category_clause(["cs.CL"]) == "cat:cs.CL"


def test_multiple_categories_are_ored_and_parenthesised() -> None:
    assert build_category_clause(["cs.AI", "cs.CL"]) == "(cat:cs.AI OR cat:cs.CL)"


def test_empty_category_list_is_rejected() -> None:
    with pytest.raises(ArxivFilterError) as excinfo:
        build_category_clause([])
    assert excinfo.value.code is ErrorCode.INVALID_SEARCH_REQUEST


def test_no_wildcard_syntax_is_ever_emitted() -> None:
    """方案 §4.3：不假定 ``cat:cs.*`` 这类未被官方确认的通配语法。"""
    table = load_taxonomy()
    query = build_query(request(category_id=None), taxonomy=table)
    assert ".*" not in query.expression
    assert "*" not in query.expression
    # 每个叶子都写成精确的 cat: 条件
    assert query.expression.count("cat:") == len(query.category_ids)


# —— 7 个 golden query ——


def test_golden_1_category_and_date() -> None:
    query = build_query(request(group_id="cs", category_id="cs.CL"))
    assert query.expression == (
        "cat:cs.CL AND submittedDate:[202501010000 TO 202501072359]"
    )
    assert query.sort_by == "submittedDate"
    assert query.sort_order == "descending"
    assert query.category_ids == ["cs.CL"]


def test_golden_2_all_subcategories_expand_to_exact_or_list() -> None:
    table = load_taxonomy()
    query = build_query(request(group_id="cs", category_id=None), taxonomy=table)
    assert query.category_ids == list(table.leaves_of_group("cs"))
    assert query.expression.startswith("(cat:cs.AI OR cat:cs.AR OR ")
    assert query.expression.endswith("AND submittedDate:[202501010000 TO 202501072359]")
    # 别名不得出现，规范名 eess.SY 必须在
    assert "cat:cs.SY" not in query.expression
    assert "cat:eess.SY" in query.expression


def test_golden_3_scope_all_prefixes_every_leaf() -> None:
    query = build_query(request(keywords="transformer", scope=SearchScope.ALL))
    assert query.expression == (
        "(cat:cs.CL AND submittedDate:[202501010000 TO 202501072359])"
        " AND (all:transformer)"
    )


@pytest.mark.parametrize(
    ("scope", "prefix"),
    [
        (SearchScope.ALL, "all"),
        (SearchScope.TITLE, "ti"),
        (SearchScope.ABSTRACT, "abs"),
        (SearchScope.AUTHOR, "au"),
    ],
)
def test_golden_3_all_four_scopes(scope: SearchScope, prefix: str) -> None:
    query = build_query(request(keywords="transformer", scope=scope))
    assert f"({prefix}:transformer)" in query.expression


def test_golden_4_phrase_is_quoted() -> None:
    query = build_query(request(keywords='"large language model"'))
    assert " AND (all:\"large language model\")" in query.expression


def test_golden_5_and() -> None:
    query = build_query(request(keywords="A AND B"))
    assert " AND (all:A AND all:B)" in query.expression


def test_golden_6_or_is_parenthesised() -> None:
    query = build_query(request(keywords="A OR B"))
    assert " AND ((all:A OR all:B))" in query.expression


def test_golden_7_not_becomes_andnot() -> None:
    """``A NOT B`` → ``all:A ANDNOT all:B``（方案 §5.2）。"""
    query = build_query(request(keywords="A NOT B"))
    assert " AND (all:A ANDNOT all:B)" in query.expression


def test_golden_sort_modes() -> None:
    assert build_query(request(sort=SortMode.SUBMITTED_DESC)).sort_by == "submittedDate"
    assert build_query(request(sort=SortMode.RELEVANCE)).sort_by == "relevance"
    assert build_query(request(sort=SortMode.UPDATED_DESC)).sort_by == "lastUpdatedDate"
    for mode in SortMode:
        assert build_query(request(sort=mode)).sort_order == "descending"


def test_category_and_date_are_always_parenthesised_together() -> None:
    """基础条件整体加括号，避免与关键词的 Boolean 优先级混在一起。"""
    query = build_query(request(keywords="A OR B"))
    assert query.expression.startswith(
        "(cat:cs.CL AND submittedDate:[202501010000 TO 202501072359]) AND ("
    )


def test_no_keywords_omits_the_keyword_clause() -> None:
    query = build_query(request(keywords="   "))
    assert query.expression == "cat:cs.CL AND submittedDate:[202501010000 TO 202501072359]"
    assert " AND (" not in query.expression


# —— NOT 的语义边界 ——


def test_leading_not_absorbs_the_base_condition() -> None:
    """``NOT x`` 单独出现时，基础条件充当 ANDNOT 的左侧（方案 §4.3 第 3 条）。"""
    query = build_query(request(keywords="NOT survey"))
    assert query.expression == (
        "(cat:cs.CL AND submittedDate:[202501010000 TO 202501072359])"
        " ANDNOT all:survey"
    )


def test_all_negated_conjuncts_absorb_the_base_condition() -> None:
    """``NOT a AND NOT b`` 必须让基础条件当左侧。

    如果错误地渲染成 ``all:a ANDNOT all:b``，语义就完全反了——
    那是「含 a 但不含 b」，而用户要的是「既不含 a 也不含 b」。
    """
    query = build_query(request(keywords="NOT a AND NOT b"))
    assert query.expression == (
        "(cat:cs.CL AND submittedDate:[202501010000 TO 202501072359])"
        " ANDNOT all:a ANDNOT all:b"
    )
    # 错误实现会渲染成 ``all:a ANDNOT all:b``——必须以基础条件开头，
    # 而不是以第一个被否定的词开头。
    assert query.expression.startswith("(cat:cs.CL")
    assert not query.expression.startswith("all:")


def test_mixed_conjuncts_keep_the_base_outside() -> None:
    query = build_query(request(keywords="a AND NOT b"))
    assert query.expression == (
        "(cat:cs.CL AND submittedDate:[202501010000 TO 202501072359])"
        " AND (all:a ANDNOT all:b)"
    )


def test_not_of_a_group_is_expressed_with_parentheses() -> None:
    query = build_query(request(keywords="NOT (a OR b)"))
    assert query.expression.endswith("ANDNOT (all:a OR all:b)")


def test_not_inside_or_is_rejected_with_a_helpful_message() -> None:
    """``a OR NOT b`` 无法在不引入全集的情况下表达，必须明确拒绝。"""
    with pytest.raises(ArxivFilterError) as excinfo:
        build_query(request(keywords="a OR NOT b"))
    assert excinfo.value.code is ErrorCode.INVALID_KEYWORD_SYNTAX
    assert excinfo.value.details["reason"] == "not_inside_or"
    assert "AND NOT" in excinfo.value.message_zh


def test_render_keywords_refuses_a_bare_not() -> None:
    from arxiv_filter.arxiv.query_builder import render_keywords
    from arxiv_filter.models.query import Not, Word

    with pytest.raises(ArxivFilterError) as excinfo:
        render_keywords(Not(operand=Word(text="x", position=0)))
    assert excinfo.value.details["reason"] == "misplaced_not"


# —— 编码 ——


def test_expression_is_never_pre_encoded() -> None:
    """方案 §4.3 第 6 条：只把未编码表达式交给 httpx。"""
    query = build_query(request(keywords='"large language model"'))
    assert "%" not in query.expression
    assert "+" not in query.expression.replace("C++", "")


def test_chinese_keywords_stay_readable_in_the_expression() -> None:
    query = build_query(request(keywords="机器学习"))
    assert "all:机器学习" in query.expression


def test_encoded_length_reflects_url_encoding() -> None:
    assert encoded_length("cat:cs.CL") == len("cat%3Acs.CL")
    assert encoded_length("all:机器学习") > len("all:机器学习")


def test_long_expansion_is_split_into_sequential_queries() -> None:
    """方案 §4.3：编码后超过 1800 字符时拆成多个顺序 GET。"""
    table = load_taxonomy()
    query = build_query(request(group_id="cs", category_id=None), taxonomy=table)
    if encoded_length(query.expression) > MAX_ENCODED_QUERY_CHARS:
        assert query.is_split
        assert len(query.split_expressions) > 1
        for chunk in query.split_expressions:
            assert encoded_length(chunk) <= MAX_ENCODED_QUERY_CHARS
    else:  # pragma: no cover - 当前 cs 组未超限
        assert not query.is_split


def test_split_expressions_cover_every_category() -> None:
    """拆分后每个分类都必须至少出现在一个分片里，且每片都在预算内。"""
    table = load_taxonomy()
    query = build_query(request(group_id="cs", category_id=None), taxonomy=table)
    chunks = query.split_expressions or [query.expression]

    joined = " ".join(chunks)
    for category_id in query.category_ids:
        assert f"cat:{category_id}" in joined, category_id
    for chunk in chunks:
        assert encoded_length(chunk) <= MAX_ENCODED_QUERY_CHARS


def test_small_query_is_never_split() -> None:
    assert build_query(request()).split_expressions == []


# —— cache key ——


def test_cache_key_is_sha256_hex() -> None:
    query = build_query(request())
    assert len(query.cache_key) == 64
    assert all(char in "0123456789abcdef" for char in query.cache_key)


def test_cache_key_is_stable_across_calls() -> None:
    assert build_query(request()).cache_key == build_query(request()).cache_key


def test_cache_key_ignores_json_field_order() -> None:
    payload_a = {"b": 1, "a": 2}
    payload_b = {"a": 2, "b": 1}
    assert canonical_json(payload_a) == canonical_json(payload_b)


@pytest.mark.parametrize(
    "change",
    [
        {"category_id": "cs.AI"},
        {"start_date": date(2025, 1, 2)},
        {"end_date": date(2025, 1, 8)},
        {"keywords": "other"},
        {"scope": SearchScope.TITLE},
        {"sort": SortMode.RELEVANCE},
        {"result_cap": 100},
    ],
)
def test_cache_key_changes_when_upstream_request_changes(change: dict[str, object]) -> None:
    assert build_query(request(**change)).cache_key != build_query(request()).cache_key


@pytest.mark.parametrize(
    "change",
    [
        {"read_filter": "new"},
        {"read_filter": "unread"},
        {"allow_stale_cache": True},
    ],
)
def test_presentation_only_fields_do_not_change_the_cache_key(change: dict[str, object]) -> None:
    """``read_filter`` / ``allow_stale_cache`` 不改上游请求，不能进 cache key。"""
    assert build_query(request(**change)).cache_key == build_query(request()).cache_key


def test_cache_key_is_independent_of_keyword_input_formatting() -> None:
    """``a b`` 与 ``a AND b`` 是同一个上游查询，必须命中同一个缓存。"""
    assert build_query(request(keywords="a b")).cache_key == (
        build_query(request(keywords="a AND b")).cache_key
    )


def test_canonical_payload_contains_the_expected_fields() -> None:
    import json

    payload = json.loads(build_query(request(keywords="a")).canonical_json)
    assert payload["provider"] == "arxiv_api"
    assert payload["query_semantics_version"] == 1
    assert payload["start_utc"] == "2025-01-01T00:00:00Z"
    assert payload["end_utc"] == "2025-01-07T23:59:00Z"
    assert payload["keyword_ast"] == {"kind": "word", "text": "a"}
    assert payload["result_cap"] == 2000


def test_empty_keywords_produce_null_keyword_ast_in_cache_key() -> None:
    import json

    payload = json.loads(build_query(request()).canonical_json)
    assert payload["keyword_ast"] is None


# —— 错误传播 ——


def test_unknown_category_propagates_as_422() -> None:
    with pytest.raises(ArxivFilterError) as excinfo:
        build_query(request(category_id="cs.NOPE"))
    assert excinfo.value.code is ErrorCode.UNKNOWN_CATEGORY
    assert excinfo.value.http_status == 422


def test_keyword_syntax_error_propagates_with_position() -> None:
    with pytest.raises(ArxivFilterError) as excinfo:
        build_query(request(keywords="cat:cs.CL"))
    assert excinfo.value.code is ErrorCode.INVALID_KEYWORD_SYNTAX
    assert excinfo.value.details["position"] == 3


def test_build_expression_without_keywords_returns_base_unchanged() -> None:
    assert build_expression("cat:cs.CL AND submittedDate:[A TO B]", None) == (
        "cat:cs.CL AND submittedDate:[A TO B]"
    )


def test_expression_has_balanced_parentheses() -> None:
    for keywords in ("a", "a AND b", "a OR b", "a NOT b", "NOT a", "(a OR b) AND c"):
        expression = build_query(request(keywords=keywords)).expression
        assert expression.count("(") == expression.count(")"), (keywords, expression)


def test_ast_is_parsed_once_into_a_reusable_expression() -> None:
    ast = parse_keywords("a AND b")
    assert build_expression("base", ast) == "(base) AND (all:a AND all:b)"
