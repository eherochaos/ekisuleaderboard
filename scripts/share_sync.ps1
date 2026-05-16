param(
    [string]$Contributor = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$LocalContributorPath = Join-Path $Root "data\share_contributor.txt"
$ProjectPython = Join-Path $Root ".venv\Scripts\python.exe"
$Python = if (Test-Path $ProjectPython) { $ProjectPython } else { "python" }

if (-not $Contributor) {
    if ($env:EIKETSU_SHARE_CONTRIBUTOR) {
        $Contributor = $env:EIKETSU_SHARE_CONTRIBUTOR
    } elseif (Test-Path $LocalContributorPath) {
        $Contributor = (Get-Content -Encoding UTF8 $LocalContributorPath -Raw).Trim()
    }
}

if (-not $Contributor) {
    $Contributor = Read-Host "Contributor nickname"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LocalContributorPath) | Out-Null
    Set-Content -Encoding UTF8 -Path $LocalContributorPath -Value $Contributor
}

Push-Location $Root
try {
    & $Python -m eiketsu_env doctor browser --auth-source auto
    & $Python -m eiketsu_env share sync --contributor $Contributor --auth-source auto
} finally {
    Pop-Location
}
