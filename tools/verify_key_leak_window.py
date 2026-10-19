# -*- coding: utf-8 -*-
"""★★★ 终极端到端验证: 用**真实前台窗口**检测键盘是否泄漏到主机。

## 之前的测试都有同一个缺陷
所有"钩子吞键"测试只看 hook.kbd_eaten 计数 —— 但**计数为 0 也可能是
回调没被调用**。而用户真正的症状是"主机焦点窗口收到了按键"。

## 本工具直接测那个症状
1. 创建一个**真实的前台窗口**(tkinter 窗口 + 焦点), 带 KeyPress 绑定
2. 进入穿越态(relay=True + hide)
3. 注入无 TAG 键盘事件(模拟物理键)
4. **关键判据**: 前台窗口的 KeyPress 回调是否被触发
   - 触发 = 键泄漏到主机 = 用户症状复现
   - 不触发 = 键被吞, 正常
5. 对照: 退出穿越态(relay=False)后注入, 窗口**应该**收到(证明窗口能收键)
"""
from __future__ import annotations

import sys
import time
import tkinter as tk

sys.path.insert(0, ".")

from com_bridge.server import Server
from inject_util import PROBE_VK, burst, require_injection

result = {"keys": [], "error": None}


def run():
    try:
        print("== 1. 创建真实前台窗口(tkinter, 有焦点) ==")
        win = tk.Toplevel(root)
        win.title("ComBridge KeyLeak Test")
        win.geometry("400x200+100+100")
        win.deiconify()
        win.lift()
        win.focus_force()

        def on_key(ev):
            result["keys"].append(ev.keysym)

        win.bind("<Key>", on_key)
        win.update()
        time.sleep(0.5)
        print(f"  窗口已创建, 焦点={win.focus_displayof()}")

        print()
        print("== 2. 起 Server(测试后门) ==")
        srv = Server(name="leak", side="right", accept_synthetic_input=True)
        srv.start()
        time.sleep(0.8)
        hook = srv._hook
        print(f"  钩子 installed={hook.installed}")

        # 基线: relay=False 时, 窗口应该能收到注入的键
        print()
        print("== 3. 基线: relay=False, 键应到达窗口 ==")
        result["keys"].clear()
        win.focus_force()
        time.sleep(0.2)
        burst(3, PROBE_VK)
        time.sleep(0.5)
        base_keys = list(result["keys"])
        print(f"  窗口收到的键: {base_keys}")
        print(f"  (0xE8 可能不出 keysym, 所以这里只作参考)")

        # 穿越态
        print()
        print("== 4. 进入穿越态(relay=True + hide) ==")
        srv._edge._outside = True
        srv._edge._streak = 2
        srv._acquire_control()
        time.sleep(0.6)
        d = hook.diag()
        print(f"  relay={d['relay']} installed={d['installed']}")

        print()
        print("== 5. 穿越态注入键盘 —— 键**不应**到达窗口 ==")
        result["keys"].clear()
        b = hook.diag()
        sent = burst(5, PROBE_VK)
        require_injection(sent, 10)
        time.sleep(0.6)
        a = hook.diag()
        leak_keys = list(result["keys"])
        d_calls = a["kbd_calls"] - b["kbd_calls"]
        d_eaten = a["kbd_eaten"] - b["kbd_eaten"]

        print(f"  钩子: kbd_calls+{d_calls} kbd_eaten+{d_eaten}")
        print(f"  窗口收到: {leak_keys}")

        result["calls"] = d_calls
        result["eaten"] = d_eaten
        result["leaked"] = len(leak_keys)
        result["pump"] = a.get("hook_pump_age_s", -1)

        # 退出
        srv._edge._outside = False
        srv._release_control()
        time.sleep(0.4)
        srv.stop()
        win.destroy()
    except BaseException as exc:  # noqa: BLE001
        import traceback
        result["error"] = traceback.format_exc()
        print(f"  异常: {exc}")
    finally:
        root.quit()


root = tk.Tk()
root.withdraw()
root.after(200, run)
root.mainloop()

print()
print("== 结论 ==")
if result.get("error"):
    print(f"  异常: {result['error']}")
    sys.exit(2)
print(f"  kbd_calls+{result.get('calls')}  kbd_eaten+{result.get('eaten')}  "
      f"窗口泄漏={result.get('leaked')}  泵心跳={result.get('pump'):.2f}s")
if result.get("calls", 0) == 0:
    print("  ★★ 复现: 钩子回调从未被调用 -> 键必然泄漏到主机(用户症状)")
    sys.exit(1)
if result.get("eaten", 0) == 0:
    print("  ★ 回调被调用了但没吞键 -> relay 判定有问题")
    sys.exit(1)
if result.get("leaked", 0) > 0:
    print("  ★ 钩子吞了键, 但窗口仍收到 -> 存在第二条泄漏路径")
    sys.exit(1)
print("  ✓ 钩子正常吞键, 窗口没收到键 —— 本机环境下症状未复现")
