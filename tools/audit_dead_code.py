# -*- coding: utf-8 -*-
"""审查: 找出定义但从未被调用的方法/函数(冗余候选)。

只统计**跨文件引用**: 一个名字在定义处出现 1 次, 若全仓总出现次数
也约等于定义次数, 说明没有调用点。
"""
from __future__ import annotations

import ast
import collections
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PKG = ROOT / "com_bridge"

defined: dict[str, list[tuple[str, int]]] = {}
for f in PKG.glob("*.py"):
    try:
        tree = ast.parse(f.read_text(encoding="utf-8"))
    except SyntaxError:
        continue
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("__"):
                continue
            defined.setdefault(node.name, []).append((f.name, node.lineno))

scan = list(PKG.glob("*.py"))
for sub in ("tests", "tools"):
    d = ROOT / sub
    if d.exists():
        scan += list(d.glob("*.py"))
for extra in ("launch.py", "launch_gui.py", "build_exe.ps1"):
    p = ROOT / extra
    if p.exists():
        scan.append(p)

refs: collections.Counter[str] = collections.Counter()
for f in scan:
    try:
        src = f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        continue
    for name in defined:
        refs[name] += src.count(name)

print(f"扫描 {len(scan)} 个文件, 定义 {len(defined)} 个方法/函数\n")
print("=== 定义处出现次数 >= 全仓引用次数(疑似无调用点) ===")
suspicious = []
for name, locs in sorted(defined.items()):
    if refs[name] <= len(locs):
        suspicious.append((name, locs, refs[name]))
if not suspicious:
    print("  (无)")
for name, locs, c in suspicious:
    print(f"  {name:40} 定义={locs} 全仓出现={c}")

print()
print("=== 只有 1 个定义处 + 1 次调用(可能只用一次, 非冗余) ===")
for name, locs in sorted(defined.items()):
    if refs[name] == len(locs) + 1:
        print(f"  {name:40} 定义={locs} 出现={refs[name]}")
