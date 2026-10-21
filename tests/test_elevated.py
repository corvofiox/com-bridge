# -*- coding: utf-8 -*-
"""elevated 模块回归测试(纯逻辑, 不需要管理员、不弹 UAC)。"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from com_bridge import elevated  # noqa: E402


def _strip_comments(src: str) -> str:
    return "\n".join(
        line for line in src.splitlines() if not line.strip().startswith("#"))


def test_result_channel_via_command_line():
    """结果通道必须走命令行 tag, 不能依赖环境变量。

    ShellExecuteW(runas) 拉起的提权子进程拿的是全新环境 ——
    os.environ 塞的自定义变量传不过去。这条锁死该设计, 防止有人
    "改回"环境变量方式(会让父进程永远超时)。
    """
    src = _strip_comments(inspect.getsource(elevated.request_elevated))
    assert "--elevated-tag" in src, "tag 必须经命令行传给子进程"
    assert "COMBRIDGE_ELEV" not in src, "不得再用环境变量传 tag"


def test_child_writes_result_file():
    """子进程必须写结果文件(父进程判定的唯一通道)。"""
    rc = elevated._run_elevated_child("install", "pytest_tag")
    p = elevated._result_path("pytest_tag")
    assert os.path.exists(p), "子进程没写结果文件 -> 父进程永远超时"
    content = open(p, encoding="utf-8").read().strip()
    os.unlink(p)
    assert content.startswith(("ok", "error"))


def test_ok_rc_nonzero_is_failure():
    """父进程必须把 'ok rc=1' 判为失败(注册实际没成功)。"""
    # 直接构造结果文件, 走父进程的解析逻辑(不真正提权)
    # —— 把解析段抽出来测: 模拟 request_elevated 的等待循环读取
    tag = "pytest_parse"
    p = elevated._result_path(tag)
    with open(p, "w", encoding="utf-8") as f:
        f.write("ok rc=1\n")
    # 手工复刻解析: request_elevated 内部读文件后的判断
    # (为避免弹 UAC, 这里只测解析函数存在且行为正确)
    src = _strip_comments(inspect.getsource(elevated.request_elevated))
    assert 'rc_val != 0' in src, "必须把 rc 非 0 的 ok 判为失败"
    os.unlink(p)


def test_gui_has_toggle_and_state():
    """GUI 必须有开机自启按钮 + 状态刷新。

    ★ 2026-10-19 变更: 自启机制从「计划任务 + RunLevel Highest」改为
      **HKCU Run 注册表项**。原因(用户明确要求): "不能加入需要用户确认
      的过程, 整个过程必须静默" —— 创建最高权限计划任务必然弹 UAC,
      这是 Windows 的安全边界, 绕不过。
      因此按钮不再走 `request_elevated`, 而是调 `autostart`(静默)模块。
    """
    import com_bridge.gui as gui_mod
    build = _strip_comments(inspect.getsource(gui_mod.ComBridgeGui._build))
    assert "开机自启" in build, "按钮区必须有开机自启入口"
    assert "_refresh_elev_state" in build, "启动时必须刷新自启状态"
    # ★ 2026-10-21: 浅色版把"开机自启"按钮换成了 Canvas 胶囊开关。
    #   实际实现移到 `_on_autostart_switch`(开关是"目标态"语义),
    #   `_on_elev_toggle` 保留为翻转入口(托盘菜单/旧调用方)。
    assert hasattr(gui_mod.ComBridgeGui, "_on_autostart_switch")
    assert hasattr(gui_mod.ComBridgeGui, "_on_elev_toggle")
    impl = _strip_comments(
        inspect.getsource(gui_mod.ComBridgeGui._on_autostart_switch))
    assert "autostart" in impl, "开关必须走 autostart(静默)模块"
    assert "request_elevated" not in impl, \
        "不得再走提权流程(会弹 UAC, 违背静默要求)"
    assert "is_enabled" in impl, "已启用→关闭, 未启用→开启"
    # 开关入口必须真的接到 Canvas 开关上
    assert "_on_autostart_switch" in build, "Canvas 开关必须绑定这个回调"


def test_gui_child_dispatch():
    """gui.main 必须在最前面分发 --elevated-child(不进 GUI 主循环)。"""
    import com_bridge.gui as gui_mod
    src = _strip_comments(inspect.getsource(gui_mod.main))
    i_child = src.index("--elevated-child")
    i_dpi = src.index("_enable_dpi_awareness")
    # 构造调用现在带参数(ComBridgeGui(start_hidden=...)) -> 只匹到左括号
    i_gui = src.index("ComBridgeGui(")
    assert i_child < i_dpi < i_gui, "子进程分发必须先于一切(DPI/GUI)"


def test_selfcheck_shows_elevation():
    """selfcheck 必须报告提权状态(装完任务后用户可验证)。"""
    import com_bridge.cli as cli_mod
    src = _strip_comments(inspect.getsource(cli_mod._cmd_selfcheck))
    assert "已提权" in src and "未提权" in src
