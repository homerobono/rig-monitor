<#
  Run rig-monitor in the foreground with a visible log - useful for a first run or
  for troubleshooting. Use install-windows.ps1 for the permanent background setup.
#>
param([int] $Port = 8600)

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $root
try {
  $py = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
  if (-not $py) { $py = (Get-Command py.exe -ErrorAction Stop).Source }
  & $py -m rigmon serve --port $Port
} finally {
  Pop-Location
}
