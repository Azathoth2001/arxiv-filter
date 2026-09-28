"""Five safe, deterministic export formats for already-fetched papers."""

from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
from collections import Counter
from datetime import date, datetime, timezone
from typing import Sequence

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill

from arxiv_filter.api.ports import GeneratedFile
from arxiv_filter.api.schemas import ExportFormat
from arxiv_filter.models.paper import PaperView
from arxiv_filter.models.query import SearchRequest

FIELD_NAMES = (
    "canonical_id", "versioned_id", "title", "authors", "published_utc",
    "updated_utc", "primary_category", "categories", "external_categories",
    "summary", "comment", "journal_ref", "doi", "abs_url", "pdf_url",
    "is_new", "is_read", "relevance_score",
)
MEDIA_TYPES = {
    "csv": "text/csv; charset=utf-8",
    "json": "application/json; charset=utf-8",
    "markdown": "text/markdown; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "bibtex": "application/x-bibtex; charset=utf-8",
}
EXTENSIONS = {"csv": "csv", "json": "json", "markdown": "md", "xlsx": "xlsx", "bibtex": "bib"}


class CompositeExporter:
    async def export(
        self,
        papers: Sequence[PaperView],
        *,
        file_format: ExportFormat,
        query: str,
        include_state: bool,
        exported_at: datetime,
        request: SearchRequest | None = None,
    ) -> GeneratedFile:
        rows = [_paper_data(paper, include_state=include_state) for paper in papers]
        if file_format == "csv":
            content = _csv(rows)
        elif file_format == "json":
            content = _json(rows, query=query, exported_at=exported_at)
        elif file_format == "markdown":
            content = _markdown(rows, query=query, exported_at=exported_at)
        elif file_format == "xlsx":
            content = _xlsx(rows)
        elif file_format == "bibtex":
            content = _bibtex(rows)
        else:  # guarded by the request schema
            raise ValueError("Unsupported export format")
        filename = build_filename(request, exported_at, EXTENSIONS[file_format])
        return GeneratedFile(content, filename, MEDIA_TYPES[file_format])


def _paper_data(paper: PaperView, *, include_state: bool) -> dict[str, object]:
    def iso(value: datetime | None) -> str | None:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None

    return {
        "canonical_id": paper.canonical_id,
        "versioned_id": paper.versioned_id,
        "title": paper.title,
        "authors": [author.name for author in paper.authors],
        "published_utc": iso(paper.published_at),
        "updated_utc": iso(paper.updated_at),
        "primary_category": paper.primary_category,
        "categories": list(paper.categories),
        "external_categories": list(paper.external_categories),
        "summary": paper.summary_raw,
        "comment": paper.comment,
        "journal_ref": paper.journal_ref,
        "doi": paper.doi,
        "abs_url": paper.abs_url,
        "pdf_url": paper.pdf_url,
        "is_new": paper.is_new if include_state else None,
        "is_read": paper.is_read if include_state else None,
        "relevance_score": paper.relevance_score,
    }


def _formula_safe(value: object) -> object:
    if isinstance(value, str) and re.match(r"^[\s\t\r\n]*[=+\-@]", value):
        return "'" + value
    return value


