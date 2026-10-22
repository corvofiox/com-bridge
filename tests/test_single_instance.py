# -*- coding: utf-8 -*-
"""★★★ 回归: 单实例互斥(用户实测"能同时启动两个甚至多个")。

## 多实例的真实危害(不只是多个窗口)
* 都去 bind 24802/TCP + 4010/UDP -> 后启动的绑定失败, 界面报"启动失败";
* 都装低级键鼠钩子 -> 事件被吞两次/转发两次, 跨屏输入重影;
* 托盘里出现多个同名图标;
* 都写同一份 config.json -> 状态互相覆盖。

## 实现
命名互斥量(内核对象, 跨进程共享) + 第二个实例唤起已有窗口后退出。

## 为什么不用锁文件
锁文件在进程被强杀后会残留 -> 下次启动被误判"已在运行", 用户必须
手动删文件。命名互斥量由内核管理, 进程无论怎么死都会自动释放
(实测验证: 强杀持有者后, 新进程可立即获取)。
"""
import inspect
import os
import subprocess
import sys
import uuid

# ★ 审查项 #9: 用**独立互斥量名**跑这三条用例。
#   原来用生产名, 于是"程序正在运行"时互斥量已被真实进程占着 ->
#   test_acquire_is_idempotent / test_cross_process_is_blocked /
#   test_released_after_release 必然失败。那不是回归, 但 CI 永远红,
#   也容易让人以为单实例逻辑坏了。
#   必须在 import single_instance **之前**设: MUTEX_NAME 在模块导入时求值。
os.environ["COM_BRIDGE_MUTEX_NAME"] = (
    "Local\\ComBridge-Test-{%s}" % uuid.uuid4())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import com_bridge.gui as gui_mod  # noqa: E402
import com_bridge.single_instance as si  # noqa: E402


def test_default_name_used_when_no_override():
    """不设环境变量时必须回到生产名(否则两个用户各跑一个的语义会变)。"""
    code = ("import sys; sys.path.insert(0,'.');"
            "from com_bridge import single_instance as s;"
            "print(s.MUTEX_NAME)")
    env = dict(os.environ)
    env.pop("COM_BRIDGE_MUTEX_NAME", None)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, timeout=60, env=env,
                       cwd=os.path.dirname(
                           os.path.dirname(os.path.abspath(__file__))))
    assert "SingleInstance-{6F1A2B3C" in r.stdout, r.stdout


def test_test_run_uses_isolated_name():
    """本测试进程必须用独立名, 否则会和正在运行的程序抢同一把锁。"""
    assert "ComBridge-Test-" in si.MUTEX_NAME


def test_mutex_name_is_session_local():
    """互斥量名必须是 Local\\ 前缀(不同用户各开一个实例是合理的)。"""
    assert si.MUTEX_NAME.startswith("Local\\"), \
        "应用 Local\\ 前缀: 不同登录用户应能各跑一个实例"
    assert "ComBridge" in si.MUTEX_NAME


def test_acquire_is_idempotent():
    """同进程重复 acquire 必须稳定返回 True(不能把自己拦住)。"""
    si.release()
    assert si.acquire() is True
    assert si.acquire() is True, "同进程第二次 acquire 不该失败"
    si.release()


def test_cross_process_is_blocked():
    """★ 父进程持有时, 子进程 acquire 必须返回 False。"""
    si.release()
    assert si.acquire() is True
    try:
        code = ("import sys; sys.path.insert(0,'.');"
                "from com_bridge import single_instance as s;"
                "print('ACQUIRED' if s.acquire() else 'BLOCKED')")
        r = subprocess.run([sys.executable, "-c", code],
                           capture_output=True, text=True, timeout=60,
                           cwd=os.path.dirname(
                               os.path.dirname(os.path.abspath(__file__))))
        assert "BLOCKED" in r.stdout, f"子进程应被拦住, 实际: {r.stdout}"
    finally:
        si.release()


def test_released_after_release():
    """释放后新进程应能获取。"""
    si.release()
    code = ("import sys; sys.path.insert(0,'.');"
            "from com_bridge import single_instance as s;"
            "print('ACQUIRED' if s.acquire() else 'BLOCKED')")
    r = subprocess.run([sys.executable, "-c", code],
                       capture_output=True, text=True, timeout=60,
                       cwd=os.path.dirname(
                           os.path.dirname(os.path.abspath(__file__))))
    assert "ACQUIRED" in r.stdout, f"释放后应可获取, 实际: {r.stdout}"


def test_gui_main_checks_single_instance():
    """★★ GUI 入口必须做互斥检查, 并在失败时唤起已有窗口后退出。"""
    src = inspect.getsource(gui_mod.main)
    assert "single_instance" in src, "必须引入互斥模块"
    assert "acquire()" in src, "必须调用 acquire"
    assert "notify_existing" in src, "第二实例应唤起已有窗口(符合双击预期)"
    # 必须在创建 GUI **之前**检查(否则会先闪一个窗口再退出)
    i_check = src.index("acquire()")
    # 构造调用现在带参数(ComBridgeGui(start_hidden=...)) -> 只匹到左括号
    i_gui = src.index("ComBridgeGui(")
    assert i_check < i_gui, "互斥检查必须在创建窗口之前"


def test_window_title_is_findable():
    """主窗口标题必须以固定标记开头, 否则第二个实例找不到它。"""
    src = inspect.getsource(gui_mod.ComBridgeGui._build)
    assert f'"{si.WINDOW_TITLE_MARK}' in src or \
        f"'{si.WINDOW_TITLE_MARK}" in src, \
        f"窗口标题必须以 {si.WINDOW_TITLE_MARK!r} 开头(供单实例查找)"


def test_activate_uses_attach_thread_input():
    """★ 唤起窗口必须用 AttachThreadInput 强制。

    第二个实例不是前台进程, Windows 会拒绝裸 SetForegroundWindow
    (实测返回 True 但没生效), 用户看到"双击了没反应"。
    """
    src = inspect.getsource(si.activate_window)
    assert "AttachThreadInput" in src
    assert "ShowWindow" in src, "必须先 ShowWindow 恢复(可能藏在托盘)"
    i_show = src.index("ShowWindow")
    i_fg = src.index("SetForegroundWindow(hwnd)")
    assert i_show < i_fg, "必须先恢复窗口再激活"


def test_find_window_excludes_self():
    """查找已有窗口时必须排除本进程(否则会把自己当成'已有实例')。"""
    src = inspect.getsource(si.find_existing_window)
    assert "GetCurrentProcessId" in src
