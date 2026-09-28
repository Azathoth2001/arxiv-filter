from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime, timezone
from pathlib import Path
import re

from openpyxl import load_workbook

from arxiv_filter.exporters import CompositeExporter, FIELD_NAMES, build_filename
from arxiv_filter.models.paper import Author, PaperView
from arxiv_filter.models.query import SearchRequest

UTC = timezone.utc
EXPORTED_AT = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def paper(
    canonical_id: str = "2609.12345",
    *,
    title: str = "A small \"test\", with punctuation",
    summary: str = "First line, with a comma.\nSecond line: $E=mc^2$. <script>alert(1)</script>",
) -> PaperView:
    return PaperView(
        canonical_id=canonical_id,
        versioned_id=f"{canonical_id}v2",
        version=2,
        title=title,
        authors=[Author(name="Ada Lovelace"), Author(name="李雷")],
        summary_raw=summary,
        published_at=datetime(2026, 9, 20, 9, 30, tzinfo=UTC),
        updated_at=datetime(2026, 9, 21, 10, 0, tzinfo=UTC),
        primary_category="cs.CL",
        categories=["cs.CL", "cs.AI"],
        external_categories=["ACM-F.2.2"],
        comment="=HYPERLINK(\"https://example.invalid\")",
        doi="10.1234/example",
        abs_url=f"https://arxiv.org/abs/{canonical_id}v2",
        pdf_url=f"https://arxiv.org/pdf/{canonical_id}v2",
        retrieved_at=EXPORTED_AT,
        is_new=True,
        is_read=False,
        relevance_score=0.75,
    )


def request(**updates) -> SearchRequest:
    values = {
        "group_id": "cs",
        "category_id": "cs.CL",
        "start_date": date(2026, 9, 17),
        "end_date": date(2026, 9, 25),
        "keywords": '"large language model"',
    }
    values.update(updates)
    return SearchRequest(**values)


async def export(papers, file_format, *, include_state=True):
    return await CompositeExporter().export(
        papers,
        file_format=file_format,
        query='cat:cs.CL AND "large language model"',
        include_state=include_state,
        exported_at=EXPORTED_AT,
        request=request(),
    )


def expected_raw_fields(value: PaperView, *, include_state: bool = True) -> dict[str, object]:
    def iso_utc(timestamp: datetime | None) -> str | None:
        return timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z") if timestamp else None

    return {
        "canonical_id": value.canonical_id,
        "versioned_id": value.versioned_id,
        "title": value.title,
        "authors": [author.name for author in value.authors],
        "published_utc": iso_utc(value.published_at),
        "updated_utc": iso_utc(value.updated_at),
        "primary_category": value.primary_category,
        "categories": list(value.categories),
        "external_categories": list(value.external_categories),
        "summary": value.summary_raw,
        "comment": value.comment,
        "journal_ref": value.journal_ref,
        "doi": value.doi,
        "abs_url": value.abs_url,
        "pdf_url": value.pdf_url,
        "is_new": value.is_new if include_state else None,
        "is_read": value.is_read if include_state else None,
        "relevance_score": value.relevance_score,
    }


def expected_csv_row(fields: dict[str, object]) -> list[str]:
    values: list[str] = []
    for name in FIELD_NAMES:
        value = fields[name]
        if isinstance(value, list):
            value = "; ".join(str(item) for item in value)
        elif isinstance(value, bool):
            value = "true" if value else "false"
        elif value is None:
            value = ""
        text_value = str(value)
        if re.match(r"^[\s\t\r\n]*[=+\-@]", text_value):
            text_value = "'" + text_value
        values.append(text_value)
    return values


def expected_xlsx_row(fields: dict[str, object]) -> list[object]:
    values: list[object] = []
    for name in FIELD_NAMES:
        value = fields[name]
        if isinstance(value, list):
            value = "; ".join(str(item) for item in value)
        if name in {"published_utc", "updated_utc"} and value:
            value = datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
        if isinstance(value, str) and re.match(r"^[\s\t\r\n]*[=+\-@]", value):
            value = "'" + value
        values.append(value)
    return values


