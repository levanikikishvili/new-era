# Creates / refreshes the "New Era" Desktop + Start Menu shortcuts (double-click launch, custom icon).
# Run once:  powershell -ExecutionPolicy Bypass -File .\create_desktop_shortcut.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonw = Join-Path $root ".venv-api\Scripts\pythonw.exe"
$launcher = Join-Path $root "NewEraLauncher.pyw"
$icon = Join-Path $root "assets\newera.ico"

if (-not (Test-Path $pythonw)) { throw "pythonw.exe not found: $pythonw" }
if (-not (Test-Path $launcher)) { throw "Launcher not found: $launcher" }

$shell = New-Object -ComObject WScript.Shell
$targets = @(
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "New Era.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Programs")) "New Era.lnk")
)
foreach ($lnkPath in $targets) {
    $lnk = $shell.CreateShortcut($lnkPath)
    $lnk.TargetPath = $pythonw
    $lnk.Arguments = "`"$launcher`""
    $lnk.WorkingDirectory = $root
    $lnk.Description = "New Era 2.0 - AI Trading Terminal (7-Bot Fleet, MT5 DEMO)"
    if (Test-Path $icon) { $lnk.IconLocation = "$icon,0" }
    $lnk.WindowStyle = 1
    $lnk.Save()
    Write-Host "Shortcut created: $lnkPath" -ForegroundColor Green
}
# Refresh the Windows icon cache so the new icon shows immediately
Start-Process -FilePath "ie4uinit.exe" -ArgumentList "-show" -WindowStyle Hidden -ErrorAction SilentlyContinue
