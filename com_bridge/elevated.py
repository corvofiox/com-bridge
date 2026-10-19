# -*- coding: utf-8 -*-
"""提权自启管理: 让 GUI 里的一个按钮完成"登录时以最高权限自动启动"的注册。

## 为什么是"计划任务 + RunLevel Highest"而不是 Windows 服务

服务跑在 Session 0, 那里没有用户桌面 —— 装在服务里的低级钩子**收不到
你会话的键鼠**(等于哑巴)。Deskflow 自己也没让服务装钩子: 它的 Windows
服务(SYSTEM)只是个启动器, 真正装钩子的 deskflow-core 是服务用**用户令牌
在用户会话里提权拉起**的(daemon 日志: `running command (elevated)`)。
"登录计划任务 + RunLevel Highest" 与之完全等价, 且以后每次登录自动
启动、无 UAC 弹窗。

## GUI 集成的实现方式

注册/卸载需要管理员权限。GUI 进程本身保持未提权(用户平时手动启动时
不该被迫弹 UAC), 点按钮时才用 `ShellExecuteW(verb="runas")` 提权调起
**本 exe 自己**, 带 `--install-elevated` / `--uninstall-elevated` 隐藏
参数在子进程里完成注册后立即退出 —— 这是 Windows 上 GUI 程序请求
一次 UAC 的标准做法(ShellExecute 带 runas 是官方文档姿势)。

★ 子进程回传结果: 写到 %TEMP% 下的结果文件, GUI 轮询等待后读取展示
  (提权进程与普通进程之间最简单可靠的信道; stdout 不可用, 因为
  --noconsole 打包下子进程没有控制台)。
"""
from __future__ import annotations

import ctypes
import os
import sys
import time

TASK_NAME = "ComBridgeElevatedAutostart"
_RESULT_TIMEOUT_S = 25.0


def _result_path(tag: str) -> str:
    return os.path.join(os.environ.get("TEMP", "."),
                        f"combridge_elev_{tag}.txt")


def is_elevated() -> bool:
    """当前进程是否已提权(供 GUI 状态展示)。"""
    try:
        advapi = ctypes.windll.advapi32
        kernel = ctypes.windll.kernel32
        from ctypes import wintypes
        advapi.OpenProcessToken.restype = wintypes.BOOL
        advapi.OpenProcessToken.argtypes = [
            wintypes.HANDLE, wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE)]
        tok = wintypes.HANDLE()
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008,
                                       ctypes.byref(tok)):
            return False
        v = wintypes.DWORD()
        rl = wintypes.DWORD()
        advapi.GetTokenInformation.restype = wintypes.BOOL
        return bool(advapi.GetTokenInformation(tok, 20, ctypes.byref(v), 4,
                                               ctypes.byref(rl)) and v.value)
    except Exception:  # noqa: BLE001
        return False


def task_exists(force: bool = False) -> bool:
    """自启任务是否已注册。

    ★★★ 为什么不能只用 schtasks(用户实测"每次操作卡 3 秒"):
      `subprocess.run(["schtasks", ...])` 要**创建子进程** ——
      在打包 exe 里(单文件解包 + 杀软扫描)实测可达 1~3 秒。
      它在主线程被调用就会**冻住整个界面**, 这正是用户报的
      "启动/开启自启/关闭自启时锁死 3 秒"。

    ★★★ 缓存必须能被**主动作废**(这里踩过一个真实回归):
      注册/卸载发生在**提权子进程**里, 那里调 `invalidate_task_cache()`
      只清掉子进程自己的内存, **父进程的缓存纹丝不动**。
      于是父进程操作完成后查询仍返回旧值 -> 界面继续显示"未启用"
      -> 用户认为"注册不成功"。
      所以: 任何"状态可能已变"的地方, 都必须显式 `force=True` 或先
      调 `invalidate_task_cache()`。
    """
    cached = _TASK_EXISTS_CACHE.get("value")
    if cached is not None and not force:
        return bool(cached)
    result = _query_task_real(0.35)
    _TASK_EXISTS_CACHE["value"] = result
    return result


