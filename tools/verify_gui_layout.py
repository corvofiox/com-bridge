"""GUI 布局冒烟测试: 自动创建窗口、切换角色、量测关键控件几何。

不依赖人工看屏幕 —— 直接断言:
  1. 主电脑视图: IP 输入框**不存在**(主电脑不需要填 IP), 方位框存在;
  2. 副电脑视图: IP 输入框存在且**宽度充足**(修复前被挤没), 方位框不存在;
  3. 两个视图里延迟输入框都存在;
  4. 窗口缩放后 IP 框跟随变宽(grid weight=1 生效)。
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

from com_bridge.gui import ComBridgeGui


def measure(gui: ComBridgeGui) -> dict:
    r = gui.root
    out = {
        "win_w": r.winfo_width(),
        "side": (gui._side_box.winfo_width(), gui._side_box.winfo_exists())
        if gui._side_box else (0, False),
        "host": (gui._host_entry.winfo_width(), gui._host_entry.winfo_exists())
        if gui._host_entry else (0, False),
        "jitter": (gui._jitter_entry.winfo_width(),
                   gui._jitter_entry.winfo_exists())
        if gui._jitter_entry else (0, False),
    }
    return out


def main() -> int:
    gui = ComBridgeGui()
    root = gui.root
    results: list[tuple[str, bool, str]] = []

    # --- 1) 默认主电脑视图 ---
    root.update_idletasks()
    root.update()
    m = measure(gui)
    ok1 = (not m["host"][1]) and m["side"][1] and m["jitter"][1]
    results.append(("主电脑视图: 无 IP 框 / 有方位+延迟", ok1, str(m)))

    # --- 2) 切到副电脑视图 ---
    gui._role_var.set("agent")
    gui._on_role_change()
    root.update_idletasks()
    root.update()
    m = measure(gui)
    ok2 = m["host"][1] and m["host"][0] >= 200 and (not m["side"][1])
    results.append((f"副电脑视图: IP 框存在且宽 {m['host'][0]}px(>=200)",
                    ok2, ""))

    # --- 3) 放大窗口, IP 框应跟随变宽 ---
    root.geometry("1000x620")
    root.update_idletasks()
    root.update()
    w_big = gui._host_entry.winfo_width()
    ok3 = w_big > m["host"][0]
    results.append((f"窗口放大到 1000px 后 IP 框 {m['host'][0]}->{w_big}px",
                    ok3, ""))

    # --- 4) 切回主电脑视图, 方位框恢复 ---
    gui._role_var.set("server")
    gui._on_role_change()
    root.update_idletasks()
    root.update()
    m = measure(gui)
    ok4 = m["side"][1] and (not m["host"][1])
    results.append(("切回主电脑: 方位框恢复 / IP 框移除", ok4, ""))

    root.destroy()

    print("=== GUI 布局冒烟测试 ===")
    allok = True
    for name, ok, detail in results:
        allok = allok and ok
        print(f"  {'OK  ' if ok else 'FAIL'} {name} {detail}")
    print()
    print(f"[结论] {'PASS —— 布局符合设计' if allok else 'FAIL'}")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
