# -*- coding: utf-8 -*-
"""键盘链路回归: 钩子自产键盘事件(不依赖真实桌面/钩子安装)。

背景(tools/diag_hook_kbd_rawinput.py 四阶段实测):
  被键盘钩子(WH_KEYBOARD_LL)吞掉(return 1)的按键**不会**产生 RawInput ——
  与鼠标不对称(鼠标被吞后 RawInput 照常送达)。因此键盘事件必须由钩子回调
  自产(照搬 Deskflow keyboardHookHandler 的 PostThreadMessage 范式),
  否则穿越期间主机收不到任何物理键盘事件, 副机完全没键盘。
"""
from __future__ import annotations

import ctypes

import pytest

from com_bridge.hook import (
    KBDLLHOOKSTRUCT,
    LowLevelHook,
    PASSTHROUGH_VKS,
    _HOOKPROC,
    decide_key_eat,
)
from com_bridge.protocol import (
    EV_KEY,
    InputEvent,
    KEY_DOWN,
    KEY_EXTENDED,
    KEY_UP,
)
from com_bridge.server import Server

TAG = 0x43425247

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105


def feed_kbd(hook: LowLevelHook, vk: int, scan: int, flags: int,
             extra: int, msg: int, drain: bool = True) -> int:
    """构造 KBDLLHOOKSTRUCT 内存并直接调 _kbd_proc(模拟系统调用)。

    ★ 回调只**入队**(见 LowLevelHook._kbd_proc 的 300ms 超时说明), 所以
      默认再排空一次队列, 让断言看到最终交付的事件; 需要观察"回调本身做了
      什么"时传 drain=False。
    """
    info = KBDLLHOOKSTRUCT()
    info.vkCode = vk
    info.scanCode = scan
    info.flags = flags
    info.dwExtraInfo = extra
    ref = _HOOKPROC(hook._kbd_proc)
    try:
        r = ref(0, msg, ctypes.addressof(info))
    finally:
        del ref
    if drain:
        hook.drain_sync()
    return r


class TestDecideKeyEatBaseline:
    """既有吞/放行语义不得被自产改动破坏。"""

    def test_no_relay_passes(self):
        assert not decide_key_eat(False, 0x41, 0)

    def test_relay_eats_plain_key(self):
        assert decide_key_eat(True, 0x41, 0)

    def test_indicator_keys_pass(self):
        for vk in PASSTHROUGH_VKS:
            assert not decide_key_eat(True, vk, 0)

    def test_tagged_injection_passes(self):
        assert not decide_key_eat(True, 0x41, TAG)


class TestKbdProcEmitsEvents:
    """吞掉的键必须自产 InputEvent 字段(修"副机无键盘"的根因)。"""

    @pytest.fixture()
    def hook(self):
        h = LowLevelHook(on_key_event=None)
        produced = []
        h._relay = True
        h._on_key_event = lambda *a: produced.append(a)
        h._produced = produced
        return h

    def test_eaten_returns_1_and_emits_down(self, hook):
        r = feed_kbd(hook, 0x41, 0x1E, 0, 0, WM_KEYDOWN)
        assert r == 1
        assert len(hook._produced) == 1
        ev_type, flags, vk, scan, b, ts = hook._produced[0]
        assert ev_type == EV_KEY
        assert flags & KEY_DOWN and not flags & KEY_UP
        assert vk == 0x41
        assert scan == 0x1E          # 扫描码承载在 a 字段(与 RawInput 一致)

    def test_keyup_emits_up(self, hook):
        feed_kbd(hook, 0x41, 0x1E, 0, 0, WM_KEYUP)
        ev_type, flags, vk, scan, b, ts = hook._produced[0]
        assert flags & KEY_UP and not flags & KEY_DOWN

    def test_extended_key_flag(self, hook):
        # 右 Ctrl: LLKHF_EXTENDED(0x01) → KEY_EXTENDED(0x04)
        feed_kbd(hook, 0xA3, 0x1D, 0x01, 0, WM_KEYDOWN)
        assert hook._produced[0][1] & KEY_EXTENDED

    def test_syskeydown_down_syskeyup_up(self, hook):
        # Alt 组合键走 WM_SYSKEYDOWN/WM_SYSKEYUP
        feed_kbd(hook, 0x73, 0x3E, 0x20, 0, WM_SYSKEYDOWN)   # Alt+F4 down
        feed_kbd(hook, 0x73, 0x3E, 0x20, 0, WM_SYSKEYUP)     # Alt+F4 up
        assert hook._produced[0][1] & KEY_DOWN
        assert hook._produced[1][1] & KEY_UP

    def test_pass_through_no_event(self, hook):
        hook._relay = False
        r = feed_kbd(hook, 0x41, 0x1E, 0, 0, WM_KEYDOWN)
        assert r != 1                    # 放行
        assert not hook._produced        # 且不自产事件

    def test_passthrough_vks_never_reach_callback(self, hook):
        # Caps/Num/Scroll: 既不吞也不自产(本机指示灯同步)
        r = feed_kbd(hook, 0x14, 0x3A, 0, 0, WM_KEYDOWN)
        assert r != 1
        assert not hook._produced

    def test_tagged_injection_not_reemitted(self, hook):
        # 带 TAG 的是我们自己注入的(副机回声), 放行且绝不自产
        r = feed_kbd(hook, 0x41, 0x1E, 0, TAG, WM_KEYDOWN)
        assert r != 1
        assert not hook._produced

    def test_callback_exception_still_eats(self, hook):
        def boom(*a):
            raise RuntimeError("boom")

        hook._on_key_event = boom
        r = feed_kbd(hook, 0x41, 0x1E, 0, 0, WM_KEYDOWN)
        assert r == 1    # 异常不能影响吞的决断(否则按键落回主机)

    def test_vk_ff_skipped(self, hook):
        feed_kbd(hook, 0xFF, 0, 0, 0, WM_KEYDOWN)
        assert not hook._produced


