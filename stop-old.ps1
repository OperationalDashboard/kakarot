# Stops a Kakarot that is already running (for example an older version started earlier),
# so start.bat can start the current one. Only touches the Python processes serving Kakarot's
# port and the old start.bat window looking after them - never this window.
$ErrorActionPreference = "SilentlyContinue"
$port = 5050
try { $port = [int]((Get-Content (Join-Path $PSScriptRoot "config.json") -Raw | ConvertFrom-Json).port) } catch {}
$me = (Get-CimInstance Win32_Process -Filter "ProcessId=$PID").ParentProcessId
$pids = @(Get-NetTCPConnection -LocalPort $port -State Listen | Select-Object -ExpandProperty OwningProcess -Unique)
$stopped = 0
foreach ($id in $pids) {
  $p = Get-CimInstance Win32_Process -Filter "ProcessId=$id"
  if (-not $p -or $p.Name -notlike "python*") { continue }
  $chain = @($p)
  $up = Get-CimInstance Win32_Process -Filter "ProcessId=$($p.ParentProcessId)"
  if ($up -and $up.Name -like "python*") { $chain += $up; $up = Get-CimInstance Win32_Process -Filter "ProcessId=$($up.ParentProcessId)" }
  # close the old start.bat window first so it cannot start the old copy again
  if ($up -and $up.Name -eq "cmd.exe" -and $up.ProcessId -ne $me -and $up.CommandLine -like "*start.bat*") {
    Stop-Process -Id $up.ProcessId -Force
  }
  foreach ($c in $chain) { Stop-Process -Id $c.ProcessId -Force }
  $stopped++
}
if ($stopped) { Write-Host "  Stopped the Kakarot that was already running."; Start-Sleep -Seconds 2 }
