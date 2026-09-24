"""T1.2 受限关键词 lexer/parser（方案 §4.3）。

覆盖：词、中文、短语、嵌套括号、大小写运算符、隐式 AND、前导 NOT、
未闭引号、非法字符、深度 / token / 长度限制，以及错误位置。
"""

from __future__ import annotations

import pytest

from arxiv_filter.arxiv.query_lexer import (
    MAX_INPUT_CHARS,
    MAX_PAREN_DEPTH,
    MAX_TOKENS,
    TokenKind,
    leaf_count,
    node_depth,
    parse_keywords,
    tokenize,
)
from arxiv_filter.errors import ArxivFilterError, ErrorCode
from arxiv_filter.models.query import And, Not, Or, Phrase, Word


def assert_syntax_error(raw: str, reason: str, *, position: int | None = None) -> ArxivFilterError:
    """断言解析失败，并检查错误码、原因与（可选的）位置。"""
    with pytest.raises(ArxivFilterError) as excinfo:
        parse_keywords(raw)
    error = excinfo.value
    assert error.code is ErrorCode.INVALID_KEYWORD_SYNTAX
    assert error.details["reason"] == reason, error.details
    if position is not None:
        assert error.details["position"] == position, error.details
    return error


# —— 词法 ——


def test_single_word() -> None:
    ast = parse_keywords("transformer")
    assert ast == Word(text="transformer", position=0)


def test_empty_input_means_no_keywords() -> None:
    assert parse_keywords("") is None
    assert parse_keywords("   \t\n ") is None


def test_phrase_captures_internal_spaces() -> None:
    ast = parse_keywords('"large language model"')
    assert ast == Phrase(text="large language model", position=0)


def test_chinese_word() -> None:
    ast = parse_keywords("机器学习")
    assert ast == Word(text="机器学习", position=0)


def test_chinese_phrase_with_spaces() -> None:
    ast = parse_keywords('"图 神经网络"')
    assert ast == Phrase(text="图 神经网络", position=0)


def test_positions_point_into_the_original_input() -> None:
    ast = parse_keywords("abc def")
    assert ast == And(
        operands=[Word(text="abc", position=0), Word(text="def", position=4)]
    )


def test_operators_are_case_insensitive() -> None:
    for text in ("a and b", "a AND b", "a And b", "a aNd b"):
        ast = parse_keywords(text)
        assert ast == And(operands=[Word(text="a", position=0), Word(text="b", position=len(text) - 1)]), text


def test_quoted_operator_is_a_plain_word() -> None:
    """要搜索单词 “and” 就写成 ``"and"``（方案 §4.3 第 2 条）。"""
    ast = parse_keywords('"and"')
    assert ast == Phrase(text="and", position=0)


def test_tokenize_reports_positions() -> None:
    tokens = tokenize('a AND "b c"')
    assert [(t.kind, t.text, t.position) for t in tokens] == [
        (TokenKind.WORD, "a", 0),
        (TokenKind.AND, "AND", 2),
        (TokenKind.PHRASE, "b c", 6),
    ]


def test_control_characters_are_rejected() -> None:
    assert_syntax_error("a\x00b", "control_char", position=1)


# —— 布尔运算 ——


def test_explicit_and() -> None:
    ast = parse_keywords("a AND b")
    assert isinstance(ast, And)
    assert [leaf.text for leaf in ast.leaves()] == ["a", "b"]


def test_implicit_and_from_whitespace() -> None:
    """``a b`` 等价于 ``a AND b``。

    比较 ``canonical()``（不含字符位置）——位置本来就应当不同，它指向用户输入。
    """
    assert parse_keywords("a b").canonical() == parse_keywords("a AND b").canonical()  # type: ignore[union-attr]


def test_chain_of_implicit_ands_flattens() -> None:
    ast = parse_keywords("a b c")
    assert isinstance(ast, And)
    assert len(ast.operands) == 3


def test_or_has_lower_precedence_than_and() -> None:
    """``a AND b OR c`` 应当是 ``(a AND b) OR c``。"""
    ast = parse_keywords("a AND b OR c")
    assert isinstance(ast, Or)
    assert isinstance(ast.operands[0], And)
    assert ast.operands[1] == Word(text="c", position=11)


