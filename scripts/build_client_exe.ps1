param(
    [string]$Name = "EiketsuCollector",
    [string]$Version = "",
    [ValidateSet("gui", "cli")]
    [string]$Mode = "gui"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creating .venv with current python."
    python -m venv .venv
}

if ([string]::IsNullOrWhiteSpace($Version)) {
    $ProjectText = Get-Content -Path "pyproject.toml" -Raw -Encoding UTF8
    $VersionMatch = [regex]::Match($ProjectText, '(?m)^version\s*=\s*"([^"]+)"')
    if ($VersionMatch.Success) {
        $Version = $VersionMatch.Groups[1].Value
    }
}
if ([string]::IsNullOrWhiteSpace($Version)) {
    throw "Project version not found"
}

$OutputName = $Name
if ($OutputName -notmatch "_$([regex]::Escape($Version))$") {
    $OutputName = "${Name}_${Version}"
}

$PyInstaller = ".\.venv\Scripts\pyinstaller.exe"
$EntryPoint = "src\eiketsu_env\client_gui.py"
$WindowArgs = @("--windowed")
if ($Mode -eq "cli") {
    $EntryPoint = "src\eiketsu_env\client_cli.py"
    $WindowArgs = @()
}

if (-not (Test-Path $PyInstaller)) {
    .\.venv\Scripts\python.exe -m pip install -e ".[client-build]"
    if ($LASTEXITCODE -ne 0) {
        throw "pip install failed"
    }
} else {
    Write-Host "Using existing PyInstaller: $PyInstaller"
}

$PythonBase = (& .\.venv\Scripts\python.exe -c "import sys; print(sys.base_prefix)")
$TclDir = Join-Path $PythonBase "tcl"
$TclLibraryDir = Join-Path $TclDir "tcl8.6"
$TkLibraryDir = Join-Path $TclDir "tk8.6"
$TkinterDir = Join-Path $PythonBase "Lib\tkinter"
$ExtraArgs = @()
if ($Mode -eq "gui") {
    $ExtraArgs += @("--hidden-import", "tkinter", "--hidden-import", "_tkinter")
    # Bundle Tcl/Tk data from the active Python install so the exe can run on machines without Python.
    if (Test-Path $TclLibraryDir) {
        $ExtraArgs += @("--add-data", "$TclLibraryDir;_tcl_data")
    }
    if (Test-Path $TkLibraryDir) {
        $ExtraArgs += @("--add-data", "$TkLibraryDir;_tk_data")
    }
    if (Test-Path $TkinterDir) {
        $ExtraArgs += @("--add-data", "$TkinterDir;tkinter")
    }
}

& $PyInstaller --clean --onefile --name $OutputName --specpath build --paths src @WindowArgs @ExtraArgs $EntryPoint
if ($LASTEXITCODE -ne 0) {
    throw "pyinstaller failed"
}

Write-Host "Client exe created: dist\$OutputName.exe"
