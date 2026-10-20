# -*- coding: utf-8 -*-
"""真实物理按键 vs 键盘钩子: hider 持前台时, 物理键到底能不能到达钩子?

## 为什么必须由人来按
前面所有 probe 都是 **SendInput 注入**。实测已发现:
前台是**任何窗口**时(哪怕不是本进程), 注入键都进不了本进程的低级键盘钩子。
所以 probe 在"有前台窗口"时天然是 False —— 它可能一直是**假阴性**。

真实生产里用户是**物理打字**。因此必须区分:
  (a) 物理键在 hider 持前台时**仍能**到达钩子
      -> 键盘转发本来就是好的, 是自愈逻辑用假阴性 probe 把它停了(修诊断即可)
  (b) 物理键也到达不了
      -> 必须改架构(抢前台这个方案不可用)

## 交互
脚本会在每个阶段提示"现在请按键", 并实时显示计数。
你只要在每个阶段**按几下任意键**即可, 不需要回车。

用法: python tools/exp_physical_key_vs_hook.py
"""
from __future__ import annotations

import ctypes
import sys
import time

sys.path.insert(0, ".")
user32 = ctypes.windll.user32


def title(h) -> str:
    if not h:
        return "(none)"
    n = user32.GetWindowTextLengthW(h)
    buf = ctypes.create_unicode_buffer(n + 2)
    user32.GetWindowTextW(h, buf, n + 2)
    return buf.value or f"<{h}>"


def main() -> int:
    from com_bridge.hook import LowLevelHook
    from com_bridge.input import EV_KEY, RawInputListener
    from com_bridge.cursor import CursorHider

    rk = []

    def on_ev(ev):
        if ev.type == EV_KEY:
            rk.append((ev.code, time.monotonic()))

    hook = LowLevelHook()
    assert hook.start()
    time.sleep(0.3)
    li = RawInputListener(on_event=on_ev, accept_synthetic=True)
    li.start()
    assert li.wait_ready()
    time.sleep(0.3)

    hider = CursorHider(hide_on_motion=False, name="PhysHider")
    hider.start()
    time.sleep(0.4)

    results = {}

    def phase(idx: int, tag: str, seconds: float) -> None:
        d0 = hook.diag()
        c0, r0 = d0["kbd_calls"], len(rk)
        print()
        print("=" * 64)
        print(f"### 阶段 {idx}: {tag}")
        print(f"### >>> 现在请按键盘上的任意键, 随便按 5~10 下 <<<")
        print(f"### 你有 {seconds:.0f} 秒; 前台窗口 = {title(user32.GetForegroundWindow())}")
        print("=" * 64, flush=True)
        t0 = time.monotonic()
        last = -1
        while time.monotonic() - t0 < seconds:
            time.sleep(0.4)
            d = hook.diag()
            cur = d["kbd_calls"] - c0
            if cur != last:
                print(f"    [t={time.monotonic() - t0:4.1f}s] "
                      f"钩子收到键盘事件 +{cur}  "
                      f"RawInput +{len(rk) - r0}", flush=True)
                last = cur
        d = hook.diag()
        kc = d["kbd_calls"] - c0
        rc = len(rk) - r0
        results[idx] = (kc, rc)
        print(f"  --> 阶段 {idx} 小结: 钩子 +{kc}, RawInput +{rc}", flush=True)

    shell = user32.GetShellWindow()
    if shell:
        user32.SetForegroundWindow(shell)
    time.sleep(0.5)

    # ★★★ 关键: 先等用户就位。上一版一启动就开始倒计时, 用户还没看提示
    #   就结束了(三阶段全 0, 连基线都 0) —— 那不能作为任何证据。
    print()
    print("#" * 64)
    print("#  即将开始, 共 3 个阶段, 每阶段约 10~12 秒。")
    print("#  每个阶段开始时屏幕会提示, 你**只需按键盘任意键 5~10 下**。")
    print("#  不需要回车, 不需要点窗口 —— 直接按键即可。")
    print("#")
    print("#  准备好后请按【空格键】开始(脚本会等你按)...")
    print("#" * 64, flush=True)

    # 等到真的检测到一次按键(说明用户已在键盘前且钩子能收到), 最多等 60s
    base_calls = hook.diag()["kbd_calls"]
    t_wait = time.monotonic()
    while time.monotonic() - t_wait < 60.0:
        time.sleep(0.1)
        if hook.diag()["kbd_calls"] > base_calls or len(rk) > 0:
            print(f"  已检测到你的按键(等待 {(time.monotonic() - t_wait):.1f}s), "
                  f"现在正式开始。", flush=True)
            break
    else:
        print("  !! 60 秒内没有检测到任何按键。可能是: 没按 / 键盘被别的程序"
              "独占 / 钩子不可用。", flush=True)
    time.sleep(0.8)

    phase(1, "正常状态(前台=桌面, 无 hider)", 12.0)

    hider.hide()
    time.sleep(0.6)
    phase(2, "hider 持前台(这就是生产穿越态)", 12.0)

    hider.show()
    time.sleep(0.6)
    phase(3, "hider 已退出(恢复之后)", 10.0)

    hider.stop()
    li.stop()
    hook.stop()
    if shell:
        user32.SetForegroundWindow(shell)

    print()
    print("=" * 64)
    print("结论:")
    for i in (1, 2, 3):
        kc, rc = results.get(i, (-1, -1))
        print(f"  阶段 {i}: 钩子 +{kc}  RawInput +{rc}")
    print()
    k1 = results.get(1, (0, 0))[0]
    k2 = results.get(2, (0, 0))[0]
    if k1 == 0:
        print("  !! 阶段 1 就是 0 —— 说明按键没有到达(可能没按, 或被别的钩子挡住),")
        print("     本实验无效, 请重跑。")
    elif k2 > 0:
        print("  ★ 阶段 2 钩子仍收到物理键 => probe(注入)在穿越态是**假阴性**。")
        print("     键盘转发本来是好的, 是自愈逻辑把正常键盘停掉了。修诊断即可。")
    else:
        print("  ✗ 阶段 2 钩子收不到物理键 => 抢前台确实杀死键盘。")
        print("     必须改架构(不能靠抢前台隐藏光标)。")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
