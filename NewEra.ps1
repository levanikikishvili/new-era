param(
    [ValidateSet("List", "Status", "Validate", "DryRun", "Research", "SyncGate", "BuildImage", "BuildKronosImage", "ExportMarketData", "KronosDryRun", "KronosResearch", "KronosBacktest", "SyncKronosGate", "ComboDryRun", "ComboResearch", "ApiInstall", "ApiStart")]
    [string]$Action = "Status",
    [string]$Ticker = "XAUUSD",
    [string]$Date = (Get-Date -Format "yyyy-MM-dd"),
    [string]$Analysts = "market,news,social",
    [string]$Provider = "",
    [string]$Gate = "",
    [int]$GateTtlMinutes = 90,
    [string]$KronosInputCsv = "",
    [Nullable[double]]$SpreadPoints = $null,
    [int]$Bars = 5000,
    [string]$PythonPath = "",
    [ValidateSet("combined", "matrix-analog")]
    [string]$BacktestMode = "combined",
    [int]$StartIndex = 3000,
    [int]$EndIndex = 0,
    [int]$Step = 12,
    [int]$ApiPort = 8787,
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = if ($PythonPath) { $PythonPath } else { "python" }
if (-not $PythonPath -and (Test-Path "C:\Python315\python.exe")) {
    $python = "C:\Python315\python.exe"
}

function Invoke-Manager([string]$managerAction, [string]$gatePath = "") {
    if ($gatePath) {
        & $python (Join-Path $root "bot_manager.py") $managerAction --gate $gatePath
    }
    else {
        & $python (Join-Path $root "bot_manager.py") $managerAction
    }
    if ($LASTEXITCODE -ne 0) {
        throw "bot_manager.py failed with code $LASTEXITCODE"
    }
}

switch ($Action) {
    "List" {
        Invoke-Manager "list"
    }
    "Status" {
        Invoke-Manager "status"
    }
    "Validate" {
        Invoke-Manager "validate"
    }
    "DryRun" {
        & (Join-Path $root "run_tradingagents_research.ps1") -DryRun
        if ($LASTEXITCODE -ne 0) {
            throw "research dry-run failed with code $LASTEXITCODE"
        }
        Invoke-Manager "validate-gate"
    }
    "Research" {
        $researchScript = Join-Path $root "run_tradingagents_research.ps1"
        if ($Provider -and $Gate) {
            & $researchScript -Ticker $Ticker -Date $Date -Analysts $Analysts -GateTtlMinutes $GateTtlMinutes -Provider $Provider -Gate $Gate
        }
        elseif ($Provider) {
            & $researchScript -Ticker $Ticker -Date $Date -Analysts $Analysts -GateTtlMinutes $GateTtlMinutes -Provider $Provider
        }
        elseif ($Gate) {
            & $researchScript -Ticker $Ticker -Date $Date -Analysts $Analysts -GateTtlMinutes $GateTtlMinutes -Gate $Gate
        }
        else {
            & $researchScript -Ticker $Ticker -Date $Date -Analysts $Analysts -GateTtlMinutes $GateTtlMinutes
        }
        if ($LASTEXITCODE -ne 0) {
            throw "research run failed with code $LASTEXITCODE"
        }
        Invoke-Manager "validate-gate"
    }
    "SyncGate" {
        if ($WhatIf) {
            & (Join-Path $root "sync_ai_gate_to_mt5.ps1") -WhatIf
        }
        else {
            & (Join-Path $root "sync_ai_gate_to_mt5.ps1")
        }
        if ($LASTEXITCODE -ne 0) {
            throw "gate sync failed with code $LASTEXITCODE"
        }
    }
    "BuildImage" {
        & docker build --tag "new-era-tradingagents:local" (Join-Path $root "TradingAgents")
        if ($LASTEXITCODE -ne 0) {
            throw "Docker image build failed with code $LASTEXITCODE"
        }
    }
    "BuildKronosImage" {
        & docker build --file (Join-Path $root "Dockerfile.kronos") --tag "new-era-kronos:local" $root
        if ($LASTEXITCODE -ne 0) {
            throw "Kronos Docker image build failed with code $LASTEXITCODE"
        }
    }
    "ExportMarketData" {
        $exportArgs = @(
            "--symbol", $Ticker,
            "--bars", $Bars,
            "--output", (Join-Path $root "TradingAgentsData\market\xauusd_m5.csv")
        )
        & $python (Join-Path $root "export_mt5_closed_bars.py") @exportArgs
        if ($LASTEXITCODE -ne 0) {
            throw "MT5 market-data export failed with code $LASTEXITCODE"
        }
    }
    "KronosDryRun" {
        $kronosScript = Join-Path $root "run_kronos_matrix_research.ps1"
        if ($KronosInputCsv) {
            & $kronosScript -DryRun -InputCsv $KronosInputCsv
        }
        else {
            & $kronosScript -DryRun
        }
        if ($LASTEXITCODE -ne 0) {
            throw "Kronos dry-run failed with code $LASTEXITCODE"
        }
    }
    "KronosResearch" {
        $kronosScript = Join-Path $root "run_kronos_matrix_research.ps1"
        if ($KronosInputCsv -and ($SpreadPoints -ne $null)) {
            & $kronosScript -InputCsv $KronosInputCsv -SpreadPoints $SpreadPoints
        }
        elseif ($KronosInputCsv) {
            & $kronosScript -InputCsv $KronosInputCsv
        }
        elseif ($SpreadPoints -ne $null) {
            & $kronosScript -SpreadPoints $SpreadPoints
        }
        else {
            & $kronosScript
        }
        if ($LASTEXITCODE -ne 0) {
            throw "Kronos research failed with code $LASTEXITCODE"
        }
    }
    "KronosBacktest" {
        $backtestScript = Join-Path $root "run_kronos_matrix_backtest.ps1"
        if ($KronosInputCsv) {
            & $backtestScript -Mode $BacktestMode -InputCsv $KronosInputCsv -SpreadPoints $(if ($SpreadPoints -ne $null) { $SpreadPoints } else { 25.0 }) -StartIndex $StartIndex -EndIndex $EndIndex -Step $Step
        }
        else {
            & $backtestScript -Mode $BacktestMode -SpreadPoints $(if ($SpreadPoints -ne $null) { $SpreadPoints } else { 25.0 }) -StartIndex $StartIndex -EndIndex $EndIndex -Step $Step
        }
        if ($LASTEXITCODE -ne 0) {
            throw "Kronos Matrix backtest failed with code $LASTEXITCODE"
        }
    }
    "SyncKronosGate" {
        if ($WhatIf) {
            & (Join-Path $root "sync_kronos_gate_to_mt5.ps1") -WhatIf
        }
        else {
            & (Join-Path $root "sync_kronos_gate_to_mt5.ps1")
        }
        if ($LASTEXITCODE -ne 0) {
            throw "Kronos gate sync failed with code $LASTEXITCODE"
        }
    }
    "ComboDryRun" {
        & (Join-Path $root "run_tradingagents_research.ps1") -DryRun
        if ($LASTEXITCODE -ne 0) {
            throw "TradingAgents dry-run failed with code $LASTEXITCODE"
        }
        & (Join-Path $root "run_kronos_matrix_research.ps1") -DryRun
        if ($LASTEXITCODE -ne 0) {
            throw "Kronos Matrix dry-run failed with code $LASTEXITCODE"
        }
        & $python (Join-Path $root "combine_research_gates.py")
        if ($LASTEXITCODE -ne 0) {
            throw "combined dry-run gate failed with code $LASTEXITCODE"
        }
        Invoke-Manager "validate-kronos-gate" (Join-Path $root "TradingAgentsData\research\new_era_kronos_matrix_gate.csv")
    }
    "ComboResearch" {
        $tradingScript = Join-Path $root "run_tradingagents_research.ps1"
        if ($Provider) {
            & $tradingScript -Provider $Provider
        }
        else {
            & $tradingScript
        }
        if ($LASTEXITCODE -ne 0) {
            throw "TradingAgents research failed with code $LASTEXITCODE"
        }

        $kronosScript = Join-Path $root "run_kronos_matrix_research.ps1"
        if ($KronosInputCsv -and ($SpreadPoints -ne $null)) {
            & $kronosScript -InputCsv $KronosInputCsv -SpreadPoints $SpreadPoints
        }
        elseif ($KronosInputCsv) {
            & $kronosScript -InputCsv $KronosInputCsv
        }
        elseif ($SpreadPoints -ne $null) {
            & $kronosScript -SpreadPoints $SpreadPoints
        }
        else {
            & $kronosScript
        }
        if ($LASTEXITCODE -ne 0) {
            throw "Kronos Matrix research failed with code $LASTEXITCODE"
        }

        & $python (Join-Path $root "combine_research_gates.py")
        if ($LASTEXITCODE -ne 0) {
            throw "combined research gate failed with code $LASTEXITCODE"
        }
        Invoke-Manager "validate-kronos-gate" (Join-Path $root "TradingAgentsData\research\new_era_kronos_matrix_gate.csv")
    }
    "ApiInstall" {
        & $python -m pip install -r (Join-Path $root "requirements_api.txt")
        if ($LASTEXITCODE -ne 0) {
            throw "API dependency installation failed with code $LASTEXITCODE"
        }
    }
    "ApiStart" {
        $apiScript = Join-Path $root "start_newera_api.ps1"
        if ($PythonPath) {
            & $apiScript -PythonPath $PythonPath -Port $ApiPort
        }
        else {
            & $apiScript -Port $ApiPort
        }
        if ($LASTEXITCODE -ne 0) {
            throw "New Era API stopped with code $LASTEXITCODE"
        }
    }
}