def test_csv_bom_rfc4180_field_order_arrays_formula_safety_and_state_toggle() -> None:
    source = paper(title="=1+1")
    artifact = __import__("asyncio").run(export([source], "csv", include_state=False))
    assert artifact.content.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(artifact.content.decode("utf-8-sig"), newline="")))
    assert tuple(rows[0]) == FIELD_NAMES
    assert len(rows[1]) == len(FIELD_NAMES)
    assert rows[1] == expected_csv_row(expected_raw_fields(source, include_state=False))
    assert rows[1][3] == "Ada Lovelace; 李雷"
    assert rows[1][8] == "ACM-F.2.2"
    assert rows[1][10].startswith("'=HYPERLINK")
    assert rows[1][15:17] == ["", ""]
    assert "\r\n" in artifact.content.decode("utf-8-sig")
    assert artifact.filename == "arxiv_cs.CL_20260917-20260925_large_language_model.csv"


def test_json_is_unicode_rfc3339_and_preserves_structured_fields() -> None:
    artifact = __import__("asyncio").run(export([paper()], "json"))
    assert artifact.content.decode("utf-8").find("李雷") >= 0
    payload = json.loads(artifact.content)
    assert payload["schema_version"] == "1.0"
    assert payload["exported_at"] == "2026-09-25T12:00:00Z"
    assert payload["papers"][0]["authors"] == ["Ada Lovelace", "李雷"]
    assert payload["papers"][0]["published_utc"] == "2026-09-20T09:30:00Z"


def test_markdown_uses_cards_not_tables_and_escapes_html_in_summary() -> None:
    artifact = __import__("asyncio").run(export([paper()], "markdown"))
    content = artifact.content.decode("utf-8")
    assert '## A small "test", with punctuation' in content
    assert "| canonical_id |" not in content
    assert "> First line, with a comma." in content
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in content
    assert "<script>" not in content


def test_xlsx_freeze_filter_widths_dates_and_formula_are_safe() -> None:
    source = paper(title="=1+1")
    artifact = __import__("asyncio").run(export([source], "xlsx"))
    workbook = load_workbook(io.BytesIO(artifact.content), read_only=False, data_only=False)
    sheet = workbook["Papers"]
    assert sheet.freeze_panes == "A2"
    assert sheet.auto_filter.ref == f"A1:R2"
    assert sheet.column_dimensions["C"].width == 48
    assert sheet.column_dimensions["J"].width == 90
    actual = tuple(next(sheet.iter_rows(min_row=2, values_only=True)))
    expected = tuple(expected_xlsx_row(expected_raw_fields(source)))
    assert actual == expected
    assert sheet["E2"].value == datetime(2026, 9, 20, 9, 30)
    assert sheet["K2"].value.startswith("'=HYPERLINK")
    assert sheet["K2"].data_type == "s"
    workbook.close()


def test_bibtex_keys_conflicts_latex_unicode_and_fields_are_balanced() -> None:
    first = paper(title=r"A {Nested} $x^2$ \LaTeX")
    second = paper(title="Second")
    artifact = __import__("asyncio").run(export([first, second], "bibtex"))
    content = artifact.content.decode("utf-8")
    assert "@misc{Lovelace2026arxiv12345," in content
    assert "@misc{Lovelace2026arxiv12345a," in content
    assert r"$x^2$ \LaTeX" in content
    assert "archivePrefix = {arXiv}" in content
    assert "primaryClass = {cs.CL}" in content
    for entry in re.findall(r"@misc\{.*?\n\}", content, flags=re.S):
        assert entry.count("{") == entry.count("}")


def test_filename_sanitization_and_windows_reserved_names() -> None:
    value = build_filename(
        request(group_id="CON", category_id="cs/CL?", keywords="*" * 40),
        EXPORTED_AT,
        "csv",
    )
    assert not any(char in value for char in '<>:"/\\|?*')
    assert len(Path(value).stem) <= 120
    assert not value.endswith((".", " "))
    assert build_filename(None, EXPORTED_AT, "json") == "arxiv_export.json"
