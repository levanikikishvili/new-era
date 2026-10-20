param(
    [ValidateSet("combined", "matrix-analog")]
    [string]$Mode = "combined",
    [string]$InputCsv = "",
    [double]$SpreadPoints = 25.0,
    [int]$StartIndex = 3000,
    [int]$EndIndex = 0,
    [int]$Step = 12
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$image = "new-era-kronos:local"
$dataDir = Join-Path $root "TradingAgentsData"
$kronosData = Join-Path $root "KronosData"
if (-not $InputCsv) {
    $InputCsv = Join-Path $dataDir "market\xauusd_m5.csv"
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

New-Item -ItemType Directory -Force -Path (Join-Path $dataDir "research") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $kronosData "hf-cache") | Out-Null

$args = @(
    "run", "--rm",
    "--entrypoint", "python",
    "-v", "$root`:/workspace",
    "-v", "$kronosData`:/home/appuser/.cache/huggingface",
    "-e", "HF_HOME=/home/appuser/.cache/huggingface",
    $image,
    "/workspace/backtest_kronos_matrix.py",
    "--mode", $Mode,
    "--input-csv", ("/workspace/" + $inputRelative),
    "--spread-points", [string]$SpreadPoints,
    "--start-index", [string]$StartIndex,
    "--step", [string]$Step,
    "--output", "/workspace/TradingAgentsData/research/kronos_matrix_backtest.json",
    "--trades-output", "/workspace/TradingAgentsData/research/kronos_matrix_backtest_trades.csv"
)
if ($EndIndex -gt 0) {
    $args += @("--end-index", [string]$EndIndex)
}

& docker @args
if ($LASTEXITCODE -ne 0) {
    throw "Kronos Matrix backtest failed with code $LASTEXITCODE"
}
