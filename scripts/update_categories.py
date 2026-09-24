#!/usr/bin/env python
"""从官方来源生成 arxiv-filter 的分类快照（方案 T0.3 / §5.9）。

权威输入（两者都抓，记录 URL、时间与 SHA256）：

1. 人工可读：https://arxiv.org/category_taxonomy
2. 机器校验：https://github.com/arXiv/arxiv-canonical 的 schema-extended/Taxonomy.json

流程（严格按方案 §5.9）：

1. 抓两个官方源，记录 URL、git commit / 抓取时间、SHA256；
2. 输出 ``categories.candidate.json``；**未经 ``--approve`` 绝不覆盖正式文件**；
3. 已有 ID 继承人工中文名；新增 ID 标 ``needs_translation=true``；
4. 新增 / 删除 / alias 变化生成人工 diff；中文名不参与 API query；
5. 保留 Group / Archive / Category 与 alias；
6. 正式 JSON 携带 ``schema_version/source/checked_at/source_sha256``。

用法::

    # 生成候选并打印 diff（推荐每次发布前执行）
    python scripts/update_categories.py

    # 用已下载的副本离线重跑
    python scripts/update_categories.py --from-cache .cache/taxonomy

    # 人工审核通过后提升为正式文件
    python scripts/update_categories.py --approve
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Sequence

try:
    import httpx
except ImportError:  # pragma: no cover
    print("缺少 httpx，请先安装项目依赖。", file=sys.stderr)
    raise SystemExit(2)


def _force_utf8_stdio() -> None:
    """把标准输出/错误切到 UTF-8（Windows 控制台默认编码会让中文变乱码）。

    刻意只在 :func:`main` 里调用：导入模块不应产生全局副作用，而 pytest 会导入本文件。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover - 流不支持重配置
                pass


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OFFICIAL = REPO_ROOT / "src" / "arxiv_filter" / "data" / "categories.zh-CN.json"
DEFAULT_CANDIDATE = REPO_ROOT / "src" / "arxiv_filter" / "data" / "categories.candidate.json"
DEFAULT_TRANSLATIONS = REPO_ROOT / "scripts" / "data" / "translations.zh-CN.json"

HTML_URL = "https://arxiv.org/category_taxonomy"
CANONICAL_URL = (
    "https://raw.githubusercontent.com/arXiv/arxiv-canonical/develop/schema-extended/Taxonomy.json"
)
CANONICAL_COMMIT_API = "https://api.github.com/repos/arXiv/arxiv-canonical/commits/develop"

USER_AGENT = "arxiv-filter-taxonomy-updater/0.1 (personal research client)"
TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)

SCHEMA_VERSION = 1

#: 非规范别名 → 规范 ID。来源：方案 §5.9「当前已知 alias」。
#: 这些映射无法从官方 schema 推导（schema 只标注哪些是别名），
#: 因此显式登记；校验脚本会检查目标存在、无自环、无多级链。
KNOWN_ALIAS_TARGETS: dict[str, str] = {
    "math.MP": "math-ph",
    "stat.TH": "math.ST",
    "math.IT": "cs.IT",
    "q-fin.EC": "econ.GN",
    "cs.SY": "eess.SY",
    "cs.NA": "math.NA",
}