class TestCallbackStaysCheap:
    """★ BUG1 回归: 钩子回调里**绝不能**做转发工作。

    Windows 对低级钩子有约 300ms 的 LowLevelHooksTimeout, 回调超时会被
    **静默摘钩**(无错误码、无回调)。摘钩后物理键不再被吞 —— 主机焦点应用
    收到一份, 钩子自产又转发给副机一份, 用户看到的就是
    "鼠标进入副机后, 主机和副机键盘同时生效"。

    ★★ 而且转发工作也不能放在**钩子线程**里: 系统只在钩子线程处理消息时
    才调用钩子过程, 钩子线程一忙, 回调较重的键盘钩子先超时被摘、鼠标钩子
    幸存 —— 症状恰好是"鼠标正常但键盘两端同时生效"。所以真正的工作在
    **独立转发线程**(_key_worker_loop)上做。
    """

    def test_callback_does_not_invoke_handler_inline(self):
        h = LowLevelHook(on_key_event=None)
        h._relay = True
        calls = []
        h._on_key_event = lambda *a: calls.append(a)
        # drain=False: 只看回调本身做了什么
        feed_kbd(h, 0x41, 0x1E, 0, 0, WM_KEYDOWN, drain=False)
        assert not calls, "回调里直接调用了转发函数 -> 有被摘钩的风险"
        assert h._key_queue, "回调必须把事件入队"
        # 同步排空(测试专用)后事件才真正交付
        h.drain_sync()
        assert len(calls) == 1
        assert not h._key_queue

    def test_callback_does_not_touch_hook_thread_loop(self):
        """回调只入队 + 置事件, 不得自己做网络/注入类工作。"""
        import ast as _ast
        from pathlib import Path as _Path
        src = (_Path(__file__).resolve().parents[1]
               / "com_bridge" / "hook.py").read_text(encoding="utf-8")
        tree = _ast.parse(src)
        fn = next(n for n in _ast.walk(tree)
                  if isinstance(n, _ast.FunctionDef) and n.name == "_enqueue_key")
        body = _ast.get_source_segment(src, fn) or ""
        assert "append(" in body
        assert "_key_wake.set()" in body, "必须唤醒独立转发线程"
        # 回调路径里不得出现锁(锁争用会阻塞回调 -> 超时)
        assert "with self._key_lock" not in body, \
            "回调里加锁 -> 争用时会阻塞回调 -> 被摘钩"

    def test_worker_thread_exists_and_is_started(self):
        h = LowLevelHook(on_key_event=None)
        assert hasattr(h, "_key_worker_loop")
        import inspect as _inspect
        start_src = _inspect.getsource(LowLevelHook.start)
        assert "_key_worker_loop" in start_src, \
            "必须启动独立转发线程, 否则工作又回到钩子线程"

    def test_worker_actually_delivers_events(self):
        """真启动 worker, 确认入队的事件会被它异步交付。"""
        import time as _time
        h = LowLevelHook(on_key_event=None)
        h._relay = True
        got = []
        h._on_key_event = lambda *a: got.append(a)
        h._worker = __import__("threading").Thread(
            target=h._key_worker_loop, daemon=True)
        h._worker.start()
        try:
            h._enqueue_key(EV_KEY, KEY_DOWN, 0x41, 0x1E, 0, 0)
            deadline = _time.monotonic() + 2.0
            while _time.monotonic() < deadline and not got:
                _time.sleep(0.02)
        finally:
            h._stop.set()
            h._key_wake.set()
            h._worker.join(timeout=2.0)
        assert got and got[0][2] == 0x41, f"worker 未交付事件: {got}"

    def test_enqueue_never_raises_without_thread(self):
        # 未 start() 时没有 worker/线程 id, 入队不得抛(否则吞的决断受影响)
        h = LowLevelHook(on_key_event=None)
        h._relay = True
        h._on_key_event = lambda *a: None
        r = feed_kbd(h, 0x41, 0x1E, 0, 0, WM_KEYDOWN, drain=False)
        assert r == 1

    def test_drain_swallows_handler_exception(self):
        h = LowLevelHook(on_key_event=None)
        h._relay = True

        def boom(*a):
            raise RuntimeError("boom")

        h._on_key_event = boom
        feed_kbd(h, 0x41, 0x1E, 0, 0, WM_KEYDOWN)   # 不得抛出
        assert not h._key_queue

    def test_queue_preserves_order(self):
        h = LowLevelHook(on_key_event=None)
        h._relay = True
        seen = []
        h._on_key_event = lambda *a: seen.append(a[2])
        feed_kbd(h, 0x41, 0x1E, 0, 0, WM_KEYDOWN, drain=False)
        feed_kbd(h, 0x42, 0x30, 0, 0, WM_KEYDOWN, drain=False)
        feed_kbd(h, 0x41, 0x1E, 0, 0, WM_KEYUP, drain=False)
        h.drain_sync()
        assert seen == [0x41, 0x42, 0x41]


