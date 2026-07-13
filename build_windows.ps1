# Build the standalone Windows executable for FLIndexRenders.
# Requires: Python 3.9+ and PyInstaller (pip install pyinstaller pillow).
# Output: dist\FLIndexRenders.exe  (portable, no install needed)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

# Regenerate icon assets (safe to re-run; needs Pillow).
python assets\make_icon.py

python -m PyInstaller `
    --onefile --windowed --noconfirm --clean `
    --name FLIndexRenders `
    --icon assets\icon.ico `
    --version-file version_info.txt `
    --add-data "assets\icon.png;assets" `
    --add-data "assets\icon.ico;assets" `
    app.py

Write-Host ""
if (Test-Path dist\FLIndexRenders.exe) {
    $f = Get-Item dist\FLIndexRenders.exe
    Write-Host ("Built dist\FLIndexRenders.exe  ({0:N1} MB)" -f ($f.Length/1MB))
} else {
    Write-Error "Build failed: dist\FLIndexRenders.exe not found"
}
