# -*- coding: utf-8 -*-
"""★★★ 回归: 2026-10-19 用户报告的四个问题。

## BUG A: 出点在可交互元素上时主机指针不消失
   根因: 浏览器滚动条/图片查看器这类窗口响应 hover 时会自己 SetCapture,
   我们的捕获被持续争抢; 而原实现只在收到 WM_CAPTURECHANGED 时重夺**一次**,
   追不上对方的持续重抢。捕获丢失期间鼠标消息流回对方线程, 本线程的
   ShowCursor 计数器失去效力 -> 光标现形。
   修复: hider 加 100ms 保活定时器, 主动查 GetCapture, 不是自己就重夺
   并补一次抖动注入强制提交计数器。

## BUG B: 程序窗口前台时副机键盘同步进主机窗口
   两个叠加根因:
   a) `_top_hwnd` 缓存的是**窗口成型前**记录的 hwnd(实测 top=8259710
      而真实根窗口是 2753054) -> tick 里 `fg == top` 永远 False ->
      焦点监控形同虚设。修复: 每次 tick 动态解析。
   b) 8 秒焦点宽限期在穿越态下仍在生效 -> GUI 在前台期间键盘钩子失效,
      副机每一次击键都泄漏进主机 GUI。修复: 穿越态下宽限期让位。

## BUG C: 实时状态窗口运行一段时间后不更新
   根因: hide() 会 after_cancel 掉刷新链, 而 show() 只做 deiconify,
   从不重启 tick -> 关一次再开就永久冻结。
   修复: show() 调 on_reopen(), 子类恢复自己的循环。

## BUG D: 水位破 200ms 伴随零碎杂音
   根因: 硬保护阈值 240ms 太高(60~240ms 区间无人管), 且 budget 被钉死
   在"补满渲染缓冲", 高水位只能靠积分项以数十秒时间常数慢慢削。
   修复: 软阈值(1.5x 目标)起就在 budget 上叠加超调量加速排水, 保留
   240ms 硬兜底处理极端失控。
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import com_bridge.cursor as cursor_mod  # noqa: E402
import com_bridge.gui as gui_mod  # noqa: E402
import com_bridge.gui_windows as gw_mod  # noqa: E402
import com_bridge.server as srv_mod  # noqa: E402


# --- BUG A: hider 保活 ------------------------------------------------

def test_hider_has_keepalive_timer():
    """★ 必须有周期保活来对抗可交互窗口的持续捕获争抢。"""
    src = inspect.getsource(cursor_mod)
    assert "KEEPALIVE_TIMER_ID" in src
    assert "KEEPALIVE_MS" in src
    proc = inspect.getsource(cursor_mod.CursorHider._proc)
    assert "WM_TIMER" in proc, "必须处理保活定时器消息"
    assert "GetCapture() != hwnd" in proc, "必须检测捕获是否还在手上"


def test_hider_starts_and_stops_timer():
    """hide 时启动保活, show 时停止(否则无谓空转)。"""
    hide_src = inspect.getsource(cursor_mod.CursorHider._do_hide)
    show_src = inspect.getsource(cursor_mod.CursorHider._do_show)
    assert "SetTimer" in hide_src, "hide 必须启动保活"
    assert "KillTimer" in show_src, "show 必须停止保活"


def test_hider_reacquire_represses_cursor():
    """保活重夺后必须**重新压光标可见性**。

    ★ 实现演进(2026-10-19 第二版): 早期用"补一次抖动注入"来提交计数器,
      但实测发现更可靠的是直接 `_set_cursor_visibility(False)` ——
      因为外部软件(如 Quicker 悬浮面板)会调 ShowCursor(TRUE) 把
      计数器加回去, 光靠提交旧计数器压不住。
    """
    proc = inspect.getsource(cursor_mod.CursorHider._proc)
    i_timer = proc.index("WM_TIMER")
    # ★ 2026-10-21: 切片上界改用"下一个分支"锚定, 不再用固定长度 ——
    #   定时器分支里新增了抢前台刹车(见 CursorHider._steal_disabled),
    #   固定字符数会把断言窗口切短。语义不变: 该分支内必须同时有
    #   "重夺捕获"与"重压光标可见性"。
    i_next = proc.find("WM_CAPTURECHANGED", i_timer)
    seg = proc[i_timer:i_next] if i_next > i_timer else proc[i_timer:]
    assert "_set_cursor_visibility" in seg, \
        "重夺后必须重新压光标可见性"
    assert "_cursor_guard_visible" in proc, \
        "必须有周期性光标可见性守卫(对抗外部软件 ShowCursor)"


def test_hider_rechecks_foreground_in_keepalive():
    """★ 保活必须同时重验前台 —— 前台是 SetCapture 生效的前提。"""
    proc = inspect.getsource(cursor_mod.CursorHider._proc)
    i_timer = proc.index("WM_TIMER")
    seg = proc[i_timer:i_timer + 2200]
    assert "GetForegroundWindow" in seg, "必须检查前台是否还在手上"
    assert "SetForegroundWindow" in seg, "前台丢了要重夺"


def test_hider_counts_capture_changes():
    """争抢次数必须可诊断(现场排障需要)。"""
    src = inspect.getsource(cursor_mod.CursorHider.__init__)
    assert "_capture_changed" in src


# --- BUG B: 焦点监控 --------------------------------------------------

def test_top_hwnd_resolved_dynamically():
    """★★★ 顶层 hwnd 必须**每次动态解析**, 不能缓存。

    实测: 缓存的 _top_hwnd 是窗口成型前记录的错误句柄
    (top=8259710, 真实根窗口 2753054), 导致 fg == top 恒 False,
    整个焦点监控失效 —— 这是"程序窗口前台时副机键盘泄漏"的隐藏根因。
    """
    tick = inspect.getsource(gui_mod.ComBridgeGui._focus_watch_tick)
    assert "GetAncestor" in tick, "tick 里必须现场解析顶层窗口"
    # 不得再用"缓存优先"的写法
    assert "getattr(self, \"_top_hwnd\", 0) or" not in tick, \
        "不得再依赖缓存的 _top_hwnd"


def test_focus_watch_yields_grace_when_crossing():
    """★★★ 穿越态下焦点宽限期必须让位。

    用户实测: "程序窗口处于前台时, 副机的键盘输入依然会同步到主机程序
    的窗口中"。GUI 在前台 = 键盘钩子失效 = 8 秒宽限等于 8 秒键盘泄漏。
    """
    tick = inspect.getsource(gui_mod.ComBridgeGui._focus_watch_tick)
    assert "crossing" in tick, "必须判定穿越态"
    assert "is_outside" in tick, "穿越态判据来自 edge.is_outside"
    assert "(not crossing)" in tick, "宽限期只在非穿越态生效"


# --- BUG C: 次级窗口刷新链 --------------------------------------------

def test_reopen_restarts_tick():
    """★ 窗口重开必须重启刷新循环。

    根因: hide() 调 hide_children() 会 after_cancel 掉 tick 链, 而
    show() 原来只做 deiconify —— 定时器一旦取消就永远没人再排,
    窗口重开后界面冻结在旧值。
    """
    base_show = inspect.getsource(gw_mod._BaseWindow.show)
    assert "on_reopen" in base_show, "show() 必须通知子类恢复循环"
    assert hasattr(gw_mod._BaseWindow, "on_reopen")
    for cls in (gw_mod.StatusWindow, gw_mod.LogWindow):
        assert hasattr(cls, "on_reopen"), f"{cls.__name__} 必须实现 on_reopen"
        src = inspect.getsource(cls.on_reopen)
        assert "_tick" in src, "on_reopen 必须重启 tick"


def test_tick_guard_does_not_block_reopen():
    """on_reopen 必须能识别"定时器已停"并重启(不能盲目返回)。"""
    src = inspect.getsource(gw_mod.StatusWindow.on_reopen)
    assert "_timer is None" in src, "必须先判断定时器确实停了"


# --- BUG D: 水位软阈值 ------------------------------------------------

def test_watermark_control_is_smooth_sliding():
    """★★★ 水位控制必须是"每轮小步滑窗", 不是"越限就大砍一刀"。

    用户证据: 爆音阈值始终紧跟水位上限(300/200/80ms)
    -> 噪音来自执行上限的机制本身(一次性丢弃 = 硬切 = 爆音)。

    新机制有两层:
      a) **比例项 P**: 水位超过目标 -> ratio 略增 -> 重采样器多消费,
         完全不丢样本(最快无感);
      b) **小步 trim**: 仍超软阈值时, 每轮最多丢 2ms(断口极小)。
    不再有"budget 叠加超调量一次猛灌"的机制(那会产出一大段 PCM,
    WASAPI 放不下的部分被截掉 = 又一次硬切)。
    """
    src = inspect.getsource(srv_mod.Server._render_loop)
    # a) 比例项 —— 用**真实 WASAPI** 实测有效:
    #    供给快 2% 时 ratio=1.008 把水位峰值从 55ms 压到 20ms 且零丢弃
    #    (tools/verify_ratio_real_wasapi.py)。
    #    机制: pop_aligned 的 input_budget = max_frames × ratio,
    #    ratio 变大 -> 队列真正多被取出, 而产出帧数不变 -> 水位下降。
    assert "RATIO_P_MAX" in src, "必须有比例项让重采样器先行消化"
    assert "over" in src, "比例项应随超调程度增长"
    # b) 小步滑窗作为**兜底**(阈值放宽到 1.8x, 日常由比例项负责)
    assert "max_skip_ms" in src, "trim 必须限幅(小步)"
    assert "1.8" in src, "兜底阈值应为 1.8x 目标"
    # 不应再有"叠加超调量猛灌预算"
    assert "over_ms" not in src, (
        "不得再用 over_ms 叠加预算(会一次猛灌再截断)")


def test_hard_guard_still_present():
    """240ms 硬兜底必须保留(处理极端失控)。"""
    src = inspect.getsource(srv_mod.Server._render_loop)
    assert "trim_to" in src, "硬兜底仍要一次性弃帧"
    assert "max_level_ms" in src
