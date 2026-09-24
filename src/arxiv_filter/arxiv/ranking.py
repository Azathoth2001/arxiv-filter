"""可解释的本地相关度（方案 §4.4）。

.. math::

    S_{\\text{text}} = 6T_p + 3T_w + 2A_p + A_w + 2U

.. math::

    S_{\\text{fresh}} = \\exp\\left(-\\ln 2 \\cdot \\frac{d}{30}\\right),
    \\qquad S = S_{\\text{text}} + 0.25 S_{\\text{fresh}}

其中 ``T_p/T_w`` 是标题中的短语/词命中次数，``A_p/A_w`` 是摘要命中次数，
``U`` 是作者命中次数，``d`` 是距查询截止日的非负天数。

**MVP 只在发生拆分合并时才启用**；普通 relevance 直接采用上游顺序，
不用本地分数覆盖官方排序（方案 §4.4）。计数前做 NFKC + casefold；
负词先排除。权重进 cache key（``RANKING_WEIGHTS_VERSION``）。
"""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Sequence

from arxiv_filter.models.paper import Paper
from arxiv_filter.models.query import And, KeywordExpression, Not, Or, Phrase, Word

#: 30 天新鲜度半衰期内部的常数（方案 §4.4 的公式）。
_LN2 = math.log(2.0)


@dataclass(frozen=True, slots=True)
class RankingWeights:
    """相关度权重。放进 config，并因此进入 cache key。"""

    title_phrase: float = 6.0
    title_word: float = 3.0
    abstract_phrase: float = 2.0
    abstract_word: float = 1.0
    author_word: float = 2.0
    freshness_weight: float = 0.25
    freshness_half_life_days: float = 30.0

    def as_dict(self) -> dict[str, float]:
        return {
            "title_phrase": self.title_phrase,
            "title_word": self.title_word,
            "abstract_phrase": self.abstract_phrase,
            "abstract_word": self.abstract_word,
            "author_word": self.author_word,
            "freshness_weight": self.freshness_weight,
            "freshness_half_life_days": self.freshness_half_life_days,
        }


DEFAULT_WEIGHTS = RankingWeights()


@dataclass(slots=True)
class QueryTerms:
    """从关键词 AST 抽出的检索词；**已排除被 NOT 的项**。"""

    phrases: list[str] = field(default_factory=list)
    words: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.phrases and not self.words


@dataclass(slots=True)
class ScoreBreakdown:
    """可解释的打分明细——UI 可以据此说明「为什么这条排在前面」。"""

    title_phrases: int = 0
    title_words: int = 0
    abstract_phrases: int = 0
    abstract_words: int = 0
    author_words: int = 0
    freshness: float = 0.0
    total: float = 0.0


def normalise_for_matching(text: str) -> str:
    """NFKC + casefold（方案 §4.4：计数前做 NFKC + casefold）。"""
    return unicodedata.normalize("NFKC", text).casefold()


def extract_terms(expression: KeywordExpression | None) -> QueryTerms:
    """从 AST 抽取检索词，跳过所有被 ``NOT`` 的子树。"""
    terms = QueryTerms()
    if expression is None:
        return terms

    def walk(node: KeywordExpression, negated: bool) -> None:
        if isinstance(node, Not):
            walk(node.operand, not negated)
            return
        if isinstance(node, (Word, Phrase)):
            if negated:
                return
            text = normalise_for_matching(node.text)
            if isinstance(node, Phrase):
                terms.phrases.append(text)
            else:
                terms.words.append(text)
            return
        if isinstance(node, (And, Or)):
            for operand in node.operands:
                walk(operand, negated)

    walk(expression, False)
    return terms


def _count_occurrences(haystack: str, needle: str) -> int:
    if not needle:
        return 0
    return haystack.count(needle)


def freshness_score(published: date, end_date: date, weights: RankingWeights) -> float:
    """``S_fresh``：距查询截止日的**非负**天数，指数衰减。"""
    days = (end_date - published).days
    if days < 0:
        days = 0
    half_life = weights.freshness_half_life_days or 1.0
    return math.exp(-_LN2 * days / half_life)


def score_paper(
    paper: Paper,
    terms: QueryTerms,
    *,
    end_date: date,
    weights: RankingWeights = DEFAULT_WEIGHTS,
) -> ScoreBreakdown:
    """计算一篇论文的相关度明细。"""
    title = normalise_for_matching(paper.title)
    abstract = normalise_for_matching(paper.summary_raw)
    authors = normalise_for_matching(" ".join(author.name for author in paper.authors))

    breakdown = ScoreBreakdown(
        title_phrases=sum(_count_occurrences(title, p) for p in terms.phrases),
        title_words=sum(_count_occurrences(title, w) for w in terms.words),
        abstract_phrases=sum(_count_occurrences(abstract, p) for p in terms.phrases),
        abstract_words=sum(_count_occurrences(abstract, w) for w in terms.words),
        author_words=sum(_count_occurrences(authors, w) for w in terms.words),
    )

    text_score = (
        weights.title_phrase * breakdown.title_phrases
        + weights.title_word * breakdown.title_words
        + weights.abstract_phrase * breakdown.abstract_phrases
        + weights.abstract_word * breakdown.abstract_words
        + weights.author_word * breakdown.author_words
    )
    breakdown.freshness = freshness_score(paper.published_at.date(), end_date, weights)
    breakdown.total = text_score + weights.freshness_weight * breakdown.freshness
    return breakdown


def rank_papers(
    papers: Iterable[Paper],
    terms: QueryTerms,
    *,
    end_date: date,
    weights: RankingWeights = DEFAULT_WEIGHTS,
) -> list[tuple[Paper, ScoreBreakdown]]:
    """按本地相关度降序排列；同分保持输入顺序（稳定排序）。"""
    scored = [(paper, score_paper(paper, terms, end_date=end_date, weights=weights)) for paper in papers]
    return sorted(scored, key=lambda item: -item[1].total)


def rank_and_score(
    papers: Sequence[Paper],
    expression: KeywordExpression | None,
    *,
    end_date: date,
    weights: RankingWeights = DEFAULT_WEIGHTS,
) -> list[tuple[Paper, float]]:
    """便捷入口：直接从关键词 AST 出发，返回 ``(paper, score)`` 列表。"""
    terms = extract_terms(expression)
    return [
        (paper, breakdown.total)
        for paper, breakdown in rank_papers(papers, terms, end_date=end_date, weights=weights)
    ]


__all__ = [
    "DEFAULT_WEIGHTS",
    "QueryTerms",
    "RankingWeights",
    "ScoreBreakdown",
    "extract_terms",
    "freshness_score",
    "normalise_for_matching",
    "rank_and_score",
    "rank_papers",
    "score_paper",
]
