# Registers the Windows Task Scheduler tasks that run the Hotel IT Management System.
#
# Run from the application folder in an *Administrator* PowerShell:
#   powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1 [-Port 8000] [-BackupTime 02:30]
#        [-User "HOTEL\svc-hotelit"] [-BehindProxy]
#
# Tasks created (re-running the script replaces them):
#   HotelIT Server  - the Waitress web server; starts at boot and restarts automatically if it stops.
#   HotelIT Jobs    - escalations, maintenance reminders, recurring tasks; every 10 minutes.
#   HotelIT Backup  - nightly backup with verification and copy to BACKUP_COPY_TO.
#
# Without -User the tasks run as SYSTEM. A dedicated low-privilege account is better: create it, give it
# Modify rights on the data folders (scripts\install.ps1 -ServiceAccount does this) and pass it here;
# you will be asked for its password.
param(
    [int]$Port = 8000,
    [string]$Listen = "0.0.0.0",
    [string]$BackupTime = "02:30",
    [string]$User = "SYSTEM",
    # Use when IIS or Caddy terminates HTTPS in front of Waitress on the same server (docs/DEPLOYMENT.md).
    [switch]$BehindProxy
)
$ErrorActionPreference = "Stop"
$AppDir = Split-Path -Parent $PSScriptRoot
$py = Join-Path $AppDir ".venv\Scripts\python.exe"
$waitress = Join-Path $AppDir ".venv\Scripts\waitress-serve.exe"
if (-not (Test-Path $waitress)) { throw "Run scripts\install.ps1 first (.venv not found)." }

if ($BehindProxy) { $Listen = "127.0.0.1" }
$serverArgs = "--listen=${Listen}:${Port} --threads=8 --max-request-body-size=1073741824 --channel-timeout=300"
if ($BehindProxy) {
    # The header list is space-separated, so that argument is quoted as a whole.
    $serverArgs += ' --trusted-proxy=127.0.0.1 "--trusted-proxy-headers=x-forwarded-proto x-forwarded-for" --clear-untrusted-proxy-headers'
}
$serverArgs += " config.wsgi:application"

if ($User -eq "SYSTEM") {
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Limited
    $register = @{ Principal = $principal }
} else {
    $cred = Get-Credential -UserName $User -Message "Password for the account that will run Hotel IT"
    $register = @{ User = $cred.UserName; Password = $cred.GetNetworkCredential().Password; RunLevel = "Limited" }
}

function Register-HotelTask($name, $description, $action, $trigger, $settings) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
    }
    Register-ScheduledTask -TaskName $name -Description $description -Action $action -Trigger $trigger `
        -Settings $settings @register | Out-Null
    Write-Host "Registered: $name"
}

# Web server: at boot, never time out, restart every minute if it stops.
$serverSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
Register-HotelTask "HotelIT Server" "Hotel IT Management System web server (Waitress) on port $Port." `
    (New-ScheduledTaskAction -Execute $waitress -Argument $serverArgs -WorkingDirectory $AppDir) `
    (New-ScheduledTaskTrigger -AtStartup) $serverSettings

# Background jobs: every 10 minutes.
$jobSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew
$jobTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 10) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
Register-HotelTask "HotelIT Jobs" "Escalations, maintenance reminders, recurring tasks, auto-close." `
    (New-ScheduledTaskAction -Execute $py -Argument "manage.py run_jobs" -WorkingDirectory $AppDir) `
    $jobTrigger $jobSettings

# Nightly backup with verification.
$backupSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 6) `
    -MultipleInstances IgnoreNew
Register-HotelTask "HotelIT Backup" "Nightly verified backup of the database and uploads." `
    (New-ScheduledTaskAction -Execute $py -Argument "manage.py backup --scheduled --verify" -WorkingDirectory $AppDir) `
    (New-ScheduledTaskTrigger -Daily -At $BackupTime) $backupSettings

Start-ScheduledTask -TaskName "HotelIT Server"
Write-Host ""
Write-Host "The server is starting on port $Port. Check: Get-ScheduledTaskInfo 'HotelIT Server'" -ForegroundColor Green
Write-Host "Run a first backup now:  Start-ScheduledTask 'HotelIT Backup'  (result on Administration > Backups)"