_TASK_EXISTS_CACHE: dict[str, bool] = {}


def invalidate_task_cache() -> None:
    """作废查询缓存。注册/卸载后**必须**调用, 否则界面显示旧状态。"""
    _TASK_EXISTS_CACHE.pop("value", None)


def _query_task_real(timeout: float = 0.35) -> bool:
    """真正查询(可能起子进程)。**只应由后台线程调用**。

    ★ timeout 给得很小: schtasks 正常情况下 15~30ms 就返回; 若它卡住,
      说明系统调度服务有问题 —— 此时宁可判"未知(False)"也不要冻界面。
    """
    import subprocess
    try:
        r = subprocess.run(
            ["schtasks", "/Query", "/TN", TASK_NAME],
            capture_output=True, timeout=max(0.3, timeout))
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- 子进程侧

def _run_elevated_child(mode: str, tag: str = "") -> int:
    """在(已被 runas 提权的)子进程里执行注册/卸载, 返回退出码。

    mode: "install" | "uninstall"
    tag:  父进程通过 `--elevated-tag` 传来的结果文件名标记 —— 结果写到
          对应文件, 供父进程轮询读取。(提权子进程环境是全新的,
          环境变量传不过去, 所以必须走命令行。)
    """
    try:
        rc = _do_register() if mode == "install" else _do_unregister()
        # 状态已变 -> 清掉查询缓存, 否则界面显示的仍是旧状态
        invalidate_task_cache()
        if tag:
            with open(_result_path(tag), "w", encoding="utf-8") as f:
                # ★ 把 PowerShell 的真实错误一起带回去: "ok rc=1" 什么
                #   都说明不了, 而 Access denied / 参数不合法 的处理方式
                #   完全不同。父进程会把它拼进用户可见的提示。
                detail = (LAST_ERROR or "").replace("\n", " ")[:300]
                f.write(f"ok rc={rc}" + (f" detail={detail}" if detail
                                         and rc != 0 else "") + "\n")
        return rc
    except Exception as exc:  # noqa: BLE001
        invalidate_task_cache()
        if tag:
            try:
                with open(_result_path(tag), "w", encoding="utf-8") as f:
                    f.write(f"error {exc}\n")
            except OSError:
                pass
        return 1


def _powershell(script: str) -> int:
    """跑一段 PowerShell(已是管理员上下文), 返回退出码。

    ★ 失败时把 stderr 记进 LAST_ERROR —— 否则上层只能报"退出码 1",
      用户和我们都不知道真实原因(Access denied / 任务不存在 /
      参数不合法 全都长得一样)。
    """
    global LAST_ERROR
    import subprocess
    LAST_ERROR = ""
    # ★ 让 PowerShell 用 UTF-8 输出: 否则中文 Windows 上 stderr 是 GBK,
    #   我们按 UTF-8 解码会得到乱码("�ܾ�����"), 用户看不到真实原因。
    #   OutputEncoding 管 stdout, 错误流同样受影响。
    wrapped = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        "$OutputEncoding=[System.Text.Encoding]::UTF8;" + script
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command", wrapped],
            capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        LAST_ERROR = "PowerShell 超时(30s)"
        return 124
    except OSError as exc:
        LAST_ERROR = f"无法启动 PowerShell: {exc}"
        return 125
    if r.returncode != 0:
        # ★ 编码: 中文 Windows 上 PowerShell 的 stderr 是**本地代码页**
        #   (GBK/936), 直接按 UTF-8 解码会得到乱码(实测:
        #   "Register-ScheduledTask : �ܾ����ʡ�")。先试 UTF-8, 失败退
        #   回 mbcs(Windows 的 ANSI 代码页), 保证用户看到可读的原因。
        def _decode(b: bytes) -> str:
            if not b:
                return ""
            for enc in ("utf-8", "mbcs", "gbk"):
                try:
                    return b.decode(enc).strip()
                except (UnicodeDecodeError, LookupError):
                    continue
            return b.decode("utf-8", "replace").strip()

        err = _decode(r.stderr or b"")
        out = _decode(r.stdout or b"")
        LAST_ERROR = (err or out or f"退出码 {r.returncode}")[:400]
    return r.returncode


