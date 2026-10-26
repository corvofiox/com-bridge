$ErrorActionPreference = 'Stop'

# 1) 现状
'HKCU Run (ComBridge): ' + (Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -ErrorAction SilentlyContinue).ComBridge
$t = Get-ScheduledTask -TaskName 'ComBridgeElevatedAutostart' -ErrorAction SilentlyContinue
if ($t) { foreach ($a in $t.Actions) { 'BEFORE args: ' + $a.Arguments } } else { 'task: (absent)' }

# 2) 用正确的参数重注册(带 --autostart = 静默启动)
$exe = 'D:\Software\Com Bridge\com-bridge-gui.exe'
# WORKGROUP 机器上 USERDOMAIN 是 WORKGROUP, 拼主体会 0x80070534;
# 用当前身份的真实账户名(本地账户 -> COMPUTERNAME\user)
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$a = New-ScheduledTaskAction -Execute $exe -Argument 'gui --autostart'
$tr = New-ScheduledTaskTrigger -AtLogOn -User $user
$pr = New-ScheduledTaskPrincipal -UserId $user -RunLevel Highest -LogonType Interactive
$st = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
Register-ScheduledTask -TaskName 'ComBridgeElevatedAutostart' -Action $a -Trigger $tr -Principal $pr -Settings $st -Force | Out-Null

$t2 = Get-ScheduledTask -TaskName 'ComBridgeElevatedAutostart'
foreach ($x in $t2.Actions) { 'AFTER  args: ' + $x.Arguments }
$xml = schtasks /Query /TN ComBridgeElevatedAutostart /XML ONE
'XML has silent flag: ' + ($xml -match '--autostart')
