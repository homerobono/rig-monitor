<#
.SYNOPSIS
  Sets rig-monitor up to run in the background on Windows 11 and opens it to the LAN.

.DESCRIPTION
  Creates a scheduled task that starts the collector at logon (hidden, no console
  window) and adds an inbound firewall rule scoped to your local subnet.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\install-windows.ps1
  powershell -ExecutionPolicy Bypass -File .\install-windows.ps1 -Port 8600 -Subnet 192.168.0.0/24
#>
[CmdletBinding()]
param(
  [int]    $Port         = 8600,
  [string] $Subnet       = '192.168.0.0/24',
  [string] $TaskName     = 'rig-monitor',
  [switch] $Uninstall
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Assert-Admin {
  $id = [Security.Principal.WindowsIdentity]::GetCurrent()
  if (-not ([Security.Principal.WindowsPrincipal]$id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this from an elevated PowerShell (right click -> Run as administrator).'
  }
}

Assert-Admin

if ($Uninstall) {
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
  Remove-NetFirewallRule -DisplayName "rig-monitor ($Port)" -ErrorAction SilentlyContinue
  Write-Host 'rig-monitor removed.' -ForegroundColor Yellow
  return
}

# --- locate pythonw.exe so the collector runs without a console window ----------
$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw) {
  $py = (Get-Command py.exe -ErrorAction SilentlyContinue).Source
  if ($py) { $pythonw = (& $py -c "import sys,os;print(os.path.join(sys.base_prefix,'pythonw.exe'))") }
}
if (-not $pythonw -or -not (Test-Path $pythonw)) {
  throw "pythonw.exe not found. Install Python 3.11+ (winget install Python.Python.3.12) and re-run."
}
Write-Host "python      : $pythonw"

# --- sanity check the data source before installing anything --------------------
$python = $pythonw -replace 'pythonw\.exe$', 'python.exe'
if (Test-Path $python) {
  Write-Host ''
  Push-Location $root
  try { & $python -m rigmon check 2>&1 | ForEach-Object { Write-Host "  $_" } }
  finally { Pop-Location }
  Write-Host ''
}

# --- scheduled task at logon ----------------------------------------------------
$action = New-ScheduledTaskAction -Execute $pythonw `
  -Argument "-m rigmon serve --port $Port" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartInterval (New-TimeSpan -Minutes 1) `
  -RestartCount 999 -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
  -LogonType Interactive -RunLevel Highest

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
  -Settings $settings -Principal $principal `
  -Description 'Records hardware telemetry and serves the rig-monitor dashboard.' | Out-Null
Write-Host "task        : '$TaskName' registered (starts at logon)" -ForegroundColor Green

# --- firewall, restricted to the local subnet -----------------------------------
Remove-NetFirewallRule -DisplayName "rig-monitor ($Port)" -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName "rig-monitor ($Port)" -Direction Inbound -Action Allow `
  -Protocol TCP -LocalPort $Port -Profile Private -RemoteAddress $Subnet | Out-Null
Write-Host "firewall    : TCP $Port allowed from $Subnet (private networks)" -ForegroundColor Green

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3

$ip = (Get-NetIPAddress -AddressFamily IPv4 |
       Where-Object { $_.IPAddress -notlike '127.*' -and $_.PrefixOrigin -ne 'WellKnown' } |
       Select-Object -First 1).IPAddress
Write-Host ''
Write-Host 'rig-monitor is running.' -ForegroundColor Green
Write-Host "  on this PC     http://localhost:$Port/"
Write-Host "  on the LAN     http://${ip}:$Port/"
Write-Host ''
Write-Host 'LibreHardwareMonitor must be running as administrator with' -ForegroundColor DarkGray
Write-Host 'Options -> Remote Web Server -> Run enabled on port 8085.' -ForegroundColor DarkGray