class TestHookReinstall:
    """★ BUG1 自愈: 摘钩后能重装, 而不是永久退化为"键盘无人吞"。"""

    def test_reinstall_messages_defined(self):
        from com_bridge.hook import WM_APP_KEY, WM_APP_REINSTALL
        assert WM_APP_REINSTALL != WM_APP_KEY
        assert WM_APP_REINSTALL >= 0x8000   # WM_APP 区间, 不与系统消息冲突

    def test_request_reinstall_posts_thread_message(self):
        from com_bridge.hook import WM_APP_REINSTALL
        h = LowLevelHook(on_key_event=None)
        posted = []
        import com_bridge.hook as hook_mod
        orig = hook_mod.user32.PostThreadMessageW
        try:
            hook_mod.user32.PostThreadMessageW = \
                lambda tid, msg, w, l: posted.append((tid, msg))
            h._thread_id = 4242
            h.request_reinstall()
        finally:
            hook_mod.user32.PostThreadMessageW = orig
        assert posted == [(4242, WM_APP_REINSTALL)]

    def test_reinstall_counts_and_sets_installed(self, monkeypatch):
        import com_bridge.hook as hook_mod
        h = LowLevelHook(on_key_event=None)
        monkeypatch.setattr(hook_mod.user32, "SetWindowsHookExW",
                            lambda *a: 0x1234)
        h._reinstall()
        assert h.installed
        assert h.stats["reinstalls"] == 1

    def test_reinstall_failure_is_not_fatal(self, monkeypatch):
        """重装失败只能置 installed=False, 不能让钩子线程崩掉。"""
        import com_bridge.hook as hook_mod
        h = LowLevelHook(on_key_event=None)
        monkeypatch.setattr(hook_mod.user32, "SetWindowsHookExW",
                            lambda *a: 0)
        h._reinstall()                      # 不得抛出
        assert not h.installed
        assert h.stats["reinstalls"] == 0

    def test_reinstall_exception_is_swallowed(self, monkeypatch):
        import com_bridge.hook as hook_mod

        def boom(*a):
            raise ctypes.ArgumentError("bad")

        h = LowLevelHook(on_key_event=None)
        monkeypatch.setattr(hook_mod.user32, "SetWindowsHookExW", boom)
        h._reinstall()                      # 不得抛出(否则钩子线程退出)
        assert not h.installed

    def test_server_requests_reinstall_before_pin_fallback(self):
        """健康自检必须先请求重装, 只有反复失败才退化为钉住。

        此前一检测到位移就 `_pin_fallback = True` —— 键盘侧从此无人吞键,
        正是"两端键盘同时生效"的成因。
        """
        import inspect
        src = inspect.getsource(Server._on_input_event)
        assert "request_reinstall()" in src
        # 重装必须在退化之前(源码顺序)
        assert src.index("request_reinstall()") < \
            src.index("_pin_fallback = True")

    def test_server_has_reinstall_limit(self):
        assert Server.HOOK_REINSTALL_LIMIT >= 1


