"""Shared explicit resource collection for the Windows PyInstaller specs."""

from __future__ import annotations

import os
from pathlib import Path

#: Conda-side runtime DLLs shipped next to the interpreter.
#:
#: Used as the discovery fallback, and as the reference set for
#: :func:`verify_runtime_binaries`. Each entry prevented a real failure of the
#: frozen app; see :func:`required_conda_dlls`.
FALLBACK_CONDA_DLLS = (
    # Paired with _ssl.pyd; must be the matching Conda OpenSSL build.
    "libssl-3-x64.dll",
    "libcrypto-3-x64.dll",
    # Required by _ctypes.pyd (libffi).
    "ffi-8.dll",
    # Required by _bz2.pyd, pyexpat.pyd and _lzma.pyd respectively.
    "libbz2.dll",
    "libexpat.dll",
    "liblzma.dll",
    # Linked by python312.dll and _sqlite3.pyd under these exact names.
    "zlib.dll",
    "sqlite3.dll",
)

#: The Windows loader resolves these itself; never bundle them.
_SYSTEM_DLL_PREFIXES = (
    "api-ms-",
    "ext-ms-",
    "advapi32",
    "bcrypt",
    "cabinet",
    "cfgmgr32",
    "combase",
    "comdlg32",
    "crypt32",
    "cryptbase",
    "dbghelp",
    "dnsapi",
    "gdi32",
    "imm32",
    "iphlpapi",
    "kernel32",
    "kernelbase",
    "msi",
    "msvcrt",
    "mswsock",
    "ncrypt",
    "netapi32",
    "normaliz",
    "ntdll",
    "ole32",
    "oleaut32",
    "powrprof",
    "psapi",
    "rpcrt4",
    "sechost",
    "secur32",
    "setupapi",
    "shell32",
    "shlwapi",
    "user32",
    "userenv",
    "version",
    "win32u",
    "winmm",
    "winspool",
    "wintrust",
    "ws2_32",
)


def collect_runtime_data(project_root: Path) -> list[tuple[str, str]]:
    """Preserve package-relative resource paths expected by ``paths.resource_root``."""
    package_root = project_root / "src" / "arxiv_filter"
    destinations = ("data", "static", "storage/migrations")
    collected: list[tuple[str, str]] = []
    for relative_root in destinations:
        source_root = package_root / relative_root
        for source in sorted(source_root.rglob("*")):
            if not source.is_file():
                continue
            relative_parent = source.relative_to(package_root).parent
            destination = Path("arxiv_filter") / relative_parent
            collected.append((str(source), destination.as_posix()))
    return collected


def _is_system_dll(name: str) -> bool:
    lowered = name.lower()
    if lowered.startswith(_SYSTEM_DLL_PREFIXES):
        return True
    # The MSVC runtime ships with every supported Windows; PyInstaller collects
    # whichever pieces it decides it needs.
    return lowered.startswith(
        ("msvcp", "vcruntime", "ucrtbase", "concrt", "vcomp", "vcamp", "vccorlib")
    )


def _extension_modules(python_base: Path) -> list[Path]:
    """C extensions shipped with this interpreter."""
    modules: list[Path] = []
    for directory in (python_base / "DLLs", python_base / "Lib" / "lib-dynload"):
        if directory.is_dir():
            modules.extend(sorted(directory.glob("*.pyd")))
    return modules


def _imported_dlls(path: Path) -> list[str]:
    """Names in a PE file's import table; ``[]`` when pefile is unavailable."""
    try:
        import pefile
    except ImportError:
        return []
    try:
        pe = pefile.PE(str(path), fast_load=True)
        pe.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]]
        )
    except Exception:  # noqa: BLE001 - a malformed or non-PE file is simply skipped
        return []
    try:
        return [
            entry.dll.decode("ascii", "replace")
            for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []) or []
            if entry.dll
        ]
    finally:
        pe.close()


