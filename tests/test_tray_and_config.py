# -*- coding: utf-8 -*-
"""回归: 托盘 / 配置记忆 / 次级窗口 / 自动重连。

## 用户需求(2026-10-19)
1. 程序注册为开机自启, 主副机静默自动连接
   -> 副机要记忆 IP + 记忆"上次是否运行中"; 运行状态退出的才自动重连
2. 托盘图标: 按 X 只关窗口, 托盘右键才彻底退出; 服务启动时托盘必须存在
3. 实时状态与日志从主 UI 移到次级弹窗
4. UI 整体现代化重设计
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import com_bridge.config as cfg  # noqa: E402
import com_bridge.gui as gui_mod  # noqa: E402
import com_bridge.tray as tray_mod  # noqa: E402


def _strip_comments(src: str) -> str:
    return "\n".join(line for line in src.splitlines()
                     if not line.strip().startswith("#"))


# --- 配置记忆 ---------------------------------------------------------

def test_config_roundtrip(tmp_path=None):
    """配置能写入并读回(含 IP 与运行标志)。"""
    before = cfg.load()
    try:
        cfg.mark_running("agent", "right", 55.0, "10.1.2.3")
        loaded = cfg.load()
        assert loaded["role"] == "agent"
        assert loaded["server_host"] == "10.1.2.3"
        assert loaded["jitter_ms"] == 55.0
        assert loaded["was_running"] is True
        assert cfg.should_autostart() is True
    finally:
        cfg.save(before)


def test_mark_stopped_clears_autostart():
    """★ 用户点"停止" -> 清除运行标志 -> 下次不自动连。"""
    before = cfg.load()
    try:
        cfg.mark_running("agent", "right", 60.0, "10.1.2.3")
        assert cfg.should_autostart() is True
        cfg.mark_stopped()
        assert cfg.should_autostart() is False, \
            "用户主动停止后, 下次不应自动重连"
        # 停止不清 IP(用户下次手动启动还要用)
        assert cfg.default_host_for_autostart() == "10.1.2.3"
    finally:
        cfg.save(before)


def test_exit_keeps_running_state():
    """★ 退出程序(非停止) -> 保持 was_running -> 下次自动连。"""
    before = cfg.load()
    try:
        cfg.mark_running("server", "right", 60.0, "")
        cfg.mark_exit_keeping_state()
        assert cfg.should_autostart() is True, \
            "从运行状态退出, 下次应自动重连"
    finally:
        cfg.save(before)


def test_config_survives_corrupt_file():
    """配置文件损坏时退回默认值, 不能让程序启动失败。"""
    path = cfg.config_path()
    backup = None
    try:
        if os.path.exists(path):
            backup = open(path, encoding="utf-8").read()
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ this is not json")
        loaded = cfg.load()
        assert isinstance(loaded, dict)
        assert "was_running" in loaded
    finally:
        if backup is not None:
            with open(path, "w", encoding="utf-8") as f:
                f.write(backup)


# --- 托盘 -------------------------------------------------------------

def test_tray_module_exists():
    assert hasattr(tray_mod, "TrayIcon")
    assert hasattr(tray_mod.TrayIcon, "start")
    assert hasattr(tray_mod.TrayIcon, "stop")
    assert hasattr(tray_mod.TrayIcon, "set_status")


def test_tray_has_taskbar_recreated():
    """★ 必须处理 TaskbarCreated: 资源管理器重启后要重新加图标,
    否则用户会以为程序退出了。"""
    src = inspect.getsource(tray_mod)
    assert "TaskbarCreated" in src
    assert "RegisterWindowMessageW" in src


def test_tray_menu_has_exit():
    """托盘右键菜单必须有"退出"(用户要求: 只有它能彻底关闭程序)。"""
    src = inspect.getsource(tray_mod.TrayIcon._show_menu)
    assert "退出" in src
    src2 = inspect.getsource(tray_mod)
    assert "CMD_EXIT" in src2


def test_tray_set_foreground_before_menu():
    """★ 弹菜单前必须 SetForegroundWindow, 否则点空白处菜单不消失。"""
    src = inspect.getsource(tray_mod.TrayIcon._show_menu)
    i_fg = src.index("SetForegroundWindow")
    i_track = src.index("TrackPopupMenu")
    assert i_fg < i_track, "必须在 TrackPopupMenu 之前 SetForegroundWindow"


def test_tray_does_not_override_getcursorpos_argtypes():
    """★★★ tray 不得声明 GetCursorPos.argtypes —— ctypes 是全局共享的。

    实测踩到: tray 用自己定义的 POINT 类声明 argtypes, 覆盖了
    input.py 里基于 wintypes.POINT 的声明, 之后 input.get_cursor_pos()
    直接抛 "expected LP_POINT instance instead of pointer to POINT",
    表现为**所有坐标读取失败**。

    ★ 只检查**可执行代码行**(注释里提到该名字是允许的, 用于说明原因)。
    """
    live = [ln for ln in inspect.getsource(tray_mod).splitlines()
            if "GetCursorPos.argtypes" in ln
            and not ln.strip().startswith("#")]
    assert not live, f"不得声明 GetCursorPos.argtypes, 实际有: {live}"


def test_tray_reuses_input_structures():
    """★★★ tray 必须**复用** input.py 的 WNDCLASSW/WNDPROC, 不能自己再定义。

    实测踩到(直接导致主电脑完全无法启动):
      tray 自己定义了一份 WNDCLASSW 并声明
      `RegisterClassW.argtypes = [POINTER(tray 的 WNDCLASSW)]`,
      覆盖掉 input.py 基于自己那份 WNDCLASSW 的声明, 于是输入监听线程抛:
        ctypes.ArgumentError: expected LP_WNDCLASSW instance
                              instead of pointer to WNDCLASSW
      -> RawInputListener 线程崩溃 -> "原始输入监听启动失败"。
    这是同一个坑的第二次(第一次是 POINT), 所以用**身份断言**锁死。
    """
    from com_bridge import input as input_mod
    assert tray_mod.WNDCLASSW is input_mod.WNDCLASSW, \
        "tray 必须复用 input 的 WNDCLASSW(各写一份会导致 ctypes 类型冲突)"
    assert tray_mod.WNDPROC is input_mod.WNDPROC


def test_tray_does_not_redeclare_shared_functions():
    """tray 不得重复声明 input.py 也用的 Win32 函数签名。

    共享要求: CreateWindowExW / DefWindowProcW / RegisterClassW /
    GetCursorPos —— 它们的签名统一由 input.py 负责。
    """
    live = [ln for ln in inspect.getsource(tray_mod).splitlines()
            if not ln.strip().startswith("#")
            and any(f"{fn}.argtypes" in ln or f"{fn}.restype" in ln
                    for fn in ("CreateWindowExW", "DefWindowProcW",
                               "RegisterClassW", "GetCursorPos"))]
    assert not live, f"tray 不得重复声明这些函数的签名: {live}"


def test_rawinput_listener_works_after_tray_import():
    """★ 先导入 tray 再启动 RawInputListener 必须成功(导入顺序无关)。"""
    from com_bridge.input import RawInputListener
    lis = RawInputListener(on_event=lambda e: None)
    lis.start()
    ok = lis.wait_ready(3.0)
    lis.stop()
    assert ok, "tray 导入不应破坏 RawInputListener 的启动"


def test_get_cursor_pos_works_after_tray_import():
    """先导入 tray 再读坐标必须正常(导入顺序不该影响坐标 API)。"""
    # tray 已在模块顶部导入, 这里直接验证坐标可用
    from com_bridge.input import get_cursor_pos
    x, y = get_cursor_pos()
    assert isinstance(x, int) and isinstance(y, int)


def test_tray_status_icons_differ():
    """运行/停止必须用不同颜色(用户要求直观分辨)。"""
    src = inspect.getsource(tray_mod.TrayIcon._icon_handle)
    assert "46, 160, 67" in src, "运行中应为绿色"
    assert "140, 140, 140" in src, "停止应为灰色"


# --- 托盘图标必须真的画得出内容 ---------------------------------------

def test_dot_icon_creates_valid_handle():
    """★★★ 图标必须能创建成功(用户实测: 托盘里是空白图标)。

    原实现(CreateDIBSection 32bpp + CreateIconIndirect)返回 0:
      * 坑1: `gdi32.CreateBitmap.restype` 写在调用**之后**, 句柄被当
             c_int 截断成 32 位 -> CreateIconIndirect 拿到无效句柄;
      * 坑2: 32bpp 图标只靠 DIB 的 alpha 通道表达透明不可靠,
             必须用 AND 掩码(圆内=0 不透明, 圆外=1 透明)兜底。
    """
    hicon = tray_mod._make_dot_icon((46, 160, 67))
    assert hicon, "圆点图标必须创建成功(返回 0 = 托盘显示空白)"
    # 有效句柄必须能取出掩码与彩色位图
    import ctypes
    from ctypes import wintypes

    class ICONINFO(ctypes.Structure):
        _fields_ = [("fIcon", wintypes.BOOL),
                    ("xHotspot", wintypes.DWORD),
                    ("yHotspot", wintypes.DWORD),
                    ("hbmMask", wintypes.HBITMAP),
                    ("hbmColor", wintypes.HBITMAP)]

    u = ctypes.windll.user32
    u.GetIconInfo.argtypes = [wintypes.HICON, ctypes.c_void_p]
    u.GetIconInfo.restype = wintypes.BOOL
    info = ICONINFO()
    assert u.GetIconInfo(hicon, ctypes.byref(info)), "GetIconInfo 应成功"
    assert info.hbmMask, "必须有 AND 掩码(透明靠它表达)"
    assert info.hbmColor, "必须有彩色位图"
    g = ctypes.windll.gdi32
    for bm in (info.hbmMask, info.hbmColor):
        if bm:
            g.DeleteObject(bm)
    u.DestroyIcon(hicon)


def test_dot_icon_restype_declared_before_call():
    """★ 所有 GDI restype 必须在调用之前声明(否则句柄被截断)。

    这是本项目第三次踩同类坑(前两次是 POINT / WNDCLASSW 的 argtypes),
    所以用静态检查锁死: CreateBitmap 的 restype 赋值行号必须早于
    它的调用行号。
    """
    src = inspect.getsource(tray_mod._make_dot_icon)
    lines = src.splitlines()
    decl = None
    call = None
    for i, ln in enumerate(lines):
        if "gdi32.CreateBitmap.restype" in ln:
            decl = i
        elif "gdi32.CreateBitmap(" in ln and "restype" not in ln \
                and "argtypes" not in ln:
            call = i if call is None else call
    assert decl is not None, "必须声明 CreateBitmap.restype"
    assert call is not None, "应当调用 CreateBitmap 创建掩码"
    assert decl < call, "restype 声明必须早于调用(否则句柄截断)"


def test_tray_icon_handle_is_valid_after_start():
    """托盘启动后图标句柄必须有效。"""
    t = tray_mod.TrayIcon(tooltip="Com Bridge · 自检")
    try:
        assert t.start(), "托盘应能启动"
        assert t._hicon, "启动后必须有有效图标句柄"
    finally:
        t.stop()


def test_tray_reports_icon_status():
    """★★ 托盘必须自报图标状态(否则"托盘没图标"无从诊断)。

    用户实测报过"托盘没有图标", 当时没有日志可查 —— 只能靠猜。
    现在 `icon_ok` / `icon_detail` 会记录 Shell_NotifyIcon 的返回值与
    图标句柄, GUI 启动时写进日志, 一眼能区分:
      * 图标生成失败(hIcon=0 -> 退回系统默认图标)
      * 加入托盘失败(Shell_NotifyIcon=False)
    """
    t = tray_mod.TrayIcon(tooltip="Com Bridge · 自检")
    try:
        assert t.icon_ok is False, "启动前应为 False"
        assert t.icon_detail == "未初始化"
        assert t.start()
        assert t.icon_ok is True, "启动成功后 icon_ok 必须为 True"
        assert "Shell_NotifyIcon=True" in t.icon_detail
        assert "hIcon=" in t.icon_detail
    finally:
        t.stop()


def test_gui_logs_tray_status():
    """GUI 初始化托盘时必须把状态写进日志。"""
    src = inspect.getsource(gui_mod.ComBridgeGui._init_tray)
    assert "icon_detail" in src, "必须记录图标细节"
    assert "icon_ok" in src, "必须判断图标是否真的就绪"
    assert "_log_q.put" in src, "状态必须进日志(用户可见)"


# --- 主窗口: X 只隐藏 / 托盘退出 ---------------------------------------

def test_close_only_hides_when_tray_exists():
    """★ 点 X 只隐藏窗口(托盘程序的标准行为)。"""
    src = _strip_comments(inspect.getsource(gui_mod.ComBridgeGui._on_close))
    assert "withdraw" in src, "有托盘时应隐藏而不是销毁"
    assert "_quit_app" in src, "没有托盘时必须能真退出(否则用户退不掉)"


def test_quit_app_stops_everything():
    """彻底退出必须停服务、停托盘、销毁次级窗口。"""
    src = _strip_comments(inspect.getsource(gui_mod.ComBridgeGui._quit_app))
    assert "role.stop()" in src
    assert "tray.stop()" in src
    assert "_closing = True" in src


# --- 次级窗口 ---------------------------------------------------------

def test_status_and_log_windows_exist():
    from com_bridge.gui_windows import LogWindow, StatusWindow
    assert hasattr(StatusWindow, "refresh")
    assert hasattr(LogWindow, "append")
    # 关闭只隐藏
    assert "withdraw" in inspect.getsource(
        __import__("com_bridge.gui_windows", fromlist=["_BaseWindow"])
        ._BaseWindow.hide)


def test_log_window_bounded():
    """日志窗口必须限制行数, 否则长时间运行会吃满内存。"""
    src = inspect.getsource(
        __import__("com_bridge.gui_windows", fromlist=["LogWindow"]).LogWindow)
    assert "5000" in src or "delete(" in src, "必须有行数上限"


def test_main_ui_has_window_entry_buttons():
    """主界面必须有打开次级窗口的入口。

    ★ 2026-10-21 经过一轮反复: 先是顶栏同时有状态+日志, 后按要求只留
      状态(日志挪到托盘), 用户反馈"日志入口没了"——判定为严重问题。
      现在**两个入口都在顶栏**, 托盘菜单那条也保留。
    """
    src = inspect.getsource(gui_mod.ComBridgeGui._build)
    assert "_open_status_window" in src, "状态入口应在主界面"
    assert "_open_log_window" in src, "日志入口应在主界面(用户明确要求)"


def test_main_ui_no_log_text_widget():
    """★ 主界面不得再内嵌日志 Text 或状态五行(已移到次级窗口)。"""
    src = _strip_comments(inspect.getsource(gui_mod.ComBridgeGui._build))
    assert "tk.Text" not in src, "主界面不应再内嵌日志文本框"
    assert "_stat_vars" not in src, "主界面不应再保留实时状态行"


# --- 自动重连 ---------------------------------------------------------

def test_autostart_reads_config():
    """启动时必须读配置决定是否自动重连。"""
    src = inspect.getsource(
        gui_mod.ComBridgeGui._restore_and_maybe_autostart)
    assert "was_running" in src
    assert "_autostart_now" in src


def test_autostart_uses_remembered_host():
    """自动重连必须用记忆里的 IP。"""
    src = inspect.getsource(gui_mod.ComBridgeGui._autostart_now)
    assert "server_host" in src
    assert "_on_start" in src