# 最近一次 _powershell 的错误文本(供上层拼进用户可见的消息)
LAST_ERROR = ""


def _exe_path() -> tuple[str, str]:
    """返回 (exe, 默认参数) —— 计划任务的动作项。

    打包场景: dist 里的 com-bridge-gui.exe, 参数 "gui"。
    源码场景: python.exe, 参数 '-m com_bridge.gui' —— exe 路径里可能
    有空格, 由 _do_register 负责加引号。
    """
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable), "gui"
    here = os.path.dirname(os.path.abspath(__file__))
    dist = os.path.join(os.path.dirname(here), "dist", "com-bridge-gui.exe")
    if os.path.exists(dist):
        return dist, "gui"
    # 源码兜底: 用当前解释器跑模块(任务只在本机有效, 开发场景够用)
    return sys.executable, "-m com_bridge.gui"


def _ps_quote(s: str) -> str:
    """PowerShell 单引号字符串转义: 内部单引号翻倍。"""
    return "'" + s.replace("'", "''") + "'"


def _do_register() -> int:
    exe, exe_args = _exe_path()
    user = os.environ.get("USERNAME", "")
    dom = os.environ.get("USERDOMAIN", "")
    full_user = f"{dom}\\{user}" if dom and user else user
    # PowerShell 里路径可能带引号(源码场景), 直接内插进脚本
    script = (
        f"$ErrorActionPreference='Stop';"
        f"try {{ Unregister-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} "
        f"-Confirm:$false -ErrorAction SilentlyContinue }} catch {{ }};"
        f"$a = New-ScheduledTaskAction -Execute {_ps_quote(exe)} "
        f"-Argument {_ps_quote(exe_args)};"
        f"$t = New-ScheduledTaskTrigger -AtLogOn "
        f"-User {_ps_quote(full_user)};"
        f"$p = New-ScheduledTaskPrincipal -UserId {_ps_quote(full_user)} "
        f"-RunLevel Highest -LogonType Interactive;"
        f"$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
        f"-DontStopIfGoingOnBatteries -StartWhenAvailable "
        f"-ExecutionTimeLimit (New-TimeSpan -Seconds 0);"
        f"Register-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} "
        f"-Action $a -Trigger $t -Principal $p -Settings $s | Out-Null;"
        f"exit 0"
    )
    return _powershell(script)


def _do_unregister() -> int:
    script = (
        f"try {{ Unregister-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} "
        f"-Confirm:$false -ErrorAction Stop; exit 0 }} "
        f"catch {{ exit 2 }}"
    )
    return _powershell(script)


# ---------------------------------------------------------------- 父进程侧