def required_conda_dlls(python_base: Path) -> list[str]:
    """Conda-side DLLs the interpreter's own C extensions load at run time.

    Conda keeps Python's C-extension dependencies in ``Library/bin``, which
    PyInstaller does **not** search while walking import tables. For any
    dependency whose basename also exists elsewhere on ``PATH``, PyInstaller
    therefore bundles a foreign build. Observed on this project (2026-09-27):

    * ``sqlite3.dll`` missing -> the frozen app never started (earlier fix);
    * ``libssl-3-x64.dll`` / ``libcrypto-3-x64.dll`` resolved to
      ``D:\\PostgreSQL\\15\\bin`` -> ``ImportError: DLL load failed while
      importing _ssl``, because ``_ssl.pyd`` was paired with an OpenSSL build
      lacking the exports it imports;
    * ``ffi-8.dll`` missing -> ``ImportError: DLL load failed while importing
      _ctypes``, which surfaced only after the OpenSSL fix, one import later.

    Discovery reads the real import tables, so a future extension that adds a
    new Conda dependency is covered without editing this module. When ``pefile``
    is unavailable the documented fallback list is used instead.
    """
    library_bin = python_base / "Library" / "bin"
    if not library_bin.is_dir():
        return []

    discovered: list[str] = []
    seen: set[str] = set()
    for module in _extension_modules(python_base):
        for dependency in _imported_dlls(module):
            key = dependency.lower()
            if key in seen or _is_system_dll(dependency):
                continue
            if (library_bin / dependency).is_file():
                seen.add(key)
                discovered.append(dependency)

    if not discovered:
        discovered = [name for name in FALLBACK_CONDA_DLLS if (library_bin / name).is_file()]
    return discovered


def collect_runtime_binaries(python_base: Path) -> list[tuple[str, str]]:
    """Bundle the Conda-side runtime DLLs that PyInstaller resolves *wrongly*.

    Naming these files in the spec's ``binaries`` list puts them ahead of
    PyInstaller's own dependency analysis, so the Conda build wins over whatever
    happens to be on ``PATH``. See :func:`required_conda_dlls` for the failures
    this prevents. Every entry is optional: a non-Conda base contributes none.
    """
    library_bin = python_base / "Library" / "bin"
    directories = (library_bin, python_base / "DLLs", python_base)
    collected: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name in required_conda_dlls(python_base):
        for directory in directories:
            source = directory / name
            if source.is_file() and name.lower() not in seen:
                seen.add(name.lower())
                collected.append((str(source), "."))
                break
    return collected


def verify_runtime_binaries(python_base: Path, bundled: list[tuple[str, str]]) -> list[str]:
    """Report collected DLLs that did not come from the Conda environment.

    A guard for the failure mode above: silently bundling a foreign build is
    worse than failing the build, because it surfaces only as an unhelpful
    ``DLL load failed`` on the user's first launch.
    """
    library_bin = python_base / "Library" / "bin"
    problems: list[str] = []
    for source, _destination in bundled:
        path = Path(source)
        if path.parent == library_bin and path.is_file():
            continue
        if (library_bin / path.name).is_file():
            problems.append(
                f"{path.name}: collected from {path.parent}, but a Conda copy exists "
                f"under {library_bin}; the foreign build is likely incompatible"
            )
        elif path.name not in FALLBACK_CONDA_DLLS:
            problems.append(
                f"{path.name}: collected from {path.parent}, which is not the Conda environment"
            )
    return problems


def optional_icon(project_root: Path) -> str | None:
    """Resolve the executable icon, or ``None`` to build without one.

    Two sources, in order:

    1. ``ARXIV_FILTER_ICON`` — an absolute path to any ``.ico``. This is how a
       maintainer builds a branded executable while keeping the artwork out of
       version control (the file is deliberately not tracked).
    2. ``packaging/arxiv-filter.ico`` — the conventional in-repo location, used
       if someone decides to ship an icon with the repository after all.

    A missing or unreadable override falls through instead of failing: an icon
    must never block a release build.
    """
    override = os.environ.get("ARXIV_FILTER_ICON", "").strip()
    if override and Path(override).is_file():
        return str(Path(override).resolve())
    icon = project_root / "packaging" / "arxiv-filter.ico"
    return str(icon) if icon.is_file() else None


__all__ = [
    "FALLBACK_CONDA_DLLS",
    "collect_runtime_binaries",
    "collect_runtime_data",
    "optional_icon",
    "required_conda_dlls",
    "verify_runtime_binaries",
]
