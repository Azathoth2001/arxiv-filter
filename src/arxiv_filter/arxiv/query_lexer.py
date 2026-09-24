"""受限关键词词法与语法分析（方案 §4.3 T1.2）。

实现的文法::

    expr       := or_expr
    or_expr    := and_expr (OR and_expr)*
    and_expr   := unary ((AND | 隐式空白) unary)*
    unary      := NOT? primary
    primary    := WORD | "PHRASE" | "(" expr ")"

设计要点（全部来自方案 §4.3）：

1. 输入做 Unicode NFKC、去控制字符、压缩外围空白；原始输入仍保存在历史里。
   **NFKC 逐个 token 做**，不整体做——否则规范化改变长度后，报错位置就对不上
   用户看到的字符了。
2. 独立 token 的 ``AND/OR/NOT`` 不区分大小写；要搜索单词 “and” 就写成 ``"and"``。
3. ``NOT`` 编译为 arXiv 的二元 ``ANDNOT``。前导 ``NOT x`` 由基础条件承接，
   得到 ``(base) ANDNOT field:x``；没有分类/日期基础条件时拒绝。
4. 未闭引号、括号深度 > 8、token 数 > 50、输入 > 500 字符、含未支持的
   ``: [ ] \\`` 都在本地拒绝并标出位置。
5. 用户词只作为叶子节点；字段前缀由 ``scope`` 决定。**不把原始字符串直接拼进 query。**
6. 中文、空格、引号只在 AST renderer 中处理；URL 编码只交给 ``httpx(params=...)``
   一次完成。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import StrEnum

from arxiv_filter.errors import ArxivFilterError, ErrorCode
from arxiv_filter.models.query import (
    And,
    KeywordExpression,
    KeywordNode,
    Not,
    Or,
    Phrase,
    Word,
)

#: 限制值（方案 §4.3 第 4 条）。
MAX_INPUT_CHARS = 500
MAX_TOKENS = 50
MAX_PAREN_DEPTH = 8

#: 本地直接拒绝的字符：它们能让用户注入字段前缀或日期区间
#: （``cat:cs.CL``、``submittedDate:[...]``），或破坏转义规则。
#: 注意 ``"`` 和 ``()`` **不在**这里——它们是结构字符，是短语与分组的定界符。
FORBIDDEN_CHARS = frozenset(":[]\\")

#: 结构性字符：在裸词里出现即结束该词。
STRUCTURAL_CHARS = frozenset('()"')

#: 控制字符（含 DEL）；制表/换行/回车由 ``str.split()`` 当空白处理，不在其列。
_CONTROL_CHARS = frozenset(
    chr(code) for code in (*range(0x00, 0x09), 0x0B, 0x0C, *range(0x0E, 0x20), 0x7F)
)

#: 裸词的终止字符：遇到其中任何一个就结束当前词，交回外层循环处理。
#: 必须包含控制字符与禁用字符，否则 ``a\x00b`` / ``cat:cs.CL`` 会被整段吃进一个词，
#: 报错位置和原因都会失真。
_WORD_STOP = STRUCTURAL_CHARS | FORBIDDEN_CHARS | _CONTROL_CHARS


class TokenKind(StrEnum):
    """token 种类。"""

    WORD = "word"
    PHRASE = "phrase"
    AND = "and"
    OR = "or"
    NOT = "not"
    LPAREN = "lparen"
    RPAREN = "rparen"


#: 运算符关键字 → token 种类。只有**完整的裸词**才算运算符。
_OPERATORS: dict[str, TokenKind] = {
    "AND": TokenKind.AND,
    "OR": TokenKind.OR,
    "NOT": TokenKind.NOT,
}

#: 能作为一个 unary 起点的 token 种类——用于判定隐式 AND。
_UNARY_STARTS = frozenset(
    {TokenKind.WORD, TokenKind.PHRASE, TokenKind.LPAREN, TokenKind.NOT}
)


@dataclass(frozen=True, slots=True)
class Token:
    """一个 token。``position`` 是它在**原始输入**里的 0 基字符偏移。"""

    kind: TokenKind
    text: str
    position: int


def _syntax_error(message_zh: str, position: int, raw: str, reason: str) -> ArxivFilterError:
    """构造带字符位置与可视化指针的中文语法错误。"""
    start = max(0, position - 20)
    snippet = raw[start : position + 20]
    indicator = " " * (position - start) + "^"
    return ArxivFilterError(
        ErrorCode.INVALID_KEYWORD_SYNTAX,
        details={"position": position, "reason": reason, "snippet": snippet},
        message_zh=(
            f"{message_zh}\n"
            f"第 {position + 1} 个字符处：\n"
            f"    {snippet}\n"
            f"    {indicator}"
        ),
    )


def _normalise_token_text(text: str, position: int, raw: str) -> str:
    """校验并归一化单个 token 的文字。

    控制字符在这里**报错而不是静默删除**：悄悄改掉用户要搜的词，比给出一条明确的
    错误更糟。方案 §4.3 第 1 条的「去控制字符」保留给 NFKC 与外围空白处理。

    归一化**之后**还要再检查一次禁用字符：全角冒号 ``：``（U+FF1A）会被 NFKC
    变成 ``:``，全角引号 ``＂`` 会变成 ``"``。只检查原始输入会漏掉这条注入路径。
    """
    for offset, char in enumerate(text):
        if char in _CONTROL_CHARS:
            raise _syntax_error(
                "关键词里有看不见的控制字符，请删掉它。",
                position + offset,
                raw,
                "control_char",
            )

    normalised = unicodedata.normalize("NFKC", text)
    for offset, char in enumerate(normalised):
        if char in FORBIDDEN_CHARS:
            raise _syntax_error(
                f"关键词里不能包含「{char}」这个字符，请去掉它或改成普通文字。",
                position + offset,
                raw,
                "forbidden_char_after_normalisation",
            )
        if char in STRUCTURAL_CHARS:
            raise _syntax_error(
                f"关键词里不能包含「{char}」这个字符（全角符号会被自动转成半角）。",
                position + offset,
                raw,
                "structural_char_after_normalisation",
            )
    return normalised


def tokenize(raw: str) -> list[Token]:
    """把原始输入切成 token 列表（方案 §4.3 T1.2）。

    位置始终指向**原始输入**，因为 NFKC 是按 token 做的。
    """
    tokens: list[Token] = []
    index = 0
    length = len(raw)

    while index < length:
        char = raw[index]

        if char.isspace():
            index += 1
            continue

        if char in _CONTROL_CHARS:
            raise _syntax_error(
                "关键词里有看不见的控制字符，请删掉它。", index, raw, "control_char"
            )

        if char in FORBIDDEN_CHARS:
            raise _syntax_error(
                f"关键词里不能包含「{char}」这个字符；"
                "本工具不允许直接写字段前缀或日期区间。",
                index,
                raw,
                "forbidden_char",
            )

        if char == "(":
            tokens.append(Token(TokenKind.LPAREN, "(", index))
            index += 1
            continue

        if char == ")":
            tokens.append(Token(TokenKind.RPAREN, ")", index))
            index += 1
            continue

        if char == '"':
            start = index
            index += 1
            buffer: list[str] = []
            while index < length and raw[index] != '"':
                buffer.append(raw[index])
                index += 1
            if index >= length:
                raise _syntax_error(
                    "双引号没有闭合，请在短语末尾补上另一个双引号。",
                    start,
                    raw,
                    "unterminated_phrase",
                )
            index += 1  # 吃掉收尾引号
            text = _normalise_token_text("".join(buffer), start + 1, raw).strip()
            if not text:
                raise _syntax_error(
                    "引号里是空的，请写上要搜索的短语。", start, raw, "empty_phrase"
                )
            tokens.append(Token(TokenKind.PHRASE, text, start))
            continue

        # 裸词：读到空白或终止字符为止
        start = index
        buffer = []
        while (
            index < length
            and not raw[index].isspace()
            and raw[index] not in _WORD_STOP
        ):
            buffer.append(raw[index])
            index += 1

        if not buffer:  # pragma: no cover - 终止字符都由外层循环先行处理
            raise _syntax_error("无法识别的字符。", start, raw, "unrecognised_char")

        text = _normalise_token_text("".join(buffer), start, raw)
        if not text:
            raise _syntax_error(
                "这里只有会被忽略的字符，请写上真正要搜索的词。", start, raw, "empty_word"
            )

        kind = _OPERATORS.get(text.upper(), TokenKind.WORD)
        tokens.append(Token(kind, text, start))
        # 运算符保留原始大小写以外的东西没有意义，统一成大写便于渲染
        if kind is not TokenKind.WORD:
            tokens[-1] = Token(kind, text.upper(), start)

    return tokens


class _Parser:
    """递归下降解析器；深度限制在进入括号时强制执行。"""

    __slots__ = ("_depth", "_index", "_max_depth", "_raw", "_tokens")

    def __init__(self, tokens: list[Token], raw: str, max_depth: int = MAX_PAREN_DEPTH) -> None:
        self._tokens = tokens
        self._raw = raw
        self._max_depth = max_depth
        self._index = 0
        self._depth = 0

    # —— token 游标 ——

    def _peek(self) -> Token | None:
        return self._tokens[self._index] if self._index < len(self._tokens) else None

    def _advance(self) -> Token:
        token = self._tokens[self._index]
        self._index += 1
        return token

    def _previous(self) -> Token | None:
        return self._tokens[self._index - 1] if self._index > 0 else None

    # —— 文法 ——

    def parse(self) -> KeywordExpression:
        expression = self._parse_or()
        leftover = self._peek()
        if leftover is not None:
            if leftover.kind is TokenKind.RPAREN:
                raise _syntax_error(
                    "这里多了一个右括号，请删掉它或补上对应的左括号。",
                    leftover.position,
                    self._raw,
                    "unbalanced_paren",
                )
            raise _syntax_error(
                f"「{leftover.text}」前面缺少要搜索的内容。",
                leftover.position,
                self._raw,
                "unexpected_token",
            )
        return expression

    def _parse_or(self) -> KeywordExpression:
        operands = [self._parse_and()]
        while (token := self._peek()) is not None and token.kind is TokenKind.OR:
            self._advance()
            operands.append(self._parse_and())
        return operands[0] if len(operands) == 1 else Or(operands=operands)

    def _parse_and(self) -> KeywordExpression:
        operands = [self._parse_unary()]
        while True:
            token = self._peek()
            if token is None or token.kind in (TokenKind.OR, TokenKind.RPAREN):
                break
            if token.kind is TokenKind.AND:
                self._advance()
                operands.append(self._parse_unary())
            elif token.kind in _UNARY_STARTS:
                # 隐式 AND：``a b`` 等价于 ``a AND b``
                operands.append(self._parse_unary())
            else:  # pragma: no cover - 所有种类都已覆盖
                raise _syntax_error(
                    f"「{token.text}」的位置不对，请检查 AND / OR / NOT 的用法。",
                    token.position,
                    self._raw,
                    "unexpected_token",
                )
        return operands[0] if len(operands) == 1 else And(operands=operands)

    def _parse_unary(self) -> KeywordExpression:
        token = self._peek()
        if token is None:
            raise _syntax_error(
                "表达式在这里就结束了，后面还缺一个词。",
                len(self._raw),
                self._raw,
                "unexpected_end",
            )

        if token.kind is TokenKind.NOT:
            self._advance()
            operand = self._parse_unary()
            if isinstance(operand, Not):
                raise _syntax_error(
                    "「NOT」不能连续出现两次，请改写表达式。",
                    token.position,
                    self._raw,
                    "double_negation",
                )
            return Not(operand=operand)

        if token.kind is TokenKind.LPAREN:
            if self._depth + 1 > self._max_depth:
                raise _syntax_error(
                    f"括号嵌套太深（最多 {self._max_depth} 层），请简化表达式。",
                    token.position,
                    self._raw,
                    "too_deep",
                )
            self._advance()
            self._depth += 1
            try:
                inner = self._parse_or()
            finally:
                self._depth -= 1

            closing = self._peek()
            if closing is None or closing.kind is not TokenKind.RPAREN:
                raise _syntax_error(
                    "左括号没有对应的右括号，请补上。",
                    token.position,
                    self._raw,
                    "unbalanced_paren",
                )
            self._advance()
            return inner

        if token.kind is TokenKind.RPAREN:
            previous = self._previous()
            if previous is not None and previous.kind is TokenKind.LPAREN:
                raise _syntax_error(
                    "括号里是空的，请写上要搜索的内容。",
                    token.position,
                    self._raw,
                    "empty_group",
                )
            raise _syntax_error(
                "这里多了一个右括号，请删掉它或补上对应的左括号。",
                token.position,
                self._raw,
                "unbalanced_paren",
            )

        if token.kind in (TokenKind.AND, TokenKind.OR):
            raise _syntax_error(
                f"「{token.text}」前面缺少要搜索的词。",
                token.position,
                self._raw,
                "missing_operand",
            )

        self._advance()
        if token.kind is TokenKind.PHRASE:
            return Phrase(text=token.text, position=token.position)
        return Word(text=token.text, position=token.position)


def parse_keywords(
    raw: str,
    *,
    max_chars: int = MAX_INPUT_CHARS,
    max_tokens: int = MAX_TOKENS,
    max_depth: int = MAX_PAREN_DEPTH,
) -> KeywordExpression | None:
    """把用户输入解析成关键词 AST。

    返回 ``None`` 表示「没有关键词」——空输入或纯空白，调用方应当省略关键词条件。

    抛出 :class:`~arxiv_filter.errors.ArxivFilterError`
    （``code=INVALID_KEYWORD_SYNTAX``），消息里带中文说明与字符位置。
    """
    if len(raw) > max_chars:
        raise _syntax_error(
            f"关键词太长了（{len(raw)} 个字符，最多 {max_chars} 个），请精简一下。",
            max_chars,
            raw,
            "too_long",
        )

    tokens = tokenize(raw)
    if not tokens:
        return None

    if len(tokens) > max_tokens:
        raise _syntax_error(
            f"关键词太复杂了（{len(tokens)} 个片段，最多 {max_tokens} 个），请精简一下。",
            tokens[max_tokens].position,
            raw,
            "too_many_tokens",
        )

    return _Parser(tokens, raw, max_depth).parse()


def node_depth(node: KeywordNode) -> int:
    """AST 的最大嵌套深度（供测试与诊断使用）。"""
    if isinstance(node, (Word, Phrase)):
        return 1
    if isinstance(node, Not):
        return node_depth(node.operand) + 1
    return 1 + max(node_depth(operand) for operand in node.operands)  # type: ignore[attr-defined]


def leaf_count(node: KeywordNode) -> int:
    """叶子（词与短语）总数。"""
    return len(node.leaves())


__all__ = [
    "FORBIDDEN_CHARS",
    "MAX_INPUT_CHARS",
    "MAX_PAREN_DEPTH",
    "MAX_TOKENS",
    "STRUCTURAL_CHARS",
    "Token",
    "TokenKind",
    "leaf_count",
    "node_depth",
    "parse_keywords",
    "tokenize",
]