def test_parentheses_override_precedence() -> None:
    ast = parse_keywords("a AND (b OR c)")
    assert isinstance(ast, And)
    assert isinstance(ast.operands[1], Or)


def test_nested_parentheses() -> None:
    ast = parse_keywords("((a OR b) AND (c OR d))")
    assert isinstance(ast, And)
    assert all(isinstance(operand, Or) for operand in ast.operands)


def test_binary_not_becomes_andnot_operand() -> None:
    """``A NOT B`` 解析成 ``And(A, Not(B))``，渲染时变成 ANDNOT。"""
    ast = parse_keywords("a NOT b")
    assert isinstance(ast, And)
    assert isinstance(ast.operands[1], Not)
    assert ast.operands[1].operand == Word(text="b", position=6)


def test_explicit_and_with_not_is_the_same_as_implicit() -> None:
    assert (
        parse_keywords("a AND NOT b").canonical()  # type: ignore[union-attr]
        == parse_keywords("a NOT b").canonical()  # type: ignore[union-attr]
    )


def test_leading_not_is_preserved() -> None:
    ast = parse_keywords("NOT survey")
    assert isinstance(ast, Not)
    assert ast.operand == Word(text="survey", position=4)


def test_leading_not_inside_parentheses() -> None:
    ast = parse_keywords("(NOT survey)")
    assert isinstance(ast, Not)


def test_has_negation_flag() -> None:
    assert parse_keywords("NOT x").has_negation() is True  # type: ignore[union-attr]
    assert parse_keywords("x").has_negation() is False  # type: ignore[union-attr]
    assert parse_keywords("a OR b").has_negation() is False  # type: ignore[union-attr]


# —— 错误与位置 ——


def test_unterminated_quote() -> None:
    error = assert_syntax_error('abc "def', "unterminated_phrase", position=4)
    assert "双引号没有闭合" in error.message_zh


def test_empty_phrase() -> None:
    assert_syntax_error('a ""', "empty_phrase", position=2)


def test_unbalanced_open_paren() -> None:
    assert_syntax_error("(a AND b", "unbalanced_paren", position=0)


def test_unbalanced_close_paren() -> None:
    assert_syntax_error("a AND b)", "unbalanced_paren", position=7)


def test_empty_group() -> None:
    assert_syntax_error("a AND ()", "empty_group", position=7)


def test_trailing_operator() -> None:
    assert_syntax_error("a AND", "unexpected_end")


def test_leading_operator() -> None:
    assert_syntax_error("AND b", "missing_operand", position=0)


def test_operator_only_input() -> None:
    assert_syntax_error("OR", "missing_operand", position=0)


def test_double_negation() -> None:
    assert_syntax_error("NOT NOT a", "double_negation", position=0)


@pytest.mark.parametrize("char", [":", "[", "]", "\\"])
def test_forbidden_characters_are_rejected(char: str) -> None:
    """字段前缀与日期区间注入必须在本地被拒绝（方案 §4.3 第 4 条）。"""
    error = assert_syntax_error(f"cat{char}cs.CL", "forbidden_char", position=3)
    assert char in error.message_zh


def test_field_prefix_injection_through_phrase() -> None:
    assert_syntax_error('"cat:cs.CL"', "forbidden_char_after_normalisation")


@pytest.mark.parametrize(
    ("raw", "position"),
    [("ｃａｔ：cs", 3), ("a＂b＂c", 1)],
)
def test_fullwidth_lookalikes_are_caught_after_nfkc(raw: str, position: int) -> None:
    """全角冒号 / 引号被 NFKC 折成半角后必须仍然被拦住。

    只检查原始输入会漏掉这条注入路径——``：``(U+FF1A) 会变成 ``:``。
    """
    with pytest.raises(ArxivFilterError) as excinfo:
        parse_keywords(raw)
    assert excinfo.value.code is ErrorCode.INVALID_KEYWORD_SYNTAX
    assert excinfo.value.details["reason"] in {
        "forbidden_char",
        "forbidden_char_after_normalisation",
        "structural_char_after_normalisation",
    }


def test_error_message_contains_a_caret_pointer() -> None:
    error = assert_syntax_error("abc : def", "forbidden_char", position=4)
    assert "^" in error.message_zh
    assert "第 5 个字符处" in error.message_zh


