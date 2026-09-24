"""T1.5 本地相关度（方案 §4.4）。"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone

import pytest

from arxiv_filter.arxiv.query_lexer import parse_keywords
from arxiv_filter.arxiv.ranking import (
    DEFAULT_WEIGHTS,
    QueryTerms,
    RankingWeights,
    extract_terms,
    freshness_score,
    normalise_for_matching,
    rank_and_score,
    rank_papers,
    score_paper,
)
from arxiv_filter.models.paper import Author, Paper

UTC = timezone.utc
END = date(2025, 1, 31)


def paper(
    title: str = "Neural networks",
    summary: str = "We study neural networks.",
    authors: list[str] | None = None,
    published: date = date(2025, 1, 31),
) -> Paper:
    return Paper(
        canonical_id="2501.01234",
        versioned_id="2501.01234v1",
        version=1,
        title=title,
        authors=[Author(name=name) for name in (authors or ["A. Author"])],
        summary_raw=summary,
        published_at=datetime(published.year, published.month, published.day, tzinfo=UTC),
        updated_at=datetime(published.year, published.month, published.day, tzinfo=UTC),
        primary_category="cs.CL",
        categories=["cs.CL"],
        abs_url="https://arxiv.org/abs/2501.01234v1",
        retrieved_at=datetime(2026, 9, 24, tzinfo=UTC),
    )


# —— 词项抽取 ——


def test_extract_terms_splits_words_and_phrases() -> None:
    terms = extract_terms(parse_keywords('"large language model" transformer'))
    assert terms.phrases == ["large language model"]
    assert terms.words == ["transformer"]


def test_extract_terms_skips_negated_subtrees() -> None:
    """负词先排除（方案 §4.4）——被 NOT 的词不能给命中加分。"""
    terms = extract_terms(parse_keywords("transformer NOT survey"))
    assert "survey" not in terms.words
    assert "transformer" in terms.words


def test_extract_terms_skips_whole_negated_group() -> None:
    terms = extract_terms(parse_keywords("a AND NOT (b OR c)"))
    assert terms.words == ["a"]


def test_extract_terms_from_none() -> None:
    assert extract_terms(None).is_empty


def test_extract_terms_casefolds() -> None:
    assert extract_terms(parse_keywords("Transformer")).words == ["transformer"]


def test_extract_terms_normalises_fullwidth() -> None:
    assert extract_terms(parse_keywords("ＡＢＣ")).words == ["abc"]


# —— 新鲜度 ——


def test_freshness_is_one_on_the_end_date() -> None:
    assert freshness_score(END, END, DEFAULT_WEIGHTS) == pytest.approx(1.0)


def test_freshness_halves_after_the_half_life() -> None:
    older = date(2025, 1, 1)
    assert freshness_score(older, END, DEFAULT_WEIGHTS) == pytest.approx(0.5)


def test_freshness_decays_exponentially() -> None:
    score = freshness_score(date(2024, 12, 2), END, DEFAULT_WEIGHTS)  # 60 天前
    assert score == pytest.approx(0.25)


def test_future_published_dates_are_clamped_to_zero_days() -> None:
    """``d`` 必须是非负天数，未来日期不能得到大于 1 的新鲜度。"""
    assert freshness_score(date(2025, 6, 1), END, DEFAULT_WEIGHTS) == pytest.approx(1.0)


# —— 打分公式 ——


def test_score_matches_the_formula_exactly() -> None:
    """S = 6*Tp + 3*Tw + 2*Ap + Aw + 2U + 0.25*S_fresh。"""
    target = paper(
        title="neural networks",           # Tw = 1
        summary="neural networks again",   # Aw = 1
        authors=["Neural Author"],         # U = 1
        published=END,
    )
    breakdown = score_paper(target, QueryTerms(words=["neural"]), end_date=END)
    assert breakdown.title_words == 1
    assert breakdown.abstract_words == 1
    assert breakdown.author_words == 1
    expected = 3 * 1 + 1 * 1 + 2 * 1 + 0.25 * 1.0
    assert breakdown.total == pytest.approx(expected)


def test_phrase_hits_are_weighted_more_than_words() -> None:
    target = paper(title="large language model", summary="", published=END)
    phrase = score_paper(target, QueryTerms(phrases=["large language model"]), end_date=END)
    words = score_paper(target, QueryTerms(words=["large"]), end_date=END)
    assert phrase.total > words.total


def test_title_hits_outweigh_abstract_hits() -> None:
    in_title = paper(title="transformer", summary="nothing here", published=END)
    in_abstract = paper(title="nothing here", summary="transformer", published=END)
    terms = QueryTerms(words=["transformer"])
    assert score_paper(in_title, terms, end_date=END).total > (
        score_paper(in_abstract, terms, end_date=END).total
    )


def test_freshness_contributes_only_a_quarter_weight() -> None:
    terms = QueryTerms(words=["nomatch"])
    fresh = score_paper(paper(published=END), terms, end_date=END)
    stale = score_paper(paper(published=date(2020, 1, 1)), terms, end_date=END)
    assert fresh.total == pytest.approx(0.25)
    assert stale.total < fresh.total


def test_no_terms_scores_purely_on_freshness() -> None:
    breakdown = score_paper(paper(published=END), QueryTerms(), end_date=END)
    assert breakdown.total == pytest.approx(0.25)


def test_matching_is_case_insensitive() -> None:
    target = paper(title="TRANSFORMER", summary="", published=END)
    assert score_paper(target, QueryTerms(words=["transformer"]), end_date=END).title_words == 1


def test_matching_normalises_fullwidth_characters() -> None:
    target = paper(title="ＡＢＣ model", summary="", published=END)
    assert score_paper(target, QueryTerms(words=["abc"]), end_date=END).title_words == 1


def test_multiple_occurrences_are_counted() -> None:
    target = paper(title="net net net", summary="", published=END)
    assert score_paper(target, QueryTerms(words=["net"]), end_date=END).title_words == 3


def test_author_matching_covers_all_authors() -> None:
    target = paper(authors=["Alice Smith", "Bob Jones"], published=END)
    breakdown = score_paper(target, QueryTerms(words=["jones"]), end_date=END)
    assert breakdown.author_words == 1


def test_author_name_is_not_part_of_title_or_abstract_matching() -> None:
    target = paper(title="nothing", summary="nothing", authors=["Transformer"], published=END)
    breakdown = score_paper(target, QueryTerms(words=["transformer"]), end_date=END)
    assert breakdown.title_words == 0
    assert breakdown.abstract_words == 0
    assert breakdown.author_words == 1


def test_normalise_for_matching_does_nfkc_and_casefold() -> None:
    assert normalise_for_matching("ＡＢＣ") == "abc"
    assert normalise_for_matching("Straße") == "strasse"


# —— 排序 ——


def test_rank_papers_sorts_by_score_descending() -> None:
    strong = paper(title="transformer transformer", published=END)
    weak = paper(title="transformer", published=END)
    none = paper(title="unrelated", published=END)
    ranked = rank_papers([none, weak, strong], QueryTerms(words=["transformer"]), end_date=END)
    assert [p.title for p, _ in ranked] == [
        "transformer transformer",
        "transformer",
        "unrelated",
    ]


def test_rank_papers_is_stable_for_equal_scores() -> None:
    first = paper(title="transformer", summary="")
    second = paper(title="transformer", summary="")
    ranked = rank_papers([first, second], QueryTerms(words=["transformer"]), end_date=END)
    assert ranked[0][0] is first
    assert ranked[1][0] is second


def test_rank_and_score_uses_the_ast() -> None:
    items = rank_and_score(
        [paper(title="transformer", summary="")],
        parse_keywords("transformer"),
        end_date=END,
    )
    assert len(items) == 1
    assert items[0][1] > 0


def test_rank_and_score_ignores_negated_terms() -> None:
    target = paper(title="survey of transformers", summary="")
    with_negation = rank_and_score([target], parse_keywords("transformer NOT survey"), end_date=END)
    without = rank_and_score([target], parse_keywords("transformer"), end_date=END)
    assert with_negation[0][1] == pytest.approx(without[0][1])


# —— 权重可配置 ——


def test_custom_weights_change_the_score() -> None:
    target = paper(title="transformer", summary="", published=END)
    heavy = RankingWeights(title_word=100.0)
    light = RankingWeights(title_word=1.0)
    terms = QueryTerms(words=["transformer"])
    assert score_paper(target, terms, end_date=END, weights=heavy).total > (
        score_paper(target, terms, end_date=END, weights=light).total
    )


def test_default_weights_match_the_plan() -> None:
    assert DEFAULT_WEIGHTS.title_phrase == 6.0
    assert DEFAULT_WEIGHTS.title_word == 3.0
    assert DEFAULT_WEIGHTS.abstract_phrase == 2.0
    assert DEFAULT_WEIGHTS.abstract_word == 1.0
    assert DEFAULT_WEIGHTS.author_word == 2.0
    assert DEFAULT_WEIGHTS.freshness_weight == 0.25
    assert DEFAULT_WEIGHTS.freshness_half_life_days == 30.0


def test_weights_are_serialisable_for_the_cache_key() -> None:
    payload = DEFAULT_WEIGHTS.as_dict()
    assert payload["title_phrase"] == 6.0
    assert set(payload) == {
        "title_phrase",
        "title_word",
        "abstract_phrase",
        "abstract_word",
        "author_word",
        "freshness_weight",
        "freshness_half_life_days",
    }


def test_half_life_uses_ln2() -> None:
    weights = RankingWeights(freshness_half_life_days=10.0)
    assert freshness_score(date(2025, 1, 21), END, weights) == pytest.approx(
        math.exp(-math.log(2) * 10 / 10)
    )
