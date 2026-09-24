#!/usr/bin/env python
"""校验分类快照的完整性（方案 T0.3 / §5.9 第 6 条）。

检查项：

1. ID 唯一（groups / archives / categories 各自内部，且 archive 与 category 不撞名）；
2. 父子引用有效（category.group_id / archive_id 必须存在；archive.group_id 必须存在）；
3. alias 有效：源与目标都存在、目标不是别名（无多级链）、无自环、无环；
4. active leaf：每个非别名 category 的 ``active`` 为真；别名 ``active`` 为假；
5. 中文名非空（或显式 ``--allow-untranslated``）；
6. 排序稳定且为规范序（group 按 id，archive 按 (group,id)，category 按 (group,archive,id)）；
7. 最大查询长度：每个 group 与其每个 archive 展开成 ``(cat:a OR cat:b ...)`` 后不超过 1800 字符
   （方案 §4.3：超过则拆成多个顺序 GET）。

退出码：0 全部通过；1 有错误。

用法::

    python scripts/validate_categories.py
    python scripts/validate_categories.py --path src/arxiv_filter/data/categories.zh-CN.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import quote

def _force_utf8_stdio() -> None:
    """Windows 控制台默认编码不是 UTF-8，中文会变乱码；强制 UTF-8 输出。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover - 流不支持重配置
                pass


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OFFICIAL = REPO_ROOT / "src" / "arxiv_filter" / "data" / "categories.zh-CN.json"

#: 方案 §4.3 的 URL 长度安全阈值。
MAX_QUERY_URL_CHARS = 1800

#: 每个 `cat:<id>` 参与编码的固定前缀长度预算。
_CAT_PREFIX = "cat:"
_OR_JOIN = " OR "


class Report:
    """收集错误与警告。"""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.checks = 0

    def check(self, condition: bool, message: str) -> bool:
        self.checks += 1
        if not condition:
            self.errors.append(message)
        return condition

    def warn(self, condition: bool, message: str) -> None:
        if not condition:
            self.warnings.append(message)

    def ok(self) -> bool:
        return not self.errors


def _index(items: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in items}


