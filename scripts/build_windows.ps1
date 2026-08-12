# Build a standalone Windows .exe for the viewer. Run this ON Windows
# (PyInstaller does not cross-compile from macOS/Linux).
#
# Usage (PowerShell):
#   cd pdf-drm
#   .\scripts\build_windows.ps1

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (-not (Test-Path ".venv")) {
    python -m venv .venv
}
& ".venv\Scripts\Activate.ps1"
pip install -q -r requirements-build.txt

Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
pyinstaller --clean --noconfirm viewer.spec

Write-Host ""
Write-Host "Built: dist\SecureViewer\SecureViewer.exe"

Compress-Archive -Path "dist\SecureViewer\*" -DestinationPath "dist\SecureViewer-Windows.zip" -Force
Write-Host "Zipped for distribution: dist\SecureViewer-Windows.zip"

# Also produce a proper Setup.exe installer (Start Menu shortcut, uninstaller
# in "Add or remove programs") if Inno Setup is available -- optional, since
# not everyone building this has it installed.
$iscc = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
if (-not $iscc) {
    $candidate = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
    if (Test-Path $candidate) { $iscc = $candidate }
}
if ($iscc) {
    & $iscc "installer\windows\SecureViewer.iss"
    Write-Host "Installer built: dist\SecureViewer-Setup.exe"
} else {
    Write-Host ""
    Write-Host "Inno Setup (ISCC.exe) not found -- skipped building dist\SecureViewer-Setup.exe."
    Write-Host "Install it from https://jrsoftware.org/isdl.php and re-run this script to"
    Write-Host "also produce a proper installer (Start Menu shortcut + uninstaller), not"
    Write-Host "just the portable .zip."
}

Write-Host ""
Write-Host "NOTE: neither the .exe nor the installer are code-signed. Windows"
Write-Host "SmartScreen will show 'Windows protected your PC' on first run --"
Write-Host "users must click 'More info' -> 'Run anyway'. Removing that warning"
Write-Host "requires an Authenticode code-signing certificate."
