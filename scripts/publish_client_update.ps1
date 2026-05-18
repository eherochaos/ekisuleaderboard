param(
    [Parameter(Mandatory = $true)]
    [string]$Version,
    [string]$Notes = "",
    [string]$ExePath = "",
    [string]$SshTarget = "ubuntu@43.128.141.76",
    [string]$RemoteDir = "~/eiketsu-env-db",
    [string]$RemoteExe = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if ([string]::IsNullOrWhiteSpace($ExePath)) {
    $ExePath = "dist\EiketsuCollector_$Version.exe"
}
if ([string]::IsNullOrWhiteSpace($RemoteExe)) {
    $RemoteExe = "/tmp/EiketsuCollector_$Version.exe"
}

if (-not (Test-Path $ExePath)) {
    $LegacyExe = "dist\EiketsuCollector.exe"
    if (Test-Path $LegacyExe) {
        throw "Client exe not found: $ExePath. Found old unversioned $LegacyExe; rebuild so the file name includes the version."
    }
    throw "Client exe not found: $ExePath. Run scripts\build_client_exe.ps1 first."
}

$ResolvedExe = (Resolve-Path $ExePath).Path
Write-Host "Uploading $ResolvedExe to ${SshTarget}:$RemoteExe"
scp $ResolvedExe "${SshTarget}:$RemoteExe"
if ($LASTEXITCODE -ne 0) {
    throw "scp upload failed"
}

function Escape-SshSingleQuoted([string]$Value) {
    return $Value.Replace("'", "'\''")
}

$SafeVersion = Escape-SshSingleQuoted $Version
$SafeNotes = Escape-SshSingleQuoted $Notes
$SafeRemoteExe = Escape-SshSingleQuoted $RemoteExe

$Command = "cd $RemoteDir && docker compose -f deploy/docker-compose.yml run --rm -v '${SafeRemoteExe}:${SafeRemoteExe}:ro' api eiketsu-server admin publish-client --version '$SafeVersion' --file '$SafeRemoteExe' --notes '$SafeNotes'"
Write-Host "Publishing client update on VPS..."
ssh $SshTarget $Command
if ($LASTEXITCODE -ne 0) {
    throw "VPS publish command failed"
}

Write-Host "Client update published. Users can download it from /downloads/EiketsuCollector_$Version.exe after the server is updated."
