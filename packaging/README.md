# Windows build toolchain

`requirements-build.lock` is a hash-locked toolchain for CPython 3.12 x64 on
Windows x64. PyInstaller outputs are platform-specific, so hashes intentionally
cover the compatible Windows wheels rather than downloading and hashing every
wheel published for every operating system.

When refreshing pins, use a trusted Python 3.12 environment with `pip-tools`
installed. Resolve pins without hashes, download only the Windows x64 wheels,
then generate the checked-in hash lock:

```powershell
python -m piptools compile --allow-unsafe `
  --output-file "$env:TEMP\arxiv-filter-build-resolved.txt" `
  packaging/requirements-build.in
python -m pip download --no-deps --only-binary=:all: `
  --platform win_amd64 --python-version 3.12 --implementation cp `
  --abi cp312 --abi none -d "$env:TEMP\arxiv-filter-build-wheelhouse" `
  -r "$env:TEMP\arxiv-filter-build-resolved.txt"
python scripts/lock_wheel_hashes.py `
  --resolved "$env:TEMP\arxiv-filter-build-resolved.txt" `
  --wheelhouse "$env:TEMP\arxiv-filter-build-wheelhouse" `
  --output packaging/requirements-build.lock
```

Run `packaging/build.ps1 -Mode All -Python <Python-3.12-x64.exe>`. By default,
managed output is written to `packaging/build` and `packaging/dist`; use
`-OutputRoot <directory>` to isolate a QA build without disturbing prior output.
The script refuses a dirty tree unless `-AllowDirty` is supplied, refuses to
erase existing managed output unless `-Clean` is supplied, and records dirty
builds as non-release artifacts. Browser tests use an installed Chrome/Edge
executable if available; otherwise Playwright downloads its Chromium test
browser. The browser is test-only and is never collected into the application
bundle.

## Application icon

Icon artwork is **not tracked in this repository** (`packaging/*.ico` is
gitignored). Builds therefore fall back to PyInstaller's built-in placeholder
icon (`icon=None` in the spec), which is valid and expected — confirmed by
comparing the icon resources of both build variants: 9 sizes when the artwork is
supplied, 7 placeholder sizes when it is not.

To build a branded executable, point `ARXIV_FILTER_ICON` at any `.ico` outside
the repository:

```powershell
$env:ARXIV_FILTER_ICON = 'D:\path\to\artwork.ico'
powershell -File packaging\build.ps1 -Mode All -Python <Python-3.12-x64.exe>
```

`spec_common.optional_icon()` resolves the icon in this order:

1. `ARXIV_FILTER_ICON`, if it points at an existing file;
2. `packaging/arxiv-filter.ico`, if someone later decides to ship artwork here.

A missing or unreadable override falls through to "no icon" rather than failing
the build — an icon must never block a release.

Recommended artwork: a multi-size `.ico` containing 16, 32, 48, 256 px (Windows
picks per context). Verify what actually landed in the executable with:

```powershell
python -c "import pefile; pe=pefile.PE(r'packaging\dist\onefile\arxiv-filter.exe'); print('icon groups:', sum(1 for e in pe.DIRECTORY_ENTRY_RESOURCE.entries if e.id==14))"
```
