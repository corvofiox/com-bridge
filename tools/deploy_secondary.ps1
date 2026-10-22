param(
    # 打开管线延迟诊断(默认关闭); 排查"某块屏鼠标迟滞"时用
    [switch]$DiagLat
)
$ErrorActionPreference = 'Continue'
# 用法(在目标机上执行):
#   1) scp dist\com-bridge-gui.exe   gaoxi@<host>:C:/Windows/Temp/cb-new.exe
#   2) scp tools\deploy_secondary.ps1 gaoxi@<host>:C:/Windows/Temp/
#   3) ssh ... "powershell -NoProfile -ExecutionPolicy Bypass -File C:/Windows/Temp/deploy_secondary.ps1"

$exe = 'D:\Software\Com Bridge\com-bridge-gui.exe'
$src = 'C:\Windows\Temp\cb-new.exe'

# 1. 停掉旧实例(PyInstaller onefile 有 parent+child 两个进程)
Get-Process com-bridge-gui -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 1500

# 2. 替换
Copy-Item $src $exe -Force
'dst hash: ' + (Get-FileHash $exe -Algorithm MD5).Hash

# 3. 诊断开关
# ★ 不要依赖 $env:LOCALAPPDATA: 经 sshd 拉起 powershell 时该变量可能为空,
#   于是 $d/$flag 全是 null, 后面每条命令都报 "Path 不能为空"(静默半途而废)。
$base = [Environment]::GetFolderPath('LocalApplicationData')
if (-not $base) { $base = $env:LOCALAPPDATA }
if (-not $base) { $base = Join-Path $env:USERPROFILE 'AppData\Local' }
"localappdata: $base"
$d = Join-Path $base 'ComBridge'
New-Item -ItemType Directory -Force -Path $d | Out-Null
$flag = Join-Path $d 'diag_lat.flag'
if ($DiagLat) {
    Set-Content -Path $flag -Value 'diagnose' -Encoding ASCII
} else {
    Remove-Item $flag -Force -ErrorAction SilentlyContinue
}
'diag_lat: ' + (Test-Path $flag)

# 4. 由**交互会话**拉起(计划任务带 Highest, 且只有会话 1 才有桌面/输入)
$t = Get-ScheduledTask -ErrorAction SilentlyContinue |
    Where-Object { $_.TaskName -like '*ComBridge*' -or $_.TaskName -like '*Com Bridge*' }
foreach ($x in $t) { "task: $($x.TaskName) state=$($x.State)" }
$auto = $t | Where-Object { $_.TaskName -like '*Elevated*' } | Select-Object -First 1
if (-not $auto) { $auto = $t | Select-Object -First 1 }
if ($auto) {
    Start-ScheduledTask -TaskName $auto.TaskName
    Start-Sleep -Seconds 6
    'started: ' + $auto.TaskName
}
Get-Process com-bridge-gui -ErrorAction SilentlyContinue |
    Select-Object Id, SessionId, @{n='MB';e={[int]($_.WorkingSet64/1MB)}} |
    Format-Table -AutoSize
'udp24801=' + (Get-NetUDPEndpoint -LocalPort 24801 -ErrorAction SilentlyContinue | Measure-Object).Count
'tcp24803=' + (Get-NetTCPConnection -LocalPort 24803 -State Listen -ErrorAction SilentlyContinue | Measure-Object).Count
$f = Join-Path $d 'logs\com-bridge-gui.log'
if (Test-Path $f) { Get-Content $f -Tail 6 }
