# -*- coding: utf-8 -*-
"""开机自启(静默, 无需管理员, 不弹 UAC)。

★ 静默的含义(2026-10-21 用户要求): 自启命令带 `--autostart` 标志, GUI
  启动后**只建托盘图标 + 自动拉起上次的角色, 不显示主窗口**。用户随时
  可以从托盘把窗口叫出来。不带标志时行为与手动双击启动完全一致。

## 为什么不用计划任务(用户明确要求"过程必须静默")
旧实现用「计划任务 + RunLevel Highest」。**创建最高权限计划任务在
Windows 上必然需要管理员提权**(UAC 弹窗), 这是系统安全边界, 绕不过。
用户要求"不能加入需要用户确认的过程, 整个过程必须静默" —— 那就只能
换成不需要提权的机制。

## 方案: HKCU\\...\\Run 注册表项
* 位置: `HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run`
* 属于**当前用户**, 读写都**不需要管理员权限**, 也不弹任何窗口;
* Windows 在用户登录时自动执行其中的命令(与托盘类程序的标准做法一致);
* 删除就是删一个值, 即时生效、可反复开关。

## 代价(必须诚实告知)
HKCU Run 启动的进程是**普通权限**, 不是"最高权限"。
本项目**运行时不依赖管理员**:
  * 低级键鼠钩子(WH_KEYBOARD_LL/WH_MOUSE_LL)在普通用户会话内可用;
  * WASAPI 环回采集、光标控制、UDP/TCP 通信都不需要提权。
唯一受限场景: 需要吞掉**其他提权窗口**的按键时可能被 UIPI 拦。
若确有此需求, 可手动用管理员身份运行本程序(或沿用旧计划任务)。

## 与旧计划任务的关系
旧版本注册过 `ComBridgeElevatedAutostart` 计划任务。若它仍存在,
登录时会与 Run 项**同时启动**, 产生两个进程 —— 但本项目有**单实例
互斥**(single_instance), 第二个会立刻退出并唤起第一个, 因此不会
出现双开问题。若想彻底清掉旧任务, 需要一次管理员权限(本模块会
在**已提权时**顺手清理, 未提权则静默跳过)。
"""
from __future__ import annotations

import os
import sys
import winreg

# 注册表位置与值名
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "ComBridge"

# ★★★ 自启标志(2026-10-21 用户要求: "开机自动启动后不弹出 GUI 前台页面,
#   而是后台静默启动")。
#   没有它时 exe 与用户双击启动完全一样 -> 主窗口直接出现在屏幕上。
#   带它启动时 GUI 只创建托盘图标 + 自动拉起上次的角色, **不显示主窗口**
#   (用户从托盘左键/双击即可随时把窗口调出来)。
AUTOSTART_FLAG = "--autostart"

# 旧计划任务名(仅用于检测/顺手清理)
LEGACY_TASK = "ComBridgeElevatedAutostart"


def _exe_command() -> str:
    """自启要执行的命令行(带引号 + 静默标志, 路径可能含空格)。

    ★ 优先用**打包好的 GUI exe**: 即使当前是源码运行, 只要 dist 里
      有 com-bridge-gui.exe, 就登记它 —— 自启要的是一个**不依赖项目
      目录、不依赖 Python 环境**的稳定入口。
      源码里的 pythonw -m com_bridge.gui 只在开发机上有效, 一旦移动
      目录或换 Python 版本就失效。
    """
    return _with_flag(_base_command())


