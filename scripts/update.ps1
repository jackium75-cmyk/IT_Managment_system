# Applies a new version of the Hotel IT Management System.
#
# 1. Copy the new code over the application folder (keep .env, media_private, backups, logs).
# 2. In an *Administrator* PowerShell in the application folder run:
#      powershell -ExecutionPolicy Bypass -File scripts\update.ps1
#
# The script makes a verified backup first and stops if it fails, so the update can always be undone
# by restoring that backup with the previous code (docs/BACKUP_AND_RESTORE.md).
$ErrorActionPreference = "Stop"
$AppDir = Split-Path -Parent $PSScriptRoot
Set-Location $AppDir
$py = Join-Path $AppDir ".venv\Scripts\python.exe"

Write-Host "==> Verified backup before updating" -ForegroundColor Cyan
& $py manage.py backup --verify
if ($LASTEXITCODE -ne 0) { throw "The backup failed, so nothing was updated." }

Write-Host "==> Stopping the server and scheduled tasks" -ForegroundColor Cyan
foreach ($t in @("HotelIT Jobs", "HotelIT Backup", "HotelIT Server")) {
    if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $t
        Disable-ScheduledTask -TaskName $t | Out-Null
    }
}

try {
    Write-Host "==> Installing packages" -ForegroundColor Cyan
    & $py -m pip install -r requirements.txt --quiet
    if ($LASTEXITCODE -ne 0) { throw "pip install failed." }
    Write-Host "==> Applying database changes" -ForegroundColor Cyan
    & $py manage.py migrate --noinput
    if ($LASTEXITCODE -ne 0) { throw "migrate failed." }
    & $py manage.py collectstatic --noinput | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "collectstatic failed." }
} finally {
    foreach ($t in @("HotelIT Server", "HotelIT Jobs", "HotelIT Backup")) {
        if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) { Enable-ScheduledTask -TaskName $t | Out-Null }
    }
    if (Get-ScheduledTask -TaskName "HotelIT Server" -ErrorAction SilentlyContinue) { Start-ScheduledTask "HotelIT Server" }
}

Write-Host "==> Readiness check" -ForegroundColor Cyan
& $py manage.py preflight
Write-Host "Update complete." -ForegroundColor Green
