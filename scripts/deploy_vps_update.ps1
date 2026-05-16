param(
    [string]$SshTarget = "ubuntu@43.128.141.76",
    [string]$RemoteDir = "~/eiketsu-env-db"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Archive = Join-Path $env:TEMP ("eiketsu-vps-update-{0}.tar" -f (Get-Date -Format "yyyyMMddHHmmss"))
$RemoteArchive = "/tmp/eiketsu-vps-update.tar"

Write-Host "Packing server/client source..."
tar -cf $Archive `
    pyproject.toml `
    README.md `
    Dockerfile `
    docker-compose.yml `
    alembic.ini `
    alembic `
    src
if ($LASTEXITCODE -ne 0) {
    throw "Packaging failed"
}

Write-Host "Uploading source archive to $SshTarget. If prompted, enter the VPS password."
scp $Archive "${SshTarget}:$RemoteArchive"
if ($LASTEXITCODE -ne 0) {
    throw "Upload failed"
}

$RemoteCommand = "mkdir -p $RemoteDir && tar -xf $RemoteArchive -C $RemoteDir && cd $RemoteDir && docker compose build api && docker compose up -d api && docker compose ps"
Write-Host "Deploying on VPS. If prompted, enter the VPS password again."
ssh $SshTarget $RemoteCommand
if ($LASTEXITCODE -ne 0) {
    throw "VPS deploy failed"
}

Remove-Item -LiteralPath $Archive -Force -ErrorAction SilentlyContinue
Write-Host "VPS deploy finished. Check: http://43.128.141.76:8000/admin/updates"
