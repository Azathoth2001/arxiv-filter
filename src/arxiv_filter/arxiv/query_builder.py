"""UI 选择 → ``search_query``（方案 §4.3 T1.3、§5.2）。

完整基础表达式（方案 §4.3）::

    (category expression)
    AND submittedDate:[YYYYMMDD0000 TO YYYYMMDD2359]
    AND (compiled keyword expression)     # keywords 为空时省略

硬规则：

* 分类、日期、关键词分别构造，**最后一次性渲染**；
* 基础分类、日期和关键词都加括号再组合，避免 Boolean 优先级歧义；
* 用户词只作为叶子节点，字段前缀由 ``scope`` 决定；不把原始字符串直接拼进 query；
* URL 编码只交给 ``httpx(params=...)`` 一次完成，**禁止预编码后再编码**；
* **不假定** ``cat:cs.*`` 之类未被官方文档确认的通配语法——「全部子类」展开成
  精确 category 的 OR 列表。

``NOT`` 与基础条件的关系是这里最容易写错的地方，见 :func:`_split_top_level`。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from urllib.parse import quote

from arxiv_filter.arxiv.query_lexer import parse_keywords
from arxiv_filter.errors import ArxivFilterError, ErrorCode
from arxiv_filter.models.query import (
    SCOPE_PREFIX,
    SORT_PARAMS,
    And,
    BuiltQuery,
    KeywordExpression,
    Not,
    Or,
    Phrase,
    SearchRequest,
    SearchScope,
    Word,
)
from arxiv_filter.taxonomy import Taxonomy, load_taxonomy, resolve_categories

#: cache key 的语义版本。任何影响上游请求含义的改动都要 +1，
#: 让旧缓存整体失效（方案 §4.5）。
QUERY_SEMANTICS_VERSION = 1

#: taxonomy alias 映射版本；alias 变化时 +1，避免旧缓存误命中。
ALIAS_CANON_VERSION = 1

#: 本地相关度权重版本；权重进 cache key（方案 §4.4）。
RANKING_WEIGHTS_VERSION = 1

#: 编码后 ``search_query`` 的安全上限。超过就拆成多个顺序 GET（方案 §4.3）。
MAX_ENCODED_QUERY_CHARS = 1800

#: 参与 cache key 的 provider 名。
PROVIDER_NAME = "arxiv_api"


def format_date_bound(value: date, *, end_of_day: bool) -> str:
    """把 UTC 自然日渲染成 arXiv 的 ``YYYYMMDDHHMM``（方案「已锁定假设」§3）。

    起止日**均包含**：开始日 ``0000``，截止日 ``2359``。
    """
    return f"{value:%Y%m%d}{'2359' if end_of_day else '0000'}"


def build_date_clause(start: date, end: date) -> str:
    """``submittedDate:[YYYYMMDD0000 TO YYYYMMDD2359]``。"""
    return (
        f"submittedDate:[{format_date_bound(start, end_of_day=False)}"
        f" TO {format_date_bound(end, end_of_day=True)}]"
    )


def build_category_clause(category_ids: list[str]) -> str:
    """单个分类直接写 ``cat:X``；多个分类展开成 ``(cat:A OR cat:B ...)``。

    不使用通配语法——``cat:cs.*`` 未被官方文档确认（方案 §4.3）。
    """
    if not category_ids:
        raise ArxivFilterError(
            ErrorCode.INVALID_SEARCH_REQUEST,
            details={"reason": "empty_category_list"},
            message_zh="没有选中任何分类，请至少选择一个分类。",
        )
    if len(category_ids) == 1:
        return f"cat:{category_ids[0]}"
    return "(" + " OR ".join(f"cat:{cid}" for cid in category_ids) + ")"


def render_keywords(node: KeywordExpression, scope: SearchScope = SearchScope.ALL) -> str:
    """把关键词 AST 渲染成 ``search_query`` 片段。

    ``Or`` 会被包进括号，避免与外层的 ``AND`` 产生优先级歧义。
    遇到 ``Not`` 直接渲染是**不允许**的——取反必须由 :func:`build_expression`
    结合基础条件处理，否则会丢掉左侧条件。
    """
    prefix = SCOPE_PREFIX[scope]

    def render(current: KeywordExpression) -> str:
        if isinstance(current, Word):
            return f"{prefix}:{current.text}"
        if isinstance(current, Phrase):
            return f'{prefix}:"{current.text}"'
        if isinstance(current, Or):
            return "(" + " OR ".join(render(operand) for operand in current.operands) + ")"
        if isinstance(current, And):
            return " AND ".join(render(operand) for operand in current.operands)
        raise ArxivFilterError(
            ErrorCode.INVALID_KEYWORD_SYNTAX,
            details={"reason": "misplaced_not"},
            message_zh=(
                "「NOT」的位置无法安全地翻译成 arXiv 的 ANDNOT，请改写表达式。"
            ),
        )

    return render(node)


def _split_top_level(
    node: KeywordExpression,
) -> tuple[list[KeywordExpression], list[KeywordExpression]]:
    """把顶层表达式拆成「肯定合取项」与「被 NOT 的项」。

    这是本模块最关键的语义处理。``NOT`` 只能编译成二元的 ``ANDNOT``，
    ``ANDNOT`` 需要左侧有一个条件，所以：

    * ``a AND NOT b``   → 肯定项 ``[a]``，否定项 ``[b]``
      → ``(base) AND (all:a ANDNOT all:b)``；
    * ``NOT a AND NOT b`` → 肯定项 ``[]``，否定项 ``[a, b]``
      → ``(base) ANDNOT all:a ANDNOT all:b``；
      这里肯定项为空，**必须**让基础条件充当 ANDNOT 的左侧，
      否则会退化成 ``all:a ANDNOT all:b``——那是完全不同的查询；
    * 顶层就是 ``NOT x`` → 肯定项 ``[]``，否定项 ``[x]``。
    """
    if isinstance(node, Not):
        return [], [node.operand]
    if isinstance(node, And):
        positives: list[KeywordExpression] = []
        negatives: list[KeywordExpression] = []
        for operand in node.operands:
            if isinstance(operand, Not):
                negatives.append(operand.operand)
            else:
                positives.append(operand)
        return positives, negatives
    return [node], []


def build_expression(
    base_clause: str,
    expression: KeywordExpression | None,
    scope: SearchScope = SearchScope.ALL,
) -> str:
    """把基础条件与关键词表达式组合成完整的 ``search_query``。

    ``base_clause`` 是已经渲染好的分类 + 日期条件（会被加括号）。
    """
    if expression is None:
        return base_clause

    positives, negatives = _split_top_level(expression)

    # 肯定项里不允许再有取反：``a OR NOT b`` 无法在不引入全集的情况下表达。
    for node in positives:
        if node.has_negation():
            raise ArxivFilterError(
                ErrorCode.INVALID_KEYWORD_SYNTAX,
                details={"reason": "not_inside_or"},
                message_zh=(
                    "「NOT」不能和 OR 混用。arXiv 的 ANDNOT 需要一个左侧条件，"
                    "请改成「A AND NOT B」这种写法。"
                ),
            )

    rendered_negatives = [render_keywords(node, scope) for node in negatives]

    if not positives:
        # 没有肯定项：让基础条件充当 ANDNOT 的左侧
        parts = [f"({base_clause})", *(f"ANDNOT {item}" for item in rendered_negatives)]
        return " ".join(parts)

    combined = " AND ".join(render_keywords(node, scope) for node in positives)
    for item in rendered_negatives:
        combined = f"{combined} ANDNOT {item}"
    return f"({base_clause}) AND ({combined})"


def build_canonical_payload(
    *,
    category_ids: list[str],
    start: date,
    end: date,
    keyword_ast: KeywordExpression | None,
    scope: SearchScope,
    sort_value: str,
    result_cap: int,
) -> dict[str, object]:
    """构造 cache key 的 canonical payload（``docs/data-schema.md`` §4）。"""
    return {
        "query_semantics_version": QUERY_SEMANTICS_VERSION,
        "provider": PROVIDER_NAME,
        "category_ids": sorted(category_ids),
        "start_utc": f"{start:%Y-%m-%d}T00:00:00Z",
        "end_utc": f"{end:%Y-%m-%d}T23:59:00Z",
        "keyword_ast": keyword_ast.canonical() if keyword_ast is not None else None,
        "scope": str(scope),
        "sort": str(sort_value),
        "result_cap": result_cap,
        "ranking_weights_version": RANKING_WEIGHTS_VERSION,
        "alias_canon_version": ALIAS_CANON_VERSION,
    }


def canonical_json(payload: dict[str, object]) -> str:
    """稳定序列化：字段顺序不影响结果，非 ASCII 原样保留。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def cache_key_for(payload: dict[str, object]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def encoded_length(expression: str) -> int:
    """``search_query`` 参数在 URL 里的编码长度。"""
    return len(quote(expression, safe=""))


def _chunk_categories(
    category_ids: list[str],
    start: date,
    end: date,
    keyword_expression: KeywordExpression | None,
    scope: SearchScope,
    budget: int,
) -> list[str]:
    """按预算把分类切成若干块，每块渲染成一个完整表达式。

    只有在**超长**时才会走到这里；单分类仍然超长时只能原样返回一块，
    由上游决定是否拒绝（现实中不会发生，因为单个分类 ID 只有几个字符）。
    """
    date_clause = build_date_clause(start, end)
    chunks: list[str] = []
    current: list[str] = []

    for category_id in category_ids:
        candidate = [*current, category_id]
        expression = build_expression(
            f"{build_category_clause(candidate)} AND {date_clause}",
            keyword_expression,
            scope,
        )
        if current and encoded_length(expression) > budget:
            chunks.append(
                build_expression(
                    f"{build_category_clause(current)} AND {date_clause}",
                    keyword_expression,
                    scope,
                )
            )
            current = [category_id]
        else:
            current = candidate

    if current:
        chunks.append(
            build_expression(
                f"{build_category_clause(current)} AND {date_clause}",
                keyword_expression,
                scope,
            )
        )
    return chunks


def build_query(
    request: SearchRequest,
    *,
    taxonomy: Taxonomy | None = None,
) -> BuiltQuery:
    """把一次 :class:`SearchRequest` 编译成可发给 arXiv 的 :class:`BuiltQuery`。

    抛出 :class:`~arxiv_filter.errors.ArxivFilterError`：

    * ``UNKNOWN_CATEGORY`` —— 分类不在白名单或不属于所选一级分类；
    * ``INVALID_KEYWORD_SYNTAX`` —— 关键词语法错误（消息带字符位置）。
    """
    table = taxonomy or load_taxonomy()

    category_ids = list(
        resolve_categories(request.group_id, request.category_id, taxonomy=table)
    )
    keyword_expression = parse_keywords(request.keywords)

    date_clause = build_date_clause(request.start_date, request.end_date)
    base_clause = f"{build_category_clause(category_ids)} AND {date_clause}"

    expression = build_expression(base_clause, keyword_expression, request.scope)

    sort_by, sort_order = SORT_PARAMS[request.sort]

    payload = build_canonical_payload(
        category_ids=category_ids,
        start=request.start_date,
        end=request.end_date,
        keyword_ast=keyword_expression,
        scope=request.scope,
        sort_value=str(request.sort),
        result_cap=request.result_cap,
    )

    split_expressions: list[str] = []
    if encoded_length(expression) > MAX_ENCODED_QUERY_CHARS:
        chunks = _chunk_categories(
            category_ids,
            request.start_date,
            request.end_date,
            keyword_expression,
            request.scope,
            MAX_ENCODED_QUERY_CHARS,
        )
        # 只有真的拆成了多块才记录；单块等于原表达式时不算拆分
        if len(chunks) > 1:
            split_expressions = chunks

    return BuiltQuery(
        category_ids=category_ids,
        expression=expression,
        sort_by=sort_by,
        sort_order=sort_order,
        canonical_json=canonical_json(payload),
        cache_key=cache_key_for(payload),
        split_expressions=split_expressions,
    )


__all__ = [
    "ALIAS_CANON_VERSION",
    "MAX_ENCODED_QUERY_CHARS",
    "PROVIDER_NAME",
    "QUERY_SEMANTICS_VERSION",
    "RANKING_WEIGHTS_VERSION",
    "build_canonical_payload",
    "build_category_clause",
    "build_date_clause",
    "build_expression",
    "build_query",
    "cache_key_for",
    "canonical_json",
    "encoded_length",
    "format_date_bound",
    "render_keywords",
]
