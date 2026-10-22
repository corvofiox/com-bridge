$ErrorActionPreference = 'Continue'
# 1. 停掉旧实例(两种版本都可能残留 parent+child)
Get-Process com-bridge-gui -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 1500

# 2. 用新构建替换
Copy-Item 'C:\Windows\Temp\cb-new.exe' 'D:\Software\Com Bridge\com-bridge-gui.exe' -Force
'dst hash: ' + (Get-FileHash 'D:\Software\Com Bridge\com-bridge-gui.exe' -Algorithm MD5).Hash

# 3. 打开管线延迟诊断(本次专门为定位"2560 屏迟滞")
$d = Join-Path $env:LOCALAPPDATA 'ComBridge'
New-Item -ItemType Directory -Force -Path $d | Out-Null
Set-Content -Path (Join-Path $d 'diag_lat.flag') -Value 'diagnose' -Encoding ASCII
'flag: ' + (Test-Path (Join-Path $d 'diag_lat.flag'))

# 4. 列出计划任务并启动自启项(必须由当前交互会话拉起, 否则 GUI/输入不可见)
$t = Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object { $_.TaskName -like '*ComBridge*' -or $_.TaskName -like '*Com Bridge*' }
foreach ($x in $t) { "task: $($x.TaskName) state=$($x.State)" }
$auto = $t | Where-Object { $_.TaskName -like '*Elevated*' } | Select-Object -First 1
if (-not $auto) { $auto = $t | Select-Object -First 1 }
if ($auto) {
    Start-ScheduledTask -TaskName $auto.TaskName
    Start-Sleep -Seconds 4
    'started: ' + $auto.TaskName
}
Get-Process com-bridge-gui -ErrorAction SilentlyContinue |
    Select-Object Id, SessionId, @{n='MB';e={[int]($_.WorkingSet64/1MB)}} | Format-Table -AutoSize
'--- listening 24801/24803 ---'
(Get-NetUDPEndpoint -LocalPort 24801 -ErrorAction SilentlyContinue | Measure-Object).Count
(Get-NetTCPConnection -LocalPort 24803 -State Listen -ErrorAction SilentlyContinue | Measure-Object).Count
