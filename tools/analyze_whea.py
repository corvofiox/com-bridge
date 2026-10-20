# -*- coding: utf-8 -*-
"""解析 WHEA-Logger 事件里的 CPER 记录(只读诊断, 不改系统状态)。

CPER = Common Platform Error Record。头部关键字段:
  0x00 "CPER" | 0x04 版本(2) | 0x06 SignatureEnd(4) | 0x0A 段数(2)
  0x0C 严重度(4): 0=Recoverable 1=Fatal 2=Corrected 3=Informational
  0x14 记录长度(4) | 0x18 时间戳(8, FILETIME)
  0x70 起是段描述符(每个 72 字节), 段类型是 16 字节 GUID。

★ 为什么要看它: BugcheckCode=0 意味着**没有蓝屏**, 那么"机器到底是怎么
  死的"只能靠 CPER。严重度 + 段类型能把故障定位到处理器 / 内存 / PCIe。
"""
from __future__ import annotations

import subprocess
import sys
import uuid
from datetime import datetime, timezone

SEVERITY = {0: "Recoverable(可恢复)", 1: "Fatal(致命)",
            2: "Corrected(已纠正)", 3: "Informational(信息)"}

# 常见段类型 GUID(小端存储, 用 uuid.UUID(bytes_le=...) 还原)
KNOWN = {
    "81212a96-09ed-4996-9471-8d729c8e69ed": "Generic(处理器/通用)",
    "a5bc1114-6f64-4ede-b863-3e83ed7c83b1": "Memory Error(内存)",
    "d995e954-bbc1-430f-ad91-b44dcb3c6f35": "PCIe Error(PCI Express)",
    "c5753963-3b84-4095-bf78-eddad3f9c9dd": "Firmware Error Record Ref",
    "2dce8bb1-bdd7-450e-b9ad-9cf4ebd4f890": "IA32/x64 Processor Error",
    "5bad89ff-b7e6-42c9-814a-cf2485d6e98a": "Platform Memory",
    "85a1cde8-b1a2-4b3d-8c6e-1a2b3c4d5e6f": "BOOT(BERT)",
}


def parse(raw: bytes) -> dict:
    out: dict = {}
    if raw[:4] != b"CPER":
        out["error"] = "不是 CPER 记录(前 4 字节 %r)" % raw[:4]
        return out
    out["revision"] = int.from_bytes(raw[4:6], "little")
    out["sections"] = int.from_bytes(raw[10:12], "little")
    sev = int.from_bytes(raw[12:16], "little")
    out["severity"] = SEVERITY.get(sev, f"未知({sev})")
    out["record_len"] = int.from_bytes(raw[20:24], "little")
    ft = int.from_bytes(raw[24:32], "little")
    try:
        out["timestamp"] = datetime.fromtimestamp(
            ft / 1e7 - 11644473600, tz=timezone.utc).astimezone().isoformat()
    except Exception:  # noqa: BLE001
        out["timestamp"] = f"raw={ft:#x}"
    secs = []
    for i in range(min(out["sections"], 16)):
        d = 128 + i * 72
        if d + 72 > len(raw):
            break
        off = int.from_bytes(raw[d:d + 4], "little")
        ln = int.from_bytes(raw[d + 4:d + 8], "little")
        g = str(uuid.UUID(bytes_le=raw[d + 16:d + 32]))
        secs.append({"offset": off, "len": ln,
                     "type": KNOWN.get(g, g)})
    out["section_desc"] = secs
    return out


def main() -> int:
    ps = r"""
$ErrorActionPreference='Stop'
$since=(Get-Date).AddDays(-60)
Get-WinEvent -FilterHashtable @{LogName='System'; StartTime=$since; ProviderName='Microsoft-Windows-WHEA-Logger'} |
  Sort-Object TimeCreated | ForEach-Object {
    $x=[xml]$_.ToXml()
    $raw=($x.Event.EventData.Data | Where-Object {$_.Name -eq 'RawData'}).'#text'
    "{0}|{1}" -f $_.TimeCreated.ToString('s'), $raw
  }
"""
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        print("查询事件日志失败:", r.stderr[:500])
        return 1
    lines = [ln for ln in r.stdout.splitlines() if "|" in ln]
    print(f"共 {len(lines)} 条 WHEA 记录\n")
    sev_count: dict[str, int] = {}
    type_count: dict[str, int] = {}
    for ln in lines:
        ts, _, hexdata = ln.partition("|")
        hexdata = "".join(hexdata.split())
        try:
            raw = bytes.fromhex(hexdata)
        except ValueError:
            print(f"{ts}  解析失败(非十六进制)")
            continue
        info = parse(raw)
        sev_count[info.get("severity", "?")] = \
            sev_count.get(info.get("severity", "?"), 0) + 1
        for s in info.get("section_desc", []):
            type_count[s["type"]] = type_count.get(s["type"], 0) + 1
        print(f"{ts}  严重度={info.get('severity')}  "
              f"记录长={info.get('record_len')}  段数={info.get('sections')}")
        for s in info.get("section_desc", []):
            print(f"           - {s['type']} (len={s['len']})")
    print("\n==== 汇总 ====")
    for k, v in sev_count.items():
        print(f"  严重度 {k}: {v} 条")
    for k, v in type_count.items():
        print(f"  段类型 {k}: {v} 段")
    return 0


if __name__ == "__main__":
    sys.exit(main())
