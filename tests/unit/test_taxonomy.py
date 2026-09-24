"""内置分类快照（T0.3）的离线校验。

`scripts/validate_categories.py` 是给人跑的命令行工具；这里把同一套规则接进
pytest，让分类表损坏在每次提交都被拦住（方案 §4.8「分类表损坏」行要求
阻止搜索而不是联网临时抓页面）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from arxiv_filter.paths import resource_path

REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATOR_PATH = REPO_ROOT / "scripts" / "validate_categories.py"
TRANSLATIONS_PATH = REPO_ROOT / "scripts" / "data" / "translations.zh-CN.json"
CONVERTER_PATH = REPO_ROOT / "scripts" / "update_categories.py"


def _load_script(path: Path, name: str) -> ModuleType:
    """按文件路径导入 scripts/ 下的脚本（它不在包内）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def validator() -> ModuleType:
    return _load_script(VALIDATOR_PATH, "_validate_categories")


@pytest.fixture(scope="module")
def taxonomy() -> dict:
    return json.loads(resource_path("data", "categories.zh-CN.json").read_text(encoding="utf-8"))


def test_official_snapshot_passes_all_checks(validator: ModuleType, taxonomy: dict) -> None:
    report = validator.validate(taxonomy)
    assert report.errors == []
    assert report.checks > 500


def test_snapshot_carries_source_provenance(taxonomy: dict) -> None:
    source = taxonomy["source"]
    assert source["html_url"] == "https://arxiv.org/category_taxonomy"
    assert "arxiv-canonical" in source["canonical_url"]
    for key in ("html_sha256", "canonical_sha256"):
        assert len(source[key]) == 64
    assert source["html_checked_at"].endswith("Z")


def test_snapshot_shape_is_stable(taxonomy: dict) -> None:
    assert taxonomy["schema_version"] == 1
    assert len(taxonomy["groups"]) == 8
    # Physics 组有 13 个具名 archive，其余 7 个组各有一个与组同名的 archive
    assert len(taxonomy["archives"]) == 20
    assert len(taxonomy["categories"]) == 155


def test_every_category_has_a_reviewed_chinese_name(taxonomy: dict) -> None:
    untranslated = [
        item["id"]
        for section in ("groups", "archives", "categories")
        for item in taxonomy[section]
        if item.get("needs_translation") or not item.get("name_zh")
    ]
    assert untranslated == []


def test_known_aliases_are_preserved(taxonomy: dict) -> None:
    """方案 §5.9 列出的 6 个别名必须仍然存在且方向正确。"""
    assert taxonomy["aliases"] == {
        "math.MP": "math-ph",
        "stat.TH": "math.ST",
        "math.IT": "cs.IT",
        "q-fin.EC": "econ.GN",
        "cs.SY": "eess.SY",
        "cs.NA": "math.NA",
    }
    for alias, target in taxonomy["aliases"].items():
        entry = next(c for c in taxonomy["categories"] if c["id"] == alias)
        assert entry["active"] is False
        assert entry["alias_of"] == target


def test_cs_cl_is_present_and_active(taxonomy: dict) -> None:
    """A2 验收场景依赖 cs.CL 存在且是规范分类。"""
    entry = next(c for c in taxonomy["categories"] if c["id"] == "cs.CL")
    assert entry["name_zh"] == "计算与语言"
    assert entry["group_id"] == "cs"
    assert entry["active"] is True
    assert entry["alias_of"] is None


def test_translation_seed_covers_every_id(taxonomy: dict) -> None:
    converter = _load_script(CONVERTER_PATH, "_update_categories")
    names = converter.load_translations(TRANSLATIONS_PATH)
    missing = [
        item["id"]
        for section in ("groups", "archives", "categories")
        for item in taxonomy[section]
        if item["id"] not in names
    ]
    assert missing == []
    for section in ("groups", "archives", "categories"):
        for item in taxonomy[section]:
            assert names[item["id"]] == item["name_zh"], item["id"]


def test_validator_rejects_alias_cycle(validator: ModuleType, taxonomy: dict) -> None:
    """别名环必须被拒绝（方案 §5.9 第 6 条）。"""
    broken = json.loads(json.dumps(taxonomy))
    broken["aliases"] = {"cs.SY": "eess.SY", "eess.SY": "cs.SY"}
    report = validator.validate(broken)
    assert report.errors


