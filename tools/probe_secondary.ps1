# 探查副机现状(只读)。用法: powershell -File tools\probe_secondary.ps1
# 通过 ssh -EncodedCommand 执行, 避免多层引号被吃掉。
$ErrorActionPreference = 'Continue'

Write-Host "=== 机器 ==="
Write-Host ("host={0} user={1}" -f $env:COMPUTERNAME, $env:USERNAME)

Write-Host "`n=== 进程 ==="
Get-Process com-bridge-gui -ErrorAction SilentlyContinue |
    Select-Object Id, SessionId, StartTime, Path |
    Format-Table -AutoSize

Write-Host "=== 自启项 ==="
$run = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
try { (Get-ItemProperty $run -ErrorAction Stop).ComBridge } catch { '<未设置>' }

Write-Host "`n=== 候选安装目录 ==="
$dirs = @('D:\Software\Com Bridge',
          'D:\我的文档\工作区\Vibe Coding\Com Bridge\dist')
foreach ($d in $dirs) {
    if (Test-Path -LiteralPath $d) {
        Write-Host "[$d]"
        Get-ChildItem -LiteralPath $d -Filter *.exe -ErrorAction SilentlyContinue |
            Select-Object Name, Length, LastWriteTime |
            Format-Table -AutoSize
    } else {
        Write-Host "[$d] 不存在"
    }
}

Write-Host "`n=== 源码副本是否存在(判断工作区是否同步) ==="
$src = 'D:\我的文档\工作区\Vibe Coding\Com Bridge\com_bridge\agent.py'
if (Test-Path -LiteralPath $src) {
    $f = Get-Item -LiteralPath $src
    Write-Host ("agent.py 存在, 修改时间={0}, 大小={1}" -f $f.LastWriteTime, $f.Length)
} else { Write-Host 'agent.py 不存在' }

Write-Host "`n=== 本机 config ==="
$cfg = Join-Path $env:LOCALAPPDATA 'ComBridge\config.json'
if (Test-Path -LiteralPath $cfg) { Get-Content -LiteralPath $cfg -Raw } else { '<无>' }

Write-Host "=== 最近日志尾部 ==="
$log = Join-Path $env:LOCALAPPDATA 'ComBridge\logs\com-bridge-gui.log'
if (Test-Path -LiteralPath $log) {
    Get-Item -LiteralPath $log | Select-Object Length, LastWriteTime | Format-Table -AutoSize
    Get-Content -LiteralPath $log -Tail 12
} else { '<无日志>' }
