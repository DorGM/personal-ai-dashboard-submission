# Helpers for tracking processes started by start.ps1 (dot-sourced; not run directly).
# A pid file stores "pid|start-time" so a reused PID is never mistaken for ours.

function Save-TrackedProcess([string]$Name, $Process) {
  "$($Process.Id)|$($Process.StartTime.ToFileTimeUtc())" | Set-Content "data\$Name.pid"
}

function Get-TrackedProcess([string]$Name) {
  $file = "data\$Name.pid"
  if (-not (Test-Path $file)) { return $null }
  $id, $started = (Get-Content $file).Split("|")
  $p = Get-Process -Id $id -ErrorAction SilentlyContinue
  if ($p -and $p.StartTime.ToFileTimeUtc() -eq [long]$started) { return $p }
  Remove-Item $file  # stale: process gone or PID reused
  return $null
}

function Stop-TrackedProcess([string]$Name) {
  $p = Get-TrackedProcess $Name
  if (-not $p) { Write-Host "$Name not running"; return }
  # /T also ends child processes (the venv python.exe launcher spawns the real interpreter).
  taskkill /PID $p.Id /T /F | Out-Null
  Remove-Item "data\$Name.pid" -ErrorAction SilentlyContinue
  Write-Host "Stopped $Name (pid $($p.Id))"
}