def test_error_is_422_and_not_retryable() -> None:
    error = assert_syntax_error("cat:cs.CL", "forbidden_char")
    assert error.http_status == 422
    assert error.retryable is False


# —— 限制 ——


def test_input_length_limit() -> None:
    error = assert_syntax_error("a" * (MAX_INPUT_CHARS + 1), "too_long")
    assert str(MAX_INPUT_CHARS) in error.message_zh


def test_input_at_the_length_limit_is_accepted() -> None:
    # "a b c ..." 形式可以在 500 字符内塞进很多 token，所以这里用单词数控制
    raw = "a" * MAX_INPUT_CHARS
    assert parse_keywords(raw) == Word(text=raw, position=0)


def test_token_count_limit() -> None:
    raw = " ".join(["w"] * (MAX_TOKENS + 1))
    assert_syntax_error(raw, "too_many_tokens")


def test_token_count_at_the_limit_is_accepted() -> None:
    raw = " ".join(["w"] * MAX_TOKENS)
    ast = parse_keywords(raw)
    assert ast is not None
    assert leaf_count(ast) == MAX_TOKENS


def test_parenthesis_depth_limit() -> None:
    raw = "(" * (MAX_PAREN_DEPTH + 1) + "a" + ")" * (MAX_PAREN_DEPTH + 1)
    assert_syntax_error(raw, "too_deep")


def test_parenthesis_depth_at_the_limit_is_accepted() -> None:
    raw = "(" * MAX_PAREN_DEPTH + "a" + ")" * MAX_PAREN_DEPTH
    ast = parse_keywords(raw)
    assert ast == Word(text="a", position=MAX_PAREN_DEPTH)
    assert node_depth(ast) == 1  # 多余括号被折叠掉，不产生无意义的 AST 深度


def test_custom_limits_are_honoured() -> None:
    with pytest.raises(ArxivFilterError):
        parse_keywords("(a)", max_depth=0)


# —— 注入与安全 ——


@pytest.mark.parametrize(
    "raw",
    [
        "submittedDate:[202401010000 TO 202401072359]",
        "cat:cs.CL",
        "ti:transformer",
        "a ANDNOT b",
    ],
)
def test_arxiv_operators_cannot_be_injected(raw: str) -> None:
    """用户不能自己写字段前缀或 ANDNOT——它们只能由 builder 添加。

    ``ANDNOT`` 会被当成普通词（它是合法的搜索词），但字段前缀与日期区间会被拒绝。
    """
    if raw == "a ANDNOT b":
        ast = parse_keywords(raw)
        from arxiv_filter.models.query import And

        assert isinstance(ast, And)
        assert ast.operands[1] == Word(text="ANDNOT", position=2)
    else:
        with pytest.raises(ArxivFilterError) as excinfo:
            parse_keywords(raw)
        assert excinfo.value.code is ErrorCode.INVALID_KEYWORD_SYNTAX


def test_semicolons_and_quotes_inside_words() -> None:
    """引号是结构字符：``don"t x"`` 被切成 词+短语 的隐式 AND，不会注入。"""
    ast = parse_keywords('don"t x"')
    assert isinstance(ast, And)
    assert ast.operands[0] == Word(text="don", position=0)
    # Phrase 的 position 指向**开引号**的位置
    assert ast.operands[1] == Phrase(text="t x", position=3)


def test_special_literals_survive_as_words() -> None:
    """``C++`` / ``state-of-the-art`` 不含保留字符，应原样成为词。"""
    ast = parse_keywords("C++ state-of-the-art")
    assert isinstance(ast, And)
    assert [leaf.text for leaf in ast.leaves()] == ["C++", "state-of-the-art"]


def test_nfkc_folds_fullwidth_letters() -> None:
    """全角字母被折成半角，方便用户从别处粘贴。"""
    ast = parse_keywords("ＡＢＣ")
    assert ast == Word(text="ABC", position=0)


def test_whitespace_is_normalised_but_positions_stay_original() -> None:
    """位置始终指向**原始输入**（含制表与换行），不是压缩空白之后的文本。"""
    raw = "a\t\tAND\n\nb"
    ast = parse_keywords(raw)
    assert isinstance(ast, And)
    assert ast.operands[1].position == raw.index("b") == 8
