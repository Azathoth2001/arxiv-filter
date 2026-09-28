"""Discovery regressions for packaging/spec_common.py, using synthetic PEs.

The real environment cannot be assumed on every machine, so these tests build a
tiny Conda-shaped fixture (DLLs/*.pyd + Library/bin/*.dll) and check that:

* discovery finds a dependency that lives in Library/bin,
* it ignores system DLLs entirely,
* it never returns a name that is absent from Library/bin,
* and collect_runtime_binaries always sources files from Library/bin.

Real PE files are needed for pefile to read an import table. Rather than ship a
binary fixture, these tests reuse a real extension module already present in the
interpreter (``_ctypes``/``_ssl``...) purely as a *source of bytes*; the point
under test is the path/name logic, not PE parsing itself.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

PACKAGING = Path(__file__).parents[2] / "packaging"
sys.path.insert(0, str(PACKAGING))

from spec_common import (  # noqa: E402
    FALLBACK_CONDA_DLLS,
    collect_runtime_binaries,
    required_conda_dlls,
    verify_runtime_binaries,
)

pefile = pytest.importorskip("pefile")


def _a_real_extension() -> Path:
    """Any real .pyd from the running interpreter, used to seed a fixture."""
    for candidate in sorted((Path(sys.base_prefix) / "DLLs").glob("_ctypes*.pyd")):
        return candidate
    for candidate in sorted((Path(sys.base_prefix) / "DLLs").glob("*.pyd")):
        return candidate
    pytest.skip("no extension module available to build a fixture from")


def _imports_of(path: Path) -> list[str]:
    pe = pefile.PE(str(path), fast_load=True)
    pe.parse_data_directories(
        directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]]
    )
    try:
        return [
            entry.dll.decode("ascii", "replace")
            for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []) or []
            if entry.dll
        ]
    finally:
        pe.close()


def test_discovery_returns_only_names_present_in_library_bin(tmp_path: Path) -> None:
    """Discovery must never return a DLL that is not actually collected."""
    base = tmp_path / "python"
    (base / "DLLs").mkdir(parents=True)
    library_bin = base / "Library" / "bin"
    library_bin.mkdir(parents=True)

    shutil.copy2(_a_real_extension(), base / "DLLs" / "_ctypes.pyd")
    # Provide a Conda copy for exactly one dependency of that extension.
    dependency = next(
        name for name in _imports_of(base / "DLLs" / "_ctypes.pyd") if name.lower().endswith(".dll")
    )
    (library_bin / dependency).write_bytes(b"conda build")

    discovered = required_conda_dlls(base)

    assert dependency in discovered
    assert all((library_bin / name).is_file() for name in discovered)


def test_discovery_skips_system_dlls(tmp_path: Path) -> None:
    """A system DLL present in Library/bin must not be dragged into the bundle."""
    base = tmp_path / "python"
    (base / "DLLs").mkdir(parents=True)
    library_bin = base / "Library" / "bin"
    library_bin.mkdir(parents=True)

    shutil.copy2(_a_real_extension(), base / "DLLs" / "_ctypes.pyd")
    for name in ("KERNEL32.dll", "api-ms-win-crt-runtime-l1-1-0.dll", "VCRUNTIME140.dll"):
        (library_bin / name).write_bytes(b"system")

    discovered = required_conda_dlls(base)

    lowered = {name.lower() for name in discovered}
    assert "kernel32.dll" not in lowered
    assert "api-ms-win-crt-runtime-l1-1-0.dll" not in lowered
    assert "vcruntime140.dll" not in lowered


def test_discovery_falls_back_when_no_extension_resolves(tmp_path: Path) -> None:
    """With nothing discoverable, the documented fallback list applies."""
    base = tmp_path / "python"
    (base / "DLLs").mkdir(parents=True)
    library_bin = base / "Library" / "bin"
    library_bin.mkdir(parents=True)
    for name in FALLBACK_CONDA_DLLS:
        (library_bin / name).write_bytes(b"conda build")

    discovered = required_conda_dlls(base)

    assert discovered == list(FALLBACK_CONDA_DLLS)


def test_no_library_bin_means_nothing_collected(tmp_path: Path) -> None:
    """A non-Conda interpreter contributes no binaries (and must not crash)."""
    base = tmp_path / "python"
    (base / "DLLs").mkdir(parents=True)

    assert required_conda_dlls(base) == []
    assert collect_runtime_binaries(base) == []


def test_collected_binaries_always_come_from_the_conda_environment(tmp_path: Path) -> None:
    base = tmp_path / "python"
    (base / "DLLs").mkdir(parents=True)
    library_bin = base / "Library" / "bin"
    library_bin.mkdir(parents=True)
    for name in FALLBACK_CONDA_DLLS:
        (library_bin / name).write_bytes(b"conda build")

    collected = collect_runtime_binaries(base)

    assert [Path(source).name for source, _ in collected] == list(FALLBACK_CONDA_DLLS)
    assert all(Path(source).parent == library_bin for source, _ in collected)
    assert all(destination == "." for _, destination in collected)
    assert verify_runtime_binaries(base, collected) == []


def test_verifier_flags_a_foreign_build_on_path(tmp_path: Path) -> None:
    """The exact 2026-09-27 failure: a foreign OpenSSL picked up from PATH."""
    base = tmp_path / "python"
    library_bin = base / "Library" / "bin"
    library_bin.mkdir(parents=True)
    (library_bin / "libcrypto-3-x64.dll").write_bytes(b"conda build")

    elsewhere = tmp_path / "PostgreSQL" / "bin"
    elsewhere.mkdir(parents=True)
    foreign = elsewhere / "libcrypto-3-x64.dll"
    foreign.write_bytes(b"foreign build")

    problems = verify_runtime_binaries(base, [(str(foreign), ".")])

    assert len(problems) == 1
    assert "libcrypto-3-x64.dll" in problems[0]
    assert "foreign build is likely incompatible" in problems[0]


def test_verifier_accepts_conda_sourced_binaries(tmp_path: Path) -> None:
    base = tmp_path / "python"
    library_bin = base / "Library" / "bin"
    library_bin.mkdir(parents=True)
    (library_bin / "ffi-8.dll").write_bytes(b"conda build")

    assert verify_runtime_binaries(base, [(str(library_bin / "ffi-8.dll"), ".")]) == []