class TestServerWiring:
    """钩子自产事件必须接入 _on_input_event 的既有转发链。"""

    def test_ensure_hook_passes_callback(self):
        import inspect
        src = inspect.getsource(Server._ensure_hook)
        assert "on_key_event=" in src
        assert "_on_hook_key_event" in src

    def test_hook_key_event_feeds_on_input_event(self):
        import inspect
        src = inspect.getsource(Server._on_hook_key_event)
        assert "_on_input_event" in src
        assert "InputEvent(" in src

    def test_end_to_end_forwarding(self):
        # 伪造最小 Server: 钩子自产事件 → _on_input_event → 转发
        srv = Server.__new__(Server)
        sent = []

        class FakeTx:
            def send(self, ev):
                sent.append(ev)

        class FakeEdge:
            is_outside = True
            self_rect = None

        class FakeHook:
            # ★ 键盘转发现在以"钩子能吞键"为前提(见 _keyboard_relay_ready):
            #   钩子不可用时转发键盘会导致"两台同时收到按键"。
            installed = True
            relay = True

        srv._edge = FakeEdge()
        srv._input_tx = FakeTx()
        srv._hook = FakeHook()
        srv._kbd_hook_verified = True
        srv._kbd_verify_mono = 1e18     # TTL 内, 避免复检(桩无 probe 方法)
        srv.stats = {"events_forwarded": 0}
        srv._on_hook_key_event(EV_KEY, KEY_DOWN, 0x41, 0x1E, 0, 123456)
        assert len(sent) == 1 and sent[0].is_key
        assert sent[0].code == 0x41

    def test_key_dropped_when_hook_unavailable(self):
        """★★★ 钩子不可用时**必须丢弃键盘事件**(不转发)。

        否则: 钩子没吞 -> 键落主机焦点窗口; 我们又转发 -> 副机也收到
        = 用户实测的"在副机打字, 主机也同步输入"。
        鼠标不受影响(仍转发)。

        ★ 这里只测**判定函数**与源码结构, 不走完整 _on_input_event ——
          后者需要一大堆真实状态(edge/pin/tx), 桩成本高且测不到重点。
        """
        srv = Server.__new__(Server)

        class FakeHook:
            def __init__(self, installed, relay):
                self.installed = installed
                self.relay = relay

        # 1. 钩子装了 + relay 开 + 自检通过 -> 可转发
        srv._hook = FakeHook(True, True)
        srv._kbd_hook_verified = True
        srv._kbd_verify_mono = 1e18      # TTL 内, 不触发复检
        assert srv._keyboard_relay_ready() is True

        # 2. 自检失败 -> 不可转发(核心场景)
        srv._kbd_hook_verified = False
        assert srv._keyboard_relay_ready() is False

        # 3. 钩子没装 -> 不可转发
        srv._hook = FakeHook(False, True)
        srv._kbd_hook_verified = None
        assert srv._keyboard_relay_ready() is False

        # 4. relay 未开(未穿越) -> 不可转发
        srv._hook = FakeHook(True, False)
        assert srv._keyboard_relay_ready() is False

        # 5. 自检未知(None, 注入失败) -> 按"能用"处理, 避免误丢键
        srv._hook = FakeHook(True, True)
        srv._kbd_hook_verified = None
        srv._kbd_verify_mono = 1e18
        assert srv._keyboard_relay_ready() is True

        # 6. 源码结构: 转发前必须检查键盘可用性并丢弃
        import inspect
        src = inspect.getsource(Server._on_input_event)
        assert "_keyboard_relay_ready" in src, \
            "转发键盘前必须检查钩子可用性"
        assert "keys_dropped_no_hook" in src, "丢弃必须计数(便于诊断)"

    def test_not_outside_not_forwarded(self):
        # 穿越态门槛在 _on_input_event: 未穿越时键盘事件不转发
        srv = Server.__new__(Server)
        sent = []

        class FakeTx:
            def send(self, ev):
                sent.append(ev)

        class FakeEdge:
            is_outside = False
            self_rect = None

        srv._edge = FakeEdge()
        srv._input_tx = FakeTx()
        srv.stats = {"events_forwarded": 0}
        srv._on_hook_key_event(EV_KEY, KEY_DOWN, 0x41, 0x1E, 0, 123456)
        assert not sent
