"""内置分类快照的运行时加载与解析。

对应方案 §4.2「分类表」一行与 §5.9：

* ``load_taxonomy() -> Taxonomy``；``resolve_categories(group, child)``；
* **运行时不抓网页**；未知 ID 返回中文 422；别名明确记录；
* 中文名只用于展示，绝不参与 ``search_query``。

快照由 ``scripts/update_categories.py`` 生成、``scripts/validate_categories.py`` 校验，
随包发布在 ``data/categories.zh-CN.json``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from arxiv_filter.errors import ArxivFilterError, ErrorCode
from arxiv_filter.paths import resource_path

#: 本模块支持的快照 schema 版本。
SUPPORTED_SCHEMA_VERSION = 1

#: 快照的默认位置（只读资源）。
DEFAULT_TAXONOMY_FILE = ("data", "categories.zh-CN.json")


@dataclass(frozen=True, slots=True)
class Group:
    """一级分类（官方 Group）。"""

    id: str
    name_en: str
    name_zh: str


@dataclass(frozen=True, slots=True)
class Archive:
    """二级归档（官方 Archive）。单 archive 组的 id 与 group 相同。"""

    id: str
    group_id: str
    name_en: str
    name_zh: str


@dataclass(frozen=True, slots=True)
class Category:
    """叶子分类。``alias_of`` 非空表示它是非规范的旧 ID。"""

    id: str
    group_id: str
    archive_id: str
    name_en: str
    name_zh: str
    active: bool
    alias_of: str | None


class Taxonomy:
    """不可变的分类快照视图。"""

    __slots__ = ("_aliases", "_archives", "_categories", "_groups", "schema_version", "source", "taxonomy_version")

    def __init__(self, document: Mapping[str, Any]) -> None:
        self.schema_version: int = int(document.get("schema_version", 0))
        self.taxonomy_version: str = str(document.get("taxonomy_version", ""))
        self.source: Mapping[str, Any] = dict(document.get("source", {}))
        self._aliases: dict[str, str] = dict(document.get("aliases", {}))
        self._groups = tuple(
            Group(id=g["id"], name_en=g["name_en"], name_zh=g.get("name_zh", ""))
            for g in document.get("groups", [])
        )
        self._archives = tuple(
            Archive(
                id=a["id"],
                group_id=a["group_id"],
                name_en=a["name_en"],
                name_zh=a.get("name_zh", ""),
            )
            for a in document.get("archives", [])
        )
        self._categories = tuple(
            Category(
                id=c["id"],
                group_id=c["group_id"],
                archive_id=c["archive_id"],
                name_en=c["name_en"],
                name_zh=c.get("name_zh", ""),
                active=bool(c.get("active", True)),
                alias_of=c.get("alias_of"),
            )
            for c in document.get("categories", [])
        )
        self._validate()

    # —— 结构校验 ——

    def _validate(self) -> None:
        """加载即校验（方案 §4.8：分类表损坏时阻止搜索，不联网临时抓页面）。"""
        if self.schema_version != SUPPORTED_SCHEMA_VERSION:
            raise ArxivFilterError(
                ErrorCode.TAXONOMY_CORRUPT,
                details={
                    "schema_version": self.schema_version,
                    "supported": SUPPORTED_SCHEMA_VERSION,
                },
            )
        if not self._groups or not self._categories:
            raise ArxivFilterError(
                ErrorCode.TAXONOMY_CORRUPT, details={"reason": "empty_taxonomy"}
            )

        group_ids = {g.id for g in self._groups}
        archive_ids = {a.id for a in self._archives}
        category_ids = {c.id for c in self._categories}
        if len(category_ids) != len(self._categories):
            raise ArxivFilterError(
                ErrorCode.TAXONOMY_CORRUPT, details={"reason": "duplicate_category_id"}
            )

        for archive in self._archives:
            if archive.group_id not in group_ids:
                raise ArxivFilterError(
                    ErrorCode.TAXONOMY_CORRUPT,
                    details={"reason": "orphan_archive", "archive": archive.id},
                )
        for category in self._categories:
            if category.group_id not in group_ids:
                raise ArxivFilterError(
                    ErrorCode.TAXONOMY_CORRUPT,
                    details={"reason": "orphan_category_group", "category": category.id},
                )
            if category.archive_id not in archive_ids:
                raise ArxivFilterError(
                    ErrorCode.TAXONOMY_CORRUPT,
                    details={"reason": "orphan_category_archive", "category": category.id},
                )
        for alias, target in self._aliases.items():
            if alias not in category_ids or target not in category_ids:
                raise ArxivFilterError(
                    ErrorCode.TAXONOMY_CORRUPT,
                    details={"reason": "dangling_alias", "alias": alias, "target": target},
                )
            if target in self._aliases:
                raise ArxivFilterError(
                    ErrorCode.TAXONOMY_CORRUPT,
                    details={"reason": "multi_level_alias", "alias": alias, "target": target},
                )

    # —— 查询 ——

    @property
    def groups(self) -> tuple[Group, ...]:
        return self._groups

    @property
    def archives(self) -> tuple[Archive, ...]:
        return self._archives

    @property
    def categories(self) -> tuple[Category, ...]:
        return self._categories

    @property
    def aliases(self) -> Mapping[str, str]:
        return dict(self._aliases)

    def group(self, group_id: str) -> Group:
        for group in self._groups:
            if group.id == group_id:
                return group
        raise ArxivFilterError(
            ErrorCode.UNKNOWN_CATEGORY,
            details={"group_id": group_id},
            message_zh=f"分类表里没有「{group_id}」这个一级分类，请重新选择。",
        )

    def archive(self, archive_id: str) -> Archive:
        for archive in self._archives:
            if archive.id == archive_id:
                return archive
        raise ArxivFilterError(
            ErrorCode.UNKNOWN_CATEGORY,
            details={"archive_id": archive_id},
            message_zh=f"分类表里没有「{archive_id}」这个二级归档，请重新选择。",
        )

    def category(self, category_id: str) -> Category:
        for category in self._categories:
            if category.id == category_id:
                return category
        raise ArxivFilterError(
            ErrorCode.UNKNOWN_CATEGORY,
            details={"category_id": category_id},
            message_zh=f"分类表里没有「{category_id}」这个分类，请更新应用或改选其它分类。",
        )

    def archives_of_group(self, group_id: str) -> tuple[Archive, ...]:
        """某一级分类下的全部归档，按 id 稳定排序。"""
        self.group(group_id)
        return tuple(a for a in self._archives if a.group_id == group_id)

    def categories_of_group(self, group_id: str) -> tuple[Category, ...]:
        self.group(group_id)
        return tuple(c for c in self._categories if c.group_id == group_id)

    def categories_of_archive(self, archive_id: str) -> tuple[Category, ...]:
        self.archive(archive_id)
        return tuple(c for c in self._categories if c.archive_id == archive_id)

    def canonicalize(self, category_id: str) -> str:
        """把旧别名解析成规范 ID；未知 ID 抛中文 ``UNKNOWN_CATEGORY``。"""
        self.category(category_id)  # 未知即抛错
        return self._aliases.get(category_id, category_id)

    def canonical_ids(self, category_ids: Any) -> tuple[str, ...]:
        """批量规范化，去重后按字典序排序（保证 cache key 稳定）。"""
        return tuple(sorted({self.canonicalize(cid) for cid in category_ids}))

    def leaves_of_group(self, group_id: str) -> tuple[str, ...]:
        """``全部 <Group>``：该一级分类下所有规范叶子分类。"""
        return self.canonical_ids(c.id for c in self.categories_of_group(group_id))

    def leaves_of_archive(self, archive_id: str) -> tuple[str, ...]:
        """``全部 <Archive>``：该归档下所有规范叶子分类。"""
        return self.canonical_ids(c.id for c in self.categories_of_archive(archive_id))

    def all_leaves(self) -> tuple[str, ...]:
        """全部规范叶子分类（用于「全选」与查询长度预算检查）。"""
        return self.canonical_ids(c.id for c in self._categories)

    def label_zh(self, category_id: str) -> str:
        """展示用中文名；未知 ID 返回原 ID，不抛错（列表渲染路径不应失败）。"""
        for category in self._categories:
            if category.id == category_id:
                return category.name_zh or category.name_en
        return category_id


def load_taxonomy(path: Path | None = None) -> Taxonomy:
    """加载并缓存内置分类快照。

    参数
    ----
    path:
        覆盖快照位置（测试用）。为 ``None`` 时读取随包发布的默认文件。
    """
    if path is None:
        return _load_cached()
    return Taxonomy(json.loads(path.read_text(encoding="utf-8")))


@lru_cache(maxsize=1)
def _load_cached() -> Taxonomy:
    target = resource_path(*DEFAULT_TAXONOMY_FILE)
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ArxivFilterError(
            ErrorCode.TAXONOMY_CORRUPT,
            details={"path": str(target), "reason": "unreadable"},
        ) from exc
    except json.JSONDecodeError as exc:
        raise ArxivFilterError(
            ErrorCode.TAXONOMY_CORRUPT,
            details={"path": str(target), "reason": "invalid_json"},
        ) from exc
    return Taxonomy(document)


def resolve_categories(
    group_id: str,
    category_id: str | None = None,
    *,
    taxonomy: Taxonomy | None = None,
) -> tuple[str, ...]:
    """把 UI 的两级选择解析成**规范** category ID 列表（方案 §4.2、§5.2）。

    * ``category_id`` 为 ``None`` → 「全部子类」：该一级分类下所有叶子分类；
    * ``category_id`` 为具体 ID → 只返回该分类，且别名会被解析成规范 ID；
    * 返回值恒为去重、排序后的元组，供 cache key 与 OR 表达式使用。
    """
    table = taxonomy or load_taxonomy()
    selected = table.group(group_id)  # 未知 group 立即抛中文 422
    if category_id is None:
        return table.leaves_of_group(selected.id)

    category = table.category(category_id)  # 未知 category 立即抛中文 422
    if category.group_id != selected.id:
        raise ArxivFilterError(
            ErrorCode.UNKNOWN_CATEGORY,
            details={"group_id": selected.id, "category_id": category.id},
            message_zh=(
                f"「{category.name_zh or category.id}」不属于"
                f"「{selected.name_zh or selected.id}」，请重新选择。"
            ),
        )
    return (table.canonicalize(category.id),)


__all__ = [
    "DEFAULT_TAXONOMY_FILE",
    "SUPPORTED_SCHEMA_VERSION",
    "Archive",
    "Category",
    "Group",
    "Taxonomy",
    "load_taxonomy",
    "resolve_categories",
]
