param(
    [string]$Python = ".venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PythonPath = Join-Path $ProjectRoot $Python
$SpecPath = Join-Path $ProjectRoot "packaging\research_discovery_gui.spec"
$WorkPath = Join-Path $ProjectRoot "build\phase15-gui"
$DistributionPath = Join-Path $ProjectRoot "dist\phase15-gui"

if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "Python environment not found at $PythonPath. Install the project's [build] extra first."
}
if (-not (Test-Path -LiteralPath $SpecPath -PathType Leaf)) {
    throw "PyInstaller spec not found at $SpecPath."
}

$OriginalPath = $env:PATH
try {
    # The desktop host can prepend Poppler's private ICU 78 DLLs to PATH.
    # PySide6's QtCore imports Windows ICU by name; collecting Poppler's
    # renamed, version-suffixed ICU exports breaks the frozen QtCore import.
    $env:PATH = (($OriginalPath -split [IO.Path]::PathSeparator) |
        Where-Object { $_ -notmatch '(?i)poppler' }) -join [IO.Path]::PathSeparator

    & $PythonPath -m PyInstaller --clean --noconfirm --distpath $DistributionPath --workpath $WorkPath $SpecPath
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE."
    }
} finally {
    $env:PATH = $OriginalPath
}
Write-Host "Built GUI bundle: $(Join-Path $DistributionPath 'ResearchDiscovery\ResearchDiscovery.exe')"