def _csv(rows: Sequence[dict[str, object]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, dialect="excel", lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(FIELD_NAMES)
    for row in rows:
        values = []
        for key in FIELD_NAMES:
            value = row[key]
            if isinstance(value, list):
                value = "; ".join(str(item) for item in value)
            elif isinstance(value, bool):
                value = "true" if value else "false"
            elif value is None:
                value = ""
            values.append(_formula_safe(value))
        writer.writerow(values)
    return stream.getvalue().encode("utf-8-sig")


def _json(rows: Sequence[dict[str, object]], *, query: str, exported_at: datetime) -> bytes:
    payload = {
        "schema_version": "1.0",
        "query": query,
        "exported_at": _utc_iso(exported_at),
        "papers": rows,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def _markdown(rows: Sequence[dict[str, object]], *, query: str, exported_at: datetime) -> bytes:
    lines = ["# arXiv Filter 导出", "", f"- 查询：{_md_text(query)}", f"- 导出时间（UTC）：{_utc_iso(exported_at)}", f"- 论文数：{len(rows)}", ""]
    for row in rows:
        title = _md_text(str(row["title"] or "（无标题）"))
        lines.extend([f"## {title}", ""])
        for label, key in (
            ("arXiv ID", "versioned_id"), ("作者", "authors"), ("提交时间（UTC）", "published_utc"),
            ("更新时间（UTC）", "updated_utc"), ("主分类", "primary_category"),
            ("分类", "categories"), ("DOI", "doi"), ("期刊信息", "journal_ref"),
            ("新增", "is_new"), ("已读", "is_read"),
        ):
            value = row[key]
            if value is None or value == []:
                continue
            if isinstance(value, list):
                value = "; ".join(str(item) for item in value)
            if isinstance(value, bool):
                value = "是" if value else "否"
            lines.append(f"- **{label}：** {_md_text(str(value))}")
        abs_url = _safe_markdown_url(str(row["abs_url"] or ""))
        pdf_url = _safe_markdown_url(str(row["pdf_url"] or ""))
        if abs_url:
            lines.append(f"- **摘要页：** <{abs_url}>")
        if pdf_url:
            lines.append(f"- **PDF：** <{pdf_url}>")
        lines.extend(["", "摘要：", ""])
        summary = str(row["summary"] or "暂无摘要。")
        lines.extend([f"> {line.replace('<', '&lt;').replace('>', '&gt;')}" if line else ">" for line in summary.splitlines()])
        lines.extend(["", "---", ""])
    return "\n".join(lines).encode("utf-8")


def _xlsx(rows: Sequence[dict[str, object]]) -> bytes:
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("Papers")
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:{_column_name(len(FIELD_NAMES))}{max(1, len(rows) + 1)}"
    for index, field in enumerate(FIELD_NAMES, start=1):
        sheet.column_dimensions[_column_name(index)].width = 48 if field == "title" else 90 if field == "summary" else 22
    header_fill = PatternFill("solid", fgColor="286E68")
    header_font = Font(color="FFFFFF", bold=True)
    header = []
    for name in FIELD_NAMES:
        cell = WriteOnlyCell(sheet, value=name)
        cell.fill = header_fill
        cell.font = header_font
        header.append(cell)
    sheet.append(header)
    for row in rows:
        values = []
        for field in FIELD_NAMES:
            value = row[field]
            if isinstance(value, list):
                value = "; ".join(str(item) for item in value)
            if field in {"published_utc", "updated_utc"} and value:
                value = datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
            if isinstance(value, str):
                value = _clean_excel_text(str(_formula_safe(value)))
            cell = WriteOnlyCell(sheet, value=value)
            if field in {"summary", "title"}:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
            values.append(cell)
        sheet.append(values)
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def _bibtex(rows: Sequence[dict[str, object]]) -> bytes:
    keys: Counter[str] = Counter()
    entries: list[str] = []
    for row in rows:
        authors = [str(author) for author in row["authors"]]  # type: ignore[union-attr]
        first = authors[0] if authors else "anon"
        surname = first.split(",", 1)[0].split()[-1] if first.strip() else "anon"
        surname = re.sub(r"[^A-Za-z0-9]", "", unicodedata.normalize("NFKD", surname)) or "anon"
        published = str(row["published_utc"] or "")
        year = published[:4] if len(published) >= 4 else "0000"
        suffix = str(row["canonical_id"]).rsplit("/", 1)[-1].split(".")[-1]
        base = re.sub(r"[^A-Za-z0-9]", "", f"{surname}{year}arxiv{suffix}") or "anon0000arxiv"
        ordinal = keys[base]
        keys[base] += 1
        key = base if ordinal == 0 else f"{base}{_alpha_suffix(ordinal - 1)}"

        fields: list[tuple[str, str]] = []
        if authors:
            fields.append(("author", " and ".join(authors)))
        fields.extend([
            ("title", str(row["title"] or "")),
            ("year", year),
            ("eprint", str(row["canonical_id"])),
            ("archivePrefix", "arXiv"),
        ])
        if row["primary_category"]:
            fields.append(("primaryClass", str(row["primary_category"])))
        if row["abs_url"]:
            fields.append(("url", str(row["abs_url"])))
        if row["doi"]:
            fields.append(("doi", str(row["doi"])))
        if row["journal_ref"]:
            fields.append(("journal", str(row["journal_ref"])))
        rendered = [f"  {name} = {{{_balanced_braces(value)}}}" for name, value in fields]
        entries.append(f"@misc{{{key},\n" + ",\n".join(rendered) + "\n}")
    return ("\n\n".join(entries) + ("\n" if entries else "")).encode("utf-8")


def build_filename(request: SearchRequest | None, exported_at: datetime, extension: str) -> str:
    if request is None:
        stem = "arxiv_export"
    else:
        category = request.category_id or request.group_id or "all"
        keyword = request.keywords.strip()[:24] or "all"
        start = request.start_date.strftime("%Y%m%d")
        end = request.end_date.strftime("%Y%m%d")
        stem = f"arxiv_{category}_{start}-{end}_{keyword}"
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", stem).rstrip(". ")
    stem = re.sub(r"\s+", "_", stem)
    if stem.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        stem = f"_{stem}"
    stem = stem[:120].rstrip(". ") or "arxiv_export"
    return f"{stem}.{extension}"


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _md_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "\\|").replace("\n", " ")


def _safe_markdown_url(value: str) -> str | None:
    return value if value.startswith(("https://", "http://")) and "\n" not in value else None


def _clean_excel_text(value: str) -> str:
    return "".join(char for char in value if char in "\t\n\r" or ord(char) >= 0x20 and ord(char) != 0x7F)


def _balanced_braces(value: str) -> str:
    depth = 0
    minimum = 0
    escaped = False
    for char in value:
        if char == "\\":
            escaped = not escaped
            continue
        if not escaped and char == "{":
            depth += 1
        elif not escaped and char == "}":
            depth -= 1
            minimum = min(minimum, depth)
        escaped = False
    if minimum < 0:
        value = "{" * -minimum + value
        depth += -minimum
    if depth > 0:
        value += "}" * depth
    return value


def _alpha_suffix(index: int) -> str:
    # 0 -> a, 25 -> z, 26 -> aa
    result = ""
    value = index
    while True:
        result = chr(ord("a") + value % 26) + result
        value = value // 26 - 1
        if value < 0:
            return result


def _column_name(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


__all__ = ["CompositeExporter", "FIELD_NAMES", "build_filename"]
