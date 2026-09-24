"""T1.5 ID 归一化（方案 §4.4）。"""

from __future__ import annotations

import pytest

from arxiv_filter.arxiv.ids import (
    InvalidArxivId,
    canonicalize_arxiv_id,
    is_valid_canonical_id,
    split_versioned_id,
    strip_wrapper,
    versioned_id_for,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2501.01234", ("2501.01234", None)),
        ("2501.01234v2", ("2501.01234", 2)),
        ("2501.01234v10", ("2501.01234", 10)),
        ("1501.00001v1", ("1501.00001", 1)),
        ("hep-th/9901001", ("hep-th/9901001", None)),
        ("hep-th/9901001v2", ("hep-th/9901001", 2)),
        ("math.GT/0309136v1", ("math.GT/0309136", 1)),
    ],
)
def test_plain_ids(raw: str, expected: tuple[str, int | None]) -> None:
    assert canonicalize_arxiv_id(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "http://arxiv.org/abs/2501.01234v2",
        "https://arxiv.org/abs/2501.01234v2",
        "https://www.arxiv.org/abs/2501.01234v2",
        "https://arxiv.org/pdf/2501.01234v2",
        "https://arxiv.org/pdf/2501.01234v2.pdf",
        "arxiv:2501.01234v2",
        "arXiv:2501.01234v2",
        "/abs/2501.01234v2",
        "abs/2501.01234v2",
        "2501.01234v2",
    ],
)
def test_url_and_prefix_variants_normalise_to_the_same_id(raw: str) -> None:
    assert canonicalize_arxiv_id(raw) == ("2501.01234", 2)


def test_query_string_and_fragment_are_dropped() -> None:
    assert canonicalize_arxiv_id("https://arxiv.org/abs/2501.01234v2?context=cs#top") == (
        "2501.01234",
        2,
    )


def test_legacy_id_slash_is_preserved() -> None:
    """方案 §4.4 第 3 条：旧式斜杠必须保留。"""
    canonical, _ = canonicalize_arxiv_id("https://arxiv.org/abs/hep-th/9901001v3")
    assert "/" in canonical
    assert canonical == "hep-th/9901001"


def test_surrounding_whitespace_is_ignored() -> None:
    assert canonicalize_arxiv_id("  2501.01234v1  ") == ("2501.01234", 1)


def test_version_must_be_at_the_end() -> None:
    """``v2`` 出现在中间时不剥离——只从末尾识别版本号。"""
    with pytest.raises(InvalidArxivId):
        canonicalize_arxiv_id("2501.01234v2extra")


def test_v0_is_not_a_version() -> None:
    """``v0`` 不是合法版本号，按方案只接受 ``v[1-9][0-9]*``。"""
    with pytest.raises(InvalidArxivId):
        canonicalize_arxiv_id("2501.01234v0")


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "not-an-id",
        "2501",
        "2501.123",          # 流水号只有 3 位
        "2501.123456",       # 流水号 6 位
        "2513.01234",        # 月份 13
        "2500.01234",        # 月份 00
        "hep-th/990100",     # 旧式流水号必须 7 位
        "hep-th/9913001",    # 旧式月份 13
        "2501.123",          # 流水号只有 3 位
    ],
)
def test_invalid_ids_are_rejected(raw: str) -> None:
    with pytest.raises(InvalidArxivId):
        canonicalize_arxiv_id(raw)


def test_trailing_pdf_on_a_bare_id_is_stripped() -> None:
    """方案 §4.4 第 1 条要求剥掉末尾 ``.pdf``——裸 ID 上的也一样。"""
    assert canonicalize_arxiv_id("2501.01234.pdf") == ("2501.01234", None)
    assert canonicalize_arxiv_id("2501.01234v2.pdf") == ("2501.01234", 2)


def test_invalid_id_message_echoes_the_input() -> None:
    with pytest.raises(InvalidArxivId, match="not-an-id"):
        canonicalize_arxiv_id("not-an-id")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2501.01234", True),
        ("1501.00001", True),
        ("hep-th/9901001", True),
        ("math.GT/0309136", True),
        ("2501.01234v2", False),   # 带版本号就不是 canonical
        ("2501.0123", True),       # 4 位流水号是合法形式
        ("2501.123", False),       # 3 位不合法
        ("2501.012345", False),    # 6 位不合法
        ("2501.01234 ", False),    # canonical 判断不做 strip
    ],
)
def test_is_valid_canonical_id(value: str, expected: bool) -> None:
    assert is_valid_canonical_id(value) is expected


def test_strip_wrapper_is_idempotent() -> None:
    once = strip_wrapper("https://arxiv.org/pdf/2501.01234v2.pdf")
    assert strip_wrapper(once) == once == "2501.01234v2"


def test_versioned_id_for_round_trips() -> None:
    for raw in ("2501.01234v2", "hep-th/9901001v1", "2501.01234"):
        canonical, version = canonicalize_arxiv_id(raw)
        assert canonicalize_arxiv_id(versioned_id_for(canonical, version)) == (canonical, version)


def test_split_versioned_id_is_an_alias() -> None:
    assert split_versioned_id("2501.01234v3") == canonicalize_arxiv_id("2501.01234v3")
