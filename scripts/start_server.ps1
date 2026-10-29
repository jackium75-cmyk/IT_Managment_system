# Starts the Hotel IT Management System with the Waitress production server in this window.
# For normal operation register the "HotelIT Server" scheduled task instead (scripts\register_tasks.ps1),
# which starts at boot and restarts automatically. Use this script for testing or troubleshooting.
#
# Usage (from the project folder, after scripts\install.ps1):
#   powershell -ExecutionPolicy Bypass -File scripts\start_server.ps1 [-Port 8000] [-Listen 0.0.0.0] [-BehindProxy]
param(
    [int]$Port = 8000,
    [string]$Listen = "0.0.0.0",
    [switch]$BehindProxy
)
$ErrorActionPreference = "Stop"
$AppDir = Split-Path -Parent $PSScriptRoot
Set-Location $AppDir
$py = Join-Path $AppDir ".venv\Scripts\python.exe"
& $py manage.py migrate --noinput
& $py manage.py collectstatic --noinput | Out-Null
$serverArgs = @("--listen=${Listen}:${Port}", "--threads=8", "--max-request-body-size=1073741824", "--channel-timeout=300")
if ($BehindProxy) {
    # Only trust X-Forwarded-* headers from a reverse proxy on this machine (IIS / Caddy doing HTTPS).
    $serverArgs = @("--listen=127.0.0.1:${Port}", "--threads=8", "--max-request-body-size=1073741824",
                    "--channel-timeout=300", "--trusted-proxy=127.0.0.1",
                    "--trusted-proxy-headers=x-forwarded-proto x-forwarded-for", "--clear-untrusted-proxy-headers")
}
Write-Host "Hotel IT is running on port ${Port} (press Ctrl+C to stop)"
# Request bodies up to 1 GB allow several large evidence videos in one upload.
& (Join-Path $AppDir ".venv\Scripts\waitress-serve.exe") @serverArgs config.wsgi:application