#: 合法的分类 ID 形状：``archive`` 或 ``archive.SUBCAT``。
_CATEGORY_ID_RE = re.compile(r"^[a-z][a-z0-9-]*(?:\.[A-Za-z0-9-]+)?$")
_ARCHIVE_TAIL_RE = re.compile(r"^(?P<name>.*?)\s*\((?P<id>[^()]+)\)\s*$")
_CATEGORY_TAIL_RE = re.compile(r"^(?P<id>\S+)\s*\((?P<name>.*)\)\s*$")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def normalise_space(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


@dataclass
class Category:
    id: str
    name_en: str
    archive_id: str
    group_id: str


@dataclass
class Archive:
    id: str
    name_en: str
    group_id: str


@dataclass
class Group:
    id: str
    name_en: str


@dataclass
class ParsedTaxonomy:
    groups: list[Group] = field(default_factory=list)
    archives: list[Archive] = field(default_factory=list)
    categories: list[Category] = field(default_factory=list)

    def category_ids(self) -> set[str]:
        return {c.id for c in self.categories}


class _TaxonomyHTMLParser(HTMLParser):
    """解析官方 ``category_taxonomy`` 页面。

    结构（2026-09 实测）：

    * ``<h2 class="accordion-head" id="accordion-head-grp_cs">`` → Group；
    * 面板内 ``<h3>Astrophysics<br><span>(astro-ph)</span></h3>`` → Archive，
      仅多 archive 的 Physics 组出现；
    * ``<h4>cs.AI <span>(Artificial Intelligence)</span></h4>`` → Category。

    单 archive 的组（cs、math、econ、eess、q-bio、q-fin、stat）没有 ``<h3>``，
    此时 archive id 与名称等于组自身。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.result = ParsedTaxonomy()
        self._list_depth = 0
        self._in_list = False
        self._capture: str | None = None
        self._capture_attrs: dict[str, str] = {}
        self._buffer: list[str] = []
        self._group: Group | None = None
        self._archive: Archive | None = None

    # —— HTMLParser 钩子 ——

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k: (v or "") for k, v in attrs}

        if tag == "div":
            if attr_map.get("id") == "category_taxonomy_list":
                self._in_list = True
                self._list_depth = 1
                return
            if self._in_list:
                self._list_depth += 1
            return

        if not self._in_list:
            return

        if tag in ("h2", "h3", "h4"):
            self._capture = tag
            self._capture_attrs = attr_map
            self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self._in_list:
            self._list_depth -= 1
            if self._list_depth <= 0:
                self._in_list = False
            return

        if self._capture == tag:
            self._flush(self._capture, self._capture_attrs, normalise_space("".join(self._buffer)))
            self._capture = None
            self._capture_attrs = {}
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._buffer.append(data)

    # —— 解析 ——

    def _flush(self, tag: str, attrs: dict[str, str], text: str) -> None:
        if not text:
            return

        if tag == "h2":
            group_id = attrs.get("id", "").removeprefix("accordion-head-grp_")
            if not group_id:
                return  # 说明区标题
            self._group = Group(id=group_id, name_en=text)
            self.result.groups.append(self._group)
            # 单 archive 组：先假定 archive == group，遇到 <h3> 再替换
            self._archive = Archive(id=group_id, name_en=text, group_id=group_id)
            self.result.archives.append(self._archive)
            return

        if tag == "h3":
            match = _ARCHIVE_TAIL_RE.match(text)
            if not match or self._group is None:
                return
            archive_id = match.group("id").strip()
            if not _CATEGORY_ID_RE.match(archive_id):
                return
            name = match.group("name").strip()
            # 把「单 archive 组」的占位 archive 替换成真正的 archive
            if self._archive is not None and self._archive.id == self._group.id and not any(
                c.archive_id == self._archive.id for c in self.result.categories
            ):
                if self.result.archives and self.result.archives[-1] is self._archive:
                    self.result.archives.pop()
            self._archive = Archive(id=archive_id, name_en=name, group_id=self._group.id)
            self.result.archives.append(self._archive)
            return

        if tag == "h4":
            if self._group is None or self._archive is None:
                return
            match = _CATEGORY_TAIL_RE.match(text)
            if not match:
                return
            category_id = match.group("id").strip()
            if not _CATEGORY_ID_RE.match(category_id):
                return  # 说明区表头 "Category Name (Category ID)"
            self.result.categories.append(
                Category(
                    id=category_id,
                    name_en=match.group("name").strip(),
                    archive_id=self._archive.id,
                    group_id=self._group.id,
                )
            )


def parse_taxonomy_html(html: str) -> ParsedTaxonomy:
    parser = _TaxonomyHTMLParser()
    parser.feed(html)
    parser.close()
    return parser.result


def parse_canonical_schema(payload: bytes) -> tuple[set[str], set[str]]:
    """从官方 schema 取出全部 active ID 与别名 ID 两个集合。"""
    document = json.loads(payload.decode("utf-8"))
    definitions = document.get("definitions", {})
    active = set(definitions.get("active_categories", {}).get("enum", []))
    aliases = set(definitions.get("category_aliases", {}).get("enum", []))
    return active, aliases


# —— 抓取 ——


@dataclass
class SourceSnapshot:
    url: str
    body: bytes
    sha256: str
    checked_at: str
    commit: str | None = None


def _download(client: httpx.Client, url: str) -> bytes:
    response = client.get(url, headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    return response.content


def fetch_sources(client: httpx.Client | None = None) -> tuple[SourceSnapshot, SourceSnapshot]:
    """抓取两个官方源并记录 SHA256 / 时间 / commit。"""
    owns_client = client is None
    client = client or httpx.Client(timeout=TIMEOUT, follow_redirects=True)
    try:
        html_body = _download(client, HTML_URL)
        canonical_body = _download(client, CANONICAL_URL)
        commit: str | None = None
        try:
            commit_response = client.get(
                CANONICAL_COMMIT_API,
                headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
            )
            if commit_response.status_code == 200:
                commit = commit_response.json().get("sha")
        except httpx.HTTPError:
            commit = None
    finally:
        if owns_client:
            client.close()

    stamp = utc_now()
    return (
        SourceSnapshot(HTML_URL, html_body, sha256_bytes(html_body), stamp),
        SourceSnapshot(CANONICAL_URL, canonical_body, sha256_bytes(canonical_body), stamp, commit),
    )


def load_cached_sources(cache_dir: Path) -> tuple[SourceSnapshot, SourceSnapshot]:
    """从已下载的副本读取（离线重跑用）。"""
    html_path = cache_dir / "category_taxonomy.html"
    canonical_path = cache_dir / "Taxonomy.json"
    missing = [str(p) for p in (html_path, canonical_path) if not p.is_file()]
    if missing:
        raise SystemExit(f"--from-cache 缺少文件：{', '.join(missing)}")

    html_body = html_path.read_bytes()
    canonical_body = canonical_path.read_bytes()
    stamp = utc_now()
    commit_file = cache_dir / "canonical_commit.txt"
    commit = commit_file.read_text(encoding="utf-8").strip() if commit_file.is_file() else None
    return (
        SourceSnapshot(HTML_URL, html_body, sha256_bytes(html_body), stamp),
        SourceSnapshot(CANONICAL_URL, canonical_body, sha256_bytes(canonical_body), stamp, commit),
    )


# —— 组装 ——


def load_translations(path: Path) -> dict[str, str]:
    """读取人工中文名种子文件。

    支持两种形状，便于与既有正式 JSON 互换：

    * ``{"groups": {...}, "archives": {...}, "categories": {...}}``
    * 扁平 ``{"cs.AI": "人工智能", ...}``（按 ID 匹配，不区分层级）
    """
    if not path.is_file():
        return {}
    document = json.loads(path.read_text(encoding="utf-8"))
    table: dict[str, str] = {}
    for key in ("groups", "archives", "categories"):
        section = document.get(key)
        if isinstance(section, dict):
            table.update({str(k): str(v) for k, v in section.items()})
    for key, value in document.items():
        if key not in {"groups", "archives", "categories", "_comment"} and isinstance(value, str):
            table[key] = value
    return table


def load_existing_names(path: Path) -> dict[str, str]:
    """从既有正式 JSON 继承已审核的中文名。"""
    if not path.is_file():
        return {}
    document = json.loads(path.read_text(encoding="utf-8"))
    table: dict[str, str] = {}
    for section in ("groups", "archives", "categories"):
        for item in document.get(section, []):
            name_zh = item.get("name_zh")
            if name_zh:
                table[item["id"]] = name_zh
    return table


def build_document(
    parsed: ParsedTaxonomy,
    canonical: SourceSnapshot,
    html_source: SourceSnapshot,
    names: dict[str, str],
    alias_targets: dict[str, str],
    translation_source: str,
) -> dict[str, Any]:
    """把解析结果 + 中文名表组装成正式 JSON 文档。"""
    active_ids, alias_ids = parse_canonical_schema(canonical.body)
    known_ids = parsed.category_ids()

    def zh(identifier: str) -> str:
        return names.get(identifier, "")

    def needs_translation(identifier: str) -> bool:
        return not names.get(identifier)

    groups = [
        {
            "id": g.id,
            "name_en": g.name_en,
            "name_zh": zh(g.id),
            "needs_translation": needs_translation(g.id),
        }
        for g in sorted(parsed.groups, key=lambda g: g.id)
    ]

    archives = [
        {
            "id": a.id,
            "group_id": a.group_id,
            "name_en": a.name_en,
            "name_zh": zh(a.id),
            "needs_translation": needs_translation(a.id),
        }
        for a in sorted(parsed.archives, key=lambda a: (a.group_id, a.id))
    ]

    categories = []
    for c in sorted(parsed.categories, key=lambda c: (c.group_id, c.archive_id, c.id)):
        alias_of = alias_targets.get(c.id)
        categories.append(
            {
                "id": c.id,
                "group_id": c.group_id,
                "archive_id": c.archive_id,
                "name_en": c.name_en,
                "name_zh": zh(c.id),
                "needs_translation": needs_translation(c.id),
                "active": c.id not in alias_ids,
                "alias_of": alias_of,
                "in_canonical_schema": c.id in active_ids,
            }
        )

    aliases = {k: v for k, v in sorted(alias_targets.items()) if k in known_ids}
    unknown_alias_sources = sorted(set(alias_ids) - known_ids)
    unknown_alias_targets = sorted(set(aliases.values()) - known_ids)

    return {
        "schema_version": SCHEMA_VERSION,
        "taxonomy_version": html_source.checked_at[:10],
        "translation_source": translation_source,
        "generated_by": "scripts/update_categories.py",
        "source": {
            "html_url": html_source.url,
            "html_sha256": html_source.sha256,
            "html_checked_at": html_source.checked_at,
            "html_bytes": len(html_source.body),
            "canonical_url": canonical.url,
            "canonical_sha256": canonical.sha256,
            "canonical_checked_at": canonical.checked_at,
            "canonical_bytes": len(canonical.body),
            "canonical_commit": canonical.commit,
        },
        "cross_check": {
            "html_categories": len(parsed.categories),
            "canonical_active_ids": len(active_ids),
            "canonical_alias_ids": len(alias_ids),
            "html_not_in_canonical": sorted(known_ids - active_ids),
            "canonical_not_in_html": sorted(active_ids - known_ids),
            "alias_sources_without_mapping": unknown_alias_sources,
            "alias_targets_missing": unknown_alias_targets,
        },
        "aliases": aliases,
        "groups": groups,
        "archives": archives,
        "categories": categories,
    }


def diff_documents(old: dict[str, Any] | None, new: dict[str, Any]) -> list[str]:
    """生成人工可读的结构 diff（新增 / 删除 / alias 变化 / 未翻译）。"""
    lines: list[str] = []
    if old is None:
        lines.append("（无既有正式文件：这是首次生成。）")
        lines.append(f"  新增 group {len(new['groups'])}、archive {len(new['archives'])}、"
                     f"category {len(new['categories'])}")
        return lines

    def index(document: dict[str, Any], section: str) -> dict[str, dict[str, Any]]:
        return {item["id"]: item for item in document.get(section, [])}

    changed = False
    for section in ("groups", "archives", "categories"):
        before, after = index(old, section), index(new, section)
        added = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        if added:
            changed = True
            lines.append(f"[{section}] 新增 {len(added)}：{', '.join(added)}")
        if removed:
            changed = True
            lines.append(f"[{section}] 删除 {len(removed)}：{', '.join(removed)}")
        renamed = sorted(
            k
            for k in set(before) & set(after)
            if before[k].get("name_en") != after[k].get("name_en")
        )
        for key in renamed:
            changed = True
            lines.append(
                f"[{section}] 英文名变化 {key}："
                f"{before[key].get('name_en')!r} → {after[key].get('name_en')!r}"
            )

    old_aliases = old.get("aliases", {})
    new_aliases = new.get("aliases", {})
    for key in sorted(set(old_aliases) | set(new_aliases)):
        if old_aliases.get(key) != new_aliases.get(key):
            changed = True
            lines.append(
                f"[alias] {key}：{old_aliases.get(key)!r} → {new_aliases.get(key)!r}"
            )

    untranslated = sorted(
        item["id"]
        for section in ("groups", "archives", "categories")
        for item in new.get(section, [])
        if item.get("needs_translation")
    )
    if untranslated:
        changed = True
        lines.append(f"[i18n] 缺少中文名 {len(untranslated)}：{', '.join(untranslated)}")

    if not changed:
        lines.append("无结构变化。")
    return lines


def preview(document: dict[str, Any], limit: int = 12) -> list[str]:
    lines = [f"taxonomy_version = {document['taxonomy_version']}"]
    cross = document["cross_check"]
    lines.append(
        f"HTML 分类 {cross['html_categories']}；schema active {cross['canonical_active_ids']}；"
        f"alias {cross['canonical_alias_ids']}"
    )
    if cross["html_not_in_canonical"]:
        lines.append(f"!! HTML 有但 schema 无：{cross['html_not_in_canonical']}")
    if cross["canonical_not_in_html"]:
        lines.append(f"!! schema 有但 HTML 无：{cross['canonical_not_in_html']}")
    if cross["alias_sources_without_mapping"]:
        lines.append(f"!! 别名缺映射：{cross['alias_sources_without_mapping']}")
    if cross["alias_targets_missing"]:
        lines.append(f"!! 别名目标不存在：{cross['alias_targets_missing']}")

    for group in document["groups"][:limit]:
        lines.append(
            f"  {group['id']:<8} {group['name_en']:<45} → {group['name_zh'] or '（缺中文名）'}"
        )
    return lines


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="update_categories.py",
        description="抓取官方 taxonomy，生成候选 JSON 并人工 diff；未经 --approve 不覆盖正式文件。",
    )
    parser.add_argument("--official", type=Path, default=DEFAULT_OFFICIAL, help="正式 JSON 路径。")
    parser.add_argument("--candidate", type=Path, default=DEFAULT_CANDIDATE, help="候选 JSON 路径。")
    parser.add_argument(
        "--translations",
        type=Path,
        default=DEFAULT_TRANSLATIONS,
        help="人工中文名种子文件。",
    )
    parser.add_argument(
        "--from-cache",
        type=Path,
        metavar="DIR",
        help="改用已下载的官方副本（离线重跑）。",
    )
    parser.add_argument(
        "--approve",
        action="store_true",
        help="人工审核通过后，把候选提升为正式文件。",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只报告是否有变化，不写候选文件（用于 CI 漂移检测）。",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _force_utf8_stdio()
    args = build_parser().parse_args(list(argv) if argv is not None else None)

    if args.from_cache:
        html_source, canonical_source = load_cached_sources(args.from_cache)
    else:
        print(f"抓取 {HTML_URL}")
        html_source, canonical_source = fetch_sources()
        print(f"抓取 {CANONICAL_URL}")

    parsed = parse_taxonomy_html(html_source.body.decode("utf-8", errors="replace"))
    if not parsed.groups or not parsed.categories:
        print("解析失败：没有从官方页面读到 group/category。", file=sys.stderr)
        return 1

    names = load_translations(args.translations) | load_existing_names(args.official)
    document = build_document(
        parsed,
        canonical_source,
        html_source,
        names,
        KNOWN_ALIAS_TARGETS,
        str(args.translations.relative_to(REPO_ROOT)) if args.translations.is_file() else "",
    )

    old: dict[str, Any] | None = None
    if args.official.is_file():
        old = json.loads(args.official.read_text(encoding="utf-8"))

    print("\n".join(preview(document)))
    print("\n—— 与正式文件的差异 ——")
    changes = diff_documents(old, document)
    print("\n".join(f"  {line}" for line in changes))

    if args.check:
        structural = [line for line in changes if not line.startswith("[i18n]")]
        return 0 if structural == ["无结构变化。"] else 1

    args.candidate.parent.mkdir(parents=True, exist_ok=True)
    args.candidate.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\n候选文件已写入 {args.candidate}")

    if args.approve:
        untranslated = [
            item["id"]
            for section in ("groups", "archives", "categories")
            for item in document[section]
            if item.get("needs_translation")
        ]
        if untranslated:
            print(
                f"\n拒绝提升：还有 {len(untranslated)} 个条目缺中文名，"
                f"请先在 {args.translations} 中补齐：\n  {', '.join(untranslated[:20])}",
                file=sys.stderr,
            )
            return 2
        args.official.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"正式文件已更新 {args.official}")
    else:
        print("未指定 --approve：正式文件保持不变。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
