# Builds dist\CallCaptions-Setup-<version>.exe. Needs Python 3.12 and Inno Setup 6
# (winget install JRSoftware.InnoSetup). Run from anywhere:
#   powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1 -Version 1.0.0
param(
    [string]$Version = "1.0.0",
    [string]$Python = ""
)
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $root

if (-not $Python) {
    $Python = if (Get-Command py -ErrorAction SilentlyContinue) { "py -3.12" } else { "python" }
}
Write-Host "== Creating build environment"
Invoke-Expression "$Python -m venv .venv-win"
$py = Join-Path $root ".venv-win\Scripts\python.exe"
& $py -m pip install --upgrade pip --quiet
& $py -m pip install -r requirements-windows.txt "pyinstaller>=6.11" --quiet
if ($LASTEXITCODE) { throw "pip install failed" }

Write-Host "== Bundling the app"
& $py -m PyInstaller packaging\windows\CallCaptions.spec --noconfirm --clean --distpath dist --workpath build
if ($LASTEXITCODE) { throw "PyInstaller failed" }

Write-Host "== Self-test of the bundled app"
$log = Join-Path $env:LOCALAPPDATA "CallCaptions\captions.log"
Remove-Item $log -ErrorAction SilentlyContinue
$test = Start-Process dist\CallCaptions\CallCaptions.exe -ArgumentList "--selftest" -Wait -PassThru
if (Test-Path $log) { Get-Content $log }
if ($test.ExitCode -ne 0) { throw "Self-test failed (exit code $($test.ExitCode))" }

Write-Host "== Building the installer"
$iscc = (Get-Command iscc -ErrorAction SilentlyContinue).Source
if (-not $iscc) {
    $iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
              "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $iscc) { throw "Inno Setup 6 not found (winget install JRSoftware.InnoSetup)" }
& $iscc "/DAppVersion=$Version" packaging\windows\installer.iss
if ($LASTEXITCODE) { throw "Inno Setup failed" }
Write-Host "== Done: dist\CallCaptions-Setup-$Version.exe"
