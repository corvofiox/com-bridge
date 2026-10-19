"""低级键盘/鼠标钩子 —— 穿越期间**吞掉**物理输入, 使它不落到本机。

## 为什么必须用钩子(而不是现有的 RawInput)

RawInput(`RIDEV_INPUTSINK`)是**只观察、不消费**的: 用户在本机按下鼠标,
Windows 照样把这次点击送给本机的焦点窗口 —— 我们只是"顺手复制"一份发给副机。
症状正是用户报的: **"进入副机后鼠标点击也不落位在副机, 还是落位在主机"**。

低级钩子(`WH_MOUSE_LL`/`WH_KEYBOARD_LL`)可以 `return 1` **吞掉**事件,
物理点击/按键根本到不了本机 —— 这样它才真正"落到副机"。
Deskflow 的 `MSWindowsHook` 就是这么做的(kHOOK_RELAY_EVENTS 模式下
`mouseHookHandler`/`keyboardHookHandler` 返回 true = 丢弃)。

## 关键事实(实测确认, 见 tools/diag_hook_rawinput.py 与 diag_hook_delta.py)

* **鼠标: 钩子吞掉事件后, RawInput 仍然照常收到它** —— RawInput 走的是设备
  级管线, 与"事件是否被系统应用"无关。实测: 钩子 `eaten=20` 而 RawInput
  `total 0→20`, 且光标纹丝不动(证明吞生效)。
* **鼠标: 被吞掉的事件, 其原始位移 `lLastX/lLastY` 完整无损** —— 实测请求
  (13,7)/(-9,4)/(21,-6), RawInput 读到的就是这三个值, 一个不差。
* **键盘: 与鼠标不对称!** 被键盘钩子吞掉的按键**不会**产生 RawInput
  (tools/diag_hook_kbd_rawinput.py 四阶段实测: 无钩子注入 total+2 →
  钩子吞后 total+0 → 同钩子放行 total+2)。因此**键盘事件必须由钩子回调
  自产**(照搬 Deskflow keyboardHookHandler :416-424 的 PostThreadMessage
  范式), RawInput 只承载鼠标。

=> 因此架构是: **鼠标 = 钩子只吞 + RawInput 事件来源; 键盘 = 钩子既吞
   又是事件来源**(通过 `on_key_event` 回调把被吞的键交给上层转发)。

## ★ 两条必须遵守的硬约束

1. **回调必须极快**: Windows 对低级钩子有超时限制(约 300ms), 超时后它会
   **静默地摘掉钩子** —— 表现为"用着用着点击又开始落到本机了", 极难自查。
   所以回调里只读一个布尔标志就返回, 绝不做 I/O / 加锁 / 分配。
   (这也是 Deskflow 在钩子里只 `PostThreadMessage` 的原因。)
2. **回调对象必须常驻引用**: `WINFUNCTYPE` 对象一旦被 GC 回收, Windows 再调用
   就是野指针 -> 访问违例, 进程**直接崩溃且没有 traceback**(实测踩到)。

## 模式

* `relay=False`(默认, 控制权在本机): 全部事件**放行**, 本地使用完全正常。
* `relay=True`(控制权在副机): 吞掉物理鼠标/键盘事件, 由上层转发给副机。
* 无论哪种模式, **带 `InputInjector.TAG` 的事件一律放行** —— 那是我们
  自己注入的(主机侧的钉住定位、副机侧的注入), 吞掉会造成自锁。
"""

from __future__ import annotations

import ctypes
import threading
import time
from collections import deque
from ctypes import POINTER as P
from ctypes import c_int, c_ssize_t, wintypes

from .input import InputInjector, kernel32, user32
from .protocol import EV_KEY, KEY_DOWN, KEY_UP, KEY_EXTENDED
from .clock import now_us

# --- 常量 ---------------------------------------------------------------

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14

WM_QUIT = 0x0012
PM_NOREMOVE = 0x0000
PM_REMOVE = 0x0001

# 钩子线程自定义消息(PostThreadMessageW 投递, 见 _kbd_proc / reinstall)
WM_APP_KEY = 0x8000 + 10        # 队列里有待转发的键盘事件
WM_APP_REINSTALL = 0x8000 + 11  # 请求重装钩子(系统摘钩后自愈)

