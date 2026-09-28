[CmdletBinding()]
param(
    [ValidateSet('All', 'Onedir', 'Onefile')]
    [string]$Mode = 'All',
    [string]$Python,
    [string]$BrowserPath,
    [string]$OutputRoot,
    [switch]$AllowDirty,
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'
$PackagingRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$RepoRoot = [System.IO.Path]::GetFullPath((Join-Path $PackagingRoot '..'))
if (-not $OutputRoot) { $OutputRoot = $PackagingRoot }
$OutputRoot = [System.IO.Path]::GetFullPath($OutputRoot)
$BuildRoot = [System.IO.Path]::GetFullPath((Join-Path $OutputRoot 'build'))
$DistRoot = [System.IO.Path]::GetFullPath((Join-Path $OutputRoot 'dist'))

function Assert-ManagedPackagingPath([string]$Path) {
    $resolved = [System.IO.Path]::GetFullPath($Path)
    $expected = @($BuildRoot, $DistRoot) | ForEach-Object { [System.IO.Path]::GetFullPath($_) }
    if ($expected -notcontains $resolved) {
        throw "Refusing to clean a path outside the managed packaging outputs: $resolved"
    }
}

foreach ($managedPath in @($BuildRoot, $DistRoot)) {
    Assert-ManagedPackagingPath $managedPath
    if (Test-Path -LiteralPath $managedPath) {
        if (-not $Clean) {
            throw "Managed output already exists: $managedPath. Preserve it or rerun with -Clean to remove only packaging/build and packaging/dist."
        }
        Remove-Item -LiteralPath $managedPath -Recurse -Force
    }
}

if (-not $Python) {
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCommand -and $pythonCommand.Source -notmatch '\\WindowsApps\\') {
        $Python = $pythonCommand.Source
    }
}
if (-not $Python -or -not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw 'Provide -Python with an installed Python 3.12 x64 executable; a Python 3.12 interpreter is required.'
}
$Python = (Resolve-Path -LiteralPath $Python).Path
& $Python -c "import struct,sys; assert sys.version_info[:2] == (3, 12), sys.version; assert struct.calcsize('P') == 8, '64-bit Python required'"
if ($LASTEXITCODE -ne 0) { throw 'The selected interpreter is not Python 3.12 x64.' }

