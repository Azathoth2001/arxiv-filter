# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import sys

SPEC_DIR = Path(SPECPATH).resolve()
PROJECT_ROOT = SPEC_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(SPEC_DIR))

from spec_common import collect_runtime_binaries, collect_runtime_data, optional_icon

datas = collect_runtime_data(PROJECT_ROOT)
runtime_binaries = collect_runtime_binaries(Path(sys.base_prefix))
icon_path = optional_icon(PROJECT_ROOT)
version_path = str(SPEC_DIR / "version_info.txt")

a = Analysis(
    [str(PROJECT_ROOT / "src" / "arxiv_filter" / "__main__.py")],
    pathex=[str(PROJECT_ROOT / "src")],
    binaries=runtime_binaries,
    datas=datas,
    hiddenimports=[
        "uvicorn.loops.asyncio",
        "uvicorn.protocols.http.h11_impl",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "pytest_asyncio", "respx", "playwright"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="arxiv-filter",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=icon_path,
    version=version_path,
)