def _base_command() -> str:
    """不带静默标志的可执行命令行。"""
    if getattr(sys, "frozen", False):
        return f'"{os.path.abspath(sys.executable)}"'
    # 源码运行: 找同项目的 dist/com-bridge-gui.exe
    here = os.path.dirname(os.path.abspath(__file__))
    dist_exe = os.path.join(os.path.dirname(here), "dist",
                           "com-bridge-gui.exe")
    if os.path.exists(dist_exe):
        return f'"{dist_exe}"'
    # 再退一步: pythonw 跑模块(仅开发机可用)
    exe = sys.executable
    pyw = exe.replace("python.exe", "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw
    return f'"{exe}" -m com_bridge.gui'


def _with_flag(cmd: str) -> str:
    """给命令行补上静默标志(已有则原样返回)。纯函数, 可单测。"""
    cmd = (cmd or "").strip()
    if not cmd:
        return cmd
    if AUTOSTART_FLAG in cmd.split():
        return cmd
    return f"{cmd} {AUTOSTART_FLAG}"


def needs_migration(stored: str) -> bool:
    """已登记的命令是否需要补静默标志(旧版本登记过的不带标志)。"""
    if not stored:
        return False
    return AUTOSTART_FLAG not in stored.split()


def migrate_command() -> str:
    """把已登记的自启命令升级为带静默标志的版本。

    为什么必须做: `enable()` 只在用户**动过开关**时才写注册表, 而
    旧版本已经写入了不带标志的命令 —— 不迁移的话, 老用户升级后照样
    会开机弹出窗口, 看起来"修了但没生效"。

    返回: "ok"(已是目标形态)/ "migrated"(已改写)/ "off"(未启用)/ "error"。
    """
    if not is_enabled():
        return "off"
    try:
        stored = stored_command()
        target = _exe_command()
        if not needs_migration(stored) and stored == target:
            return "ok"
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, VALUE_NAME, 0, winreg.REG_SZ, target)
        return "migrated"
    except OSError:
        return "error"


def is_enabled() -> bool:
    """当前用户是否已启用开机自启。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            try:
                winreg.QueryValueEx(k, VALUE_NAME)
                return True
            except FileNotFoundError:
                return False
    except OSError:
        return False


def stored_command() -> str:
    """读取已登记的命令(诊断用; 为空表示未设置)。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            val, _ = winreg.QueryValueEx(k, VALUE_NAME)
            return str(val)
    except (OSError, FileNotFoundError):
        return ""


def enable() -> tuple[bool, str]:
    """启用开机自启(静默, 无 UAC)。返回 (成功, 消息)。"""
    cmd = _exe_command()
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, VALUE_NAME, 0, winreg.REG_SZ, cmd)
    except OSError as exc:
        return False, f"写入注册表失败: {exc}"

    # 顺手清理旧计划任务(仅在已提权时能做, 未提权静默跳过)
    cleaned = _try_remove_legacy_task()
    msg = "已启用开机自启(静默启动, 无需管理员)"
    if cleaned:
        msg += "; 已清理旧版计划任务"
    elif _legacy_task_exists():
        msg += ("; 检测到旧版计划任务仍存在, 它需要管理员权限才能移除,"
                " 但不影响使用(单实例互斥会阻止双开)")
    return True, msg


def disable() -> tuple[bool, str]:
    """关闭开机自启(静默)。返回 (成功, 消息)。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            try:
                winreg.DeleteValue(k, VALUE_NAME)
            except FileNotFoundError:
                pass
    except FileNotFoundError:
        pass
    except OSError as exc:
        return False, f"删除注册表项失败: {exc}"

    cleaned = _try_remove_legacy_task()
    msg = "已关闭开机自启"
    if cleaned:
        msg += "; 已清理旧版计划任务"
    elif _legacy_task_exists():
        msg += ("; 旧版计划任务仍在(需管理员权限移除),"
                " 它会在登录时启动本程序")
    return True, msg


def _legacy_task_exists() -> bool:
    """旧计划任务是否还存在(只查询, 不需要权限)。"""
    try:
        from . import elevated
        return bool(elevated.task_exists(force=True))
    except Exception:  # noqa: BLE001
        return False


def _try_remove_legacy_task() -> bool:
    """已提权时顺手删除旧计划任务; 未提权直接跳过(不弹窗、不报错)。"""
    try:
        from . import elevated
        if not elevated.is_elevated():
            return False
        if not elevated.task_exists(force=True):
            return False
        rc = elevated._do_unregister()
        elevated.invalidate_task_cache()
        return rc == 0
    except Exception:  # noqa: BLE001
        return False


def status_text() -> str:
    """给界面用的一句话状态。"""
    if is_enabled():
        return "开机自启: 已启用(静默)"
    return "未启用开机自启"
