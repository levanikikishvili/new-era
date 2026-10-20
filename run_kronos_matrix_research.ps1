param(
    [string]$InputCsv = "",
    [string]$Ticker = "XAUUSD",
    [Nullable[double]]$SpreadPoints = $null,
    [Nullable[double]]$Point = $null,
    [Nullable[double]]$MaxSpreadPoints = 180.0,
    [Nullable[double]]$MaxSpreadToAtr = 0.45,
    [int]$GateTtlMinutes = 90,
    [string]$Device = "",
    [switch]$DryRun,
    [switch]$VerboseModel
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$image = "new-era-kronos:local"
$dataDir = Join-Path $root "TradingAgentsData"
$kronosData = Join-Path $root "KronosData"
$gateOutput = Join-Path $dataDir "research\new_era_kronos_matrix_gate.csv"
$reportOutput = Join-Path $dataDir "research\kronos_matrix_latest.json"

if (-not $InputCsv) {
    $InputCsv = Join-Path $dataDir "market\xauusd_m5.csv"
}

New-Item -ItemType Directory -Force -Path (Join-Path $dataDir "research") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $kronosData "hf-cache") | Out-Null

if ($DryRun) {
    $python = "python"
    if (Test-Path "C:\Python315\python.exe") {
        $python = "C:\Python315\python.exe"
    }
    & $python (Join-Path $root "run_kronos_matrix_research.py") `
        --symbol $Ticker `
        --output $reportOutput `
        --gate-output $gateOutput `
        --gate-ttl-minutes $GateTtlMinutes `
        --dry-run
    if ($LASTEXITCODE -ne 0) {
        throw "Kronos Matrix dry-run failed with code $LASTEXITCODE"
    }
    exit 0
}

docker image inspect $image *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Docker image '$image' is not installed. Run .\NewEra.ps1 -Action BuildKronosImage first."
}

$inputFull = [IO.Path]::GetFullPath($InputCsv)
$rootFull = [IO.Path]::GetFullPath($root)
if (-not $inputFull.StartsWith($rootFull, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Input CSV must be inside the NewEraTradingBot folder so Docker can read it."
}
$inputRelative = $inputFull.Substring($rootFull.Length).TrimStart('\', '/') -replace '\\', '/'

$dockerArgs = @(
    "run", "--rm",
    "-v", "$root`:/workspace",
    "-v", "$kronosData`:/home/appuser/.cache/huggingface",
    "-e", "HF_HOME=/home/appuser/.cache/huggingface",
    $image,
    "--input-csv", ("/workspace/" + $inputRelative),
    "--symbol", $Ticker,
    "--gate-ttl-minutes", [string]$GateTtlMinutes,
    "--output", "/workspace/TradingAgentsData/research/kronos_matrix_latest.json",
    "--gate-output", "/workspace/TradingAgentsData/research/new_era_kronos_matrix_gate.csv"
)

if ($SpreadPoints -ne $null) {
    $dockerArgs += @("--spread-points", [string]$SpreadPoints)
}
if ($Point -ne $null) {
    $dockerArgs += @("--point", [string]$Point)
}
if ($MaxSpreadPoints -ne $null) {
    $dockerArgs += @("--max-spread-points", [string]$MaxSpreadPoints)
}
if ($MaxSpreadToAtr -ne $null) {
    $dockerArgs += @("--max-spread-to-atr", [string]$MaxSpreadToAtr)
}
if ($Device) {
    $dockerArgs += @("--device", $Device)
}
if ($DryRun) {
    $dockerArgs += "--dry-run"
}
if ($VerboseModel) {
    $dockerArgs += "--verbose"
}

& docker @dockerArgs
if ($LASTEXITCODE -ne 0) {
    throw "Kronos Matrix runner exited with code $LASTEXITCODE. The gate is fail-closed."
}

Write-Host "Report: $reportOutput"
Write-Host "Gate:   $gateOutput"