def request_elevated(mode: str) -> tuple[bool, str]:
    """GUI 调用: 提权执行安装/卸载, 等待结果。

    返回 (成功与否, 给用户看的消息)。用户拒绝 UAC 时返回明确提示。
    mode: "install" | "uninstall"
    """
    if is_elevated():
        # 已提权(比如本就由任务拉起): 直接执行, 不再弹 UAC
        rc = (_do_register() if mode == "install" else _do_unregister())
        if rc == 0:
            return True, ("已注册管理员自启(本次进程已是管理员, 未弹窗)"
                          if mode == "install" else "已移除管理员自启")
        return False, f"操作失败(退出码 {rc})"

    tag = f"{mode}_{int(time.time() * 1000) & 0xFFFFFF}"
    try:
        os.unlink(_result_path(tag))
    except OSError:
        pass

    # ★ 结果通道只能走**命令行参数**: ShellExecuteW(runas) 拉起的提权
    #   子进程拿的是全新环境, os.environ 里塞的变量**传不过去**
    #   (与 CreateProcess 不同, 它不继承调用方环境块的自定义项)。
    #   子进程把 tag 写进结果文件名, 父进程按名轮询。
    exe = sys.executable if getattr(sys, "frozen", False) \
        else os.path.abspath(sys.argv[0] if sys.argv[0] else sys.executable)
    params = f'--elevated-child {mode} --elevated-tag {tag}'
    SEE_MASK_NOCLOSEPROCESS = 0x40
    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_uint), ("fMask", ctypes.c_uint),
            ("hwnd", ctypes.c_void_p), ("lpVerb", ctypes.c_wchar_p),
            ("lpFile", ctypes.c_wchar_p), ("lpParameters", ctypes.c_wchar_p),
            ("lpDirectory", ctypes.c_wchar_p), ("nShow", ctypes.c_int),
            ("hInstApp", ctypes.c_void_p), ("lpIDList", ctypes.c_void_p),
            ("lpClass", ctypes.c_wchar_p), ("hkeyClass", ctypes.c_void_p),
            ("dwHotKey", ctypes.c_uint), ("hIconOrMonitor", ctypes.c_void_p),
            ("hProcess", ctypes.c_void_p),
        ]
    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = "runas"
    sei.lpFile = exe
    sei.lpParameters = params
    sei.nShow = 0            # SW_HIDE: 子进程只做注册然后退出, 不闪窗
    shell32 = ctypes.windll.shell32
    if not shell32.ShellExecuteExW(ctypes.byref(sei)):
        err = ctypes.get_last_error()
        if err in (1223, 5):   # ERROR_CANCELLED / ACCESS_DENIED
            return False, "已取消(未授予管理员权限)"
        return False, f"提权请求失败(错误 {err})"

    # 等子进程写结果文件(它只做一次 schtasks 注册, 几秒即完)
    deadline = time.monotonic() + _RESULT_TIMEOUT_S
    path = _result_path(tag)
    while time.monotonic() < deadline:
        if os.path.exists(path):
            time.sleep(0.2)
            try:
                with open(path, encoding="utf-8") as f:
                    content = f.read().strip()
            except OSError:
                content = ""
            try:
                os.unlink(path)
            except OSError:
                pass
            if content.startswith("ok"):
                # 格式: "ok rc=N" 或 "ok rc=N detail=<PowerShell 错误>"
                rc_part = content[len("ok"):].strip()
                rc_val = 0
                detail = ""
                if rc_part.startswith("rc="):
                    rest = rc_part[3:].strip()
                    head = rest.split("detail=", 1)
                    try:
                        rc_val = int(head[0].split()[0]) if head[0].strip() \
                            else -1
                    except (ValueError, IndexError):
                        rc_val = -1
                    if len(head) > 1:
                        detail = head[1].strip()
                if rc_val != 0:
                    # ★ 把真实原因告诉用户, 并给出可操作的建议。
                    #   只报"退出码 1"等于没说 —— 拒绝访问和参数不合法
                    #   的处理方式完全不同。
                    low = detail.lower()
                    if "access is denied" in low or "0x80070005" in low:
                        advice = "拒绝访问, 请确认 UAC 弹窗中点的是「是」"
                    elif "already exists" in low:
                        advice = "任务已存在, 请先点「移除自启」再重新注册"
                    elif "cannot find" in low or "not found" in low:
                        advice = "系统找不到任务计划服务, 请检查服务是否被禁用"
                    else:
                        advice = "计划任务服务拒绝了操作"
                    msg = f"操作失败: {advice}"
                    if detail:
                        msg += f" (系统信息: {detail})"
                    return False, msg
                return True, ("已注册管理员自启(每次登录自动以最高权限启动)"
                              if mode == "install" else "已移除管理员自启")
            if content.startswith("error"):
                return False, f"注册失败: {content[6:]}"
            return False, f"结果异常: {content}"
        time.sleep(0.2)
    return False, "等待超时(子进程未回报, 请重试或查看日志)"
