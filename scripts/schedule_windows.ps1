<#
Registers the LTM GCC jobs in Windows Task Scheduler for the current user. Run once on the machine that will host
the jobs, from the project folder, after setup (venv, Playwright Chromium, .env, data\gcc.db):

    powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1
    powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1 -DailyTime 07:00 -WeeklyDay Monday -WeeklyTime 09:00 -Dashboard

Times are the machine's local time: set the machine to India Standard Time, or adjust the times.
Missed runs (machine off) start as soon as it is back on. Remove with:
    Unregister-ScheduledTask -TaskName "LTM GCC*" -Confirm:$false
#>
param(
    [string]$DailyTime = "07:00",
    [string]$WeeklyDay = "Monday",
    [string]$WeeklyTime = "09:00",
    [switch]$NoWeekly,
    [switch]$Dashboard
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "Python venv not found at $py - run the setup steps in README.md first." }

$opts = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew

$a = New-ScheduledTaskAction -Execute $py -Argument "main.py --refresh" -WorkingDirectory $root
Register-ScheduledTask -TaskName "LTM GCC daily refresh" -Action $a -Settings $opts -Force `
    -Trigger (New-ScheduledTaskTrigger -Daily -At $DailyTime) `
    -Description "Scans for new GCCs (last 3 days) and adds them to the dashboard database. No email." | Out-Null
Write-Host "Daily refresh: every day at $DailyTime"

if (-not $NoWeekly) {
    $a = New-ScheduledTaskAction -Execute $py -Argument "main.py --once" -WorkingDirectory $root
    Register-ScheduledTask -TaskName "LTM GCC weekly email" -Action $a -Settings $opts -Force `
        -Trigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek $WeeklyDay -At $WeeklyTime) `
        -Description "Sends the LTM executive email + PDF briefing for the last two weeks." | Out-Null
    Write-Host "Weekly email: every $WeeklyDay at $WeeklyTime"
}

if ($Dashboard) {
    $a = New-ScheduledTaskAction -Execute $py -Argument "main.py --dashboard" -WorkingDirectory $root
    $dash = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0
    Register-ScheduledTask -TaskName "LTM GCC dashboard" -Action $a -Settings $dash -Force `
        -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME) `
        -Description "Serves the dashboard at http://127.0.0.1:8000 while you are logged in." | Out-Null
    Write-Host "Dashboard: starts at logon (http://127.0.0.1:8000)"
}
