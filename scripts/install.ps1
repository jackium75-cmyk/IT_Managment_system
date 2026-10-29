# Installs (or repairs) the Hotel IT Management System on a Windows server.
#
# Run from the application folder in an *Administrator* PowerShell:
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#
# What it does (safe to run again):
#   1. Checks Python 3.11 or newer is installed.
#   2. Creates the virtual environment (.venv) and installs the requirements.
#   3. Creates .env from .env.example with a new random secret key (an existing .env is never overwritten).
#   4. Creates the uploads, backups and logs folders and restricts who can read them.
#   5. Applies database migrations and collects static files.
#   6. Runs the production readiness check (preflight).
#
# It does NOT install PostgreSQL, create the first administrator or register the scheduled tasks;
# see docs/DEPLOYMENT.md for those steps.
param(
    # Windows account that will run the web server and scheduled tasks (given read/write on the data folders).
    [string]$ServiceAccount = "SYSTEM"
)
$ErrorActionPreference = "Stop"
$AppDir = Split-Path -Parent $PSScriptRoot
Set-Location $AppDir

function Step($text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }

# --- 1. Python ---------------------------------------------------------------
Step "Checking Python"
if (Get-Command py -ErrorAction SilentlyContinue) { $pyLauncher = @("py", "-3") }
elseif (Get-Command python -ErrorAction SilentlyContinue) { $pyLauncher = @("python") }
else { throw "Python was not found. Install Python 3.11 or newer from python.org (tick 'Add python.exe to PATH')." }
$pyExe = $pyLauncher[0]
$pyArgs = @($pyLauncher | Select-Object -Skip 1)
$version = & $pyExe @pyArgs -c "import sys; print(str(sys.version_info[0]) + '.' + str(sys.version_info[1]))"
if ($LASTEXITCODE -ne 0 -or -not $version) { throw "Python could not be started." }
$parts = $version.Trim().Split(".")
if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 11)) {
    throw "Python $version found; version 3.11 or newer is required."
}
Write-Host "Python $version"

# --- 2. Virtual environment and packages -----------------------------------------
Step "Creating the virtual environment and installing packages"
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    & $pyExe @pyArgs -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create the virtual environment." }
}
$py = Join-Path $AppDir ".venv\Scripts\python.exe"
& $py -m pip install --upgrade pip --quiet
& $py -m pip install -r requirements.txt --quiet
if ($LASTEXITCODE -ne 0) { throw "Installing the requirements failed (check the internet connection or proxy)." }

# --- 3. .env -------------------------------------------------------------------
Step "Configuration (.env)"
if (Test-Path ".env") {
    Write-Host ".env already exists; it was left unchanged."
} else {
    $secret = & $py -c "import secrets; print(secrets.token_urlsafe(50))"
    (Get-Content ".env.example") -replace '^DJANGO_SECRET_KEY=.*$', "DJANGO_SECRET_KEY=$secret" |
        Set-Content ".env" -Encoding UTF8
    Write-Host "Created .env with a new secret key." -ForegroundColor Green
    Write-Host "Edit it now (notepad .env): DJANGO_ALLOWED_HOSTS, the database settings, BACKUP_ROOT,"
    Write-Host "BACKUP_COPY_TO and SITE_BASE_URL (docs/DEPLOYMENT.md section 4). Then run this script again."
    exit 0
}

# --- 4. Folders and permissions -------------------------------------------------
Step "Creating data folders and restricting access"
$envValues = @{}
Get-Content ".env" | Where-Object { $_ -match '^\s*([A-Z_]+)\s*=\s*(.*)$' } | ForEach-Object {
    $envValues[$Matches[1]] = $Matches[2].Trim()
}
$media = if ($envValues["PRIVATE_MEDIA_ROOT"]) { $envValues["PRIVATE_MEDIA_ROOT"] } else { Join-Path $AppDir "media_private" }
$backups = if ($envValues["BACKUP_ROOT"]) { $envValues["BACKUP_ROOT"] } else { Join-Path $AppDir "backups" }
$logs = Join-Path $AppDir "logs"
foreach ($folder in @($media, $backups, $logs)) {
    New-Item -ItemType Directory -Force -Path $folder | Out-Null
    # Remove inherited permissions; allow only Administrators, SYSTEM and the service account.
    & icacls $folder /inheritance:r /grant:r "*S-1-5-32-544:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" "${ServiceAccount}:(OI)(CI)M" | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Warning "Could not set permissions on $folder (run as Administrator)." }
    else { Write-Host "Restricted: $folder" }
}
& icacls ".env" /inheritance:r /grant:r "*S-1-5-32-544:F" "*S-1-5-18:F" "${ServiceAccount}:R" | Out-Null
if ($LASTEXITCODE -eq 0) { Write-Host "Restricted: .env (contains the secret key and database password)" }

# --- 5. Database and static files ------------------------------------------------
Step "Applying database migrations"
& $py manage.py migrate --noinput
if ($LASTEXITCODE -ne 0) { throw "Migrations failed. Check the database settings in .env." }
Step "Collecting static files"
& $py manage.py collectstatic --noinput | Out-Null
if ($LASTEXITCODE -ne 0) { throw "collectstatic failed." }

# --- 6. Readiness check ------------------------------------------------------------
Step "Production readiness check"
& $py manage.py preflight
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Fix the FAIL items above before going live, then run: .venv\Scripts\python.exe manage.py preflight"
}

Write-Host ""
Write-Host "Next steps (docs/DEPLOYMENT.md):" -ForegroundColor Green
Write-Host "  1. Create the first administrator:  .venv\Scripts\python.exe manage.py createsuperuser"
Write-Host "  2. Register the server, jobs and backup tasks:  scripts\register_tasks.ps1"
Write-Host "  3. Open the firewall port and set up HTTPS."
