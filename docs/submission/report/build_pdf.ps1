# Rebuilds the technical report PDF from technical-report.html with headless Chrome or Edge.
# No Python dependency is needed. Usage (from the repository root):
#   powershell -ExecutionPolicy Bypass -File docs\submission\report\build_pdf.ps1
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$html = Join-Path $here "technical-report.html"
$pdf = Join-Path $here "Personal-AI-Dashboard-Technical-Report.pdf"
$browsers = @(
  "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
  "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
  "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe"
) | Where-Object { Test-Path $_ }
if (-not $browsers) { throw "Chrome or Edge not found." }
$uri = ([System.Uri](Resolve-Path $html).Path).AbsoluteUri
$profileDir = Join-Path $env:TEMP "pad-pdf-profile"
& $browsers[0] --headless=new --disable-gpu --no-first-run --user-data-dir="$profileDir" `
  --no-pdf-header-footer --print-to-pdf="$pdf" $uri | Out-Null
Start-Sleep -Seconds 2
if (-not (Test-Path $pdf)) { throw "PDF was not produced." }
Write-Output "Wrote $pdf ($((Get-Item $pdf).Length) bytes)"
