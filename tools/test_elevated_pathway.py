# -*- coding: utf-8 -*-
"""验证 elevated 模块与子进程入口(不真正注册, 只验通路)。"""
import os
import sys

sys.path.insert(0, ".")
from com_bridge import elevated  # noqa: E402

print("=== 1. 基础函数 ===")
print("task_exists:", elevated.task_exists())
print("is_elevated:", elevated.is_elevated())
exe, args = elevated._exe_path()
print("exe:", exe)
print("args:", args)
print("ps_quote:", elevated._ps_quote("it's a 'test'"))
assert elevated._ps_quote("a'b") == "'a''b'", "ps 引号转义错误"

print()
print("=== 2. 子进程入口(--elevated-child + tag) ===")
# 直接调用 _run_elevated_child 模拟提权子进程(注册会因无管理员失败,
# 但结果文件必须写出来 —— 这是父进程判定的唯一通道)
rc = elevated._run_elevated_child("install", "selftest123")
print("rc =", rc, "(非0=注册失败, 当前会话无管理员, 预期)")
p = elevated._result_path("selftest123")
print("结果文件存在:", os.path.exists(p), "->", p)
if os.path.exists(p):
    content = open(p, encoding="utf-8").read().strip()
    print("内容:", content)
    os.unlink(p)
    assert content.startswith("ok rc=") or content.startswith("error"), \
        "结果文件格式不对"
    print("PASS: 结果通道完整(父进程能拿到判定)")
else:
    # rc 非 0 且没写文件 = 通路断了, 这才是 bug
    print("FAIL: 子进程没有写结果文件 —— 父进程会永远超时")
    sys.exit(1)

print()
print("=== 3. gui.main 的子进程分发(argv 解析) ===")
sys.argv = ["com-bridge-gui", "--elevated-child", "install",
            "--elevated-tag", "selftest456"]
from com_bridge import gui  # noqa: E402
rc2 = gui.main()
p2 = elevated._result_path("selftest456")
print("rc =", rc2)
print("结果文件存在:", os.path.exists(p2))
if os.path.exists(p2):
    print("内容:", open(p2, encoding="utf-8").read().strip())
    os.unlink(p2)
    print("PASS: gui.main --elevated-child 分发正确(tag 经命令行传递)")
else:
    print("FAIL: 分发/解析有问题")
    sys.exit(1)

print()
print("全部通过")
