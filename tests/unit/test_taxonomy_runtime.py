"""taxonomy 运行时加载与解析（方案 §4.2、§5.9）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arxiv_filter.errors import ArxivFilterError, ErrorCode
from arxiv_filter.paths import resource_path
from arxiv_filter.taxonomy import Taxonomy, load_taxonomy, resolve_categories


@pytest.fixture(scope="module")
def table() -> Taxonomy:
    return load_taxonomy()


def snapshot() -> dict:
    return json.loads(resource_path("data", "categories.zh-CN.json").read_text(encoding="utf-8"))


# —— 加载 ——


def test_bundled_snapshot_loads(table: Taxonomy) -> None:
    assert table.schema_version == 1
    assert len(table.groups) == 8
    assert len(table.archives) == 20
    assert len(table.categories) == 155


def test_load_taxonomy_is_cached() -> None:
    assert load_taxonomy() is load_taxonomy()


def test_load_taxonomy_from_explicit_path(table: Taxonomy, tmp_path: Path) -> None:
    target = tmp_path / "copy.json"
    target.write_text(json.dumps(snapshot(), ensure_ascii=False), encoding="utf-8")
    loaded = load_taxonomy(target)
    assert loaded.taxonomy_version == table.taxonomy_version
    assert loaded is not table


# —— 结构校验 ——


def test_unsupported_schema_version_is_rejected() -> None:
    broken = snapshot() | {"schema_version": 99}
    with pytest.raises(ArxivFilterError) as excinfo:
        Taxonomy(broken)
    assert excinfo.value.code is ErrorCode.TAXONOMY_CORRUPT


def test_empty_taxonomy_is_rejected() -> None:
    with pytest.raises(ArxivFilterError) as excinfo:
        Taxonomy({"schema_version": 1, "groups": [], "archives": [], "categories": []})
    assert excinfo.value.code is ErrorCode.TAXONOMY_CORRUPT


def test_duplicate_category_id_is_rejected() -> None:
    broken = snapshot()
    broken["categories"] = [*broken["categories"], dict(broken["categories"][0])]
    with pytest.raises(ArxivFilterError) as excinfo:
        Taxonomy(broken)
    assert excinfo.value.details["reason"] == "duplicate_category_id"


def test_orphan_archive_is_rejected() -> None:
    broken = snapshot()
    broken["archives"] = [*broken["archives"], {"id": "ghost", "group_id": "nope", "name_en": "x", "name_zh": "x"}]
    with pytest.raises(ArxivFilterError) as excinfo:
        Taxonomy(broken)
    assert excinfo.value.details["reason"] == "orphan_archive"


def test_orphan_category_archive_is_rejected() -> None:
    broken = snapshot()
    broken["categories"] = [
        dict(broken["categories"][0], archive_id="ghost"),
        *broken["categories"][1:],
    ]
    with pytest.raises(ArxivFilterError) as excinfo:
        Taxonomy(broken)
    assert excinfo.value.details["reason"] == "orphan_category_archive"


def test_dangling_alias_is_rejected() -> None:
    broken = snapshot() | {"aliases": {"cs.SY": "does.not.exist"}}
    with pytest.raises(ArxivFilterError) as excinfo:
        Taxonomy(broken)
    assert excinfo.value.details["reason"] == "dangling_alias"


def test_multi_level_alias_is_rejected() -> None:
    """别名不能再指向别名——否则 canonicalize 会静默漏掉一层。"""
    broken = snapshot() | {"aliases": {"cs.SY": "eess.SY", "math.MP": "cs.SY"}}
    with pytest.raises(ArxivFilterError) as excinfo:
        Taxonomy(broken)
    assert excinfo.value.details["reason"] == "multi_level_alias"


# —— 查询 ——


def test_group_lookup(table: Taxonomy) -> None:
    group = table.group("cs")
    assert group.name_en == "Computer Science"
    assert group.name_zh == "计算机科学"


def test_unknown_group_raises_chinese_error(table: Taxonomy) -> None:
    with pytest.raises(ArxivFilterError) as excinfo:
        table.group("nope")
    assert excinfo.value.code is ErrorCode.UNKNOWN_CATEGORY
    assert "nope" in excinfo.value.message_zh
    assert excinfo.value.http_status == 422


def test_unknown_category_raises_chinese_error(table: Taxonomy) -> None:
    with pytest.raises(ArxivFilterError) as excinfo:
        table.category("cs.NOPE")
    assert excinfo.value.code is ErrorCode.UNKNOWN_CATEGORY
    assert "cs.NOPE" in excinfo.value.message_zh


def test_canonicalize_resolves_known_aliases(table: Taxonomy) -> None:
    assert table.canonicalize("cs.SY") == "eess.SY"
    assert table.canonicalize("math.MP") == "math-ph"
    assert table.canonicalize("cs.CL") == "cs.CL"


def test_canonicalize_rejects_unknown_id(table: Taxonomy) -> None:
    with pytest.raises(ArxivFilterError):
        table.canonicalize("cs.NOPE")


def test_canonical_ids_are_sorted_and_deduplicated(table: Taxonomy) -> None:
    assert table.canonical_ids(["cs.CL", "cs.AI", "cs.CL"]) == ("cs.AI", "cs.CL")


def test_canonical_ids_collapse_alias_to_target(table: Taxonomy) -> None:
    """cs.SY 与 eess.SY 是同一个分类，规范化后必须只剩一个。"""
    assert table.canonical_ids(["cs.SY", "eess.SY"]) == ("eess.SY",)


def test_archives_of_group(table: Taxonomy) -> None:
    ids = [a.id for a in table.archives_of_group("physics")]
    assert ids == [
        "astro-ph", "cond-mat", "gr-qc", "hep-ex", "hep-lat", "hep-ph",
        "hep-th", "math-ph", "nlin", "nucl-ex", "nucl-th", "physics", "quant-ph",
    ]
    assert [a.id for a in table.archives_of_group("cs")] == ["cs"]


def test_leaves_of_group_excludes_aliases(table: Taxonomy) -> None:
    """组内展开时，别名必须被换成规范 ID——即使规范 ID 不属于本组。

    ``cs`` 组官方条目里包含 ``cs.SY``，而它是 ``eess.SY`` 的别名
    （arXiv 把 Systems and Control 移到了 eess）。所以「全部 cs 子类」
    应当包含 ``eess.SY``：它就是这个组的 Systems and Control 分类，
    只是换了个规范名字。**绝不能**把 ``cs.SY`` 本身发出去。
    """
    leaves = table.leaves_of_group("cs")

    assert "cs.AI" in leaves
    assert "cs.SY" not in leaves, "别名不得出现在查询里"
    assert "eess.SY" in leaves, "别名必须换成规范 ID，且仍然属于本组结果"
    assert leaves == tuple(sorted(leaves))
    assert len(leaves) == len(set(leaves))
    # cs 有 40 个官方条目，其中 cs.SY 是别名。改名后 cs.SY 换成 eess.SY，
    # 而 eess.SY 本来不在 cs 组里，所以条目数与规范 ID 数仍然都是 40。
    assert len(leaves) == 40


def test_group_expansion_never_emits_an_alias(table: Taxonomy) -> None:
    """对每一个组都成立：展开结果与别名集合不相交。"""
    aliases = set(table.aliases)
    for group in table.groups:
        leaves = set(table.leaves_of_group(group.id))
        assert not (leaves & aliases), group.id


def test_alias_and_target_never_both_appear_in_one_expansion(table: Taxonomy) -> None:
    """cs.SY 与 eess.SY 是同一分类，展开后不能同时出现（去重必须生效）。"""
    for group in table.groups:
        leaves = table.leaves_of_group(group.id)
        for alias, target in table.aliases.items():
            assert not (alias in leaves and target in leaves), group.id


def test_leaves_of_archive(table: Taxonomy) -> None:
    leaves = table.leaves_of_archive("astro-ph")
    assert leaves == (
        "astro-ph.CO", "astro-ph.EP", "astro-ph.GA",
        "astro-ph.HE", "astro-ph.IM", "astro-ph.SR",
    )


def test_all_leaves_counts_canonical_only(table: Taxonomy) -> None:
    leaves = table.all_leaves()
    assert len(leaves) == 149          # 155 - 6 个别名
    assert not set(leaves) & set(table.aliases)


def test_label_zh_is_display_only(table: Taxonomy) -> None:
    assert table.label_zh("cs.CL") == "计算与语言"
    assert table.label_zh("nope") == "nope"   # 渲染路径不抛错


# —— resolve_categories ——


def test_resolve_specific_category(table: Taxonomy) -> None:
    assert resolve_categories("cs", "cs.CL", taxonomy=table) == ("cs.CL",)


def test_resolve_none_means_all_children(table: Taxonomy) -> None:
    resolved = resolve_categories("cs", None, taxonomy=table)
    assert resolved == table.leaves_of_group("cs")
    assert len(resolved) > 1


def test_resolve_alias_returns_canonical_target(table: Taxonomy) -> None:
    assert resolve_categories("eess", "eess.SY", taxonomy=table) == ("eess.SY",)


def test_resolve_rejects_category_from_another_group(table: Taxonomy) -> None:
    with pytest.raises(ArxivFilterError) as excinfo:
        resolve_categories("cs", "math.NT", taxonomy=table)
    assert excinfo.value.code is ErrorCode.UNKNOWN_CATEGORY
    assert "不属于" in excinfo.value.message_zh


def test_resolve_rejects_unknown_group(table: Taxonomy) -> None:
    with pytest.raises(ArxivFilterError):
        resolve_categories("nope", None, taxonomy=table)


def test_resolve_rejects_unknown_category(table: Taxonomy) -> None:
    with pytest.raises(ArxivFilterError):
        resolve_categories("cs", "cs.NOPE", taxonomy=table)


def test_every_group_resolves_to_non_empty_leaves(table: Taxonomy) -> None:
    for group in table.groups:
        assert resolve_categories(group.id, None, taxonomy=table), group.id


def test_names_zh_are_never_used_in_lookup_keys(table: Taxonomy) -> None:
    """中文名只用于展示；用中文名当分组 ID 必须失败。"""
    with pytest.raises(ArxivFilterError):
        table.group("计算机科学")
