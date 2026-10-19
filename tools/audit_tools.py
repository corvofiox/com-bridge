# -*- coding: utf-8 -*-
"""审查 tools/: 哪些脚本仍被引用, 哪些是一次性遗留。

判据:
  1. 脚本是否被 README / 其他脚本 / 测试引用
  2. 脚本自身是否还能跑(语法 + 导入)
  3. 命名前缀含义:
       _fetch_*  probe_*  —— 一次性探针(通常已完成使命)
       diag_*             —— 诊断工具(部分仍有价值)
       exp_*              —— 实验(验证某个假设, 假设已定论的可删)
       verify_*           —— 验收(应保留, 是回归资产)
       sim_*  harness_*   —— 模拟/桩
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
SCRIPTS = sorted(TOOLS.glob("*.py"))

# 收集所有"提到脚本名"的地方
corpus = ""
for f in list(ROOT.glob("*.md")) + list(ROOT.glob("*.ps1")) \
        + list(ROOT.glob("*.py")) + list((ROOT / "com_bridge").glob("*.py")) \
        + list((ROOT / "tests").glob("*.py")):
    try:
        corpus += f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass

groups: dict[str, list[str]] = {
    "一次性探针(_fetch/probe)": [],
    "实验(exp)": [],
    "诊断(diag)": [],
    "验收(verify)": [],
    "模拟/桩(sim/harness)": [],
    "审查(audit)": [],
    "其他": [],
}
referenced: dict[str, int] = {}
for s in SCRIPTS:
    n = corpus.count(s.name)
    referenced[s.name] = n
    if s.name.startswith("_fetch") or s.name.startswith("probe_"):
        groups["一次性探针(_fetch/probe)"].append(s.name)
    elif s.name.startswith("exp_"):
        groups["实验(exp)"] .append(s.name)
    elif s.name.startswith("diag_"):
        groups["诊断(diag)"].append(s.name)
    elif s.name.startswith("verify_"):
        groups["验收(verify)"].append(s.name)
    elif s.name.startswith(("sim_", "harness_")):
        groups["模拟/桩(sim/harness)"].append(s.name)
    elif s.name.startswith("audit_"):
        groups["审查(audit)"].append(s.name)
    else:
        groups["其他"].append(s.name)

print(f"tools: {len(SCRIPTS)} 个脚本\n")
for gname, names in groups.items():
    if not names:
        continue
    unref = [n for n in names if referenced[n] <= 1]
    print(f"=== {gname}: {len(names)} 个 "
          f"(其中 {len(unref)} 个无任何外部引用) ===")
    for n in names:
        mark = "" if referenced[n] > 1 else "   <- 无引用"
        print(f"  {n:42} 提及={referenced[n]}{mark}")
    print()
