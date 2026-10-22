$ErrorActionPreference = 'Continue'
# ASCII only on purpose: PowerShell 5.1 reads BOM-less .ps1 as ANSI and
# multi-byte CJK inside quotes breaks parsing.
$base = [Environment]::GetFolderPath('LocalApplicationData')
'localappdata: ' + $base
'exe hash: ' + (Get-FileHash 'D:\Software\Com Bridge\com-bridge-gui.exe' -Algorithm MD5).Hash
$p = Get-Process com-bridge-gui -ErrorAction SilentlyContinue
'procs: ' + $p.Count + '  session: ' + (($p | ForEach-Object { $_.SessionId }) -join ',')
'udp24801: ' + (Get-NetUDPEndpoint -LocalPort 24801 -ErrorAction SilentlyContinue).Count
'tcp24803: ' + (Get-NetTCPConnection -LocalPort 24803 -State Listen -ErrorAction SilentlyContinue).Count
$log = Join-Path $base 'ComBridge\logs\com-bridge-gui.log'
'--- tail ---'
Get-Content $log -Tail 14
'--- alarms ---'
Get-Content $log -Tail 500 | Select-String -Pattern 'Traceback|UnboundLocal|new version' | Select-Object -Last 5