def test_validator_rejects_missing_translation(validator: ModuleType, taxonomy: dict) -> None:
    broken = json.loads(json.dumps(taxonomy))
    broken["categories"][0]["name_zh"] = ""
    report = validator.validate(broken)
    assert any("缺中文名" in error for error in report.errors)


def test_validator_rejects_unstable_ordering(validator: ModuleType, taxonomy: dict) -> None:
    broken = json.loads(json.dumps(taxonomy))
    broken["groups"] = list(reversed(broken["groups"]))
    report = validator.validate(broken)
    assert any("稳定排序" in error for error in report.errors)


def test_validator_allows_untranslated_when_asked(validator: ModuleType, taxonomy: dict) -> None:
    broken = json.loads(json.dumps(taxonomy))
    broken["categories"][0]["name_zh"] = ""
    report = validator.validate(broken, allow_untranslated=True)
    assert not any("缺中文名" in error for error in report.errors)
    assert any("缺中文名" in warning for warning in report.warnings)


def test_html_parser_reads_the_official_layout() -> None:
    """用一段最小 HTML 固定解析契约：Group / Archive / Category 三级。"""
    converter = _load_script(CONVERTER_PATH, "_update_categories_parse")
    html = """
    <div id="category_taxonomy_list">
      <h2 class="accordion-head" id="accordion-head-grp_cs">
        <button type="button"><span class="accordion-caret"></span>Computer Science</button>
      </h2>
      <div class="accordion-body" id="accordion-panel-grp_cs">
        <div class="columns divided">
          <div class="column is-one-fifth"><h4>cs.CL <span>(Computation and Language)</span></h4></div>
        </div>
      </div>
      <h2 class="accordion-head" id="accordion-head-grp_physics">
        <button type="button"><span class="accordion-caret"></span>Physics</button>
      </h2>
      <div class="accordion-body" id="accordion-panel-grp_physics">
        <div class="physics columns"><div class="column is-one-fifth">
          <h3>Astrophysics<br><span>(astro-ph)</span></h3>
        </div><div class="column">
          <div class="columns divided">
            <div class="column is-one-fifth"><h4>astro-ph.CO <span>(Cosmology)</span></h4></div>
          </div>
        </div></div>
        <div class="physics columns"><div class="column is-one-fifth">
          <h3>General Relativity and Quantum Cosmology<br><span>(gr-qc)</span></h3>
        </div><div class="column">
          <div class="columns divided">
            <div class="column is-one-fifth"><h4>gr-qc <span>(General Relativity)</span></h4></div>
          </div>
        </div></div>
      </div>
    </div>
    """
    parsed = converter.parse_taxonomy_html(html)

    assert [(g.id, g.name_en) for g in parsed.groups] == [
        ("cs", "Computer Science"),
        ("physics", "Physics"),
    ]
    # 单 archive 组的 archive id/name 等于组自身
    assert {(a.id, a.group_id) for a in parsed.archives} == {
        ("cs", "cs"),
        ("astro-ph", "physics"),
        ("gr-qc", "physics"),
    }
    # Physics 组的占位 archive 必须被真正的 <h3> 替换，不能残留
    assert not any(a.id == "physics" for a in parsed.archives)
    assert [(c.id, c.archive_id, c.group_id) for c in parsed.categories] == [
        ("cs.CL", "cs", "cs"),
        ("astro-ph.CO", "astro-ph", "physics"),
        ("gr-qc", "gr-qc", "physics"),
    ]


def test_html_parser_ignores_the_guide_header() -> None:
    """说明区的 "Archive Name (Archive ID)" / "Category Name (Category ID)" 不能被当成数据。"""
    converter = _load_script(CONVERTER_PATH, "_update_categories_header")
    html = """
    <div class="column guide">
      <h3>Archive Name (Archive ID)</h3>
      <h4>Category Name<br><strong>(Category ID)</strong></h4>
    </div>
    <div id="category_taxonomy_list"></div>
    """
    parsed = converter.parse_taxonomy_html(html)
    assert parsed.groups == []
    assert parsed.categories == []
