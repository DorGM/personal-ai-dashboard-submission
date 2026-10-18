# Stops the app and the tunnel started by start.ps1. Revoking the OpenAI key is a separate manual step.
Set-Location (Join-Path $PSScriptRoot "..")
. (Join-Path $PSScriptRoot "_proc.ps1")
foreach ($name in "tunnel", "server") { Stop-TrackedProcess $name }
Write-Host "Reminder: disable or revoke the OpenAI API key at platform.openai.com if the demo is over."