$gitStatus = (& git -C $RepoRoot status --porcelain) -join "`n"
if ($LASTEXITCODE -ne 0) { throw 'Unable to read Git status.' }
$commit = (& git -C $RepoRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Unable to resolve the source commit.' }
if ($gitStatus -and -not $AllowDirty) {
    throw 'The working tree is dirty. Commit/stash it, or use -AllowDirty for a clearly marked non-release QA build.'
}

$BuildEnv = Join-Path $BuildRoot 'venv'
$PythonExe = Join-Path $BuildEnv 'Scripts\python.exe'
$PyInstallerExe = Join-Path $BuildEnv 'Scripts\pyinstaller.exe'
$TestRoot = Join-Path $BuildRoot 'test-results'
$OnedirDist = Join-Path $DistRoot 'onedir'
$OnefileDist = Join-Path $DistRoot 'onefile'
New-Item -ItemType Directory -Path $BuildRoot, $TestRoot, $OnedirDist, $OnefileDist -Force | Out-Null

& $Python -m venv $BuildEnv
if ($LASTEXITCODE -ne 0) { throw 'Failed to create the clean build virtual environment.' }
& $PythonExe -m pip install --require-hashes --no-deps -r (Join-Path $PackagingRoot 'requirements-build.lock')
if ($LASTEXITCODE -ne 0) { throw 'Hash-locked build dependency installation failed.' }
& $PythonExe -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Build environment dependency check failed.' }

if (-not $BrowserPath) { $BrowserPath = $env:ARXIV_FILTER_BROWSER }
if (-not $BrowserPath) {
    $browserCandidates = @(
        (Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe'),
        (Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe')
    )
    $BrowserPath = $browserCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } | Select-Object -First 1
}
if ($BrowserPath) {
    if (-not (Test-Path -LiteralPath $BrowserPath -PathType Leaf)) { throw "Browser executable not found: $BrowserPath" }
    $env:ARXIV_FILTER_BROWSER = (Resolve-Path -LiteralPath $BrowserPath).Path
} else {
    & $PythonExe -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { throw 'Playwright Chromium installation failed.' }
}

$env:PYTHONPATH = Join-Path $RepoRoot 'src'
$unitXml = Join-Path $TestRoot 'unit-results.xml'
$e2eXml = Join-Path $TestRoot 'e2e-results.xml'
& $PythonExe -m pytest -q -m 'not e2e' --junitxml $unitXml
if ($LASTEXITCODE -ne 0) { throw 'Offline unit/integration test suite failed.' }
& $PythonExe -m pytest -q -m e2e --junitxml $e2eXml
if ($LASTEXITCODE -ne 0) { throw 'Browser E2E test suite failed.' }
[xml]$e2eReport = Get-Content -Raw -LiteralPath $e2eXml
$suite = if ($e2eReport.testsuites.testsuite) {
    $e2eReport.testsuites.testsuite
} else {
    $e2eReport.testsuite
}
if ([int]$suite.tests -lt 1 -or [int]$suite.skipped -gt 0 -or [int]$suite.failures -gt 0 -or [int]$suite.errors -gt 0) {
    throw "Release requires executed, unskipped browser tests; tests=$($suite.tests), skipped=$($suite.skipped), failures=$($suite.failures), errors=$($suite.errors)."
}

$licenseFile = Join-Path $DistRoot 'THIRD-PARTY-LICENSES.txt'
& $PythonExe (Join-Path $RepoRoot 'scripts\write_third_party_licenses.py') --lock (Join-Path $RepoRoot 'requirements.lock') --output $licenseFile
if ($LASTEXITCODE -ne 0) { throw 'Third-party license manifest generation failed.' }

$modes = if ($Mode -eq 'All') { @('Onedir', 'Onefile') } else { @($Mode) }
foreach ($buildMode in $modes) {
    if ($buildMode -eq 'Onedir') {
        $spec = Join-Path $PackagingRoot 'arxiv_filter_onedir.spec'
        $targetDist = $OnedirDist
        $work = Join-Path $BuildRoot 'work-onedir'
    } else {
        $spec = Join-Path $PackagingRoot 'arxiv_filter_onefile.spec'
        $targetDist = $OnefileDist
        $work = Join-Path $BuildRoot 'work-onefile'
    }
    & $PyInstallerExe $spec --distpath $targetDist --workpath $work --noconfirm --clean
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller $buildMode build failed." }

    if ($buildMode -eq 'Onedir') {
        $exe = Join-Path $targetDist 'arxiv-filter-windows-x64\arxiv-filter.exe'
        $smokeData = Join-Path $BuildRoot 'smoke-data-onedir'
        $bundleDir = Split-Path -Parent $exe
        Copy-Item -LiteralPath (Join-Path $RepoRoot 'start.bat') -Destination (Join-Path $bundleDir 'start.bat')
        Copy-Item -LiteralPath $licenseFile -Destination (Join-Path $bundleDir 'THIRD-PARTY-LICENSES.txt')
    } else {
        $exe = Join-Path $targetDist 'arxiv-filter.exe'
        $smokeData = Join-Path $BuildRoot 'smoke-data-onefile'
        Copy-Item -LiteralPath $exe -Destination (Join-Path $DistRoot 'arxiv-filter.exe')
    }
    if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { throw "PyInstaller output missing: $exe" }
    & $PythonExe (Join-Path $RepoRoot 'scripts\frozen_smoke.py') --executable $exe --data-dir $smokeData
    if ($LASTEXITCODE -ne 0) { throw "Frozen $buildMode smoke test failed." }
}

$payloadFiles = Get-ChildItem -LiteralPath $DistRoot -File -Recurse |
    Where-Object { $_.Name -notin @('SHA256SUMS.txt', 'release-report.json') } |
    Sort-Object FullName
$artifactRows = foreach ($file in $payloadFiles) {
    $relative = $file.FullName.Substring($DistRoot.Length).TrimStart('\')
    $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    [pscustomobject]@{ path = $relative; size_bytes = $file.Length; sha256 = $hash }
}
$sumLines = @($artifactRows | ForEach-Object { "$($_.sha256)  $($_.path)" })
[System.IO.File]::WriteAllLines(
    (Join-Path $DistRoot 'SHA256SUMS.txt'),
    $sumLines,
    [System.Text.UTF8Encoding]::new($false)
)
# Native tools write warnings to stderr (the build venv is not a Conda
# environment, so PyInstaller emits a 104/101 WARNING on every invocation).
# Under $ErrorActionPreference = 'Stop' PowerShell promotes native stderr output
# to a terminating error, which used to abort the report at the very last step
# and leave release-report.json missing. Relax the preference for this block
# only, and keep just the version line rather than the warning text.
$ErrorActionPreference = 'Continue'
$pythonVersion = ((& $PythonExe --version 2>&1 | Out-String).Trim() -split "`n" | Select-Object -Last 1).Trim()
$pyinstallerVersion = ((& $PythonExe -m PyInstaller --version 2>&1 | Out-String).Trim() -split "`n" | Select-Object -Last 1).Trim()
$report = [ordered]@{
    schema_version = 1
    built_at_utc = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
    source_commit = $commit
    working_tree_clean = [string]::IsNullOrWhiteSpace($gitStatus)
    working_tree_status = $gitStatus
    mode = $Mode
    python = $pythonVersion
    pyinstaller = $pyinstallerVersion
    e2e_test_suite = [ordered]@{ tests = [int]$suite.tests; skipped = [int]$suite.skipped; failures = [int]$suite.failures; errors = [int]$suite.errors }
    defender_scan = 'not_run_manual_release_gate'
    code_signing = 'not_configured'
    artifacts = @($artifactRows)
}
$ErrorActionPreference = 'Stop'
$report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $DistRoot 'release-report.json') -Encoding utf8
Write-Host "Release artifacts created at $DistRoot"