def validate(document: dict[str, Any], *, allow_untranslated: bool = False) -> Report:
    report = Report()

    # —— 顶层字段 ——
    for key in ("schema_version", "taxonomy_version", "source", "groups", "archives", "categories"):
        report.check(key in document, f"缺少顶层字段 {key!r}")

    if not report.ok():
        return report

    source = document["source"]
    for key in ("html_url", "html_sha256", "html_checked_at",
                "canonical_url", "canonical_sha256", "canonical_checked_at"):
        report.check(bool(source.get(key)), f"source.{key} 为空")

    for key in ("html_sha256", "canonical_sha256"):
        digest = source.get(key, "")
        report.check(
            len(digest) == 64 and all(ch in "0123456789abcdef" for ch in digest),
            f"source.{key} 不是合法 SHA256：{digest!r}",
        )

    taxonomy_version = document.get("taxonomy_version", "")
    try:
        date.fromisoformat(taxonomy_version)
    except ValueError:
        report.errors.append(f"taxonomy_version 不是 ISO 日期：{taxonomy_version!r}")
    report.checks += 1

    # —— 唯一性 ——
    groups = document["groups"]
    archives = document["archives"]
    categories = document["categories"]

    for name, items in (("groups", groups), ("archives", archives), ("categories", categories)):
        ids = [item["id"] for item in items]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        report.check(not duplicates, f"{name} 存在重复 ID：{duplicates}")

    group_ids = {g["id"] for g in groups}
    archive_ids = {a["id"] for a in archives}
    category_ids = {c["id"] for c in categories}

    report.check(bool(groups), "groups 为空")
    report.check(bool(archives), "archives 为空")
    report.check(bool(categories), "categories 为空")

    # —— 类目形状 ——
    # 注意：分类 ID 的前缀等于 **archive**，不一定等于 group。
    # 例如 Physics 组下的 astro-ph.CO 属于 archive `astro-ph`，与 `physics` 无关。
    for category in categories:
        cid = category["id"]
        archive_id = category["archive_id"]
        report.check(
            cid == archive_id or cid.startswith(archive_id + "."),
            f"{cid} 与所属 archive {archive_id!r} 前缀不一致",
        )
        if "." in cid:
            report.check(
                cid.split(".", 1)[1] != "",
                f"{cid} 的小写子类名称为空",
            )

    # —— 父子引用 ——
    for archive in archives:
        report.check(
            archive["group_id"] in group_ids,
            f"archive {archive['id']!r} 指向不存在的 group {archive['group_id']!r}",
        )
    for category in categories:
        report.check(
            category["group_id"] in group_ids,
            f"category {category['id']!r} 指向不存在的 group {category['group_id']!r}",
        )
        report.check(
            category["archive_id"] in archive_ids,
            f"category {category['id']!r} 指向不存在的 archive {category['archive_id']!r}",
        )
        archive = _index(archives).get(category["archive_id"])
        if archive is not None:
            report.check(
                archive["group_id"] == category["group_id"],
                f"{category['id']!r} 的 group/archive 不一致："
                f"{category['group_id']!r} vs archive.group_id={archive['group_id']!r}",
            )

    # —— 每个 group 至少有一个 leaf ——
    for group in groups:
        members = [c for c in categories if c["group_id"] == group["id"]]
        report.check(bool(members), f"group {group['id']!r} 没有任何分类")

    # —— alias 有效性 ——
    aliases = document.get("aliases", {})
    for alias, target in aliases.items():
        report.check(alias in category_ids, f"alias 源 {alias!r} 不在 categories 中")
        report.check(target in category_ids, f"alias {alias!r} 的目标 {target!r} 不存在")
        report.check(alias != target, f"alias {alias!r} 指向自身")
        report.check(target not in aliases, f"alias {alias!r} 指向另一个别名 {target!r}（多级链）")

    # 环检测
    for start in aliases:
        seen: set[str] = set()
        node = start
        while node in aliases:
            if node in seen:
                report.errors.append(f"alias 存在环：{start!r}")
                report.checks += 1
                break
            seen.add(node)
            node = aliases[node]

    # —— active 与 alias_of 一致性 ——
    for category in categories:
        cid = category["id"]
        is_alias = cid in aliases
        report.check(
            category.get("alias_of") == (aliases.get(cid) if is_alias else None),
            f"{cid!r} 的 alias_of 与 aliases 表不一致",
        )
        report.check(
            category.get("active") is (not is_alias),
            f"{cid!r} 的 active 与别名状态不一致（别名应为 false，规范分类应为 true）",
        )

    # —— 中文名 ——
    untranslated = [
        item["id"]
        for section in ("groups", "archives", "categories")
        for item in document[section]
        if not item.get("name_zh")
    ]
    if allow_untranslated:
        report.warn(not untranslated, f"{len(untranslated)} 个条目缺中文名（已允许）")
    else:
        report.check(
            not untranslated,
            f"{len(untranslated)} 个条目缺中文名：{', '.join(untranslated[:20])}",
        )

    for item in [*groups, *archives, *categories]:
        report.check(bool(item.get("name_en")), f"{item['id']!r} 缺英文名")

    # —— 排序稳定 ——
    report.check(
        [g["id"] for g in groups] == sorted(g["id"] for g in groups),
        "groups 未按 id 稳定排序",
    )
    report.check(
        [(a["group_id"], a["id"]) for a in archives]
        == sorted((a["group_id"], a["id"]) for a in archives),
        "archives 未按 (group_id, id) 稳定排序",
    )
    report.check(
        [(c["group_id"], c["archive_id"], c["id"]) for c in categories]
        == sorted((c["group_id"], c["archive_id"], c["id"]) for c in categories),
        "categories 未按 (group_id, archive_id, id) 稳定排序",
    )

    # —— 最大查询长度 ——
    def encode_len(cat_ids: Sequence[str]) -> int:
        expression = _OR_JOIN.join(f"{_CAT_PREFIX}{cid}" for cid in cat_ids)
        return len(quote(expression, safe=""))

    all_ids = sorted({aliases.get(c["id"], c["id"]) for c in categories})
    longest = encode_len(all_ids)
    report.warn(
        longest <= MAX_QUERY_URL_CHARS,
        f"全部 {len(all_ids)} 个分类编码后 {longest} 字符，超过 {MAX_QUERY_URL_CHARS}；"
        "需要拆成多个顺序 GET（方案 §4.3）",
    )

    for group in groups:
        ids = sorted(
            {aliases.get(c["id"], c["id"]) for c in categories if c["group_id"] == group["id"]}
        )
        length = encode_len(ids)
        report.warn(
            length <= MAX_QUERY_URL_CHARS,
            f"group {group['id']!r} 展开后 {length} 字符，超过 {MAX_QUERY_URL_CHARS}",
        )

    for archive in archives:
        ids = sorted(
            {aliases.get(c["id"], c["id"]) for c in categories if c["archive_id"] == archive["id"]}
        )
        length = encode_len(ids)
        report.warn(
            length <= MAX_QUERY_URL_CHARS,
            f"archive {archive['id']!r} 展开后 {length} 字符，超过 {MAX_QUERY_URL_CHARS}",
        )

    # —— 分类数量合理性（防止解析器静默退化成只读到一两个分类）——
    report.check(len(categories) >= 100, f"分类数量异常偏低：{len(categories)}")

    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="validate_categories.py",
        description="校验内置分类快照的 ID、层级、别名、翻译与查询长度。",
    )
    parser.add_argument("--path", type=Path, default=DEFAULT_OFFICIAL, help="要校验的 JSON 路径。")
    parser.add_argument(
        "--allow-untranslated",
        action="store_true",
        help="允许缺少中文名（只警告，用于候选文件校验）。",
    )
    parser.add_argument("--quiet", action="store_true", help="只在失败时输出。")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _force_utf8_stdio()
    args = build_parser().parse_args(list(argv) if argv is not None else None)

    if not args.path.is_file():
        print(f"找不到文件：{args.path}", file=sys.stderr)
        return 1

    try:
        document = json.loads(args.path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"JSON 解析失败：{exc}", file=sys.stderr)
        return 1

    report = validate(document, allow_untranslated=args.allow_untranslated)

    if not args.quiet:
        counts = (
            f"groups={len(document.get('groups', []))} "
            f"archives={len(document.get('archives', []))} "
            f"categories={len(document.get('categories', []))} "
            f"aliases={len(document.get('aliases', {}))}"
        )
        print(f"校验 {args.path}\n  {counts}\n  执行 {report.checks} 项检查")

    for warning in report.warnings:
        print(f"  [警告] {warning}")

    if report.ok():
        if not args.quiet:
            print("  [通过] 全部检查通过。")
        return 0

    for error in report.errors:
        print(f"  [错误] {error}", file=sys.stderr)
    print(f"\n校验失败：{len(report.errors)} 项错误。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
