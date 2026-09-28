from __future__ import annotations

import re
from pathlib import Path

from arxiv_filter import __version__
from scripts.lock_wheel_hashes import render_lock


def test_windows_version_resource_matches_package_version() -> None:
    root = Path(__file__).parents[2]
    text = (root / "packaging" / "version_info.txt").read_text(encoding="utf-8")
    parts = tuple(int(part) for part in __version__.split("."))
    assert len(parts) == 3
    version_tuple = f"({parts[0]}, {parts[1]}, {parts[2]}, 0)"
    assert re.search(rf"filevers={re.escape(version_tuple)}", text)
    assert re.search(rf"prodvers={re.escape(version_tuple)}", text)
    assert f"FileVersion', '{__version__}'" in text
    assert f"ProductVersion', '{__version__}'" in text


def test_pyinstaller_specs_bundle_only_explicit_runtime_resources() -> None:
    root = Path(__file__).parents[2]
    common = (root / "packaging" / "spec_common.py").read_text(encoding="utf-8")
    for relative in ("data", "static", "storage/migrations"):
        assert f'"{relative}"' in common
    assert "collect_runtime_binaries" in common
    # Conda side is searched via a directory tuple; sqlite3.dll stays collected.
    assert '"Library" / "bin"' in common
    assert '"sqlite3.dll"' in common
    assert "arxiv_filter" in common
    for name in ("arxiv_filter_onedir.spec", "arxiv_filter_onefile.spec"):
        spec = (root / "packaging" / name).read_text(encoding="utf-8")
        assert "upx=False" in spec
        assert "version=version_path" in spec
        assert "collect_runtime_data" in spec
        assert "binaries=runtime_binaries" in spec


def test_optional_icon_honours_env_override_without_shipping_artwork(tmp_path: Path) -> None:
    """图标不随仓库分发：靠 ARXIV_FILTER_ICON 指路，缺失时安静地不打图标。

    2026-09-27 决定：图标文件不进版本控制。因此 spec 必须支持外部路径，
    并且任何缺失/无效都要回退到"无图标"，绝不能让图标问题挡住发布构建。
    """
    import os
    import sys

    sys.path.insert(0, str(Path(__file__).parents[2] / "packaging"))
    try:
        from spec_common import optional_icon
    finally:
        sys.path.pop(0)

    project = tmp_path / "project"
    (project / "packaging").mkdir(parents=True)
    external = tmp_path / "artwork.ico"
    external.write_bytes(b"\x00\x00\x01\x00 icon fixture")

    saved = os.environ.pop("ARXIV_FILTER_ICON", None)
    try:
        # 1. 仓库内没有图标、也没有环境变量 -> 不打图标
        assert optional_icon(project) is None

        # 2. 环境变量指向真实文件 -> 使用该路径
        os.environ["ARXIV_FILTER_ICON"] = str(external)
        assert optional_icon(project) == str(external.resolve())

        # 3. 环境变量指向不存在的文件 -> 回退，而不是抛错
        os.environ["ARXIV_FILTER_ICON"] = str(tmp_path / "missing.ico")
        assert optional_icon(project) is None

        # 4. 仓库内确实放了图标时仍可用（保留此能力，尽管默认不使用）
        in_repo = project / "packaging" / "arxiv-filter.ico"
        in_repo.write_bytes(b"\x00\x00\x01\x00 icon fixture")
        os.environ.pop("ARXIV_FILTER_ICON", None)
        assert optional_icon(project) == str(in_repo)
    finally:
        if saved is None:
            os.environ.pop("ARXIV_FILTER_ICON", None)
        else:
            os.environ["ARXIV_FILTER_ICON"] = saved


def test_icon_artwork_stays_out_of_version_control() -> None:
    """美术资源不进仓库：packaging 下的 .ico 必须被 .gitignore 覆盖。"""
    root = Path(__file__).parents[2]
    ignored = (root / ".gitignore").read_text(encoding="utf-8")
    assert "packaging/*.ico" in ignored
    assert "ARXIV_FILTER_ICON" in ignored, "应在忽略规则里写明带图标的构建方式"


def test_build_hash_lock_handles_requirement_extras(tmp_path: Path) -> None:
    resolved = tmp_path / "resolved.txt"
    resolved.write_text("coverage[toml]==7.16.1\n", encoding="utf-8")
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheel = wheelhouse / "coverage-7.16.1-cp312-cp312-win_amd64.whl"
    wheel.write_bytes(b"deterministic hash fixture")

    lock = render_lock(resolved, wheelhouse)

    assert "coverage==7.16.1 \\" in lock
    assert "--hash=sha256:" in lock
