param(
    [string]$PythonPath = "",
    [int]$Port = 8787,
    [switch]$Install,
    [switch]$Reload
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = if ($PythonPath) { $PythonPath } else { "python" }

if (-not (Test-Path $python) -and $PythonPath) {
    throw "Python executable not found: $python"
}

if ($Install) {
    & $python -m pip install -r (Join-Path $root "requirements_api.txt")
    if ($LASTEXITCODE -ne 0) {
        throw "API dependency installation failed with code $LASTEXITCODE"
    }
}

$env:NEWERA_API_PORT = [string]$Port
$args = @(
    "-m", "uvicorn", "main_api:app",
    "--host", "127.0.0.1",
    "--port", [string]$Port
)
if ($Reload) {
    $args += "--reload"
}

Write-Host "Starting New Era API at http://127.0.0.1:$Port"
Write-Host "OpenAPI UI: http://127.0.0.1:$Port/docs"
& $python @args
if ($LASTEXITCODE -ne 0) {
    throw "New Era API stopped with code $LASTEXITCODE"
}
