# -*- coding: utf-8 -*-
"""★★★ 回归: GUI 主线程不得有阻塞调用(用户报的 3 秒锁死)。

## 用户报告(2026-10-19)
"程序分别在启动、开启自启服务、关闭自启服务时有 3 秒左右的锁死。
 程序启动时的锁死也源于对自启服务的探测。这个过程应该是静默的,
 不应该全局锁死控制。而且时间也显著慢于 deskflow 的实现,
 后者几乎是一瞬间的"

## 根因
`elevated.task_exists()` 内部 `subprocess.run(["schtasks", ...])`
—— 创建子进程在打包 exe 里可达 1~3 秒。而它被三个地方**在主线程**调用:
  1. `root.after(200, self._refresh_elev_state)`  启动时
  2. `_on_elev_toggle()` 开头                      点按钮时
  3. 其 `done()` 回调里再调 `_refresh_elev_state()` 完成时
后果: 整个界面(按钮/拖动/状态刷新)全部冻住。

## 修复
* 三处全部改为**后台线程查询 + root.after(0, ...) 回主线程更新 UI**
* `task_exists()` 加**会话内缓存**(首次 31ms, 之后 0ms),
  注册/卸载后 `invalidate_task_cache()` 清缓存
* 加防重入标志(_elev_querying / _elev_toggling), 避免连点堆积线程

## 实测(tools/verify_gui_responsive.py, 心跳法测主线程间隔)
  启动阶段(含自启探测): 最大间隔 140ms(首帧渲染), >200ms 次数 0
  自启状态刷新        : 最大间隔  16ms
  缓存命中            : 最大间隔  16ms
  连点按钮 3 次       : 最大间隔  16ms
  修复前是三秒级锁死。
"""
import ast
import inspect
import os
import sys
import textwrap

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def textwrap_dedent(src: str) -> str:
    return textwrap.dedent(src)

import com_bridge.elevated as elev  # noqa: E402
import com_bridge.gui as gui_mod  # noqa: E402

# 由 root.after() 调度 -> 必然跑在主线程(必须无阻塞)
MAIN_THREAD_METHODS = (
    "_refresh_elev_state",
    "_poll",
    "_focus_watch_tick",
    "_check_watchdog",
    "_set_dot",
    "_sync_tray",
    "collect_status",
)


def _body_calls(fn) -> set[str]:
    """收集函数体内的调用名(含跨模块的 self.xxx / xxx.yyy)。"""
    src = inspect.getsource(fn)
    tree = ast.parse(src.lstrip() if src.startswith(" ") else src)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute):
                names.add(f.attr)
                if isinstance(f.value, ast.Attribute):
                    names.add(f"{f.value.attr}.{f.attr}")
            elif isinstance(f, ast.Name):
                names.add(f.id)
    return names


def test_main_thread_methods_have_no_blocking_calls():
    """★ 主线程(after 回调)**直接执行的部分**不得有阻塞调用。

    ★ 检测精度很重要: 这些方法体内常常定义 `worker()` 嵌套函数,
      阻塞调用放在 worker 里是**正确**的(它跑在后台线程)。
      实现: 先找出所有嵌套函数的行号区间, 再从源码里剔除它们,
      只检查剩下的"方法主体"。
    """
    BLOCKING_TOKENS = (
        "subprocess.run", "subprocess.Popen", "subprocess.call",
        "subprocess.check_output",
        "task_exists(", "_powershell(", "time.sleep(",
        "request_elevated(",
    )
    offenders = []
    for name in MAIN_THREAD_METHODS:
        fn = getattr(gui_mod.ComBridgeGui, name, None)
        if fn is None:
            continue
        src = textwrap.dedent(inspect.getsource(fn))
        lines = src.splitlines()
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        fn_node = tree.body[0]
        # 标记要排除的行: 嵌套函数体 + docstring
        excluded: set[int] = set()
        for node in ast.walk(fn_node):
            if node is fn_node:
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.Lambda)):
                lo = node.lineno
                hi = getattr(node, "end_lineno", lo)
                excluded.update(range(lo, hi + 1))
        # docstring(函数体第一个表达式语句若是字符串常量)
        body_stmts = fn_node.body
        if (body_stmts and isinstance(body_stmts[0], ast.Expr)
                and isinstance(body_stmts[0].value, ast.Constant)
                and isinstance(body_stmts[0].value.value, str)):
            ds = body_stmts[0]
            excluded.update(range(ds.lineno,
                                  getattr(ds, "end_lineno", ds.lineno) + 1))
        # 只保留既不在嵌套函数、也不是 docstring、也不是注释的行
        body_lines = [
            ln for i, ln in enumerate(lines, start=1)
            if i not in excluded and not ln.strip().startswith("#")
        ]
        body = "\n".join(body_lines)
        hit = [t for t in BLOCKING_TOKENS if t in body]
        if hit:
            offenders.append((name, hit))
    assert not offenders, f"主线程直接执行的部分有阻塞调用: {offenders}"