# 键盘指示灯同步(Deskflow 同款): 这三个键即使在 relay 模式下也**放行**,
# 否则本机的 Caps/Num/Scroll 指示灯会与实际状态不符, 用户会以为键盘坏了。
# Deskflow 注释: "pass event on. we want to let these through to the window
# proc because otherwise the keyboard lights may not stay synchronized."
VK_CAPITAL = 0x14
VK_NUMLOCK = 0x90
VK_SCROLL = 0x91
PASSTHROUGH_VKS = frozenset({VK_CAPITAL, VK_NUMLOCK, VK_SCROLL})

# KBDLLHOOKSTRUCT.flags 位: 扩展键(右 Ctrl/Alt、方向键、Ins/Del/Home/End、
# PgUp/PgDn、Numpad Enter/斜杠等)。对应 RawInput 的 RI_KEY_E0。
LLKHF_EXTENDED = 0x01


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    """WH_MOUSE_LL 回调的 lParam 指向的结构。"""
    _fields_ = [
        ("pt", POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    """WH_KEYBOARD_LL 回调的 lParam 指向的结构。"""
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


_HOOKPROC = ctypes.WINFUNCTYPE(c_ssize_t, c_int, wintypes.WPARAM,
                              wintypes.LPARAM)

user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.SetWindowsHookExW.argtypes = [c_int, _HOOKPROC, ctypes.c_void_p,
                                     wintypes.DWORD]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
user32.CallNextHookEx.restype = c_ssize_t
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, c_int, wintypes.WPARAM,
                                  wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM]
user32.PeekMessageW.restype = wintypes.BOOL
user32.PeekMessageW.argtypes = [P(wintypes.MSG), wintypes.HWND, wintypes.UINT,
                                wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = c_int
user32.GetMessageW.argtypes = [P(wintypes.MSG), wintypes.HWND, wintypes.UINT,
                               wintypes.UINT]
kernel32.GetLastError.restype = wintypes.DWORD
kernel32.GetLastError.argtypes = []


# --- 纯决策函数(与 Windows 无关, 可单元测试) ---------------------------

def decide_mouse_eat(relay: bool, extra_info: int) -> bool:
    """鼠标事件是否应被**吞掉**(True=吞, False=放行)。"""
    if extra_info == InputInjector.TAG:
        return False            # 我们自己注入的, 必须放行(否则钉住会自锁)
    return bool(relay)


def decide_key_eat(relay: bool, vk: int, extra_info: int) -> bool:
    """键盘事件是否应被**吞掉**(True=吞, False=放行)。"""
    if extra_info == InputInjector.TAG:
        return False
    if not relay:
        return False
    if vk in PASSTHROUGH_VKS:
        return False            # 保持本机指示灯同步
    return True


# --- 钩子 -----------------------------------------------------------------

class LowLevelHook:
    """全局低级鼠标/键盘钩子, 按 `relay` 标志吞事件。

    用法:
        hook = LowLevelHook(); hook.start()
        hook.set_relay(True)    # 控制权交给副机: 开始吞物理输入
        hook.set_relay(False)   # 控制权回到本机: 全部放行
        hook.stop()

    钩子随进程生灭: 程序崩溃/退出后钩子自动被系统摘掉, 不会残留
    (与 hider window 同理, 这也是不用全局键盘改键方案的原因)。
    """

    def __init__(self, name: str = "ComBridgeHook",
                 on_key_event=None):
        self._name = name
        # ★ 键盘事件回调(照搬 Deskflow keyboardHookHandler 的
        #   PostThreadMessage 范式): 被 LL 钩子吞掉的键盘事件**不会**产生
        #   RawInput(实测 tools/diag_hook_kbd_rawinput.py: 钩子吞键后
        #   Listener total +0; 鼠标则仍会送达 —— 两者不对称), 所以键盘
        #   事件必须在钩子回调里自产, 否则穿越期间主机收不到任何物理
        #   键盘事件, 副机就完全没键盘。鼠标不适用: RawInput 的位移字段
        #   精度更高, 且实测被吞后照常送达。
        self._on_key_event = on_key_event
        self._relay = False
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._hook_mouse = None
        self._hook_kbd = None
        # ★ 常驻引用: 被 GC 回收会导致野指针 -> 进程崩溃(见模块 docstring)
        self._mouse_ref = None
        self._kbd_ref = None
        # ★ 待转发键盘事件队列(照搬 Deskflow keyboardHookHandler 的
        #   PostThreadMessage 范式)。**绝不能在回调里直接做转发工作**:
        #   低级钩子有约 300ms 的 LowLevelHooksTimeout, 超时会被 Windows
        #   **静默摘钩**(不报错、不回调), 之后物理键不再被吞 -> 主机焦点
        #   应用收到 + RawInput 也收到并转发给副机 = "两端同时生效"。
        #
        #   ★★ 而且工作也**不能在钩子线程的消息循环里做**(用户实测:
        #   鼠标好了但键盘仍两端生效)。原因: 系统只在钩子线程**处理消息**
        #   时才调用钩子过程, 所以钩子线程一旦忙于转发(网络发送等), 键盘
        #   回调就得不到服务 -> 超时 -> **只有键盘钩子**被摘除, 鼠标钩子
        #   因为回调极轻而幸存。于是症状恰好是"鼠标正常、键盘泄漏"。
        #   修法: 回调只 `deque.append`(GIL 下原子, **不加锁**) +
        #   PostThreadMessageW; 钩子线程收到消息只唤醒**独立转发线程**,
        #   真正的工作全在那个线程上做。
        self._key_queue = deque()
        self._key_lock = threading.Lock()   # 保留给测试/外部批量注入用
        # 唤醒转发线程的事件(钩子线程 set, 转发线程 wait)
        self._key_wake = threading.Event()
        self._worker: threading.Thread | None = None
        # 转发线程的存活标志(独立于 _stop, 便于诊断)
        self.worker_alive = False
        # ★ 重装标志(见 _run 的 ★★★): 钩子线程收到重装消息只置标志,
        #   在**下一轮消息循环的 GetMessage 之前**由本线程自己执行 ——
        #   LL 钩子有线程亲和(系统把回调投到安装线程的队列), 绝不能
        #   挪到别的线程装; 也不能在消息处理中途同步装(泵停顿 ->
        #   连续请求下系统静默摘钩 = 用户日志"重装3次全无效"的根源)。
        #   循环头执行 = 泵停顿永远只有一次微秒级 unhook/rehook,
        #   且执行期间新到的请求自然合并到下一轮。
        self._reinstall_requested = False
        self.installed = False
        self.last_error = 0
        # 诊断计数(供 GUI/验收读取)
        self.stats = {
            "mouse_eaten": 0, "mouse_passed": 0,
            "kbd_eaten": 0, "kbd_passed": 0,
            "injected_passed": 0,
            # ★★ 键盘回调被调用的**总次数**(在任何过滤之前自增)。
            #   为什么必须单独有它: 用户实测"鼠标计数正常增长, 键盘恒为 0"。
            #   键盘钩子是链式钩子 —— 若**别的程序**(Deskflow/Synergy/输入法
            #   等)的 WH_KEYBOARD_LL 排在我们前面且返回非零, 钩子链就断在
            #   那里, 我们的回调**永远不会被调用**。此时 kbd_eaten 与
            #   kbd_passed 都是 0, 但那是"没被调用", 不是"没有按键"。
            #   把调用次数单独记下来, 才能把这两种情况区分开。
            "kbd_calls": 0,
            "last_call": 0.0,
            # 系统静默摘钩后的自愈次数(见 request_reinstall)
            # 钩子线程心跳(每轮消息循环打点): 判定"线程还在泵消息"的
            # 唯一可观测手段 —— 用户日志三连事实(kbd_calls=0 + 泄漏触发
            # + 重装无效)只有"钩子线程不泵消息"能同时解释。
            "hook_pump_mono": 0.0,
            "reinstalls": 0,
        }

    # 每轮消息循环最多转发多少条键盘事件(见 _drain_key_queue 的说明)。
    # 取 8: 足够跟上人手打字(单键一次 down/up 共 2 条), 又短到不会让
    # 钩子线程长时间"不回消息"而触发 LowLevelHooksTimeout。
    DRAIN_BATCH = 8

    def _enqueue_key(self, ev_type, flags, code, a, b, ts_us) -> None:
        """回调里**只**入队 + 唤醒转发线程, 立刻返回。

        ★ 刻意**不加锁**: `deque.append` 在 CPython 里是原子的(单字节码
          操作), 而 `threading.Lock` 在极端争用下会让回调阻塞 —— 那正是
          要避免的(回调阻塞 = 超时 = 静默摘钩)。转发线程用 `popleft`
          同样原子, 因此无需互斥。
        """
        try:
            self._key_queue.append((ev_type, flags, code, a, b, ts_us))
            self._key_wake.set()        # 唤醒独立转发线程(不在此处做工作)
        except Exception:  # noqa: BLE001
            pass  # 入队失败也不能影响吞放行决断

    def _key_worker_loop(self) -> None:
        """独立的键盘转发线程: 唯一做"重活"(网络发送/注入)的地方。

        为什么必须独立线程(而不是钩子线程自己排空): 系统只在钩子线程
        **处理消息**时才调用钩子过程; 钩子线程一做重活, 键盘回调就得不到
        服务 -> 超时 -> 只有键盘钩子被摘(鼠标钩子回调极轻而幸存), 症状
        恰好是"鼠标正常、键盘两端同时生效"。
        """
        self.worker_alive = True
        try:
            while not self._stop.is_set():
                # 等到有活或被要求退出(超时兜底, 避免 set 丢失时永久挂住)
                self._key_wake.wait(0.5)
                self._key_wake.clear()
                while True:
                    try:
                        item = self._key_queue.popleft()
                    except IndexError:
                        break           # 队列已空
                    try:
                        self._on_key_event(*item)
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            self.worker_alive = False

    def _drain_key_queue(self) -> None:
        """唤醒转发线程(不再在本线程做转发工作)。

        保留此方法名是为了兼容既有测试/验收脚本的调用; 语义已从"就地排空"
        改为"唤醒独立线程"。测试若需要**确定性**地看到事件被交付, 用
        `drain_sync()`(它在当前线程同步排空, 仅用于测试)。
        """
        self._key_wake.set()

    def drain_sync(self) -> None:
        """在当前线程同步排空队列(**仅测试用**, 生产走 _key_worker_loop)。"""
        while True:
            try:
                item = self._key_queue.popleft()
            except IndexError:
                return
            try:
                self._on_key_event(*item)
            except Exception:  # noqa: BLE001
                pass

    def request_reinstall(self) -> None:
        """请求钩子线程重装钩子(系统静默摘钩后的自愈)。

        ★ 与 Deskflow 一致: 平时**从不**主动重装(它也只在检测到
          输入桌面切换时才 uninstall+install)。我们只在**检出异常**
          (穿越态泄漏/健康自检失败)时调用一次。
        """
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_APP_REINSTALL, 0, 0)

    def _reinstall(self) -> None:
        """重装两个钩子(照搬 Deskflow 的装钩顺序)。

        ★ 线程安全: 由**安装线程**调用(绝不跑在钩子线程 —— 见 _run 的
          ★★★ 注释)。绝不能抛: 异常会让安装线程退出。
        """
        try:
            self._unhook_handles()
            self._hook_mouse = user32.SetWindowsHookExW(
                WH_MOUSE_LL, self._mouse_ref, None, 0)
            self._hook_kbd = user32.SetWindowsHookExW(
                WH_KEYBOARD_LL, self._kbd_ref, None, 0)
        except Exception:  # noqa: BLE001
            self._hook_mouse = None
            self._hook_kbd = None
        ok = bool(self._hook_mouse and self._hook_kbd)
        self.installed = ok
        if ok:
            self.stats["reinstalls"] += 1

    # -- 对外 ----------------------------------------------------------

    @property
    def relay(self) -> bool:
        return self._relay

    def set_relay(self, on: bool) -> None:
        """切换"是否吞掉物理输入"。任意线程可调, 钩子线程下一个事件即生效。

        ★ 与 Deskflow 的 setMode(kHOOK_RELAY_EVENTS) 同型: 纯布尔开关,
          **不**在切换时重装钩子。
        """
        self._relay = bool(on)

    def start(self) -> bool:
        # ★ 先起**独立转发线程**, 再起钩子线程: 保证第一个键盘事件入队时
        #   就已有消费者, 否则事件要等到钩子线程自己排空(而那正是要避免的)。
        #   (重装不设独立线程: LL 钩子有**线程亲和** —— 必须由安装线程
        #    自己泵消息, 从别的线程装 = 装在没人泵的队列上, 实测句柄在
        #    但回调死。重装由钩子线程在消息循环头执行, 见 _run。)
        self._worker = threading.Thread(target=self._key_worker_loop,
                                        daemon=True,
                                        name=f"{self._name}-KeyWorker")
        self._worker.start()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=self._name)
        self._thread.start()
        self._ready.wait(3.0)
        return self.installed

    def stop(self) -> None:
        """卸载钩子并结束调用线程。

        ★ 必须先关 relay 再卸载: 若卸载失败(极少见), 至少不会继续吞事件,
          用户不会被"键鼠全无响应"困住。
        """
        self._relay = False
        self._stop.set()
        self._key_wake.set()        # 唤醒转发线程让它看到 _stop 并退出
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._worker is not None:
            self._worker.join(timeout=2.0)

    def probe_keyboard_hook(self) -> bool | None:
        """自检: 注入一个带 TAG 的键, 看**我们自己的**键盘回调能否看到它。

        ★★ 为什么需要它(用户实测 "鼠标计数正常、键盘恒为 0"):
          回调不被调用的**真实原因**是**本进程窗口持有前台焦点**
          —— 实测(tools/exp_window_vs_hook_precise.py, 100% 复现):
              窗口有焦点 -> kbd_calls 恒 0
              焦点转移给别的窗口 -> 立刻恢复
          该结论有可逆验证(抢回焦点 -> 再次失效)。

          ⚠ 历史注记: 早期把原因归为"别的程序的键盘钩子排在链前并吞掉
            按键"。跨进程实测(tools/audit_chain_claim.py)已否定该推测:
            即使另一进程装了"吞一切"的键盘钩子, 我们的回调照样被调用。
            保留本自检是因为它仍能**如实反映"此刻回调是否可达"**,
            无论原因是什么。

          这个自检不依赖用户打字: 注入一个**带 TAG** 的键(我们自己的标记),
          回调里对 TAG 的处理在**任何过滤之前**, 所以只要回调被调用,
          `injected_passed` 就会增长。据此可以**确定性**地判断回调是否可达。

        返回: True=回调被调用; False=没被调用;
              None=注入本身失败, 无法判定(不要据此报警)。
        """
        if not self.installed:
            return None
        before = self.stats["injected_passed"]
        try:
            from .input import InputInjector
            # 用带 TAG 的注入: 回调里 TAG 分支在 relay/放行判定**之前**,
            # 所以它既能证明"回调被调用", 又不会真的吞掉或转发任何东西。
            # ★ 必须选一个**不在 PASSTHROUGH_VKS** 里的键, 否则即使回调
            #   被调用也会走"放行"分支而不计入 injected_passed... 实际上
            #   TAG 分支在最前面, 放行键也先命中 TAG, 所以用哪个键都行;
            #   这里仍避开 Caps/Num/Scroll 以免影响用户指示灯状态。
            #   0xE8 = 未分配的虚拟键, 不产生任何字符或系统行为。
            n = InputInjector.key(0xE8, 0, True)
            InputInjector.key(0xE8, 0, False)
        except Exception:  # noqa: BLE001
            return None
        if not n:
            return None                          # 注入失败: 判据不可用
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            if self.stats["injected_passed"] > before:
                return True
            time.sleep(0.01)
        return False

    def diag(self) -> dict:
        d = dict(self.stats)
        d["relay"] = self._relay
        d["installed"] = self.installed
        # 键盘钩子与鼠标钩子是**两个独立句柄**, 系统可以单独摘掉其中一个
        # (实测: 钩子线程一忙, 回调较重的键盘钩子先死而鼠标钩子幸存, 症状
        #  正是"鼠标正常但键盘两端同时生效")。因此把两者分别暴露出来,
        #  GUI/验收才能看出"只有键盘掉了"。
        d["kbd_hook_alive"] = bool(self._hook_kbd)
        d["mouse_hook_alive"] = bool(self._hook_mouse)
        d["worker_alive"] = self.worker_alive
        d["queue_len"] = len(self._key_queue)
        # ★★ 钩子线程心跳年龄(秒): 判定"泵消息是否卡死"的唯一可观测手段。
        #   用户真机日志的三连事实(kbd_calls=0 + 泄漏检测触发 + 重装3次
        #   无效)只有"钩子线程不泵消息"能同时解释 —— GetMessageW 在有
        #   消息时每秒会被系统唤醒多次(物理鼠标一动就有), 心跳 > 60s
        #   基本可以判定卡死。正常时它通常 < 1s。
        pump = self.stats.get("hook_pump_mono", 0.0)
        d["hook_pump_age_s"] = (time.monotonic() - pump) if pump else -1.0
        return d

    # -- 钩子回调(在钩子线程被系统调用) --------------------------------

    def _mouse_proc(self, code, wparam, lparam):
        # ★ 回调必须极快且**永不抛异常**: 异常会走 ctypes 默认返回 0,
        #   而钩子返回 0 意味着"吞掉" —— 一次意外就等于把用户的鼠标吃掉。
        try:
            if code < 0:
                return user32.CallNextHookEx(None, code, wparam, lparam)
            addr = lparam & 0xFFFFFFFFFFFFFFFF
            info = ctypes.cast(addr, P(MSLLHOOKSTRUCT)).contents
            extra = info.dwExtraInfo or 0
            self.stats["last_call"] = time.monotonic()
            if extra == InputInjector.TAG:
                self.stats["injected_passed"] += 1
                return user32.CallNextHookEx(None, code, wparam, lparam)
            if self._relay:
                self.stats["mouse_eaten"] += 1
                return 1                    # 吞掉
            self.stats["mouse_passed"] += 1
            return user32.CallNextHookEx(None, code, wparam, lparam)
        except Exception:  # noqa: BLE001
            # 出错时放行, 宁可让事件落回本机也不要吞掉用户的输入
            return user32.CallNextHookEx(None, code, wparam, lparam)

    def _kbd_proc(self, code, wparam, lparam):
        try:
            # ★ 第一件事就计数: 这是"回调到底有没有被系统调用"的唯一证据。
            #   必须在 code<0 早退、TAG 过滤、relay 判定**之前**自增 ——
            #   否则"没被调用"与"被调用但全部放行"无法区分(用户实测的
            #   kbd_eaten 恒为 0 就属于前者, 却被误读成后者)。
            self.stats["kbd_calls"] += 1
            if code < 0:
                return user32.CallNextHookEx(None, code, wparam, lparam)
            addr = lparam & 0xFFFFFFFFFFFFFFFF
            info = ctypes.cast(addr, P(KBDLLHOOKSTRUCT)).contents
            extra = info.dwExtraInfo or 0
            self.stats["last_call"] = time.monotonic()
            if extra == InputInjector.TAG:
                self.stats["injected_passed"] += 1
                return user32.CallNextHookEx(None, code, wparam, lparam)
            if self._relay and info.vkCode not in PASSTHROUGH_VKS:
                self.stats["kbd_eaten"] += 1
                # ★ 关键: 吞掉之前**自产**键盘事件(被吞的键不会走 RawInput,
                #   见 __init__ 注释) —— 上层把 InputEvent 转发给副机注入。
                #   wparam 是 WM_KEYDOWN/WM_KEYUP/WM_SYSKEYDOWN/WM_SYSKEYUP,
                #   *_KEYUP 系即释放; LLKHF_EXTENDED 对应 RI_KEY_E0。
                #
                #   ★★ 回调里只**入队 + 投递线程消息**, 绝不直接转发:
                #   低级钩子有约 300ms 的 LowLevelHooksTimeout, 回调里做
                #   网络发送/注入一旦超时, 钩子会被 Windows **静默摘除**
                #   (无错误码、无回调), 之后物理键不再被吞 —— 主机焦点
                #   应用收到一份, RawInput 也收到并转发给副机一份, 就是
                #   用户报的"两个机器同时生效"。Deskflow 用
                #   PostThreadMessage(DESKFLOW_MSG_KEY) 正是为把工作移出
                #   回调(见 MSWindowsHook.cpp keyboardHookHandler)。
                if self._on_key_event is not None:
                    vk = info.vkCode & 0xFF
                    if vk != 0xFF:  # 与 RawInputListener._emit_key 同款早退
                        flags = KEY_UP if wparam in (0x0105, 0x0101) \
                            else KEY_DOWN
                        if info.flags & LLKHF_EXTENDED:
                            flags |= KEY_EXTENDED
                        self._enqueue_key(
                            EV_KEY, flags, vk, info.scanCode & 0xFFFF, 0,
                            now_us())
                return 1
            self.stats["kbd_passed"] += 1
            return user32.CallNextHookEx(None, code, wparam, lparam)
        except Exception:  # noqa: BLE001
            return user32.CallNextHookEx(None, code, wparam, lparam)

    # -- 线程 ----------------------------------------------------------

    def _run(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        # 线程要先有消息队列, SetWindowsHookEx 才能把钩子挂到它上面
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)

        self._mouse_ref = _HOOKPROC(self._mouse_proc)
        self._kbd_ref = _HOOKPROC(self._kbd_proc)
        self._hook_mouse = user32.SetWindowsHookExW(
            WH_MOUSE_LL, self._mouse_ref, None, 0)
        err_m = kernel32.GetLastError()
        self._hook_kbd = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._kbd_ref, None, 0)
        err_k = kernel32.GetLastError()

        if not self._hook_mouse or not self._hook_kbd:
            self.last_error = err_m if not self._hook_mouse else err_k
            self.installed = False
            self._ready.set()
            self._unhook()
            return

        self.installed = True
        self._ready.set()

        # ★ 线程消息(PostThreadMessageW 投递的消息 hwnd 为 NULL, 不会被
        #   DispatchMessageW 路由到窗口过程)必须**显式分派**: 这是把
        #   转发工作搬出钩子回调的落点。
        #
        #   ★★ WM_APP_KEY 只**唤醒转发线程**, 绝不在本线程排空队列 ——
        #     本线程就是钩子线程, 它一忙, 键盘回调就得不到服务而超时被摘
        #     (症状: 鼠标正常但键盘两端同时生效)。
        #
        #   ★★★ 重装必须在**本线程**(LL 钩子有线程亲和: 系统把回调调用
        #     投到安装线程的消息队列, 从别的线程装=装在没人泵的队列上,
        #     实测句柄在但回调死)。但**不能在消息处理中途同步做**(旧 bug:
        #     连续重装请求让泵长时间不回消息 -> 系统静默摘钩)。
        #     正确形状: 每轮循环**在 GetMessage 之前**检查标志 ——
        #     GetMessage 一返回就回来检查, 泵消息的停顿永远不超过
        #     一次 unhook/rehook 的时长(微秒级), 且单次执行天然合并
        #     掉密集请求(标志在执行期间被再置位, 下一轮才处理)。
        while True:
            if self._reinstall_requested:
                self._reinstall_requested = False
                try:
                    self._reinstall()      # 本线程执行: 亲和正确
                except Exception:  # noqa: BLE001
                    pass
            # 心跳打点: 判定"钩子线程还在泵消息"的唯一可观测手段。
            self.stats["hook_pump_mono"] = time.monotonic()
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r <= 0:
                break
            if msg.message == WM_APP_KEY:
                self._key_wake.set()        # 交给独立转发线程
                continue
            if msg.message == WM_APP_REINSTALL:
                self._reinstall_requested = True    # 只置标志, 下轮循环头执行
                continue
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        self._unhook()

    def _unhook_handles(self) -> None:
        """只卸载句柄, 不动 _relay(供 _reinstall 复用)。"""
        if self._hook_mouse:
            user32.UnhookWindowsHookEx(self._hook_mouse)
            self._hook_mouse = None
        if self._hook_kbd:
            user32.UnhookWindowsHookEx(self._hook_kbd)
            self._hook_kbd = None

    def _unhook(self) -> None:
        self._relay = False
        self._unhook_handles()
        self.installed = False
