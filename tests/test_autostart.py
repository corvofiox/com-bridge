# -*- coding: utf-8 -*-
"""★★★ 回归: 静默开机自启(用户明确要求"整个过程必须静默")。

## 需求
"不能加入需要用户确认的过程，整个过程必须静默"

## 方案变更
旧实现: 计划任务 + RunLevel Highest -> **必然弹 UAC**(Windows 安全边界),
        无法满足"静默"要求。
新实现: HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run
        * 属于当前用户, 读写**不需要管理员**, 零弹窗;
        * 登录时自动执行, 是托盘类程序的标准做法;
        * 删除即关闭, 可反复开关。

## 代价(已知并接受)
HKCU Run 启动的是**普通权限**进程。本项目运行时不依赖管理员:
低级键鼠钩子、WASAPI 环回、光标控制、UDP/TCP 通信都不需要提权。
唯一受限场景是"需吞掉其他提权窗口按键"时可能被 UIPI 拦。
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import com_bridge.autostart as auto  # noqa: E402
import com_bridge.gui as gui_mod  # noqa: E402


def test_uses_hkcu_run_key():
    """必须用 HKCU Run(不需要管理员), 不能用 HKLM。"""
    assert "HKEY_CURRENT_USER" in inspect.getsource(auto)
    assert "CurrentVersion\\\\Run" in inspect.getsource(auto) or \
        "CurrentVersion\\Run" in inspect.getsource(auto)
    assert "HKEY_LOCAL_MACHINE" not in inspect.getsource(auto), \
        "HKLM 需要管理员, 违背静默要求"


def test_no_uac_in_autostart_path():
    """★ 自启全流程不得出现提权调用(ShellExecute runas / 计划任务注册)。"""
    src = inspect.getsource(auto)
    for banned in ("runas", "ShellExecute", "Register-ScheduledTask",
                   "request_elevated"):
        # 允许出现在注释里(解释为什么放弃), 只禁止可执行代码
        live = [ln for ln in src.splitlines()
                if banned in ln and not ln.strip().startswith("#")]
        assert not live, f"自启流程不得使用 {banned}: {live}"


def test_enable_disable_roundtrip():
    """启用/关闭往返必须成功, 且状态可正确读回。"""
    before = auto.is_enabled()
    try:
        ok, msg = auto.enable()
        assert ok, f"enable 应成功: {msg}"
        assert auto.is_enabled() is True
        cmd = auto.stored_command()
        assert cmd, "必须登记了命令行"
        assert ".exe" in cmd.lower() or "-m com_bridge" in cmd, \
            f"登记的命令应可执行: {cmd}"

        ok2, msg2 = auto.disable()
        assert ok2, f"disable 应成功: {msg2}"
        assert auto.is_enabled() is False
    finally:
        # 恢复原状, 不给用户机器留状态
        if before:
            auto.enable()
        else:
            auto.disable()


def test_autostart_is_fast():
    """★ 必须是毫秒级(旧方案 schtasks 子进程曾要 1~3 秒)。"""
    import time
    before = auto.is_enabled()
    try:
        t0 = time.monotonic()
        auto.enable()
        dt_enable = (time.monotonic() - t0) * 1000
        t0 = time.monotonic()
        auto.is_enabled()
        dt_query = (time.monotonic() - t0) * 1000
        assert dt_enable < 500, f"enable 太慢: {dt_enable:.0f}ms (界面会卡)"
        assert dt_query < 50, f"查询太慢: {dt_query:.0f}ms (界面会卡)"
    finally:
        if before:
            auto.enable()
        else:
            auto.disable()


def test_command_points_to_exe():
    """★ 自启命令必须指向可独立运行的入口。

    源码运行时也优先用 dist/com-bridge-gui.exe —— 自启要的是不依赖
    项目目录、不依赖 Python 环境的稳定入口。
    """
    cmd = auto._exe_command()
    assert cmd.startswith('"'), "路径必须加引号(可能含空格)"
    assert cmd.endswith('"') or ".exe" in cmd.lower() or "-m " in cmd


def test_gui_button_has_no_uac():
    """GUI 按钮不得再走提权流程。"""
    src = inspect.getsource(gui_mod.ComBridgeGui._on_elev_toggle)
    assert "request_elevated" not in src, "不得弹 UAC"
    assert "autostart" in src
    build = inspect.getsource(gui_mod.ComBridgeGui._build)
    assert "开机自启" in build, "按钮文字应为'开机自启'(不再强调管理员)"


def test_status_text_readable():
    """状态文案必须能让人一眼看出开没开。"""
    before = auto.is_enabled()
    try:
        auto.enable()
        assert "已启用" in auto.status_text()
        auto.disable()
        assert "未启用" in auto.status_text()
    finally:
        if before:
            auto.enable()
        else:
            auto.disable()
