param(
    [string]$Ticker = "XAUUSD",
    [string]$Date = (Get-Date -Format "yyyy-MM-dd"),
    [string]$Analysts = "market,news,social",
    [string]$Provider = "",
    [string]$Gate = "",
    [int]$GateTtlMinutes = 90,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$image = "new-era-tradingagents:local"
$rootEnvFile = Join-Path $root ".env"
$envFile = Join-Path $root "TradingAgents\.env"
$dataDir = Join-Path $root "TradingAgentsData"

# Sync root .env variables (including GEMINI_API_KEY -> GOOGLE_API_KEY) into current PowerShell session
foreach ($ef in @($rootEnvFile, $envFile)) {
    if (Test-Path $ef) {
        Get-Content $ef | ForEach-Object {
            $line = $_.Trim()
            if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
                $parts = $line.Split("=", 2)
                $k = $parts[0].Trim()
                $v = $parts[1].Trim().Trim("'").Trim('"')
                if ($k -and $v -and -not [Environment]::GetEnvironmentVariable($k)) {
                    [Environment]::SetEnvironmentVariable($k, $v, "Process")
                }
            }
        }
    }
}
if ($env:GEMINI_API_KEY -and -not $env:GOOGLE_API_KEY) {
    $env:GOOGLE_API_KEY = $env:GEMINI_API_KEY
}
if (-not $Provider -and ($env:GEMINI_API_KEY -or $env:GOOGLE_API_KEY)) {
    $Provider = "google"
}

New-Item -ItemType Directory -Force -Path (Join-Path $dataDir "research") | Out-Null

$python = "python"
$venvPy = Join-Path $root ".venv-api\Scripts\python.exe"
if (Test-Path $venvPy) {
    $python = $venvPy
} elseif (Test-Path "C:\Python315\python.exe") {
    $python = "C:\Python315\python.exe"
}

if ($DryRun) {
    & $python (Join-Path $root "run_tradingagents_research.py") `
        --ticker $Ticker `
        --date $Date `
        --analysts $Analysts `
        --data-dir $dataDir `
        --output (Join-Path $dataDir "research\tradingagents_latest.json") `
        --mt5-gate-output (Join-Path $dataDir "research\new_era_ai_gate.csv") `
        --gate-ttl-minutes $GateTtlMinutes `
        --dry-run
    if ($LASTEXITCODE -ne 0) {
        throw "TradingAgents dry-run failed with code $LASTEXITCODE"
    }
    exit 0
}

docker image inspect $image *> $null
if ($LASTEXITCODE -ne 0) {
    if ($env:GOOGLE_API_KEY -or $env:GEMINI_API_KEY) {
        $localArgs = @(
            (Join-Path $root "run_tradingagents_research.py"),
            "--ticker", $Ticker,
            "--date", $Date,
            "--analysts", $Analysts,
            "--data-dir", $dataDir,
            "--output", (Join-Path $dataDir "research\tradingagents_latest.json"),
            "--mt5-gate-output", (Join-Path $dataDir "research\new_era_ai_gate.csv"),
            "--gate-ttl-minutes", $GateTtlMinutes
        )
        if ($Provider) {
            $localArgs += @("--provider", $Provider)
        }
        if ($Gate) {
            $localArgs += @("--gate", $Gate)
        }
        & $python @localArgs
        if ($LASTEXITCODE -ne 0) {
            throw "TradingAgents Gemini bridge exited with code $LASTEXITCODE. No MT5 order was sent."
        }
        exit 0
    }
    throw "Docker image '$image' is not installed. Build it from .\TradingAgents with: docker build --tag $image ."
}


New-Item -ItemType Directory -Force -Path $dataDir | Out-Null

$dockerArgs = @(
    "run", "--rm",
    "--entrypoint", "python",
    "-v", "$root`:/workspace",
    "-w", "/workspace",
    "-e", "PYTHONPATH=/home/appuser/app",
    "-e", "TRADINGAGENTS_DATA_DIR=/workspace/TradingAgentsData"
)

if (Test-Path $rootEnvFile) {
    $dockerArgs += @("--env-file", $rootEnvFile)
}
if (Test-Path $envFile) {
    $dockerArgs += @("--env-file", $envFile)
}
if ($env:GOOGLE_API_KEY) {
    $dockerArgs += @("-e", "GOOGLE_API_KEY=$($env:GOOGLE_API_KEY)")
}
if ($env:GEMINI_API_KEY) {
    $dockerArgs += @("-e", "GEMINI_API_KEY=$($env:GEMINI_API_KEY)")
}

$dockerArgs += @(
    $image,
    "/workspace/run_tradingagents_research.py",
    "--ticker", $Ticker,
    "--date", $Date,
    "--analysts", $Analysts,
    "--data-dir", "/workspace/TradingAgentsData",
    "--output", "/workspace/TradingAgentsData/research/tradingagents_latest.json",
    "--mt5-gate-output", "/workspace/TradingAgentsData/research/new_era_ai_gate.csv",
    "--gate-ttl-minutes", $GateTtlMinutes
)

if ($Provider) {
    $dockerArgs += @("--provider", $Provider)
}

if ($Gate) {
    if ([IO.Path]::IsPathRooted($Gate)) {
        $fullGate = [IO.Path]::GetFullPath($Gate)
        $fullRoot = [IO.Path]::GetFullPath($root)
        if (-not $fullGate.StartsWith($fullRoot, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Gate file must be inside the NewEraTradingBot folder so Docker can read it."
        }
        $Gate = $fullGate.Substring($fullRoot.Length).TrimStart('\', '/')
    }
    $dockerArgs += @("--gate", ("/workspace/" + ($Gate -replace '\\', '/')))
}

if ($DryRun) {
    $dockerArgs += "--dry-run"
}

& docker @dockerArgs
if ($LASTEXITCODE -ne 0) {
    throw "TradingAgents bridge exited with code $LASTEXITCODE. No MT5 order was sent."
}
