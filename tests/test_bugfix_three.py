# -*- coding: utf-8 -*-
"""BUG1/BUG2/BUG3 三项修复的回归锁。

这三条是用户在**真机**(两台物理 Windows)上实测报出的问题, 所以本文件
的职责是: 把"修复所依赖的那条机制"钉死, 避免后人重构时无声改回去。
测试全部是纯逻辑/源码接线断言 —— 真机行为归 tools/ 下的活体验收脚本。

BUG1  鼠标进入副机后, 主机和副机键盘同步生效
      → 根因: 低级钩子回调里做转发工作, 超过 ~300ms 的 LowLevelHooksTimeout
        被 Windows **静默摘钩**; 摘钩后物理键不再被吞, 于是主机焦点应用
        收到一份、钩子自产/转发又给副机一份。修法: 回调只入队 +
        PostThreadMessage(Deskflow keyboardHookHandler 范式), 并加**重装自愈**。

BUG2  副机退回主机后, 副机光标依然存在不消失
      → 根因(实测 tools/diag_hider_state.py, 可稳定复现): 光标隐藏要求
        hider 窗口成为**前台窗口**, 而 Windows **静默拒绝**非前台进程的
        SetForegroundWindow。被拒时窗口矩形/可见性/SetCapture 看起来全都
        正常, 但该线程收不到任何鼠标消息(moves=0), ShowCursor 计数器永不
        提交 —— 光标留在屏幕上。修法: 用官方记载的 ALT 合成键解锁前台,
        并记录 `_activated` 供验收判定。
      → 次因: hide() 自身的 ±1px 抖动注入的回声可能落在注入窗口之外,
        被误判成"用户动了本机鼠标"而收起 hider。修法: 连续 N 条 + 落定静默期。

BUG3① 主机指针进入副机时方位略有偏差
      → 已实测排除换算误差: 全链路(host px → f → 65535 → agent px → 注入
        → 读回)误差 0px。本文件锁死"换算必须像素精确", 防止未来回归。

BUG3② 副机回退主机时方位完全不继承
      → 根因: 回程只发相对位移, 而主机光标被钉在**进入时**的位置, 落点
        永远是当初跳过来的那一点。修法: 回程时副机发 EV_MOUSE_RETURN
        (沿回传边的切向比例), 主机据此**绝对定位**(与去程 EV_MOUSE_PLACE 对称)。
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from com_bridge import cursor as cursor_mod
from com_bridge import hook as hook_mod
from com_bridge import input as input_mod
from com_bridge.agent import Agent
from com_bridge.input import (fraction_to_pixel, position_fraction,
                              virtual_desktop)
from com_bridge.protocol import (EV_KEY, EV_MOUSE_MOVE_REL, EV_MOUSE_PLACE,
                                 EV_MOUSE_RETURN, InputEvent)
from com_bridge.server import Server

ROOT = Path(__file__).resolve().parents[1]
HOOK_SRC = (ROOT / "com_bridge" / "hook.py").read_text(encoding="utf-8")
CURSOR_SRC = (ROOT / "com_bridge" / "cursor.py").read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    """去掉注释, 只看代码本体(注释里允许记载历史与根因)。"""
    out = []
    for line in src.splitlines():
        i = line.find("#")
        out.append(line if i < 0 else line[:i])
    return "\n".join(out)


# ===========================================================================
# BUG1 —— 钩子回调必须廉价, 且能自愈
# ===========================================================================

class TestBug1CallbackStaysCheap:
    def test_kbd_proc_does_not_call_handler_inline(self):
        """回调里不得出现 `self._on_key_event(...)` 直接调用(必须走队列)。"""
        tree = ast.parse(HOOK_SRC)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_kbd_proc")
        src = _strip_comments(ast.get_source_segment(HOOK_SRC, fn) or "")
        assert "_enqueue_key(" in src, "键盘事件必须入队"
        assert "_on_key_event(" not in src, \
            "回调里直接转发 -> 有超时被静默摘钩的风险(BUG1)"

    def test_message_loop_only_wakes_worker(self):
        """消息循环只唤醒转发线程, **绝不**在钩子线程里排空队列。

        ★★ 用户实测"鼠标好了但键盘仍两端生效"的根因: 系统只在钩子线程
        处理消息时才调钩子过程, 所以钩子线程一排空(网络发送), 回调较重的
        键盘钩子就得不到服务而超时被摘, 鼠标钩子因回调极轻而幸存。
        """
        tree = ast.parse(HOOK_SRC)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_run")
        src = ast.get_source_segment(HOOK_SRC, fn) or ""
        assert "WM_APP_KEY" in src
        assert "_key_wake.set()" in src, "只应唤醒独立转发线程"
        assert "_drain_key_queue()" not in src, \
            "钩子线程不得自己排空队列(会把键盘钩子拖到被摘)"

    def test_worker_delivers_all_events_in_order(self):
        """独立转发线程必须一条不丢且保序。"""
        import time as _time
        h = hook_mod.LowLevelHook(on_key_event=None)
        h._relay = True
        seen = []
        h._on_key_event = lambda *a: seen.append(a[2])
        import threading as _threading
        h._worker = _threading.Thread(target=h._key_worker_loop, daemon=True)
        h._worker.start()
        n = 50
        try:
            for i in range(n):
                h._enqueue_key(EV_KEY, 1, i & 0xFF, 0, 0, 0)
            deadline = _time.monotonic() + 3.0
            while _time.monotonic() < deadline and len(seen) < n:
                _time.sleep(0.02)
        finally:
            h._stop.set()
            h._key_wake.set()
            h._worker.join(timeout=2.0)
        assert len(seen) == n, f"丢失事件: {len(seen)}/{n}"
        assert seen == [i & 0xFF for i in range(n)], "必须保序"

    def test_worker_loop_isolated_from_hook_thread(self):
        """转发线程必须是**独立线程**(不是钩子线程)。"""
        src = inspect.getsource(hook_mod.LowLevelHook.start)
        assert "_key_worker_loop" in src, "start() 必须启动独立转发线程"
        assert hasattr(hook_mod.LowLevelHook, "_key_worker_loop")

    def test_thread_message_loop_handles_reinstall(self):
        """★★ 重装的线程位置 —— 两条实测硬约束的交集。

        (1) **必须在钩子线程执行(LL 钩子线程亲和)**: 系统把回调调用投到
            **安装线程**的消息队列。从别的线程装 = 装在没人泵的队列上 ——
            实测"句柄在但回调死"(installer 线程方案被推翻的原因)。
        (2) **不能在消息处理中途同步执行**(用户真机"重装3次全无效"):
            泵停顿, 连续重装请求下系统判定超时并**静默摘钩**。
        两者交集的唯一形状: **在消息循环头(每次 GetMessage 之前)检查
        标志执行** —— 亲和正确(钩子线程), 泵停顿只有一次微秒级
        unhook/rehook, 且执行期间新到的请求自然合并到下一轮。
        """
        tree = ast.parse(HOOK_SRC)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_run")
        src = ast.get_source_segment(HOOK_SRC, fn) or ""
        assert "WM_APP_REINSTALL" in src
        # 收到重装消息只置标志
        assert "_reinstall_requested = True" in src
        # 执行点必须在 GetMessageW **之前**(循环头)
        assert "self._reinstall()" in src
        assert src.index("self._reinstall()") < src.index("GetMessageW"), \
            "重装必须在循环头(GetMessage 前)执行"
        # 不得有安装线程(实测亲和性错误: 句柄在但回调死)
        assert not hasattr(hook_mod.LowLevelHook, "_installer_loop")
        # 钩子线程心跳打点必须存在(判定泵消息是否卡死的唯一手段)
        assert "hook_pump_mono" in HOOK_SRC

    def test_reinstall_cannot_raise(self):
        """重装失败只能置 installed=False —— 抛出去会让钩子线程整个死掉。"""
        tree = ast.parse(HOOK_SRC)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_reinstall")
        src = ast.get_source_segment(HOOK_SRC, fn) or ""
        assert "except Exception" in src

    def test_server_self_heals_before_degrading(self):
        src = _strip_comments(inspect.getsource(Server._on_input_event))
        assert "request_reinstall()" in src
        assert src.index("request_reinstall()") < \
            src.index("_pin_fallback = True"), \
            "必须先请求重装, 只有反复失败才退化为钉住(BUG1 的成因)"

    def test_server_reinstall_counter_exists(self):
        assert Server.HOOK_REINSTALL_LIMIT >= 1
        assert "_hook_reinstalls" in inspect.getsource(Server.__init__)

    def test_enqueue_posts_thread_message(self):
        h = hook_mod.LowLevelHook(on_key_event=None)
        h._relay = True
        h._on_key_event = lambda *a: None
        h._enqueue_key(EV_KEY, 1, 0x41, 0x1E, 0, 0)
        # 回调路径只入队 + 唤醒(不再依赖 PostThreadMessageW)
        assert h._key_queue, "必须入队"
        assert h._key_wake.is_set(), "必须唤醒转发线程"

    def test_hider_must_steal_foreground(self):
        """★★★ hider **必须**抢前台(2026-10-19 结论反转)。

        旧测试断言"抢前台杀死钩子", 依据是 tkinter 主窗口的实测。
        重新实测 hider 窗口(WS_EX_TRANSPARENT|TOOLWINDOW):
            基线 kbd_calls +2 / hider 抢前台后 kbd_calls +2 —— 钩子照样可达。
        而 MSDN 规定 SetCapture 只在"按钮按下或窗口在前台"时生效,
        所以不抢前台 -> 捕获无效 -> 光标不消失(用户报的核心问题)。
        详见 tools/exp_hider_foreground_hook.py 与 cursor.py 的注释。
        """
        import inspect as _insp
        from com_bridge import cursor as _cur
        hide_src = _insp.getsource(_cur.CursorHider._do_hide)
        assert "SetForegroundWindow" in hide_src, \
            "must steal foreground for SetCapture to work"
        show_src = _insp.getsource(_cur.CursorHider._do_show)
        assert "SetForegroundWindow" in show_src, \
            "must restore foreground on release"

    def test_show_restores_foreground(self):
        """show() 必须还原前台(因为 hide 抢过)。"""
        show_src = _strip_comments(
            inspect.getsource(cursor_mod.CursorHider._do_show))
        assert "SetForegroundWindow" in show_src
        assert "_stolen_foreground" in show_src
        assert "__never_used__" not in show_src

    def test_no_chain_head_reclaim_machinery(self):
        """★★ **不得存在**"链首抢占"机制 —— 那是被推翻的错误推论。

        "谁后装谁赢"的翻转只在本进程合成实验里成立; 真实世界的反证是
        Deskflow: 它**只装一次**钩子, 从不抢占, 却长期有效。若真有"链首
        被抢"问题, Deskflow 早该失效了。

        因此以下机制一律**不得出现**(它们会无端反复重装钩子、注入探针键,
        反而制造"时好时坏"):
          - reclaim_head / verify_head
          - _kbd_watch 轮询线程
          - kbd_bypassed 判据
        """
        assert not hasattr(hook_mod.LowLevelHook, "reclaim_head")
        assert not hasattr(hook_mod.LowLevelHook, "verify_head")
        assert not hasattr(Server, "_kbd_watch_loop")
        assert not hasattr(Server, "_start_kbd_watch")
        assert "kbd_bypassed" not in _strip_comments(
            inspect.getsource(hook_mod.LowLevelHook.diag))
        # _acquire_control 只切开关, 不重装
        src = _strip_comments(inspect.getsource(Server._acquire_control))
        assert "set_relay(True)" in src
        assert "reclaim_head" not in src and "request_reinstall" not in src

    def test_set_relay_is_pure_flag_like_deskflow(self):
        """set_relay 与 Deskflow 的 setMode 同型: 纯布尔开关, 无副作用。

        Deskflow 的 setMode 只改一个全局枚举; 切换控制权时绝不重装钩子。
        我们必须保持一致 —— 否则每次穿越都有一段"钩子不在"的窗口,
        按键会漏到主机(正是要修的 BUG1 的形态)。
        """
        src = _strip_comments(inspect.getsource(hook_mod.LowLevelHook.set_relay))
        assert "request_reinstall" not in src
        assert "SetWindowsHookEx" not in src

    def test_reinstall_only_via_explicit_request(self):
        """重装只能由**检出异常**的路径发起(泄漏检测/健康自检),
        不得由"切换控制权"发起。"""
        acquire = _strip_comments(inspect.getsource(Server._acquire_control))
        release = _strip_comments(inspect.getsource(Server._release_control))
        for src, name in ((acquire, "_acquire_control"),
                          (release, "_release_control")):
            assert "request_reinstall" not in src, \
                f"{name} 不得发起重装(与 Deskflow 不符)"

    def test_release_probes_keyboard_hook(self):
        """★★ 交还控制权时必须复核键盘钩子(补两道检测的公共盲区)。

        穿越期间键盘钩子死亡的两条路, 现有检测**全都够不着**:
          a) 被系统静默摘除(约 300ms 超时, 句柄不置空):
             鼠标钩子通常还活着 -> 光标不自检报警;
             泄漏检测要 RawInput 键盘事件, 但摘除后键直达主机,
             RawInput 能看到 -> 泄漏检测其实够得着(但只在按键时)。
          b) 被别的程序先吞掉(键到不了我们):
             RawInput 也收不到 -> **泄漏检测同样失明**;
             kbd_calls 恒 0 但没有任何报警路径。
        用户实测正是 (b): "鼠标有数字 键盘0 回调0", 且两台同时生效,
        而日志里一个字都没有。
        修法: _release_control(每轮穿越必经)里 probe 一次, 失败重装。
        """
        src = _strip_comments(inspect.getsource(Server._release_control))
        assert "_verify_kbd_on_release" in src, \
            "交还控制权时必须复核键盘钩子(唯一的每轮必经点)"
        v = _strip_comments(inspect.getsource(Server._verify_kbd_on_release))
        assert "probe_keyboard_hook" in v, "必须用探针判据, 不依赖用户打字"
        assert "request_reinstall" in v, "失败必须重装一次(下一轮生效)"
        assert "return" in v and "installed" in v, \
            "钩子未装时必须早退, 不能在残骸上探测"

    def test_release_probe_does_not_alarm_on_none(self):
        """探针返回 None(注入本身失败)时不得报警 —— 判据不可用不制造噪声。"""
        v = _strip_comments(inspect.getsource(Server._verify_kbd_on_release))
        # 结构上: 只有 probe is False 分支里才有报警/重装
        assert "probe is False" in v

    def test_gui_shows_kbd_calls_and_wizards_dead_callback(self):
        """GUI 必须显示键盘回调次数, 并在回调恒 0 时给出可操作提示。

        ★ 提示文案已从"键被其它程序先收走"改为"窗口在前台":
          后者才是实测确认的真实原因(tools/exp_window_vs_hook_precise.py)。

        ★ 2026-10-19 重设计后: 状态文案搬到了 collect_status(次级窗口),
          _update_stats 只是兼容旧接口的薄委托 —— 所以断言要查前者。
        """
        import com_bridge.gui as gui_mod
        src = _strip_comments(
            inspect.getsource(gui_mod.ComBridgeGui.collect_status))
        assert "kbd_calls" in src, "状态必须显示键盘回调次数"
        assert "窗口在前台" in src, \
            "提示必须指向真实原因(前台焦点), 而不是已被证伪的'别的程序抢'"

    def test_gui_logs_to_file_when_nodisplay(self):
        """--noconsole 打包下 stdout 是无效句柄, 诊断必须落到文件。

        实测教训: 用户真机上所有 [server] 打印(含'键盘钩子自检失败')
        全部丢失, 排障时一个字的证据都拿不到。gui_log.install() 用
        os.dup2 把 fd1/2 重定向到 %LOCALAPPDATA%/ComBridge/logs。
        """
        import com_bridge.gui_log as gl
        # install() 在 isatty 时不动(console 版保留实时输出)
        src = inspect.getsource(gl.install)
        assert "isatty" in src
        assert "dup2" in src, "必须重定向 fd, 改 sys.stdout 覆盖不了已有对象"
        # cli 的 gui 分支必须先装日志再起 GUI
        import com_bridge.cli as cli_mod
        gsrc = _strip_comments(inspect.getsource(cli_mod._cmd_gui))
        assert "gui_log" in gsrc and "gui_main" in gsrc
        assert gsrc.index("gui_log") < gsrc.index("gui_main"), \
            "日志安装必须先于 GUI 启动"

    def test_reinstall_keeps_both_handles(self):
        """重装必须同时重装鼠标与键盘钩子, 且顺序与首次安装一致。

        两个钩子是独立句柄, 只重装一个会让另一个留旧句柄 ——
        "鼠标正常但键盘失效"的一种形态。
        """
        src = _strip_comments(inspect.getsource(hook_mod.LowLevelHook._reinstall))
        assert "WH_MOUSE_LL" in src
        assert "WH_KEYBOARD_LL" in src
        assert "_unhook_handles()" in src, "重装前必须先摘, 否则拿到旧句柄"

    def test_keyboard_hook_death_is_detected_separately(self):
        """★★ 2026-10-19/20 重写: 键盘钩子"失效检测"的正确形态。

        历史(已被物理按键实测推翻, 保留教训):
          旧判据是"relay=True 时若 RawInput 仍报来键盘事件 = 钩子没吞住",
          并据此重装钩子 + 暂停键盘转发。但真机实测证明:
            **hider 持前台 + 注册键盘 RawInput(RIDEV_INPUTSINK)** 时,
            本进程的 WH_KEYBOARD_LL **收不到任何键**(物理键 kbd_calls+0,
            而 RawInput+36) —— "RawInput 有键、钩子无键"是该组合下的
            **正常表现**, 不是钩子坏了。据此触发重装必然无效(重装改变
            不了前台归属), 只会把正常的键盘转发误停(用户日志里
            "自愈 3 次未生效 -> 暂停键盘转发"即此)。
          根因修复: 不再注册键盘 RawInput(register_keyboard=False +
          RIDEV_REMOVE), 键盘事件由低级钩子自产。原泄漏检测随之废弃。

        新断言:
          1. 键盘 RawInput 注册被移除(server 与 agent 都是);
          2. 原"RawInput 键盘泄漏检测"代码路径不存在;
          3. Deskflow 对照: 它从不注册 RawInput, 同样抢前台却无此问题。
        """
        # 1) server/agent 都不再注册键盘 RawInput
        start_src = inspect.getsource(Server._start_input)
        assert "register_keyboard=False" in start_src, \
            "server 必须关闭键盘 RawInput 注册(物理按键实测的根因修复)"
        agent_src = inspect.getsource(Agent._make_return_listener) \
            if hasattr(Agent, "_make_return_listener") else None
        if agent_src is None:
            # 回传监听器的构造点在 agent 源码里, 直接全文检索
            agent_src = (ROOT / "com_bridge" / "agent.py").read_text(
                encoding="utf-8")
        assert "register_keyboard=False" in agent_src, \
            "agent 必须关闭键盘 RawInput 注册"
        # 2) RawInputListener 支持 register_keyboard 开关 + 显式 REMOVE
        assert "register_keyboard" in inspect.getsource(
            input_mod.RawInputListener.__init__)
        run_src = inspect.getsource(input_mod.RawInputListener.run)
        assert "RIDEV_REMOVE" in run_src, \
            "必须显式注销键盘 RawInput(热重载/残留注册场景)"
        # 3) 原泄漏检测已废弃: _on_raw_input_event 不得再**调用**自愈
        #    (docstring 里的历史注记允许保留, 用 AST 只看真实调用)
        import ast as _ast
        tree = _ast.parse(
            (ROOT / "com_bridge" / "server.py").read_text(encoding="utf-8"))
        fn = next(n for n in _ast.walk(tree)
                  if isinstance(n, _ast.FunctionDef)
                  and n.name == "_on_raw_input_event")
        calls = [c.func.id for c in _ast.walk(fn)
                 if isinstance(c, _ast.Call)
                 and isinstance(c.func, _ast.Name)]
        assert "_start_kbd_heal" not in calls, \
            "RawInput 键盘泄漏检测已被实测证伪并移除, 不得恢复"

    def test_heal_loop_verifies_not_just_reinstalls(self):
        """自愈不能"重装完就算" —— 必须**验证**回调真的可达。

        重装只是让我们排到链首; 若别的程序随后也重装(或我们没抢到链首),
        回调依然不可达。用 probe_keyboard_hook() 才能确定性地判定修好了。
        """
        src = inspect.getsource(Server._kbd_heal_loop)
        assert "request_reinstall()" in src
        assert "probe_keyboard_hook()" in src
        assert Server.KBD_HEAL_ATTEMPTS >= 1
        assert Server.KBD_HEAL_INTERVAL_S > 0

    def test_heal_runs_off_the_input_path(self):
        """自愈必须在后台线程跑, 绝不阻塞输入转发。"""
        src = inspect.getsource(Server._start_kbd_heal)
        assert "Thread(" in src
        assert "daemon=True" in src
        assert "_kbd_healing" in src, "必须防止重复启动多轮自愈"

    def test_probe_uses_tagged_injection(self):
        """自检必须用**带 TAG** 的注入。

        回调里 TAG 分支在任何过滤之前, 所以只要回调被调用就会计入
        injected_passed —— 既能证明"链通了", 又不会吞掉/转发任何东西。
        """
        src = inspect.getsource(hook_mod.LowLevelHook.probe_keyboard_hook)
        assert "InputInjector.key" in src
        assert "injected_passed" in src
        assert "TAG" in src

    def test_kbd_calls_counted_before_any_filter(self):
        """kbd_calls 必须在回调**最前面**自增。

        否则"回调没被调用"与"被调用但全部放行"无法区分 —— 用户实测的
        kbd_eaten 恒为 0 正属于前者, 却被误读成后者。
        """
        src = _strip_comments(inspect.getsource(hook_mod.LowLevelHook._kbd_proc))
        assert "kbd_calls" in src
        i_calls = src.index("kbd_calls")
        for later in ("code < 0", "InputInjector.TAG", "_relay"):
            if later in src:
                assert i_calls < src.index(later), \
                    f"kbd_calls 必须早于 {later}"

    def test_leak_detector_retired_after_rawinput_fix(self):
        """★★ 2026-10-20: 原"RawInput 键盘泄漏检测"三件套已整体废弃。

        物理按键实测(用户亲手按, RawInput 计数作证)证明其判据根本错误:
        hider 持前台 + 键盘 RawInput 并存时, 钩子收不到任何键是**系统
        正常行为**, 据此触发"重装+暂停键盘转发"是误诊(重装改变不了
        前台归属, 只会把好端端的键盘转发停掉)。根因修复是移除键盘
        RawInput 注册, 因此:
          - 泄漏计数器(_kbd_leaks)与放行键排除(PASSTHROUGH_VKS)不再有
            消费者, 相关断言一并退役;
          - PASSTHROUGH_VKS 本身保留(钩子回调用它放行指示灯键),
            只是 RawInput 泄漏判据不再引用它。
        本测试锁死"不得恢复"这三件已证伪的机制。
        """
        import com_bridge.server as server_mod
        # 用 AST 判断"无真实调用"(docstring 里的历史注记允许保留)
        import ast as _ast2
        _srv_tree = _ast2.parse(
            (ROOT / "com_bridge" / "server.py").read_text(encoding="utf-8"))
        _fn = next(n for n in _ast2.walk(_srv_tree)
                   if isinstance(n, _ast2.FunctionDef)
                   and n.name == "_on_raw_input_event")
        _calls = [c.func.id for c in _ast2.walk(_fn)
                  if isinstance(c, _ast2.Call)
                  and isinstance(c.func, _ast2.Name)]
        assert "_start_kbd_heal" not in _calls
        assert not hasattr(server_mod.Server, "KBD_LEAK_REINSTALL_LIMIT"), \
            "泄漏重装上限常量已随机制废弃"

    def test_diag_exposes_hooks_separately(self):
        """诊断必须能区分"只有键盘钩子掉了"(否则现场无法自查)。"""
        src = inspect.getsource(hook_mod.LowLevelHook.diag)
        assert "kbd_hook_alive" in src
        assert "mouse_hook_alive" in src
        assert "worker_alive" in src

    def test_probe_tolerates_own_jitter(self):
        """健康自检必须有位移容差, 否则 hider 自己的 ±1px 提交抖动会被
        误判成"钩子被摘"而反复请求重装。"""
        assert Server.HOOK_PROBE_TOLERANCE_PX >= 2
        src = _strip_comments(inspect.getsource(Server._on_input_event))
        assert "HOOK_PROBE_TOLERANCE_PX" in src
        assert "_is_own_injection()" in src

    def test_reinstall_counter_resets_per_crossing(self):
        """每轮穿越重新计数: 偶发摘钩不该跨轮累积成永久退化。"""
        src = _strip_comments(inspect.getsource(Server._on_input_event))
        assert "_hook_reinstalls = 0" in src


# ===========================================================================
# BUG2 —— 1x1 hider 窗口必须跟随光标
# ===========================================================================

class TestBug2HiderActivation:
    """BUG2 的根因已修正为"激活但**不抢前台**"(见 test_hider_never_steals_foreground)。

    历史: 曾经认为必须抢前台(SetForegroundWindow)才能让 ShowCursor 计数器
    提交。实测证明 SetActiveWindow + SetCapture + 抖动注入已经足够,
    而抢前台会**杀死键盘钩子**(kbd_calls 4->0) —— 代价远大于收益。
    """

    def test_activation_state_recorded(self):
        """必须记录 _activated —— 这是 hide 是否真的生效的唯一可观测判据。"""
        assert "_activated" in inspect.getsource(
            cursor_mod.CursorHider.__init__)
        tree = ast.parse(CURSOR_SRC)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_do_hide")
        src = ast.get_source_segment(CURSOR_SRC, fn) or ""
        assert "_activated = " in src
        # 判据必须是 GetActiveWindow(不是 GetForegroundWindow —— 我们不抢前台)
        assert "GetActiveWindow" in src

    def test_both_roles_use_fullscreen(self):
        """两个角色都用全屏窗 —— 副机光标会移动, 1x1 窗盖不住它。"""
        tree = ast.parse(CURSOR_SRC)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_do_hide")
        src = _strip_comments(ast.get_source_segment(CURSOR_SRC, fn) or "")
        # 只应有一种 SetWindowPos 定位(全屏), 不应再有 1x1 跟随分支
        assert "d.width, d.height" in src
        assert "_follow_cursor" not in src


class TestBug2PhysicalMotionRobustness:
    """次因: 孤立合成消息/抖动回声不得收起 hider。"""

    def test_requires_consecutive_physical_messages(self):
        assert cursor_mod.CursorHider.PHYSICAL_STREAK_N >= 2
        tree = ast.parse(CURSOR_SRC)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_proc")
        src = _strip_comments(ast.get_source_segment(CURSOR_SRC, fn) or "")
        assert "_physical_streak" in src
        assert "PHYSICAL_STREAK_N" in src

    def test_hide_settle_period_exists(self):
        assert cursor_mod.CursorHider.HIDE_SETTLE_S > 0
        assert "_hide_mono" in inspect.getsource(
            cursor_mod.CursorHider.__init__)

    def test_injected_motion_resets_streak(self):
        tree = ast.parse(CURSOR_SRC)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_proc")
        src = _strip_comments(ast.get_source_segment(CURSOR_SRC, fn) or "")
        assert "_is_injected_motion()" in src
        assert "_do_show()" in src

    def test_streak_logic_is_pure(self):
        """直接验证计数语义: 注入/静默期都清零, 连续物理才累加。"""
        h = cursor_mod.CursorHider(hide_on_motion=True)
        h._shown = True
        h._hide_mono = 0.0            # 静默期早已过去
        h._physical_streak = 0
        # 模拟 _proc 的计数分支
        for injected, expect in ((True, 0), (False, 1), (False, 2),
                                 (True, 0), (False, 1)):
            if injected or not h._shown:
                h._physical_streak = 0
            else:
                h._physical_streak += 1
            assert h._physical_streak == expect


# ===========================================================================
# BUG3① —— 方位换算必须像素精确(已实测, 锁死防回归)
# ===========================================================================

class TestBug3EntryIsPixelExact:
    @pytest.mark.parametrize("extent", [768, 900, 1080, 1200, 1440, 2160])
    def test_fraction_roundtrip_is_exact(self, extent):
        """host px -> f -> 65535 量化 -> 还原 必须逐像素相等。

        这是"进入方位略有偏差"最可能的候选来源, 实测**零误差** ——
        该测试的作用是把这个结论钉住: 若哪天它开始失败, 偏差才有数学根因。
        """
        for v in range(extent):
            f = (v + 0.5) / extent
            q = int(round(min(max(f, 0.0), 1.0) * 65535))
            f2 = q / 65535.0
            assert input_mod._frac_to_axis(f2, extent, 0) == v

    def test_frac_to_axis_equals_floor_formula(self):
        """逆变换必须是 floor(f*extent), **不能**用 round()。

        Python 的 round() 是 banker's rounding(半值取偶): 在 f*extent 恰为
        整数时会算错(e=3, f=1/3 → round(0.5)=0, 正确值是 1)。这条断言把
        "用 floor 不用 round"钉死。
        """
        for extent in (1, 2, 3, 5, 7, 9, 1080, 1920, 65535):
            for f in (0.0, 0.25, 1.0 / 3, 0.5, 0.75, 0.99999, 1.0):
                assert input_mod._frac_to_axis(f, extent, 0) == int(f * extent)

    def test_frac_to_axis_rejects_bankers_rounding(self):
        """反例锁: e=3, f=1/3 必须得 1(而不是 round 给出的 0)。"""
        assert input_mod._frac_to_axis(1.0 / 3, 3, 0) == 1
        assert int(round((1.0 / 3) * 3 - 0.5)) == 0   # 说明 round 版本是错的

    def test_entry_lands_inside_edge(self):
        """进入点必须在边**内侧**(avoidJumpZone), 否则进入瞬间就回弹。"""
        d = virtual_desktop()
        for side in ("left", "right", "top", "bottom"):
            x, y = fraction_to_pixel(0.5, side)
            assert d.left <= x <= d.right - 1
            assert d.top <= y <= d.bottom - 1

    def test_position_fraction_is_stable_at_midpoint(self):
        d = virtual_desktop()
        f = position_fraction(d.left, d.top + d.height // 2, "right")
        assert 0.4 < f < 0.6


# ===========================================================================
# BUG3② —— 回程方位必须继承(EV_MOUSE_RETURN)
# ===========================================================================

class TestBug3DpiAwareness:
    """★★ BUG3① 的真正根因: 进程必须声明 per-monitor DPI 感知。

    未声明时进程是 DPI_UNAWARE, Windows 会把 GetSystemMetrics/GetCursorPos/
    SendInput 的坐标按显示缩放**虚拟化**。本机 96 DPI(100%)看不出问题,
    但副机 2560x1600 常是高 DPI 屏 —— 比例换算得到的"像素"与真实屏幕像素
    不是同一套坐标系, 落点因此偏移 = "方位没有等比例对齐"。
    """

    def test_enable_function_exists(self):
        assert hasattr(input_mod, "_enable_dpi_awareness")
        assert hasattr(input_mod, "dpi_awareness")

    def test_declared_at_import_time(self):
        """★★ 必须在 **input 模块导入时**就声明 —— 这是唯一无法被绕过的点。

        `input` 拥有全部坐标 API, 导入早于一切使用。若只在 cli.main() 里
        声明, 那么 `Server()`/`Agent()` 被直接构造(测试、嵌入式调用)时
        仍会是 DPI-unaware, 坐标被系统虚拟化。
        """
        assert hasattr(input_mod, "_DPI_MODE"), "导入时应已声明 DPI 感知"
        assert "unaware" not in str(input_mod._DPI_MODE), \
            f"导入时声明失败: {input_mod._DPI_MODE}"
        assert input_mod.dpi_awareness() in ("per-monitor", "system")

    def test_enable_is_idempotent(self):
        """重复调用不得报"设置失败"(Windows 对已设置的再设置会返回失败)。"""
        a = input_mod._enable_dpi_awareness()
        b = input_mod._enable_dpi_awareness()
        assert "设置失败" not in a and "设置失败" not in b
        assert "unaware" not in a and "unaware" not in b

    def test_cli_main_declares_dpi_first(self):
        """必须在 cli.main() 的最前面声明 —— 早于任何坐标 API。"""
        from com_bridge.cli import main as cli_main
        src = inspect.getsource(cli_main)
        assert "_enable_dpi_awareness" in src
        # 必须早于 argparse(以及任何可能的坐标调用)
        assert src.index("_enable_dpi_awareness") < src.index("ArgumentParser")

    def test_gui_main_also_declares(self):
        """GUI 直接入口也要兜底声明(有人会绕过 CLI 启动)。"""
        from com_bridge.gui import main as gui_main
        src = inspect.getsource(gui_main)
        assert "_enable_dpi_awareness" in src

    def test_declaration_actually_takes_effect(self):
        """调用后查询结果不应再是 unaware(本机实测变 per-monitor)。"""
        mode = input_mod._enable_dpi_awareness()
        assert "unaware" not in mode, f"DPI 声明失败: {mode}"
        assert input_mod.dpi_awareness() in ("per-monitor", "system"), \
            f"查询到 {input_mod.dpi_awareness()}"

    def test_physical_metrics_are_sane(self):
        """声明后读到的尺寸必须是合理的物理像素(不是 0、不是异常小)。"""
        input_mod._enable_dpi_awareness()
        d = input_mod.virtual_desktop()
        assert d.width >= 640 and d.height >= 480, f"尺寸异常: {d}"


class TestBug3ReturnPlacement:
    def test_protocol_constant_exists_and_is_distinct(self):
        assert EV_MOUSE_RETURN != EV_MOUSE_PLACE
        assert EV_MOUSE_RETURN != EV_MOUSE_MOVE_REL

    def test_return_event_is_not_mouse(self):
        """绝不能计入 is_mouse —— 否则会被当成用户输入再转发, 形成回环。"""
        ev = InputEvent(EV_MOUSE_RETURN, 0, 0, 100, 0, 0)
        assert not ev.is_mouse
        assert not ev.is_key
        assert not InputEvent(EV_MOUSE_PLACE, 0, 0, 100, 0, 0).is_mouse

    def test_agent_sends_return_place_before_move(self):
        """agent 必须在回传那条位移**之前**发出方位事件。

        回传判定在 `_on_input_event` 的 EV_MOUSE_MOVE_REL 分支里(那里能
        拿到触发回传的位移方向), 所以修复也在那里。
        """
        src = _strip_comments(inspect.getsource(Agent._on_input_event))
        assert "_send_return_place(" in src
        assert src.index("_send_return_place(") < src.index("self._input_tx.send(ev)")

    def test_return_event_path_also_sends_place(self):
        """副机接了物理鼠标时走 _on_return_event, 同样要带方位。"""
        src = _strip_comments(inspect.getsource(Agent._on_return_event))
        assert "_send_return_place(" in src

    def test_agent_return_place_uses_return_edge_side(self):
        src = inspect.getsource(Agent._send_return_place)
        assert "position_fraction" in src
        assert "_return_edge.side" in src

    def test_agent_return_place_quantizes_to_16bit(self):
        src = inspect.getsource(Agent._send_return_place)
        assert "65535" in src

    def test_server_positions_absolutely_on_return(self):
        """server 收到 EV_MOUSE_RETURN 必须**绝对定位**(而非相对累积)。"""
        src = _strip_comments(inspect.getsource(Server._on_return_event))
        assert "EV_MOUSE_RETURN" in src
        assert "fraction_to_pixel" in src
        assert "move_absolute" in src

    def test_server_uses_its_own_side_axis(self):
        """主机按**自己的跨越边**还原切向坐标(与去程对称)。"""
        src = inspect.getsource(Server._on_return_event)
        assert "fraction_to_pixel(f, self.side)" in src

    def test_return_place_sent_only_once_per_crossing(self):
        """回程方位每条穿越只能发一次。

        `_at_return_edge` 越过之后会**持续**返回 True(边缘态一直成立),
        不设闸就会每个事件都发一条 EV_MOUSE_RETURN, 主机会被反复绝对
        定位、反而钉死在同一个切向坐标上。
        """
        src = _strip_comments(inspect.getsource(Agent._on_input_event))
        assert "_return_place_sent" in src
        assert "if not self._return_place_sent:" in src

    def test_return_place_flag_reset_on_new_crossing(self):
        """进入副机(交接)时必须复位, 否则第二次穿越永远不发方位。"""
        src = _strip_comments(inspect.getsource(Agent._on_input_event))
        assert "self._return_place_sent = False" in src
        assert "_return_place_sent" in inspect.getsource(Agent.__init__)

    def test_return_fraction_roundtrips_through_wire(self):
        """副机算出的比例经 65535 量化后, 主机必须还原到同一相对高度。"""
        d = virtual_desktop()
        for y in (0, 1, d.height // 4, d.height // 2,
                  d.height - 2, d.height - 1):
            f = position_fraction(d.left, y, "right")
            q = int(round(min(max(f, 0.0), 1.0) * 65535))
            f2 = q / 65535.0
            x2, y2 = fraction_to_pixel(f2, "right")
            # 切向是 y(左右边), 应还原到同一行
            assert y2 == max(d.top, min(d.bottom - 1, y)), \
                f"回程方位在 y={y} 处丢失"
