"""阶段 0 骨架冒烟测试：入口契约、版本信息、错误信封。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from arxiv_filter import ARXIV_ACKNOWLEDGEMENT, SCHEMA_VERSION, __version__
from arxiv_filter.__main__ import main as module_main
from arxiv_filter.launcher import build_parser, main as launcher_main, version_text


def test_version_is_semver() -> None:
    parts = __version__.split(".")
    assert len(parts) == 3
    assert all(part.isdigit() for part in parts)


def test_schema_version_starts_at_one() -> None:
    assert SCHEMA_VERSION == 1


def test_acknowledgement_matches_required_wording() -> None:
    assert ARXIV_ACKNOWLEDGEMENT == (
        "Thank you to arXiv for use of its open access interoperability."
    )


def test_version_text_mentions_version_and_schema(capsys: pytest.CaptureFixture[str]) -> None:
    assert launcher_main(["--version"]) == 0
    out = capsys.readouterr().out.strip()
    assert out == version_text()
    assert __version__ in out
    assert f"schema {SCHEMA_VERSION}" in out


def test_module_main_reaches_launcher(capsys: pytest.CaptureFixture[str]) -> None:
    assert module_main(["--version"]) == 0
    assert __version__ in capsys.readouterr().out


def test_run_without_version_reports_phase0_and_exits_nonzero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert launcher_main([]) == 1
    err = capsys.readouterr().err
    assert "T4.3" in err


def test_help_lists_supported_flags() -> None:
    parser = build_parser()
    help_text = parser.format_help()
    for flag in ("--version", "--no-browser", "--port", "--data-dir", "--log-level"):
        assert flag in help_text


def test_data_dir_flag_overrides_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arxiv_filter import paths

    override = tmp_path / "custom"
    monkeypatch.setenv("ARXIV_FILTER_DATA_DIR", str(tmp_path / "default"))
    launcher_main(["--data-dir", str(override)])
    assert os.environ["ARXIV_FILTER_DATA_DIR"] == str(override)
    assert paths.data_dir() == override.resolve()


def test_unknown_flag_exits_with_usage_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        launcher_main(["--definitely-not-a-flag"])
    assert excinfo.value.code == 2
