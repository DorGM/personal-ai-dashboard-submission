# Starts the app on 127.0.0.1:8000. Without -NoTunnel it first opens a Cloudflare quick tunnel
# and starts the app with PUBLIC_BASE_URL set to the tunnel URL (it changes on every start).
# -Demo starts Demo Mode (APP_MODE=demo, synthetic data, http://localhost only) and never
# opens a tunnel.
param([switch]$NoTunnel, [switch]$Demo, [int]$Port = 8000)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
. (Join-Path $PSScriptRoot "_proc.ps1")

$python = ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Missing $python. See README setup." }
foreach ($name in "server", "tunnel") {
  if (Get-TrackedProcess $name) { throw "A $name from an earlier start is still running. Run scripts\stop.ps1 first." }
}
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
  throw "Port $Port is already in use."
}
New-Item -ItemType Directory -Force -Path data | Out-Null
if ($Demo) { $NoTunnel = $true }  # Demo Mode is local-only: cloudflared is never started

if (-not $NoTunnel) {
  if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) { throw "cloudflared is not installed or not on PATH." }
  $log = "data\tunnel.log"
  Remove-Item $log -ErrorAction SilentlyContinue
  $tunnel = Start-Process -PassThru -WindowStyle Hidden -FilePath "cloudflared" `
    -ArgumentList "tunnel", "--url", "http://127.0.0.1:$Port" -RedirectStandardError $log
  Save-TrackedProcess "tunnel" $tunnel
  $url = $null
  for ($i = 0; $i -lt 30 -and -not $url; $i++) {
    Start-Sleep -Seconds 1
    $m = Select-String -Path $log -Pattern "https://[a-z0-9-]+\.trycloudflare\.com" -ErrorAction SilentlyContinue |
      Select-Object -First 1
    if ($m) { $url = $m.Matches[0].Value }
  }
  if (-not $url) { Stop-TrackedProcess "tunnel"; throw "Tunnel URL did not appear; see $log" }
}

# The child inherits PUBLIC_BASE_URL; restore this window's value so a later -NoTunnel run
# doesn't reuse a dead tunnel URL.
$savedBaseUrl = $env:PUBLIC_BASE_URL
$savedMode = $env:APP_MODE
if (-not $NoTunnel) { $env:PUBLIC_BASE_URL = $url }
if ($Demo) {
  $env:APP_MODE = "demo"
  $env:PUBLIC_BASE_URL = "http://localhost:$Port"
}
try {
  $server = Start-Process -PassThru -WindowStyle Hidden -FilePath $python `
    -ArgumentList "-m", "uvicorn", "app.main:create_app", "--factory", "--host", "127.0.0.1", "--port", "$Port" `
    -RedirectStandardError "data\server.log"
} finally {
  $env:PUBLIC_BASE_URL = $savedBaseUrl
  $env:APP_MODE = $savedMode
}
Save-TrackedProcess "server" $server

$healthy = $false
for ($i = 0; $i -lt 20 -and -not $healthy; $i++) {
  Start-Sleep -Milliseconds 500
  try { $healthy = (Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$Port/healthz").StatusCode -eq 200 } catch { }
}
if (-not $healthy) {
  Stop-TrackedProcess "server"; Stop-TrackedProcess "tunnel"
  throw "App did not become healthy. Last lines of data\server.log:`n$((Get-Content data\server.log -Tail 15) -join "`n")"
}

Write-Host "App running at http://localhost:$Port"
if ($Demo) { Write-Host "Demo Mode: open http://localhost:$Port and choose 'Explore demo data'. Synthetic data only." }
if (-not $NoTunnel) {
  Write-Host "Public URL: $url"
  Write-Host "Before signing in, add $url/auth/callback to the Google OAuth client's authorized redirect URIs."
}
