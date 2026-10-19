# 侦查两个 com-bridge-gui 实例的窗口与线程
Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class WD {
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
  [DllImport("user32.dll")] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint p);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  public delegate bool EnumProc(IntPtr h, IntPtr l);
}
"@
$targets = @(41428, 52920)
foreach ($tp in $targets) {
  $p = Get-Process -Id $tp -ErrorAction SilentlyContinue
  if ($p) {
    $cnt = $p.Threads.Count
    $hc = $p.HandleCount
    $st = $p.StartTime.ToString('HH:mm:ss')
    Write-Output ("PID {0}: threads={1} handles={2} start={3}" -f $tp, $cnt, $hc, $st)
  } else {
    Write-Output ("PID {0}: (已退出)" -f $tp)
  }
}
Write-Output ""
$wins = @{}
$cb = [WD+EnumProc]{ param($h,$l)
  $procId = [uint32]0
  [void][WD]::GetWindowThreadProcessId($h, [ref]$procId)
  if ($procId -in $targets) {
    $sb = New-Object System.Text.StringBuilder 128
    [void][WD]::GetClassName($h,$sb,128)
    $key = [string]$procId
    if (-not $wins.ContainsKey($key)) { $wins[$key] = New-Object System.Collections.ArrayList }
    [void]$wins[$key].Add(("{0} vis={1}" -f $sb.ToString(), [WD]::IsWindowVisible($h)))
  }
  return $true
}
[void][WD]::EnumWindows($cb,[IntPtr]::Zero)
foreach ($k in $wins.Keys) {
  Write-Output ("PID {0} 的窗口类:" -f $k)
  $wins[$k] | Sort-Object -Unique | ForEach-Object { Write-Output ("   " + $_) }
}
