# Lint, type-check and test. Exits non-zero on the first failure.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
.venv\Scripts\python.exe -m ruff check app tests scripts; if ($LASTEXITCODE) { exit $LASTEXITCODE }
.venv\Scripts\python.exe -m mypy; if ($LASTEXITCODE) { exit $LASTEXITCODE }
.venv\Scripts\python.exe -m pytest; exit $LASTEXITCODE
