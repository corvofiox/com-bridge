# Build the single-file GUI executable with PyInstaller.
# Usage:  powershell -ExecutionPolicy Bypass -File build_exe.ps1
# Output: dist\com-bridge-gui.exe  (no Python needed on target machines)
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 parses .ps1 files
# as ANSI unless they carry a UTF-8 BOM; the edit tool writes UTF-8 without
# BOM, and Chinese comments then break the parser (stray ')' errors).

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

# PyInstaller has no official pycaw hook; pycaw's api.* subpackages are
# imported lazily inside functions, so list them all explicitly.
$hidden = @(
    "pycaw", "pycaw.utils", "pycaw.pycaw", "pycaw.constants", "pycaw.magic",
    "pycaw.callbacks",
    "pycaw.api.audioclient", "pycaw.api.audioclient.depend",
    "pycaw.api.mmdeviceapi", "pycaw.api.mmdeviceapi.depend",
    "pycaw.api.mmdeviceapi.depend.structures",
    "pycaw.api.audiopolicy", "pycaw.api.endpointvolume",
    "pycaw.api.endpointvolume.depend", "pycaw.api.policyconfig",
    "comtypes", "comtypes.automation", "comtypes.client"
)

$hiddenArgs = foreach ($h in $hidden) { "--hidden-import=$h" }

# tkinter must stay: the GUI is built on it.
$excludes = @(
    "--exclude-module=matplotlib", "--exclude-module=scipy",
    "--exclude-module=pandas", "--exclude-module=IPython",
    "--exclude-module=pytest"
)

& pyinstaller --noconfirm --clean --onefile --noconsole --name com-bridge-gui @hiddenArgs @excludes launch_gui.py

if ($LASTEXITCODE -ne 0) { throw "PyInstaller GUI build failed" }

# Only the GUI exe ships. Older builds also produced a console CLI
# (com-bridge.exe) plus .bat launchers; those are no longer built, so remove
# any leftovers from previous runs to keep dist/ unambiguous.
$stale = @("com-bridge.exe", "server.bat", "agent.bat",
           "selfcheck.bat", "gui.bat")
foreach ($name in $stale) {
    $p = Join-Path "$PSScriptRoot\dist" $name
    if (Test-Path -LiteralPath $p) {
        Remove-Item -LiteralPath $p -Force
        Write-Host "removed stale: dist\$name"
    }
}

Write-Host ""
Write-Host "done: $PSScriptRoot\dist\com-bridge-gui.exe"
Get-ChildItem "$PSScriptRoot\dist" -Filter *.exe | ForEach-Object {
    Write-Host ("{0}: {1:N1} MB" -f $_.Name, ($_.Length / 1MB))
}
