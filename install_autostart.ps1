<#
    Register the bot to start when you log in to Windows.

    Run once:
        powershell -ExecutionPolicy Bypass -File install_autostart.ps1

    Remove it again:
        powershell -ExecutionPolicy Bypass -File install_autostart.ps1 -Uninstall

    A scheduled task is used rather than a Startup-folder shortcut because the
    task can restart the bot if it dies, wait for the network to come up, and
    keep running when the laptop is on battery. None of that is configurable
    from a shortcut.

    The task runs as you, only while you are logged in, and needs no admin
    rights and no stored password.
#>
param(
    [switch]$Uninstall,
    [string]$TaskName = "Memehunter bot",
    # Windows can reach the logon trigger before the Wi-Fi associates. The bot
    # retries on its own as well; this just avoids the first few failures.
    [int]$DelayMinutes = 1
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed the scheduled task '$TaskName'."
        Write-Host "The bot keeps running until you close it or restart the laptop."
    } else {
        Write-Host "No scheduled task named '$TaskName' to remove."
    }
    return
}

# pythonw.exe, not python.exe: the windowless build, so nothing flashes up at
# logon. The bot writes bot.log itself, so losing the console costs nothing.
$python = Join-Path (Split-Path -Parent (Get-Command python).Source) "pythonw.exe"
if (-not (Test-Path $python)) {
    throw "pythonw.exe not found next to python.exe. Looked in: $python"
}
$entry = Join-Path $root "main.py"
if (-not (Test-Path $entry)) { throw "main.py not found in $root" }
if (-not (Test-Path (Join-Path $root ".env"))) {
    Write-Warning "No .env in $root - the bot will start and stop again until one exists."
}

$action = New-ScheduledTaskAction -Execute $python -Argument "`"$entry`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = "PT${DelayMinutes}M"

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force `
    -Description "Starts the Memehunter Telegram bot at logon. See $root" | Out-Null

Write-Host "Registered '$TaskName'."
Write-Host "  runs    : $python `"$entry`""
Write-Host "  from    : $root"
Write-Host "  when    : at your logon, after $DelayMinutes minute(s)"
Write-Host "  on crash: restarts up to 3 times, a minute apart"
Write-Host ""
Write-Host "Start it now without logging out:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Check on it:                       Get-ScheduledTaskInfo -TaskName '$TaskName'"
Write-Host "Read its log:                      Get-Content '$root\bot.log' -Tail 20"