def test_refresh_elev_state_is_async():
    """自启状态查询必须在后台线程。

    ★ 2026-10-19: 机制已换成 HKCU Run(注册表读取是微秒级), 但**仍保持
      异步**——"主线程不做任何 I/O"是硬原则, 不能因为"现在很快"就破例。
      旧实现用 schtasks 子进程查询要 1~3 秒, 曾经冻结界面。
    """
    src = inspect.getsource(gui_mod.ComBridgeGui._refresh_elev_state)
    assert "Thread" in src, "必须放到后台线程"
    assert "_post_ui" in src, "结果必须回主线程更新 UI(否则 tk 不安全)"
    assert "autostart" in src, "应当查询静默自启状态"


def test_elev_toggle_is_async_and_silent():
    """自启开关必须在后台线程执行, 且**不弹 UAC**。

    ★ 用户明确要求: "不能加入需要用户确认的过程, 整个过程必须静默"。
    """
    src = inspect.getsource(gui_mod.ComBridgeGui._on_autostart_switch)
    assert "Thread" in src
    assert "autostart" in src, "必须走静默的 autostart 模块"
    assert "request_elevated" not in src, "不得弹 UAC"
    assert "_elev_toggling = True" in src, "必须有防重入"
    # 翻转入口必须真的转调过去(不能是空壳)
    toggle_src = inspect.getsource(gui_mod.ComBridgeGui._on_elev_toggle)
    assert "_on_autostart_switch" in toggle_src


def test_task_exists_is_cached():
    """★ task_exists 必须缓存, 避免每次操作都起子进程。"""
    src = inspect.getsource(elev.task_exists)
    assert "CACHE" in src.upper(), "必须有缓存"
    assert hasattr(elev, "invalidate_task_cache")


def test_task_exists_force_bypasses_cache():
    """★★★ 必须能强制绕过缓存(用户实测"自启注册不成功")。

    故障链: 注册发生在**提权子进程**里, 那里调的 invalidate_task_cache()
    只清子进程自己的内存 —— **父进程的缓存纹丝不动**。
    于是父进程操作完成后查询仍返回旧值 -> 界面继续显示"未启用"
    -> 用户认为"注册不成功"。

    修法: `task_exists(force=True)` + 父进程在 done() 里先作废缓存。
    """
    src = inspect.getsource(elev.task_exists)
    assert "force" in src, "必须支持 force 参数"
    # 真实行为验证: force 与不带 force 都能返回布尔
    elev.invalidate_task_cache()
    v1 = elev.task_exists()
    v2 = elev.task_exists(force=True)
    assert isinstance(v1, bool) and isinstance(v2, bool)
    assert v2 == v1, "同一时刻两者应一致"
    elev.invalidate_task_cache()


def test_gui_reads_real_state_after_toggle():
    """★ 操作完成后界面必须显示**真实当前状态**。

    这个 bug 真实发生过: 旧实现里父进程的 task_exists 缓存不会被子进程
    的 invalidate 清掉 -> 注册成功了界面仍显示"未启用" -> 用户以为失败。
    现在改为读注册表(无缓存层), 但**刷新动作仍必须发生**, 否则界面上
    的按钮/开关会停留在旧值。

    ★ 2026-10-21: 实现位置从 `_on_elev_toggle` 移到 `_on_autostart_switch`
      (浅色版把按钮换成了 Canvas 开关)。不变量不变: 操作完成后必须刷新。
    """
    impl_src = inspect.getsource(gui_mod.ComBridgeGui._on_autostart_switch)
    assert "_refresh_elev_state" in impl_src, \
        "操作完成后必须刷新界面状态"
    # 翻转入口也要能走到刷新(经转调)
    toggle_src = inspect.getsource(gui_mod.ComBridgeGui._on_elev_toggle)
    assert "_on_autostart_switch" in toggle_src
    refresh_src = inspect.getsource(
        gui_mod.ComBridgeGui._refresh_elev_state)
    assert "is_enabled" in refresh_src, "必须读取真实状态"


def test_autostart_messages_reach_file_log():
    """★ 自启结果必须写进日志文件(否则事后无从查证)。

    实测: 用户报"注册不成功"时, 日志文件里一条 [自启] 记录都没有 ——
    因为这些消息只进了界面队列(_log_q), 进程结束就消失。
    """
    impl_src = inspect.getsource(gui_mod.ComBridgeGui._on_autostart_switch)
    assert "gui_log" in impl_src and "write_line" in impl_src, \
        "自启结果必须落盘"


