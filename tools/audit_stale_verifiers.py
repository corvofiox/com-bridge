# -*- coding: utf-8 -*-
"""批量检查 tools/verify_*.py 是否仍然可运行(语法 + 关键导入)。

用于识别"已失效的验收脚本"(例如引用了已删除的方法/属性)。
只做**静态检查**(导入 + 属性存在性), 不真正运行(运行会抢键鼠)。
"""
from __future__ import annotations

import ast
import importlib
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

bad: list[tuple[str, str]] = []
ok_count = 0
for f in sorted((ROOT / "tools").glob("verify_*.py")):
    try:
        tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError as exc:
        bad.append((f.name, f"语法错误: {exc}"))
        continue
    # 检查 from com_bridge.X import Y 里的 Y 是否存在
    problem = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module \
                and node.module.startswith("com_bridge"):
            try:
                mod = importlib.import_module(node.module)
            except Exception as exc:  # noqa: BLE001
                problem = f"{node.module} 导入失败: {exc}"
                break
            for alias in node.names:
                if not hasattr(mod, alias.name):
                    problem = (f"{node.module} 无 {alias.name}"
                               f" (第 {node.lineno} 行)")
                    break
        if problem:
            break
    if problem:
        bad.append((f.name, problem))
    else:
        ok_count += 1

print(f"verify_* 脚本: {ok_count} 个正常, {len(bad)} 个失效\n")
for name, why in bad:
    print(f"  {name:38} {why}")
