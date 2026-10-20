param(
    [string]$GateFile = "",
    [string]$Destination = "",
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = "python"
if (Test-Path "C:\Python315\python.exe") {
    $python = "C:\Python315\python.exe"
}
if (-not $GateFile) {
    $GateFile = Join-Path $root "TradingAgentsData\research\new_era_kronos_matrix_gate.csv"
}
if (-not $Destination) {
    $Destination = Join-Path $env:APPDATA "MetaQuotes\Terminal\Common\Files\new_era_kronos_matrix_gate.csv"
}

if (-not (Test-Path $GateFile)) {
    throw "Kronos Matrix gate file does not exist: $GateFile"
}

& $python (Join-Path $root "bot_manager.py") validate-kronos-gate --gate $GateFile
if ($LASTEXITCODE -ne 0) {
    throw "Kronos Matrix gate validation failed; nothing was copied."
}

if ($WhatIf) {
    Write-Host "Would copy $GateFile -> $Destination"
    exit 0
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Destination) | Out-Null
Copy-Item -Force $GateFile $Destination
Write-Host "Copied validated Kronos Matrix gate to: $Destination"
