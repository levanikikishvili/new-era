param(
    [string]$PythonPath = "",
    [string]$ApiHost = "127.0.0.1",
    [int]$ApiPort = 8787,
    [switch]$SkipInstall,
    [switch]$ForceInstall,
    [switch]$NoAutoStartApi,
    [switch]$SmokeTest
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $root ".venv-api\Scripts\python.exe"

if ($PythonPath) {
    $python = $PythonPath
}
elseif (Test-Path $venvPython) {
    $python = $venvPython
}
else {
    Write-Host "[NewEra UI] Creating .venv-api virtual environment..." -ForegroundColor Cyan
    $basePython = if (Test-Path "C:\Python315\python.exe") { "C:\Python315\python.exe" } else { "python" }
    & $basePython -m venv (Join-Path $root ".venv-api")
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create .venv-api virtual environment (exit code $LASTEXITCODE)"
    }
    $python = $venvPython
}

if (-not (Test-Path $python)) {
    throw "Python executable not found: $python"
}

$needsInstall = $ForceInstall.IsPresent
if (-not $SkipInstall -and -not $needsInstall) {
    & $python -c "import PyQt6, PyQt6.QtWebEngineWidgets, fastapi, uvicorn, requests, websockets" 2>$null
    if ($LASTEXITCODE -ne 0) {
        $needsInstall = $true
    }
}

if ($needsInstall) {
    Write-Host "[NewEra UI] Installing PyQt6, PyQt6-WebEngine, and API requirements in .venv-api..." -ForegroundColor Cyan
    & $python -m pip install --disable-pip-version-check -r (Join-Path $root "requirements_api.txt")
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install requirements_api.txt in $python (exit code $LASTEXITCODE)"
    }
}

# Ensure MetaTrader5 is installed in Python 3.14 bridge if present
$py314 = Join-Path $env:LOCALAPPDATA "Programs\Python\Python314\python.exe"
if (Test-Path $py314) {
    & $py314 -c "import MetaTrader5" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[NewEra UI] Installing MetaTrader5 in Python 3.14 bridge..." -ForegroundColor Cyan
        & $py314 -m pip install --disable-pip-version-check MetaTrader5
    }
}

$apiUrl = "http://${ApiHost}:${ApiPort}"
$wsUrl = "ws://${ApiHost}:${ApiPort}/ws/logs"
$healthUrl = "$apiUrl/health"
$mt5StatusUrl = "$apiUrl/mt5/status"

function Test-NewEraApiUpToDate {
    try {
        $resp = Invoke-RestMethod -Uri $healthUrl -Method Get -TimeoutSec 3 -ErrorAction Stop
        if ($null -eq $resp -or $resp.status -ne "ok") {
            return $false
        }
        return ($null -ne $resp.PSObject.Properties["gemini_key_configured"])
    }
    catch {
        return $false
    }
}

$startedBackgroundApi = $null
if (-not (Test-NewEraApiUpToDate) -and -not $NoAutoStartApi) {
    # Stop any outdated uvicorn process bound to $ApiPort
    $portConn = Get-NetTCPConnection -LocalPort $ApiPort -State Listen -ErrorAction SilentlyContinue
    if ($portConn) {
        foreach ($conn in $portConn) {
            if ($conn.OwningProcess -gt 0) {
                Write-Host "[NewEra UI] Restarting outdated API server (PID $($conn.OwningProcess))..." -ForegroundColor Yellow
                Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue
            }
        }
        Start-Sleep -Milliseconds 600
    }

    Write-Host "[NewEra UI] Starting FastAPI + MT5 Direct server on $apiUrl..." -ForegroundColor Cyan
    $startedBackgroundApi = Start-Process -FilePath $python `
        -ArgumentList @("-m", "uvicorn", "main_api:app", "--host", $ApiHost, "--port", [string]$ApiPort) `
        -WorkingDirectory $root `
        -WindowStyle Hidden `
        -PassThru

    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 400
        if (Test-NewEraApiUpToDate) {
            Write-Host "[NewEra UI] FastAPI + MT5 Direct backend online at $apiUrl" -ForegroundColor Green
            break
        }
    }
}
else {
    Write-Host "[NewEra UI] Connected to running FastAPI + MT5 Direct server at $apiUrl" -ForegroundColor Green
}

$uiScript = Join-Path $root "ui\desktop_app.py"
$uiArgs = @($uiScript, "--api-url", $apiUrl, "--ws-url", $wsUrl)
if ($SmokeTest) {
    $uiArgs += "--smoke-test"
}

Write-Host "[NewEra UI] Launching New Era Enterprise AI Quant Platform..." -ForegroundColor Cyan
try {
    & $python @uiArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Desktop UI exited with code $LASTEXITCODE"
    }
}
finally {
    if ($SmokeTest -and $null -ne $startedBackgroundApi -and -not $startedBackgroundApi.HasExited) {
        Stop-Process -Id $startedBackgroundApi.Id -Force -ErrorAction SilentlyContinue
    }
}
