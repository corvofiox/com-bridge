"""回归: 函数内"局部 import 遮住模块级名字"导致的 UnboundLocalError。

2026-10-23 实测踩到: `Server._start_input` 原本在函数**中段**有一句
`from .input import dpi_awareness, virtual_desktop`。Python 把这种赋值当作
**整个函数**的局部绑定, 于是函数**前面**用到 `virtual_desktop` 的那一行抛
`UnboundLocalError: cannot access local variable 'virtual_desktop'`。

后果很隐蔽: 主机 `Server.start()` 在渲染线程里直接崩掉, 日志里只有一行
traceback, 而副机侧表现为"一直等主电脑就绪" —— 看起来像网络问题。

单元测试很难覆盖到 `_start_input`(要真显示器/真 socket), 所以这里用 AST
做静态检查: 同一函数内, 任何**局部 import 绑定的名字**不允许出现在该 import
语句**之前**的引用里。
"""
import ast
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

MODULES = sorted(
    os.path.join(ROOT, "com_bridge", f)
    for f in os.listdir(os.path.join(ROOT, "com_bridge"))
    if f.endswith(".py")
)


def _direct_nodes(func: ast.AST):
    """遍历函数体, 但**不进入**嵌套函数/lambda(它们是独立作用域)。"""
    stack = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.Lambda, ast.ClassDef)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _bound_by_import(node: ast.AST) -> set[str]:
    if isinstance(node, ast.ImportFrom):
        return {(a.asname or a.name).split(".")[0] for a in node.names}
    if isinstance(node, ast.Import):
        return {(a.asname or a.name).split(".")[0] for a in node.names}
    return set()


def _shadow_violations(tree: ast.AST) -> list[str]:
    out = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        nodes = list(_direct_nodes(func))
        imports = [n for n in nodes if isinstance(n, (ast.Import, ast.ImportFrom))]
        if not imports:
            continue
        lines = [n.lineno for n in imports]
        first_import = min(lines)
        # 该函数内局部 import 绑定的全部名字
        local_names: dict[str, int] = {}
        for node in imports:
            for name in _bound_by_import(node):
                local_names.setdefault(name, node.lineno)
        if not local_names:
            continue
        # 只看**最后一条** import 之前对该名字的"读"引用
        last_import = max(lines)
        for node in nodes:
            if not isinstance(node, ast.Name) or not isinstance(node.ctx, ast.Load):
                continue
            if node.lineno >= first_import:
                continue
            if node.id in local_names:
                out.append(
                    f"{os.path.basename('?')}:{node.lineno} 函数 {func.name} "
                    f"在局部 import(第 {local_names[node.id]} 行)之前"
                    f"引用了 {node.id!r} -> UnboundLocalError")
    return out


@pytest.mark.parametrize("path", MODULES, ids=[os.path.basename(p) for p in MODULES])
def test_no_local_import_shadowing(path):
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    bad = []
    for line in _shadow_violations(tree):
        bad.append(f"{os.path.basename(path)}:{line}")
    assert not bad, "局部 import 遮住了同名引用:\n" + "\n".join(bad)


def test_detector_catches_known_pattern():
    """自检: 检查器必须能抓到真实踩过的写法(否则它会静默失效)。"""
    src = """
def f():
    x = virtual_desktop()
    from .input import dpi_awareness, virtual_desktop
    return x
"""
    bad = _shadow_violations(ast.parse(src))
    assert bad and "virtual_desktop" in bad[0]


def test_detector_allows_import_before_use():
    src = """
def f():
    from .input import virtual_desktop
    return virtual_desktop()
"""
    assert _shadow_violations(ast.parse(src)) == []


def test_detector_ignores_nested_function_scope():
    src = """
def f():
    from .input import virtual_desktop
    def g():
        return virtual_desktop()
    return g
"""
    assert _shadow_violations(ast.parse(src)) == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
