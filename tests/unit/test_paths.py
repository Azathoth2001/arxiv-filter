"""路径解析与便携模式判定。"""

from __future__ import annotations

from pathlib import Path

import pytest

from arxiv_filter import paths


def test_data_dir_honours_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "override"
    monkeypatch.setenv("ARXIV_FILTER_DATA_DIR", str(target))
    assert paths.data_dir() == target.resolve()


def test_derived_paths_live_under_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "root"
    monkeypatch.setenv("ARXIV_FILTER_DATA_DIR", str(target))

    root = paths.data_dir()
    assert paths.config_path() == root / "config.json"
    assert paths.db_path() == root / "arxiv-filter.db"
    assert paths.runtime_path() == root / "runtime.json"
    assert paths.log_path() == root / "logs" / "app.log"
    assert paths.exports_dir() == root / "exports"
    assert paths.backup_dir() == root / "backup"


def test_ensure_dirs_creates_writable_subdirectories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARXIV_FILTER_DATA_DIR", str(tmp_path / "fresh"))
    paths.ensure_dirs()
    for directory in (paths.data_dir(), paths.logs_dir(), paths.exports_dir(), paths.backup_dir()):
        assert directory.is_dir()

    # 幂等：再次调用不应抛错
    paths.ensure_dirs()


def test_resource_path_points_inside_package() -> None:
    root = paths.resource_root()
    assert (root / "__init__.py").is_file()
    assert paths.resource_path("data", "categories.zh-CN.json").parent == root / "data"
    assert paths.static_dir() == root / "static"
    assert paths.migrations_dir() == root / "storage" / "migrations"


def test_source_mode_uses_package_directory() -> None:
    assert paths.is_frozen() is False
    assert paths.resource_root() == Path(paths.__file__).resolve().parent


def test_portable_mode_absent_without_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paths, "executable_dir", lambda: tmp_path)
    decision = paths.evaluate_portable_mode()
    assert decision.enabled is False
    assert "portable.flag" in decision.reason_zh


def test_portable_mode_enabled_in_writable_directory_with_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / paths.PORTABLE_FLAG).write_text("", encoding="utf-8")
    monkeypatch.setattr(paths, "executable_dir", lambda: tmp_path)
    decision = paths.evaluate_portable_mode()
    assert decision.enabled is True
    assert decision.root == tmp_path / paths.PORTABLE_DATA_DIR


def test_portable_mode_rejected_under_program_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    program_files = tmp_path / "Program Files" / "ArxivFilter"
    program_files.mkdir(parents=True)
    (program_files / paths.PORTABLE_FLAG).write_text("", encoding="utf-8")

    monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files"))
    monkeypatch.setattr(paths, "executable_dir", lambda: program_files)

    decision = paths.evaluate_portable_mode()
    assert decision.enabled is False
    assert "受保护目录" in decision.reason_zh


@pytest.mark.parametrize("flag_name", [paths.PORTABLE_FLAG])
def test_portable_flag_name_is_stable(flag_name: str) -> None:
    assert flag_name == "portable.flag"