def test_powershell_error_detail_preserved():
    """★ 注册失败时必须保留 PowerShell 的真实错误。

    只报"退出码 1"等于没说: 拒绝访问 / 任务已存在 / 服务被禁用
    的处理方式完全不同, 用户拿到"退出码 1"只会更困惑。
    """
    src = inspect.getsource(elev._powershell)
    assert "LAST_ERROR" in src, "必须记录错误文本"
    # 结果文件里要带上 detail
    child_src = inspect.getsource(elev._run_elevated_child)
    assert "detail=" in child_src, "结果文件必须携带错误详情"
    # 父进程要解析并给出建议
    req_src = inspect.getsource(elev.request_elevated)
    assert "系统信息" in req_src or "advice" in req_src, \
        "必须把系统错误转成可读提示"


def test_cache_invalidated_after_register():
    """注册/卸载后必须清缓存, 否则界面显示旧状态。"""
    src = inspect.getsource(elev._run_elevated_child)
    assert "invalidate_task_cache" in src


def test_no_blocking_in_startup_path():
    """启动路径(首帧后触发的探测)不得同步查询。"""
    build_src = inspect.getsource(gui_mod.ComBridgeGui._build)
    assert "_refresh_elev_state" in build_src, "启动时会调度自启状态刷新"
    # 但该方法本身必须是异步的(见 test_refresh_elev_state_is_async)
    fn_src = inspect.getsource(gui_mod.ComBridgeGui._refresh_elev_state)
    assert "Thread" in fn_src, "查询必须在后台线程"


# --- 异步化的配套约束(都是实测踩出来的) ------------------------------

def test_background_threads_use_post_ui():
    """★★★ 后台线程必须用 _post_ui 投递 UI 回调, 不能裸调 root.after。

    实测踩到: 程序退出后后台线程仍调 `root.after`,
    tkinter 抛 `RuntimeError: main thread is not in main loop`
    (不是 TclError!), 未捕获会让线程带 traceback 崩掉,
    日志里出现吓人的异常, 而实际只是收尾竞态。
    """
    assert hasattr(gui_mod.ComBridgeGui, "_post_ui")
    src = inspect.getsource(gui_mod.ComBridgeGui._post_ui)
    assert "RuntimeError" in src, "必须同时捕获 RuntimeError"
    assert "_closing" in src, "退出中应直接放弃投递"

    # 自启查询/切换与托盘回调都必须走 _post_ui
    # ★ 2026-10-21: 自启的**实际实现**从 `_on_elev_toggle` 移到了
    #   `_on_autostart_switch`(浅色版把按钮换成了 Canvas 开关,
    #   开关是"目标态"语义, 需要显式 want_on)。`_on_elev_toggle` 保留为
    #   翻转入口(托盘菜单/旧调用方)。不变量不变: 后台回调必须走 _post_ui。
    for name in ("_refresh_elev_state", "_on_autostart_switch", "_init_tray"):
        fn_src = inspect.getsource(getattr(gui_mod.ComBridgeGui, name))
        assert "_post_ui" in fn_src, f"{name} 的后台回调必须走 _post_ui"
    # 翻转入口必须转调新实现(不能是空壳)
    toggle_src = inspect.getsource(gui_mod.ComBridgeGui._on_elev_toggle)
    assert "_on_autostart_switch" in toggle_src, \
        "_on_elev_toggle 必须转调 _on_autostart_switch"


def test_log_backlog_preserved_when_window_closed():
    """★★ 日志窗口没打开时, 日志必须留在主窗口缓冲里, 不能丢弃。

    实测踩到: 早先 `_poll` 只在 `_log_win is not None` 时才 append,
    于是日志窗口从未打开时**所有日志被取出直接丢掉** ——
    托盘启动失败、启动报错这些关键信息全部消失, 无从查证。
    """
    poll_src = inspect.getsource(gui_mod.ComBridgeGui._poll)
    assert "_log_backlog" in poll_src, "必须先存进主窗口缓冲"
    # 只看可执行代码行(注释里也会提到这些名字, 不能参与比较)
    code = [ln for ln in poll_src.splitlines()
            if not ln.strip().startswith("#")]
    i_backlog = next(i for i, ln in enumerate(code)
                     if "_log_backlog.append" in ln)
    i_forward = next(i for i, ln in enumerate(code)
                     if "_log_win.append" in ln)
    assert i_backlog < i_forward, \
        "必须先 append 到 backlog, 再转发给日志窗口"

    init_src = inspect.getsource(gui_mod.ComBridgeGui.__init__)
    assert "_log_backlog" in init_src, "缓冲必须初始化"

    open_src = inspect.getsource(gui_mod.ComBridgeGui._open_log_window)
    assert "_log_backlog" in open_src, "首次打开日志窗口时要灌入历史"
