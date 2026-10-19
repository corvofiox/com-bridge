# -*- coding: utf-8 -*-
"""单实例互斥 —— 保证同一时间只有一个 Com Bridge 在跑。

## 为什么必须有(用户实测)
"当前程序没有做互斥性检查, 我能同时启动两个甚至多个"

多实例的后果不是"多开几个窗口"这么简单:
  * 两个实例都会去 bind 同一组端口(24802/TCP、4010/UDP) ——
    后启动的那个会**绑定失败**, 表现为"启动失败"但窗口还在, 用户困惑;
  * 两个实例都会装低级键鼠钩子 -> 事件被**吞两次/转发两次**,
    跨屏输入出现重影;
  * 两个实例都往托盘塞图标 -> 托盘里出现多个同名图标;
  * 两个实例都会写同一份 config.json -> 状态互相覆盖。

## 实现
1. **命名互斥量**判定唯一性: 内核对象跨进程共享, 第二个实例创建时
   `GetLastError() == ERROR_ALREADY_EXISTS(183)` 就知道自己不是第一个。
2. 第二个实例**不启动 GUI**, 而是**找到已有窗口并把它激活到前台**
   (用户双击 exe 的预期就是"把那个窗口叫出来"), 然后自己退出。
3. 查找方式: `EnumWindows` 找**类名/标题**匹配的顶层窗口。
   窗口标题里带固定标记(`Com Bridge`), 且必须可见或有托盘 ——
   注意隐藏到托盘的窗口 `IsWindowVisible` 为 False, 所以要一并
   尝试 `ShowWindow(SW_RESTORE)`。

## 为什么不用"锁文件"
锁文件在进程被强杀后会残留, 下次启动被误判为"已在运行", 用户必须手动
删文件 —— 对普通用户是不可接受的故障。命名互斥量由内核管理, 进程无论
怎么死(包括崩溃/任务管理器结束)都会自动释放。

## 为什么不只靠广播消息
`PostMessage(HWND_BROADCAST, ...)` 只对**顶层窗口**生效, 而且我们的窗口
可能是隐藏(在托盘)状态, 广播消息容易丢。直接找到窗口句柄再操作更可靠。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

kernel32 = ctypes.windll.kernel32
user32 = ctypes.windll.user32

ERROR_ALREADY_EXISTS = 183
SW_RESTORE = 9
SW_SHOW = 5

# 互斥量名: 用 GUID 风格避免撞名。
# Local\ 前缀 = 当前登录会话内唯一(不同用户各开一个是合理的)。
MUTEX_NAME = "Local\\ComBridge-SingleInstance-{6F1A2B3C-9D4E-4F5A-8B7C-1D2E3F4A5B6C}"

# 主窗口标题前缀(用于查找已有实例)
WINDOW_TITLE_MARK = "Com Bridge"

# 保存互斥量句柄: 必须**常驻**到进程结束, 否则互斥量被释放, 单实例失效
_handle = None
_created = False

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                 wintypes.LPARAM)


def acquire() -> bool:
    """尝试成为唯一实例。

    返回 True  = 我是第一个实例, 可以继续启动 GUI;
    返回 False = 已有实例在运行(调用方应唤起它并退出)。
    """
    global _handle, _created
    if _handle is not None:
        return _created
    try:
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                          wintypes.LPCWSTR]
        kernel32.GetLastError.restype = wintypes.DWORD
        h = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if not h:
            # 创建失败(极罕见): 保守允许启动, 不让用户完全用不了
            _handle = 0
            _created = True
            return True
        already = (kernel32.GetLastError() == ERROR_ALREADY_EXISTS)
        _handle = h
        _created = not already
        return _created
    except Exception:  # noqa: BLE001
        _handle = 0
        _created = True
        return True


def find_existing_window() -> int:
    """找到已运行实例的主窗口句柄, 找不到返回 0。

    ★ 为什么按标题找而不是记 PID/句柄: 第二个实例是**全新进程**,
      没有任何共享状态可用; 窗口标题是唯一稳定的跨进程线索。
      我们给主窗口标题加了固定前缀, 见 gui._build 里的 set_title。
    """
    found: list[int] = []

    def _cb(hwnd, _lp):
        try:
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 2)
            user32.GetWindowTextW(hwnd, buf, n + 2)
            title = buf.value
            if not title.startswith(WINDOW_TITLE_MARK):
                return True
            # 排除我们自己的（第二个实例还没建窗口，所以不会误匹配）
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == kernel32.GetCurrentProcessId():
                return True
            found.append(int(hwnd))
        except Exception:  # noqa: BLE001
            pass
        return True

    try:
        user32.EnumWindows.restype = wintypes.BOOL
        user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
        user32.EnumWindows(WNDENUMPROC(_cb), 0)
    except Exception:  # noqa: BLE001
        return 0
    return found[0] if found else 0


def activate_window(hwnd: int) -> bool:
    """把已有窗口显示并激活到前台(含从托盘恢复)。

    ★ 必须用 AttachThreadInput 强制: 第二个实例不是前台进程,
      Windows 会拒绝裸 `SetForegroundWindow`(实测返回 True 但没生效),
      用户看到的就是"双击了没反应"。
    """
    if not hwnd:
        return False
    try:
        user32.ShowWindow.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.BringWindowToTop.restype = wintypes.BOOL
        user32.BringWindowToTop.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.AttachThreadInput.restype = wintypes.BOOL
        user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD,
                                             wintypes.BOOL]
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD

        # 最小化/隐藏(托盘)都要先恢复
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.ShowWindow(hwnd, SW_SHOW)

        fg = user32.GetForegroundWindow()
        this_tid = kernel32.GetCurrentThreadId()
        fg_tid = wintypes.DWORD()
        if fg:
            user32.GetWindowThreadProcessId(fg, ctypes.byref(fg_tid))
        attached = False
        if fg_tid.value and fg_tid.value != this_tid:
            attached = bool(user32.AttachThreadInput(fg_tid.value, this_tid,
                                                     True))
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(fg_tid.value, this_tid, False)
        return True
    except Exception:  # noqa: BLE001
        return False


def notify_existing() -> bool:
    """唤起已运行实例的窗口。返回是否找到并激活成功。"""
    hwnd = find_existing_window()
    if not hwnd:
        return False
    return activate_window(hwnd)


def release() -> None:
    """释放互斥量(进程退出时系统会自动做, 这里显式释放便于测试)。"""
    global _handle, _created
    try:
        if _handle:
            kernel32.CloseHandle(wintypes.HANDLE(_handle))
    except Exception:  # noqa: BLE001
        pass
    _handle = None
    _created = False
