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
    $GateFile = Join-Path $root "TradingAgentsData\research\new_era_ai_gate.csv"
}
if (-not $Destination) {
    $Destination = Join-Path $env:APPDATA "MetaQuotes\Terminal\Common\Files\new_era_ai_gate.csv"
}

if (-not (Test-Path $GateFile)) {
    throw "AI gate file does not exist: $GateFile"
}

$validation = & $python (Join-Path $root "bot_manager.py") validate-gate --gate $GateFile
if ($LASTEXITCODE -ne 0) {
    throw "AI gate validation failed; nothing was copied."
}

$destinationDirectory = Split-Path -Parent $Destination
if ($WhatIf) {
    Write-Host "Would copy $GateFile -> $Destination"
    exit 0
}

New-Item -ItemType Directory -Force -Path $destinationDirectory | Out-Null
Copy-Item -Force $GateFile $Destination
Write-Host "Copied validated AI gate to: $Destination"
