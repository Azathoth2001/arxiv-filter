"""T1.4 Atom 解析（方案 §4.1、§7.2）。

fixture 都放在 ``tests/fixtures/atom/``；``normal.xml`` 是 2026-09-24 的
**原始响应字节**，未做任何修改，指纹记录在 ``normal.meta.json``。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from arxiv_filter.arxiv.atom_parser import known_categories_from, parse_atom, parse_paper
from arxiv_filter.errors import ArxivFilterError, ErrorCode
from arxiv_filter.taxonomy import load_taxonomy

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "atom"
RETRIEVED = datetime(2026, 9, 24, 14, 0, 0, tzinfo=timezone.utc)


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def parse(name: str):
    return parse_atom(load(name), RETRIEVED)


# —— normal.xml（真实响应）——


def test_normal_fixture_hash_matches_its_metadata() -> None:
    """fixture 一旦改动，meta 里的 SHA256 必须同步更新（方案 §7.3 第 6 条）。"""
    meta = json.loads((FIXTURES / "normal.meta.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256(load("normal.xml")).hexdigest()
    assert digest == meta["sha256"]
    assert meta["query_kind"] == "id_list"


def test_normal_fixture_parses_three_entries() -> None:
    page = parse("normal.xml")
    assert page.total_results == 3
    assert page.start_index == 0
    assert page.items_per_page == 3
    assert len(page.papers) == 3
    assert page.feed_updated_at is not None
    assert page.warnings == []


def test_normal_fixture_first_paper_fields() -> None:
    paper = parse("normal.xml").papers[0]
    assert paper.canonical_id == "1706.03762"
    assert paper.versioned_id == "1706.03762v7"
    assert paper.version == 7
    assert paper.title == "Attention Is All You Need"
    assert paper.primary_category == "cs.CL"
    assert paper.categories == ["cs.CL", "cs.LG"]
    assert paper.comment == "15 pages, 5 figures"
    assert paper.journal_ref is None
    assert paper.doi is None
    assert paper.abs_url == "https://arxiv.org/abs/1706.03762v7"
    assert paper.pdf_url == "https://arxiv.org/pdf/1706.03762v7"
    assert paper.published_at == datetime(2017, 6, 12, 17, 57, 34, tzinfo=timezone.utc)
    assert paper.updated_at == datetime(2023, 8, 2, 0, 41, 18, tzinfo=timezone.utc)
    assert paper.retrieved_at == RETRIEVED
    assert paper.source == "arxiv_api"
    assert len(paper.authors) == 8
    assert paper.authors[0].name == "Ashish Vaswani"


def test_normal_fixture_preserves_unicode_title() -> None:
    paper = parse("normal.xml").papers[1]
    assert paper.title == "Etat de l'art sur l'application des bandits multi-bras"
    assert paper.canonical_id == "2101.00001"


def test_normal_fixture_reads_affiliation_and_doi() -> None:
    paper = parse("normal.xml").papers[2]
    assert paper.canonical_id == "1503.00001"
    assert paper.doi == "10.1093/mnras/stv241"
    assert paper.authors[0].affiliation == (
        "Institute of Astronomy, School of Science, the University of Tokyo"
    )
    assert paper.primary_category == "astro-ph.GA"


def test_doi_link_is_not_mistaken_for_the_pdf_link() -> None:
    """DOI 与 PDF 都是 ``rel="related"``，只能靠 ``type``/``title`` 区分。"""
    paper = parse("normal.xml").papers[2]
    assert paper.pdf_url == "https://arxiv.org/pdf/1503.00001v1"
    assert "doi.org" not in (paper.pdf_url or "")
    assert "doi.org" not in paper.abs_url


def test_entities_in_summary_are_decoded() -> None:
    """``&amp;`` 等实体由解析器解码，摘要里保留原始字符。"""
    paper = parse("normal.xml").papers[2]
    assert "Dexter & Agol" in paper.summary_raw
    assert "&amp;" not in paper.summary_raw


def test_summary_latex_is_kept_verbatim() -> None:
    paper = parse("normal.xml").papers[2]
    assert "$σ_{\\text{int}}$" in paper.summary_raw


# —— empty ——


def test_empty_feed_has_no_papers() -> None:
    page = parse("empty.xml")
    assert page.total_results == 0
    assert page.items_per_page == 0
    assert page.papers == []
    assert page.is_empty is True


# —— optional fields ——


def test_every_optional_element_present() -> None:
    paper = parse("optional-fields.xml").papers[0]
    assert paper.comment == "Accepted at a venue"
    assert paper.journal_ref == "Journal of Examples 12 (2025) 345-356"
    assert paper.doi == "10.1234/example.2025.001"
    assert paper.authors[0].affiliation == "Example University"
    assert paper.authors[1].affiliation is None


def test_no_optional_elements_gives_none() -> None:
    paper = parse("optional-fields.xml").papers[1]
    assert paper.comment is None
    assert paper.journal_ref is None
    assert paper.doi is None
    assert paper.pdf_url is None
    assert paper.authors[0].affiliation is None


def test_missing_primary_category_warns_and_does_not_guess() -> None:
    """方案 §4.1：缺失则记警告并留 ``None``，**不凭第一个 category 猜测**。"""
    page = parse("optional-fields.xml")
    paper = page.papers[2]
    assert paper.primary_category is None
    assert paper.categories == ["cs.CL"]
    assert any("primary_category" in warning for warning in page.warnings)


def test_external_categories_are_separated() -> None:
    """ACM/MSC 等非 arXiv 分类不能混进 ``categories``。"""
    paper = parse("optional-fields.xml").papers[3]
    assert paper.categories == ["cs.CL"]
    assert paper.external_categories == ["I.2.7", "68T50"]


def test_known_categories_filter_can_be_disabled() -> None:
    page = parse_atom(load("optional-fields.xml"), RETRIEVED, known_categories=None)
    # None 表示回退到内置 taxonomy——外部分类仍然被分离
    assert page.papers[3].external_categories == ["I.2.7", "68T50"]


def test_alias_terms_are_kept_as_arxiv_categories() -> None:
    """别名（如 cs.SY）属于 arXiv 分类，不能被误判成外部分类。"""
    table = load_taxonomy()
    known = known_categories_from(table)
    assert "cs.SY" in known
    assert "math.MP" in known


# —— cross-list ——


def test_cross_listed_entry_keeps_both_categories() -> None:
    page = parse("cross-list.xml")
    paper = page.papers[0]
    assert paper.primary_category == "cs.LG"
    assert paper.categories == ["cs.LG", "cs.CL"]
    # 命中 cs.CL 但主分类是 cs.LG——cross-list 语义（方案 §5.1）
    assert paper.primary_category != "cs.CL"


def test_cross_list_order_follows_the_feed() -> None:
    paper = parse("cross-list.xml").papers[1]
    assert paper.categories == ["cs.CL", "stat.ML"]
    assert paper.primary_category == "cs.CL"


# —— legacy IDs ——


def test_legacy_ids_keep_their_slash() -> None:
    page = parse("legacy-id.xml")
    assert [paper.canonical_id for paper in page.papers] == [
        "hep-th/9901001",
        "hep-th/9901001",
        "math.GT/0309136",
    ]


def test_legacy_id_versions_are_parsed() -> None:
    page = parse("legacy-id.xml")
    assert page.papers[0].version == 2
    assert page.papers[1].version == 1
    assert page.papers[0].versioned_id == "hep-th/9901001v2"


def test_legacy_sub_archive_id() -> None:
    paper = parse("legacy-id.xml").papers[2]
    assert paper.canonical_id == "math.GT/0309136"
    assert paper.versioned_id == "math.GT/0309136v1"
    assert paper.primary_category == "math.GT"
    assert paper.pdf_url == "https://arxiv.org/pdf/math.GT/0309136v1"


# —— 错误 ——


def test_atom_error_entry_raises_with_upstream_summary() -> None:
    with pytest.raises(ArxivFilterError) as excinfo:
        parse("api-error.xml")
    error = excinfo.value
    assert error.code is ErrorCode.ARXIV_ATOM_ERROR
    assert error.details["upstream_summary"] == "incorrect id format for 0"
    assert "incorrect id format for 0" in error.message_zh
    assert error.http_status == 422


def test_malformed_xml_raises_bad_response() -> None:
    with pytest.raises(ArxivFilterError) as excinfo:
        parse("malformed.xml")
    error = excinfo.value
    assert error.code is ErrorCode.ARXIV_BAD_RESPONSE
    assert error.details["reason"] == "xml_parse_error"
    assert error.retryable is False


def test_entity_attack_is_refused() -> None:
    """defusedxml 必须直接拒绝 DOCTYPE/ENTITY，而不是展开它们。"""
    with pytest.raises(ArxivFilterError) as excinfo:
        parse("entity-attack.xml")
    error = excinfo.value
    assert error.code is ErrorCode.ARXIV_BAD_RESPONSE
    assert error.details["reason"] == "forbidden_xml_construct"


def test_html_body_raises_bad_response() -> None:
    with pytest.raises(ArxivFilterError) as excinfo:
        parse_atom(b"<html><body>proxy error</body></html>", RETRIEVED)
    assert excinfo.value.details["reason"] == "not_an_atom_feed"


def test_erroneous_500_atom_error_is_still_an_atom_error() -> None:
    """500 也可能是 Atom error entry——判定必须看内容而不是只看状态码。"""
    with pytest.raises(ArxivFilterError) as excinfo:
        parse("api-error.xml")
    assert excinfo.value.code is ErrorCode.ARXIV_ATOM_ERROR


def test_diagnostic_prefix_is_bounded() -> None:
    huge = b"<feed xmlns='http://www.w3.org/2005/Atom'><title>" + b"x" * 50_000
    with pytest.raises(ArxivFilterError) as excinfo:
        parse_atom(huge, RETRIEVED)
    assert len(excinfo.value.details.get("body_prefix", "")) <= 200


def test_empty_body_raises_bad_response() -> None:
    with pytest.raises(ArxivFilterError):
        parse_atom(b"", RETRIEVED)


# —— 解析细节 ——


def test_missing_total_results_yields_none_for_indeterminate_progress() -> None:
    """``opensearch:totalResults`` 缺失时必须是 ``None``，前端才能显示不定进度。"""
    xml = b"""<?xml version='1.0' encoding='UTF-8'?>
    <feed xmlns="http://www.w3.org/2005/Atom"
          xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
      <updated>2026-09-24T00:00:00Z</updated>
      <opensearch:startIndex>0</opensearch:startIndex>
    </feed>"""
    page = parse_atom(xml, RETRIEVED)
    assert page.total_results is None
    assert page.start_index == 0
    assert page.items_per_page == 0


def test_missing_feed_updated_is_none() -> None:
    xml = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<feed xmlns='http://www.w3.org/2005/Atom'"
        " xmlns:opensearch='http://a9.com/-/spec/opensearch/1.1/'>"
        "<opensearch:totalResults>0</opensearch:totalResults>"
        "</feed>"
    ).encode("utf-8")
    page = parse_atom(xml, RETRIEVED)
    assert page.feed_updated_at is None
    assert page.total_results == 0


def test_title_with_newlines_is_collapsed() -> None:
    entry = _entry_xml("<title>A very\n      long   title</title>")
    paper = _parse_single(entry)
    assert paper.title == "A very long title"


def test_multi_line_summary_keeps_internal_newlines() -> None:
    entry = _entry_xml("<summary>line one\n\nline two</summary>")
    paper = _parse_single(entry)
    assert paper.summary_raw == "line one\n\nline two"


def test_entry_without_id_is_skipped_with_a_warning() -> None:
    xml = _feed("<entry><title>No id here</title></entry>")
    page = parse_atom(xml, RETRIEVED)
    assert page.papers == []
    assert any("id" in warning for warning in page.warnings)


def test_entry_with_unrecognised_id_is_skipped() -> None:
    xml = _feed(
        "<entry><id>http://arxiv.org/abs/not-an-id</id><title>x</title>"
        "<updated>2025-01-01T00:00:00Z</updated>"
        "<link href='https://arxiv.org/abs/not-an-id' rel='alternate'/></entry>"
    )
    page = parse_atom(xml, RETRIEVED)
    assert page.papers == []
    assert any("无法识别" in warning for warning in page.warnings)


def test_entry_without_abs_link_falls_back_to_id_url() -> None:
    """没有 ``rel="alternate"`` 时用 Atom 的 ``<id>``——它本身就是规范 URL。"""
    xml = _feed(
        "<entry><id>http://arxiv.org/abs/2501.40001v1</id><title>Fallback</title>"
        "<updated>2025-01-01T00:00:00Z</updated>"
        "<published>2025-01-01T00:00:00Z</published>"
        "<arxiv:primary_category xmlns:arxiv='http://arxiv.org/schemas/atom' term='cs.CL'/>"
        "</entry>"
    )
    page = parse_atom(xml, RETRIEVED)
    assert page.papers[0].abs_url == "http://arxiv.org/abs/2501.40001v1"


def test_entry_missing_published_is_rejected() -> None:
    xml = _feed(
        "<entry><id>http://arxiv.org/abs/2501.40002v1</id><title>No published</title>"
        "<updated>2025-01-01T00:00:00Z</updated>"
        "<link href='https://arxiv.org/abs/2501.40002v1' rel='alternate'/></entry>"
    )
    with pytest.raises(ArxivFilterError) as excinfo:
        parse_atom(xml, RETRIEVED)
    assert excinfo.value.details["reason"] == "missing_timestamp"


def test_paper_without_title_is_still_usable() -> None:
    entry = _entry_xml("")
    paper = _parse_single(entry)
    assert paper.title == ""
    assert paper.display_title == "（无标题）2501.50001"


def test_retrieved_at_is_recorded() -> None:
    assert _parse_single(_entry_xml("")).retrieved_at == RETRIEVED


# —— 辅助 ——


def _entry_xml(body: str) -> str:
    """构造一个最小可用 entry，``body`` 插入到 title 的位置。"""
    return (
        "<entry>"
        "<id>http://arxiv.org/abs/2501.50001v1</id>"
        f"{body or '<title></title>'}"
        "<updated>2025-01-01T00:00:00Z</updated>"
        "<published>2025-01-01T00:00:00Z</published>"
        "<link href='https://arxiv.org/abs/2501.50001v1' rel='alternate' type='text/html'/>"
        "<arxiv:primary_category xmlns:arxiv='http://arxiv.org/schemas/atom' term='cs.CL'/>"
        "</entry>"
    )


def _feed(entries: str) -> bytes:
    return (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<feed xmlns='http://www.w3.org/2005/Atom'"
        " xmlns:opensearch='http://a9.com/-/spec/opensearch/1.1/'"
        " xmlns:arxiv='http://arxiv.org/schemas/atom'>"
        "<updated>2026-09-24T00:00:00Z</updated>"
        f"{entries}</feed>"
    ).encode("utf-8")


def _parse_single(entry_xml: str):
    page = parse_atom(_feed(entry_xml), RETRIEVED)
    assert len(page.papers) == 1, page.warnings
    return page.papers[0]


def test_parse_paper_accepts_a_preparsed_element() -> None:
    from defusedxml import ElementTree as SafeET

    root = SafeET.fromstring(_feed(_entry_xml("<title>Direct call</title>")))
    entry = root.find("{http://www.w3.org/2005/Atom}entry")
    paper = parse_paper(entry, RETRIEVED)
    assert paper is not None
    assert paper.title == "Direct call"
