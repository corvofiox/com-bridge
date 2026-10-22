# Com Bridge 技术文档

实现细节、设计取舍与踩坑记录。面向二次开发与排障。

以下为实现细节与踩坑记录，供二次开发与排障参考。

## 架构

```
┌──────────────────┐  键鼠事件(TCP)   ┌──────────────────┐
│   主电脑 server   │ ───────────────▶ │   副电脑 agent    │
│                  │                  │                  │
│  RawInput 捕获    │  音频(UDP 48k)   │  WASAPI loopback │
│  越界判定/hysteresis│ ◀─────────────── │  本地静音        │
│  渲染 peer 音频    │                  │                  │
└──────────────────┘                  └──────────────────┘
```

两条独立链路, 各自可单独使用:

* **输入(主 → 副)**: 主电脑用 Raw Input(带 `RIDEV_INPUTSINK` 的消息窗口)捕获全局键鼠, 按显示器几何判定光标越界(带 2 帧确认 + 8px 迟滞), 越界后把事件 UDP 发给副电脑, 副电脑用 `SendInput` 注入。光标拖回时副电脑把事件回传, 主电脑把自己被钳在边界的光标往屏内推。
* **音频(副 → 主)**: 副电脑以 WASAPI shared loopback 抓整机输出 → 20ms 分包, 按**主机时钟域**打时间戳 → UDP → 主电脑按时间戳放入抖动缓冲(目标 60ms), 以渲染端点格式对齐、微重采样抵消晶振偏差, 写入自己的默认输出端点。主电脑**不做**本机音频的软件混音(Windows 在端点混音器里完成), 因此本机声音零额外延迟且无反馈回路。

## 发布包与 GUI 细节

`dist/` 目录是一个可直接分发的包, 拷到两台机器即可运行, 目标机无需安装 Python。
发布包里**只有一个可执行文件**:

| 双击 | 说明 |
|---|---|
| `com-bridge-gui.exe` | 图形界面: 选角色/配置/启动停止/托盘/实时状态/日志 |

GUI 窗口里有角色选择(主电脑/副电脑)、屏幕方位、主电脑 IP、延迟目标、启动/停止按钮;
实时状态在独立次级窗口里(顶栏「状态」打开),运行日志在托盘右键菜单「查看日志」打开
(2026-10-21 按用户要求,顶栏只保留图标与名称,日志入口移到托盘)。
关窗口只隐藏到托盘, 托盘右键"退出"才彻底关闭。

方位(`side`)默认 `right`, 若副电脑在主电脑屏幕的其它方向, 在 GUI 里选择。**两边的 side 必须一致**。

界面是 v6 设计(0.4.0, 2026-10-21 重做): **两套配色**——深色(炭黑底 + 悬浮卡片 + 蓝强调)
与浅色(浅灰蓝底 + 白卡片 + 靛蓝强调),布局与尺寸完全一致。顶栏「深色/浅色」按钮
可即时切换并记住选择;首次运行默认 `auto`,跟随 Windows 的「应用深色模式」设置。

视觉语言:卡片圆角 16 + 1px 极淡描边 + 高斯阴影;每行设置带图标徽章;选项卡是分段
控件;主按钮为渐变 + 下方光晕。所有圆角/渐变/阴影均由 `ui_render` 以 4x 超采样渲染,
文字则在**降采样之后**按最终分辨率绘制(否则细笔画会被平均掉、整屏发虚——踩过)。

注意: exe 未签名, 首次运行可能触发 SmartScreen 提示("更多信息 → 仍要运行"); 如需控制 UAC 提权窗口, 以管理员身份运行。

## 从源码运行

依赖: Python 3.10+、`comtypes`、`pycaw`、`numpy`(见 `pyproject.toml`), 两台均为 Windows 10 1809+。

```powershell
pip install -e .
python -m com_bridge.cli gui                 # 图形界面
python -m com_bridge.cli server --side right    # 主电脑(CLI)
python -m com_bridge.cli agent --side right     # 副电脑(CLI)
```

启动后副电脑扬声器会被自动静音(避免与主电脑的声音形成时间差叠加); `--keep-audio` 可关闭。

退出: 两边都 `Ctrl+C`, 副电脑会自动解除静音。

> 命令行子命令(`server` / `agent` / `selfcheck` / `monitors` / `loopback`)
> 只在**源码运行**时可用: `python -m com_bridge.cli selfcheck`。
> 发布包不再包含 console 版 exe(早期版本的 `com-bridge.exe` 与
> `server.bat`/`agent.bat`/`selfcheck.bat`/`gui.bat` 均已移除)。

## 端口与防火墙

| 端口 | 协议 | 用途 |
|---|---|---|
| 24800/UDP | 广播 | 服务发现信标 |
| 24802/TCP | 控制 | 对时(NTP 四时间戳)、能力协商 |
| 4010/UDP | 音频 | PCM 帧 |
| 24801/UDP | 输入 | 键鼠事件(双向) |

两台机器都需在防火墙放行以上端口(专用网络)。

## 自环测试(单机)

```powershell
python -m com_bridge.loopback_test --seconds 12 --tone-hz 440
```

同一进程内同时扮演 server 与 agent(连 127.0.0.1), 用 440Hz 合成音代替真实采集, 验证"传输 → 对时 → 对齐 → 渲染"整条链路。`--tone-hz 0` 切换为真实 loopback 采集(单机上会形成回声, 仅用于观察帧统计)。

## 品牌图标(2026-10-21 定稿)

图形是 **「H」**:两根竖直圆角柱 + 一条横杠 = 两台电脑 + 一条连接链路。
几何**只在 `com_bridge/brand.py` 定义一次**,四处共用,避免各自漂移:

| 用途 | 实现 |
|---|---|
| 窗口 / 任务栏 / Alt+Tab | `com_bridge/appicon.py`(base64 内嵌 9 个尺寸) |
| `.exe` 文件图标 | `com-bridge.ico`(16/24/32/48/64/128/256 七档) |
| 托盘 | `tray._make_channel_icon` 走 GDI 逐像素画,叠加黄/绿状态点 |
| 主界面左上角标记 | `ui_render.brand_icon` |

重新生成:`python tools/icons/gen_app_icon.py`(末尾自带形状自检:柱身行须为
「两段」、横杠行须连通两柱,不合格非零退出)。

## 关键设计决策

1. **主电脑不做软件混音**。若把本机 loopback 也采进来混一遍再写回同一块声卡, 会形成"采集混合结果 → 再混 → 再写回"的闭环, 幅度逐圈累加只能靠削波压住, 表现为持续自激失真。本机音频由 Windows 端点混音器与 peer 流自然相加, 本机零额外延迟。
2. **对时用单调时钟, 且 offset = 对端 − 本地**。`time.monotonic()` 在两台机器上纪元不同(各自的开机时刻), 差值可能是数小时; 媒体时间戳必须与对时用同一时钟域。offset 的符号推导见 `com_bridge/clock.py` 模块 docstring 与 `tests/test_clock.py` 的固定向量——写反后单机测试毫无症状, 双机部署才爆炸。
3. **抖动缓冲的到期判定**。`ts` 记录采集时刻, 播出时刻是 `ts + target_ms`; `pop_aligned` 的 horizon 必须往回退 `target_ms`, 否则刚到的包立刻"到期", 缓冲等于不存在。
4. **取出预算严格守恒**。渲染循环按 `max_frames` 预留缓冲空间, `pop_aligned` 若允许整块超出预算, 超出部分会被 `write()` 丢弃——已从队列摘除并计入统计的帧静默消失。块必须切分而不是丢弃。
5. **注入的合成事件必须过滤, 且必须打标记**。`SendInput` 产生的事件同样会投递给 Raw Input 监听者。仅靠 `hDevice == NULL` **不可靠**(绝对移动事件常带非 NULL hDevice), 因此所有注入都带 `dwExtraInfo` 标记(`InputInjector.TAG`), 监听器按标记无条件丢弃。不过滤/过滤不全会造成两机之间的事件回声。
6. **穿越期间绝不注入"钳制"事件**。Windows 本来就会把光标钳在屏幕边缘; `clamp_to_edge` 只把坐标限制在屏幕范围内, 对已在屏内的坐标原样返回, 因此那种注入既无效又只会制造回声事件(表现为光标在边缘被疯狂弹回)。
7. **跨屏状态机的单一判定者**。进入穿越态 = 抵边 + 持续朝外推(只看**位移方向**, 绝不看绝对坐标——坐标会被自身的注入干扰); 退出由副电脑的回程信号决定(光标抵达回传边), 主电脑保留**光标回屏内**与**时间**两重兜底。两端各按自己的计时器抢控制权会造成弹回。
8. **NAT 环境下必须靠心跳打洞, 不能靠 TCP 源地址**。主路由若是虚拟机里的 OpenWrt(实测 `192.168.10.1` 带 Hyper-V MAC `00:15:5D`), 副电脑的连接会被 SNAT, 主电脑从 TCP accept 学到的"源地址"其实是**网关**。此时音频正常(副电脑主动发, NAT 放行)而鼠标完全不动(主电脑主动发, NAT 不转发)——极具迷惑性。因此: agent 在 `CAPS` 里上报 `adv_host`, 并且**双向**周期性发 UDP 心跳, 双方各自从"实际收到的心跳源地址"学习回程地址。
9. **端口限制型 NAT 要求回程包的源端口 == 对端发包的目标端口**。实测症状极有指向性: 心跳(从 24801 发)全部到达, 输入事件(从 InputSender 的临时端口发)全部丢失——两者目标地址完全相同。因此 `InputSender.use_socket()` **借用输入接收端的 socket** 发送; 且 `server.start()` 里 `_start_input()` 必须在 `_start_discovery()` **之前**(否则 agent 连上时接收端还没建好, 借不到 socket)。
10. **跨屏位置控制照搬 Deskflow 的三件套**(参考其 `server/Server.cpp`):
    * **交接时绝对定位**: `switchComputer()` 会调 `enter(x, y, ...)` 把坐标绝对告知目标机。只发相对位移的话, 副机光标从"上次停在哪"开始累积, 进入位置完全不可控(实测副机双屏时光标停在右屏右边缘蹭)。实现: `EV_MOUSE_PLACE` 事件 + 副机 `move_absolute`。
    * **位置传比例而非像素**: `mapToFraction`/`mapToPixel`——主机 50% 高度出去, 副机就在 50% 高度进来, 分辨率不同也成立。实现: `position_fraction`/`fraction_to_pixel`(0..65535 定点承载)。
    * **边界基准 = 整机完整 shape**: 多显示器时 `_main_monitor()`(面积最大的那块)会把回传边算在**某一屏的边缘**(如 x=1920)而非整机最左(x=0)——正是"往左拉一点就回主机"的原因。实现: `virtual_desktop()`。
    * **回传只看位置, 不看时间**: Deskflow 的 `onMouseMoveSecondary` 纯位置判定, 没有"持续拉动 N 秒"的条件。曾有 `RETURN_INTENT_S=0.7s` 的 workaround(因进入位置不可控而用时间猜意图), 副机双屏时用户正常左移 0.7s 就被误拽回主机——绝对定位修好后已整体移除。
11. **方位(side)由主电脑下发**。side 的语义是"副电脑位于主电脑的哪一侧", 副机取对侧作为回传边。让用户在两台机器上各填"对方方位"必然填反(副机填 left 时回传边算反, 主机转发来的每个位移都被判成"要回主机"而原样退回, 光标进不去副机)。现在主电脑把自己的 side 编进反向心跳(4 字节心跳的第 2 字节)下发, 副机自动采用; GUI 上副机角色的方位框禁用。
12. **光标隐藏必须"接管鼠标输入", 光有全屏窗口不够**。hider 窗口(全屏 + `WS_EX_TRANSPARENT` + 窗口类为全透明光标)是必要条件但**不充分**: 实测证明 `ShowCursor` 的可见性计数器和窗口类光标都是**按线程**生效的, 真正说了算的是"正在接收鼠标输入的那个线程"——hider 线程若不接管鼠标输入, 我们在自己线程里 `ShowCursor(FALSE)` 对屏幕毫无影响, 用户看到的正是"主机光标没有隐藏"。Deskflow 为此在 `deskLeave` 里调 `SetActiveWindow`(主机)/`SetCapture`(副机); 本项目统一用 `SetCapture`(不抢前台, 因而不破坏主机键盘焦点, 且 `hide_on_motion` 能收到本机物理移动)。另外 `ShowCursor` 必须**循环调用**直到计数器达标(Deskflow 同款)。实测生产序列: `hide()` 后本线程计数器 `-1`、`GetCursorInfo` 报 `bShowing=False / hCursor=NULL`, 之后持续 50 次绝对定位注入仍保持隐藏; `show()` 后全部复原。
13. **钉住必须钉"交接时的固定点", 且归一化要除以 w-1**。穿越期间主机光标被钉在边缘, 这是"两台鼠标协同一体"的前提。两个真实教训: (a) 归一化写成 `int((v-origin)*65535/w)`(除以 w 且截断)会让每次注入欠一点; (b) 钉住传的是**读回来的当前坐标**, 等于切向(垂直方向)零约束 —— 用户顶墙猛推时手腕自然上旋, 误差逐事件累积, 光标就沿边缘一路爬到角落。用户实测的"主机鼠标沿边缘一路向上, 最终停在右上角"正是两者叠加。正确做法: 照搬 Deskflow 的 `deskMouseMove` 公式 `65535*(v-origin)/(span-1)+0.5`(幂等, 同一目标反复注入落在同一像素), 并像 Deskflow 存 `m_xCenter/m_yCenter` 那样, 在**交接那一刻**定下钉住点后不再读回。
14. **穿越期间必须用低级钩子"吞掉"本机的物理键鼠, 否则点击会落在主机**。这是用户报的"进入副机后鼠标点击还是落位在主机"的根因: `RawInput`(`RIDEV_INPUTSINK`)是**只观察、不消费**的 —— 你在主机按下鼠标, Windows 照样把这次点击送给主机的焦点窗口, 我们只是"顺手复制"一份发出去。Deskflow 靠 `WH_MOUSE_LL`/`WH_KEYBOARD_LL` 的 `return 1` **吞掉**事件(`MSWindowsHook` 的 `kHOOK_RELAY_EVENTS` 模式), 物理点击根本到不了本机。本项目新增 `LowLevelHook`, 其架构经**实测**确认可行:
    * **钩子吞掉事件后 RawInput 仍照常收到它**(实测 `eaten=20` 而 `total 0→20`, 光标纹丝不动证明吞生效)——RawInput 走设备级管线, 与事件是否被系统应用无关。因此架构是"**钩子只负责吞, RawInput 仍是唯一事件来源**": 位移精度最高, 且钩子回调里无需任何耗时工作。
    * 被吞掉的事件**位移数值完整无损**(实测请求 (13,7)/(−9,4)/(21,−6), 读到的就是这三个值)。
    * 一个附带的巨大简化: 位移被吞掉后**主机光标根本不会移动**, 于是"钉住光标"从根上不再需要 —— 顺带消灭了"归一化丢像素""沿边缘爬角落"一整类 bug(`_pin_cursor_to_edge` 仅保留为钩子不可用时的退化路径)。
    * **两条硬约束**: 回调必须极快(低级钩子有约 300ms 超时, 超时会被 Windows **静默摘掉**, 表现为"用着用着点击又落回主机", 极难自查); `WINFUNCTYPE` 回调对象必须**常驻引用**(被 GC 回收就是野指针 → 进程直接崩溃且无 traceback, 实测踩到)。
    * **我们自己注入的事件(带 `InputInjector.TAG`)必须放行**, 否则副机回程输入会被本机钩子吃掉, 用户被困副机; `Caps/Num/Scroll` 三键即使在 relay 模式也放行, 以保持本机指示灯同步(Deskflow 同款)。所有退出路径(回程信号/时间兜底/`stop()`)都必须**先关 relay** —— 否则用户面对一台"键鼠全无响应"的电脑。
15. **穿越期间键盘必须跟着走, 且不能用 RawInput 单条件过滤**。旧代码写的是 `if ev.is_mouse:`, 把键盘整个丢掉了 —— 用户在副机上打不了字。控制权在谁手里、键盘就归谁: 主机侧靠钩子吞掉物理键盘并转发(副机 `SendInput` 注入), 主机**不**在本机注入键盘(否则两边同时收到按键)。副机侧的 `_on_return_event` 也**只回传鼠标位移**(那是"拖到边界"的手势本身): 副机没有钩子, 若在那里回传键盘, 物理按键会同时落到副机焦点应用和主电脑, 表现为"两边各打一半"。
16. **注入相对位移必须临时禁用指针加速**。Windows 对**注入的**相对位移同样应用加速曲线 —— 实测请求 (40,25)×5 实际走 (457,285), 放大 **2.29 倍**。后果: 副机鼠标手感与主机完全不同, 用户会觉得"飘、不听使唤、划不动", 而且毫无线索指向加速。照搬 Deskflow 的 `deskMouseRelativeMove`(注释原文: *"relative moves are subject to cursor acceleration which we don't want. so we disable acceleration, do the relative move, then restore acceleration"*): `SPI_GETMOUSE` 存旧值 → `SPI_SETMOUSE {0,0,0}` 关加速 → 注入 → `SPI_SETMOUSE` 还原。实测补偿后严格 1:1, 且用户设置分毫不动(用户原本已禁用加速时不写入; 注入抛异常也必须还原, 否则用户鼠标手感被永久改掉, 故用 `try/finally` + 锁串行化)。
17. **`RAWMOUSE` 结构体必须显式补 2 字节填充 —— 否则点击永远无法转发**。这是"进入副机后点击完全没作用、但鼠标能移动"的根因, 也是本项目最隐蔽的一个 bug。Windows 的定义里 `usFlags` 后面是一个 `union { ULONG ulButtons; struct { USHORT usButtonFlags; USHORT usButtonData; } }`; union 含 `ULONG` 所以**按 4 字节对齐**, 编译器会在 `usFlags` 之后插入 **2 字节填充**, 使 `usButtonFlags` 落在 **offset 4**、`usButtonData` 落在 offset 6。而 ctypes 按字段自然对齐, 把 2 字节的 `usButtonFlags` 直接排在 **offset 2** —— 于是读到的"按钮标志"其实是 `usButtonData` 的高半部分, **真正的标志位永远是 0**。
    * 后果: `_emit_mouse` 里每一个 `if bf & RI_MOUSE_*` 都永不成立, **点击与滚轮事件一次都没有被转发出去**; 而位移字段 `lLastX/lLastY` 恰好在 offset 12(两种布局一致), 所以移动始终正常 —— 症状正是"能跨屏移动, 但点击完全没作用到副机"。
    * 证据是**字节级**的(不依赖活动光标, 因此在被其它 KVM 占用的机器上也能确定): 实测捕获的左键按下原始字节为 `00000000 01000000 ...`, 正确布局解出 `usButtonFlags=0x0001`, 错误布局解出 `0x0000`。回归见 `tests/test_rawmouse_layout.py`(用实测字节向量锁死每个字段偏移)。
    * `RAWKEYBOARD` 也一并锁定布局(避免同类错误)。
18. **所有注入都必须打 `dwExtraInfo` 标记, 不只是位移**。历史实现里只有 `move_relative`/`move_absolute` 打了 `InputInjector.TAG`, 而 `button()`/`wheel()`/`key()` 漏了 —— 尽管类文档声称"所有注入都带标记"。后果: 主机侧的低级钩子**只放行带 TAG 的事件**, 于是自己注入的点击被**自己吞掉**(回程点击永远到不了本机); `RawInputListener` 也无法把它识别为合成事件, 形成回声。三类注入现在都打标记, 且用 spy 断言(`tools/verify_button_forward.py` 覆盖 5 种注入全部带 TAG)。
19. **RawInput 的过滤判据顺序: 先 TAG、后 `hDevice == NULL`**。`hDevice == NULL` 是"合成事件"的常见特征但**两个方向都会出错**: 反向会漏判(绝对移动等注入常带非 NULL hDevice → 回声), 正向会误杀(**被低级钩子吞掉的物理事件实测也可能 hDevice 为 NULL** → 真实输入被当合成丢弃, 表现与钩子完全失效一样)。因此先用 TAG 精确识别自己的注入, 再把 `hDevice == NULL` 当兜底(仍能挡掉别的程序的合成事件)。
20. **进入点必须推过"跳入区"** (照搬 Deskflow `Server.cpp` 的 `avoidJumpZone`)。交接定位的落点如果恰好在回传边上, 进入瞬间一次朝主机的轻微抖动就会被判成"要回去"而弹回, 用户感觉"进不去副机"。现在 `fraction_to_pixel` 把落点向屏内推 `ENTRY_JUMP_ZONE_PX = 12` 像素, 与 Deskflow 语义一致。
21. **穿越态有三重自动退出防护, 但没有"时间兜底"**(照搬 Deskflow: 空闲绝不退出): (a) 副机回程信号(正常路径); (b) **心跳丢失看门狗** `PeerWatchdog`——副机心跳静默超过 `PEER_LOSS_S=5s` 判失联, 强制交回控制权(替代早期 `RETURN_FALLBACK_S=2.5s` 的活动间隙兜底——那个把"用户在副机停手"误判成"回程丢失", 造成"停几秒鼠标自动回退"); (c) **会话锁防护** `_start_session_guard`——0.5s 轮询输入桌面名(照搬 Deskflow `checkDesk`), 锁屏/UAC 安全桌面切换时强制交回, 否则穿越态跨锁屏存续, 解锁后键鼠失灵。另加 **钩子健康自检**: 穿越态下主机光标本不该动, 若 `get_cursor_pos` 位移**超过容差**(`HOOK_PROBE_TOLERANCE_PX=2`, 排除 hider 自己的 ±1px 提交抖动)且**不在注入窗口期内**, 说明低级钩子被系统静默摘除(约 300ms 超时机制)。此时**先请求重装钩子**(通常一次即恢复, 见决策 29), 只有重装反复失败才退化为钉住兜底 —— 早期版本一检测到位移就直接转钉住, 键盘侧从此无人吞键, 正是 BUG1 的成因。
22. **穿越交接时同步修饰键快照** (照搬 Deskflow `enter` 的 `toggleMask`)。用户"按住 Shift 跨屏拖选"时, 副机必须知道主机还按着哪些修饰键, 否则拖选变成普通移动。实现: 主机进入穿越态时发 `EV_MOUSE_MODS`(code 承载 `MODFLAG_SHIFT/CTRL/ALT/WIN` 位掩码, 读 `GetKeyState & 0x8000`), 副机对"主机按着而本机没按"的修饰键注入 down 并纳入**卡键防护**跟踪(`_down_keys`), 因此即使后续修饰键的 key-up 包丢失, 心跳超时看门狗也会释放它——两套防护互相兜底。
23. **UDP 键盘必须防"卡键"** (照搬 Deskflow `updateKeysCB`/`leave`)。UDP 会丢包, 丢掉一个 key-up, 副机上那个键就永久卡住(持续输入字符)。副机维护按下状态表 `_down_keys`/`_down_buttons`, 三处触发兜底释放: 重连成功后(Deskflow `cleanupComputer`→`disable` 语义)、心跳静默超 `STUCK_KEY_AFTER_S=5s` 的看门狗、`stop()` 退出前。释放用幂等注入(重复 up 无害)。
24. **被键盘钩子吞掉的按键不会产生 RawInput —— 键盘必须由钩子自产事件**(与鼠标不对称, 实测决定性实验 `tools/diag_hook_kbd_rawinput.py`)。鼠标被 LL 钩子吞掉后 RawInput 照常送达(决策 14 的架构对鼠标成立), 但键盘**不行**: 四阶段实验显示钩子 `return 1` 吞掉按键后 RawInput total 纹丝不动(无钩子时同注入 total+2)。因此生产架构是"鼠标 = 钩子只吞 + RawInput 作事件来源; **键盘 = 钩子既吞又是事件来源**": `_kbd_proc` 在吞键的**同一回调里入队** `InputEvent(EV_KEY, ...)`(up/down/扩展键/WM_SYSKEY* 全覆盖, 与 Deskflow `keyboardHookHandler` 的 `PostThreadMessage` 同型), 由钩子线程消息循环排空后交给与转发链同一个 `_on_input_event` —— 穿越态门槛与计数统计天然复用。★ 注意是"入队"而非"直接回调": 回调里做转发工作会超时被摘钩(决策 29)。
25. **hider 区分"注入 vs 物理"移动不能靠 `GetMessageExtraInfo`, 用注入时间戳**(实测三坑: ①系统合并连续注入后, 消息的 extra 可能取链中某条 —— 60 对带标记注入合并成 2 条消息, 1 条 extra 恰好非标记, hider 被误顶掉; ②窗口激活/`SetWindowPos`/捕获变化会**合成** extra=0 的 `WM_MOUSEMOVE`, 恰好长得像物理移动; ③文档语义有歧义)。现行判据: `InputInjector._last_move_ts` 在每次注入移动成功时打点, `CursorHider._is_injected_motion()` 查 `last_move_age() <= INJECTED_MOVE_WINDOW_S(0.25s)`。与旧"hide() 宽限期"的本质区别: 窗口由**最近一次注入**锚定 —— 主机持续转发注入时窗口一直在(副机光标不误恢复), 主机停注入 0.25s 后副机物理鼠标一动立即生效。
26. **光标隐藏的决定性条件是"hider 窗口真的成为前台窗口" —— 而 `SetForegroundWindow` 会被系统静默拒绝**(BUG2 的真正根因, 实测 `tools/diag_hider_state.py` 稳定复现)。Windows 只允许前台进程(或刚收到用户输入者)抢前台; 被拒时窗口矩形、可见性、`SetCapture` **全都看起来正常**, 但该线程收不到任何鼠标消息(`moves=0`), `ShowCursor` 计数器永不提交 —— 光标就留在屏幕上。成功与失败的唯一可观测差异是 `GetForegroundWindow() == hwnd`:
    * 成功: `is_fg=True` `moves=48` `cursor_showing=False`
    * 失败: `is_fg=False` `moves=0`  `cursor_showing=True`
    修法: `_force_foreground()` 先直接抢, 失败则合成一次 **ALT 按下/抬起**(Windows 官方记载的解锁手法, 单键无任何命令语义)后再抢, 最后退回 `AttachThreadInput` 路径; 并把结果记进 `_activated` 供验收判定。判据用 `GetCursorInfo().flags & CURSOR_SHOWING`(系统直接给出可见性, 不需要目视或截图), 见 `tools/verify_bug2_cursor.py`。
27. **两个角色都用全屏 hider 窗, 不用 Deskflow 的 1x1 窗**。Deskflow 的 1x1 窗之所以有效, 是因为它同时把光标**钉在窗口中心**(`deskLeave` 末尾的 `deskMouseMove(m_xCenter, m_yCenter)`); 我们的副机光标是**会移动**的(主机持续转发注入), 1x1 窗盖不住它 —— 曾据此实现"窗口跟随光标", 但 `WM_MOUSEMOVE` 会被系统**合并**(实测 60 次注入只送达个位数条), 跟随逻辑永远追不上。代价是客户区坐标恒非零, 于是"注入 vs 物理"不能再用 Deskflow 的客户区坐标判据 —— 那本来就是次判据, 主判据是注入时间戳(决策 25), 所以代价为零。
28. **"本机物理鼠标动了"必须连续多条 + 落定静默期才算数**。两道防护都是为滤掉**孤立的合成消息**: ①窗口激活/`SetWindowPos` 会合成一条 `WM_MOUSEMOVE`, 它可能被延迟投递到注入窗口之外, 单条判据会把它误判成物理移动而收起 hider; ②`hide()` 自身为提交光标可见性会注入 ±1px 抖动, 那两条消息的延迟回声同样会落进窗口外。故要求连续 `PHYSICAL_STREAK_N=2` 条非注入移动, 且 `hide()` 后 `HIDE_SETTLE_S=0.4s` 内一律不计。实测把隐藏成功率从约 2/3 提到 24/25 探针全绿(`tools/diag_hider_state.py`)。
29. **钩子回调里绝不能做转发工作, 且必须能自愈重装**(BUG1 的根因)。低级钩子有约 300ms 的 `LowLevelHooksTimeout`, 回调超时会被 Windows **静默摘钩**(无错误码、无回调); 摘钩后物理键不再被吞 —— 主机焦点应用收到一份, 钩子自产/转发又给副机一份, 用户看到的就是"鼠标进入副机后, 主机和副机键盘同步生效"。因此照搬 Deskflow `keyboardHookHandler` 的 `PostThreadMessage` 范式: `_kbd_proc` **只入队 + 唤醒**, 真正的转发在**独立转发线程** `_key_worker_loop` 上做。
    * ★★ **转发工作也不能放在钩子线程里**(用户第二轮实测: 鼠标好了但键盘仍两端生效)。原因: 系统只在钩子线程**处理消息**时才调用钩子过程, 所以钩子线程一忙于转发, 回调较重的**键盘**钩子就得不到服务而超时被摘, 而回调极轻的**鼠标**钩子幸存 —— 症状恰好是"鼠标能跨屏、键盘两台同时生效"。修法: 钩子线程收到 `WM_APP_KEY` 只 `_key_wake.set()`, 工作全在独立线程; 回调路径**连锁都不加**(`deque.append` 在 CPython 下原子, 加锁的争用会阻塞回调)。
    * ★★ **两个钩子是独立句柄, 必须分别检测存活**。`WH_KEYBOARD_LL` 还是**链式**钩子: 系统按"后装的先调用"依次调用, 任何一环返回非零, 链就在那里断掉, **后面的钩子永远不会被调用**。若别的程序(Deskflow/Synergy/输入法全局热键/宏工具/安全软件)的键盘钩子排在我们前面并吞掉按键, 我们的 `_kbd_proc` 一次都不执行 —— `kbd_eaten` 恒为 0 而鼠标一切正常。判据(基于实测: 被键盘钩子吞掉的键**不产生 RawInput**): **穿越态下若 RawInput 仍报来键盘事件, 就是没吞住**。
    * ★ **启动即自检**: `probe_keyboard_hook()` 注入一个带 TAG 的键, 看**自己的回调**能否看到它(TAG 分支在一切过滤之前)。这是"回调到底能不能被调用"的确定性判据, 不依赖用户打字。失败时启动日志直接说明原因, 而不是让人猜。
    * ★ **自愈要验证, 不能"重装完就算"**: 重装可能因句柄竞争、时机窗口没真正生效。`_kbd_heal_loop` 每次重装后**用 `probe_keyboard_hook()` 复验**(注入带 TAG 的键看自己的回调能否收到; 判据不依赖用户打字), 最多 `KBD_HEAL_ATTEMPTS=3` 次, 全程在后台线程(绝不阻塞输入转发)。`kbd_calls` 计数(回调被调用的总次数)已加入 GUI 状态行 —— 现场可区分"回调没被调用(恒 0, 键被别的程序先收走)"与"被调用但没吞(泄漏, 会触发重装自愈)"。
    * ⚠ 检测必须排除 **`PASSTHROUGH_VKS`**(Caps/Num/Scroll): 它们**故意放行**, 本来就产生 RawInput, 不排除则每按 CapsLock 都误判。
    * `diag()` 还新增 `kbd_calls`(回调被调用的**总次数**, 在任何过滤之前自增)—— 没有它就无法区分"回调没被调用"与"被调用但全部放行", 用户实测的 `kbd_eaten=0` 正属于前者却被误读成后者。
    * 自愈: 健康自检发现异常时先 `request_reinstall()` 重装(通常一次即恢复), **只有反复失败**才退化为钉住模式 —— 此前一检测到就转钉住, 键盘侧从此无人吞键, 正是 BUG1 的成因。重装次数**每轮穿越重置**, 且重装失败只能置 `installed=False`, 绝不能抛(抛出去会让钩子线程整个退出)。
30. **回程必须继承副机的实际方位, 不能只发相对位移**(BUG3②)。回程只发相对位移时, 主电脑光标从"它被钉在哪"开始累积 —— 而穿越期间主机光标恰好被钉在**进入时的位置**, 于是"从副机哪里回来"完全丢失: 落点永远是当初跳过去的那个点(用户原话: "依然从主机跳转到副机时指针的位置回退")。修法: 与去程 `EV_MOUSE_PLACE` 完全对称, 新增 `EV_MOUSE_RETURN`(a = 沿回传边的切向比例 0..65535), 副机在越过回传边**那一刻**发一次(状态翻转处, 不重复发), 主机用 `fraction_to_pixel(f, self.side)` **绝对定位**。验证见 `tools/verify_bug3_return.py`: 副机 y=100 回传 → 主机落 y=141(而钉住点是 540)。
31. **比例↔像素的逆变换必须用 `floor(f*extent)`, 不能用 `round()`**。`position_fraction` 给出 `f = (v - origin + 0.5)/extent`, 反解得 `v = f*extent - 0.5 + origin`; 取整要用**四舍五入到最近整数**, 即 `floor(f*extent - 0.5 + 0.5) = floor(f*extent)` —— 那个 `+0.5` 偏置与 `floor` 恰好抵消。**千万别写成 `int(round(f*extent - 0.5))`**: Python 的 `round()` 是 **banker's rounding**(半值取偶), 在 `f*extent` 恰为整数时会算错(实测 `extent=3, f=1/3` 得 0 而正确值是 1)。这是本次修复中**我自己引入又修掉**的一个真实回归, 已由 `tests/test_bugfix_three.py::test_frac_to_axis_rejects_bankers_rounding` 锁死。
    * 附带结论(排除法): BUG3① 的"进入方位略有偏差"**不是换算误差**。全链路实测(host px → f → 65535 量化 → agent px → 注入 → 读回)在 768/900/1080/1200/1440/2160 各高度上**误差 0px**, 跨分辨率最大偏差 0.64px(纯量化残差)。
32. **进程必须声明 per-monitor DPI 感知 —— 这才是 BUG3① 的真正根因**(用户提供分辨率后定位)。进程默认是 `DPI_UNAWARE`, 此时 Windows 会对**所有**坐标类 API 做 **DPI 虚拟化**: 按当前显示缩放把真实像素换算成"逻辑像素"再返回。本机是 96 DPI(100%)所以看不出问题; 而**副机 2560x1600 通常是高 DPI 屏**(150%/200%), 于是 `GetSystemMetrics(SM_CXVIRTUALSCREEN)` 返回缩放后尺寸(2560 在 200% 下读成 1280), `GetCursorPos`/`SetCursorPos`/`SendInput` 也都按逻辑坐标解释 —— "按比例算出的像素"与"屏幕真实像素"根本不是同一套坐标系, 落点自然偏移, 表现就是**方位没有等比例对齐**。
    * 修法: `input._enable_dpi_awareness()` 在**`input.py` 模块导入时**就调用 —— 该模块拥有全部坐标 API, 导入早于一切使用, 因此**任何调用方都绕不过**(包括直接 `Server()` 而不经 `cli.main()` 的路径)。函数幂等, `cli.main()`/`gui.main()` 中的重复调用无副作用。优先 `SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2)`, 退 `shcore.SetProcessDpiAwareness(2)`, 再退老 `SetProcessDPIAware()`。实测本机由 `unaware` 变为 `per-monitor-v2`。
    * 选 per-monitor 而非 system-aware: 多显示器各自缩放不同时, system-aware 仍会被虚拟化, 只有 per-monitor 才"永远拿到物理像素"。
    * 诊断: `input.dpi_awareness()` 可随时查询当前模式; `com-bridge monitors` 打印的尺寸现在就是**物理像素**; server/agent 启动日志也会打印 `DPI 感知=...`。
33. **~~"链首抢占"推论已被推翻并撤除~~ —— 教训: 合成实验证明不了生产故障**。曾基于"低级钩子链式调用 + 后装者先调用"的同进程实验, 加过三套机制(每次穿越 `reclaim_head()` 重装 / `_kbd_watch_loop` 2 秒轮询复验 / `kbd_bypassed` 判据), 全部已撤除。
    * **推翻它的证据是用户给的**: Deskflow **只装一次**钩子、从不抢占, 却长期有效运行 —— 若"谁后装谁赢"在生产中真的成立, Deskflow 早就失效了。同进程双钩子实验观察到的翻转, 是**合成环境**的产物(两个钩子属于同一进程、都吞键、注入的是带标记的合成键), 不能外推到真实机器。
    * **合成实验的翻转也无法解释用户症状**: 若真有程序在链首吞掉了按键, 键就**到不了主机**, 用户就不会看到"主机输入法被唤起" —— 但用户恰恰看到了。
    * 现行设计与 Deskflow 同构: **装一次**(`_ensure_hook`), 平时只切 `set_relay`(同 Deskflow `setMode`), **仅**在检出异常时(泄漏检测/健康自检/自愈复验)重装一次。
    * 保留的有价值产物: `kbd_calls` 计数(区分"回调没被调用"与"被调用但没吞"—— 现场最需要的数字, GUI 已显示)、`probe_keyboard_hook()`(不依赖用户打字的确定性复验判据)、`inject_util.py`(防"测量空气")。
    * 锁死撤除状态的回归测试: `test_no_chain_head_reclaim_machinery` / `test_set_relay_is_pure_flag_like_deskflow` / `test_reinstall_only_via_explicit_request`。
34. **`SetForegroundWindow` 的返回值不可信, 且会间歇性失败**(BUG2 残留症状的真正原因)。
    * 实测: 它返回 `True` 时 `GetForegroundWindow()` 仍**不是**我们 —— 系统只闪了一下任务栏。原实现 `if SetForegroundWindow(...): return` 据此提前返回, 于是**永远跳过**了真正有效的 `AttachThreadInput` 路径。唯一可信的判据是 `GetForegroundWindow() == hwnd`。
    * 实测同进程按 `_do_hide` 的确切顺序连续抢 5 次: **成功 1 次 / 失败 4 次**。这精确对应"第一次进副机光标能消失, 退回再进就不消失"(用户报的 BUG2)。
    * 修法(照搬 `MSWindowsDesks.cpp:613-623`): `AttachThreadInput` 是**主路径**而非兜底 —— 始终挂上前台线程的输入队列再抢, 抢完摘下; 抢后用 `GetForegroundWindow()` **验证**, 失败则重试(每轮**重新读取**当前前台窗口, 因为上一次尝试可能已改变它), 必要时用 ALT 合成那一手解锁。修复后同款实验 **8/8 成功**。
35. **诊断脚本必须复用生产结构, 否则会"测量空气"并产出错误根因**(两次真实踩坑的教训)。诊断脚本曾自己拼 `INPUT` 的 union 而只放入 `KEYBDINPUT` → 得到 **32 字节**, 而 64 位下必须是 **40 字节**(union 里 `MOUSEINPUT` 32B 才是最大成员)。`SendInput` 的 `cbSize` 传错时**不抛异常**, 而是返回 0 且 `GetLastError()`=**87 (ERROR_INVALID_PARAMETER)** —— **注入根本没发生**。后果极隐蔽: 钩子计数为 0, 看起来像"钩子收不到按键"的结论, 实际上什么都没测到, 并据此推出过完全错误的根因。
    * 修法: 抽出 `tools/inject_util.py` 供所有诊断脚本复用 —— 内部只用生产 `INPUT` 并 **import 时断言 `sizeof(INPUT)==40`**, 每次注入检查返回码, 提供 `require_injection()` 强制"注入不足就抛异常"而不是让实验继续产出假结论。已修 3 个带此 bug 的脚本(`diag_hook_two_instances.py`、`diag_kbd_rawinput_fallback.py`、`diag_kbd_eat_live.py`)。
    * 副产品: 修好后 `tools/diag_kbd_eat_live.py` **全部通过**(此前被判为"本机无法测量") —— 钩子吞键机制在本机完全成立, 旧的失败结论部分正是由这个 bug 造成的。
36. **诊断脚本之间会互相污染, 活体验收必须串行**。同一 shell 命令里并行跑多个涉及真实光标/hider 窗口/钩子的脚本时, 它们互相抢前台与光标 —— 曾导致 `verify_bug2_cursor.py` 报 3 项失败、`verify_hider.py` 间歇失败, 而**串行重跑即全绿**。凡 `verify_bug2_cursor` / `verify_hider` / `verify_hook_*` 等活体脚本, **必须串行执行**。
37. **必须完整读参考实现, 不要凭推测**(本项目最深刻的一次教训)。
    * 事实: 本项目长期只保存了 Deskflow 的 **3 个**参考文件, 且其中 `MSWindowsHook.cpp` 是 **749 行/22571 字节**, 而真实仓库是 **779 行/23962 字节** —— **缺了 30 行关键逻辑**(`g_lastChars`/`mapKeyDown`/按键释放)。更严重的是, Deskflow 的键盘路径早已**不在** `MSWindowsDesks` 里, 而在 **`MSWindowsComputer.cpp`(1746 行)**。
    * 后果: 我据此连续给出过**多个被推翻的"根因"**(钩子超时被摘 / 键盘钩子单独死亡 / `DESKFLOW_MSG_SWITCH` 每次穿越都重装)。其中最后一条是**误读**: `DESKFLOW_MSG_SWITCH` 由 `checkDesk()` 每 0.2s 轮询 `openInputDesktop()` 触发, 那是 **Windows 输入桌面**(默认/锁屏/UAC 安全桌面)**变了**才发, 与"跨越到副机"**无关**。
    * 另一个被读出来的事实: Deskflow **只装一次**钩子, 用 `setMode()` 切换行为, 且全仓库**没有任何**"钩子存活检测"—— 因为它**假设自己是唯一的钩子拥有者**。我们不是(用户机器上 Deskflow 常驻), 所以必须自己处理链首竞争。
    * 现已在 `tools/deskflow_full/` 完整 clone(864 个文件)。**遇到别人已解决的问题, 先完整读实现再动手。**
38. **诊断脚本必须复用生产结构, 否则会"测量空气"**(两次真实踩坑)。诊断脚本曾自己拼 `INPUT` 的 union 而只放 `KEYBDINPUT` → **32 字节**, 而 64 位下必须是 **40 字节**(union 里 `MOUSEINPUT` 32B 才是最大成员)。`SendInput` 的 `cbSize` 传错时**不抛异常**, 而是返回 0 且 `GetLastError()`=**87** —— **注入根本没发生**。后果极隐蔽: 钩子计数为 0, 看起来像"钩子收不到按键", 并据此推出过完全错误的根因。
    * 修法: `tools/inject_util.py` 是所有注入的**唯一入口** —— 内部只用生产 `INPUT` 并**在 import 时断言 `sizeof(INPUT)==40`**, 每次注入检查返回码, `require_injection()` 强制"注入不足就抛异常"而不是让实验继续产出假结论。已修 3 个带此 bug 的脚本。
    * 副产品: 修好后 `tools/diag_kbd_eat_live.py` **全部通过**(此前被判为"本机无法测量")。
39. **"键被别的程序先收走"是两道检测的公共盲区, 由交还时的复核兜底**(用户真机数据: "鼠标有数字 键盘0 回调0")。键盘钩子在穿越中失效的两条路 —— (a) 被系统静默摘除(300ms 超时, 句柄不置空); (b) 被别的程序先吞掉(键到不了我们) —— 现有检测**全部失明**: 鼠标自检看的是光标位移, 鼠标钩子活着就不报警; 泄漏检测要 RawInput 键盘事件, 而路径 (b) 下键根本不产生 RawInput。于是"键同时落两台"而日志里一个字都没有。修法: `_release_control()`(每轮穿越必经点)里 `_verify_kbd_on_release()` 用探针复核一次, False 就重装(保证**下一轮**穿越是好的; 穿越中不做, 不打断输入)。判据有效性已由 `tools/exp_injected_key_chain.py` 证明: **注入键(带/不带 TAG)都走完整钩子链**; 真实第三方(吞一切不看 TAG)面前探针正确返回 False。⚠ 实验陷阱: 用生产 LowLevelHook 当"抢键者"会让探针误报 True(生产回调放行带自己 TAG 的键, 探针恰好带 TAG)—— 以后"抢键"实验必须用不看 TAG 的原生钩子。复现闭环见 `tools/verify_kbd_blindspot.py`(5/5: 盲区复现/老检测失明/复核发现/重装/恢复)。
40. **Deskflow 在这台主机上是 Windows 服务(SYSTEM), 这才是它"任何情况下永久有效"的真正原因**(回答用户"它只装一次为什么永久有效"的质疑): ① 钩子由 SYSTEM 进程安装 → UIPI 永不过滤; ② C++ 回调纳秒级 → 永不触发 300ms 超时摘钩。"只装一次"的前提是"回调永远够快", Python 做不到第②条, 所以我们必须带检测-重装。现场: `Get-Service Deskflow` = Running/Automatic, core 以 `elevated` 运行, 10:25:55 由服务拉起(比我们的 GUI 晚 3 秒 → 按链式顺序它在前), 配置 `Corvo-Desktop: right = Corvo-Laptop` —— **与 Com Bridge 抢同一条边 + 同一个键盘钩子**。给用户的处置: `Stop-Service Deskflow`(先停服务再杀进程, 否则服务会拉起它)/`Set-Service Deskflow -StartupType Disabled`, 或**以管理员运行 com-bridge-gui.exe**。
41. **--noconsole 打包下 stdout 是无效句柄, 诊断必须落到文件**(自发现的盲点): 用户真机上所有 `[server]` 打印(含"键盘钩子自检失败")全部丢失, 排障等于全盲。修法: `com_bridge/gui_log.py` 在 `cli._cmd_gui` 最早期用 `os.dup2` 把 fd1/2 重定向到 `%LOCALAPPDATA%/ComBridge/logs/com-bridge-gui.log`(2MB 轮转); console 版不受影响(`isatty` 检查)。GUI 状态行同步升级: 钩子三数合一(鼠标吞/过, 键盘吞/过/回调 N), 并在"relay+句柄在+kbd_calls=0+鼠标有数字"时直接提示"键被其它程序先收走 —— 请退出 Deskflow/Synergy 或以管理员运行"。
    * ★ 追加修正(第二次踩坑): 只 `dup2` **不够**。`--noconsole` 下 Python 启动时把 `sys.stdout/stderr` 初始化为 **None**, `print` 根本不走 fd 1 —— 实测 10 个 GUI 会话只写了会话分隔行, 角色输出**一条都没落下**。必须用 `io.open(1, "w", ...)` **重建**这两个对象。已加回归锁死。
42. **★★★ `SetForegroundWindow` 会杀死键盘钩子 —— 这是 BUG1("在副机打字, 主机也同步输入")的最终根因。**
    * 症状: 鼠标进副机后, 在副机打字, **主机焦点窗口/输入法也收到按键**; 自愈日志报"键盘钩子失效(回调从未被调用)", 重装 3 次才恢复, 反复出现。
    * 逐步归因实验(`tools/exp_hide_step_attribution.py`, 每步后测 `kbd_calls`):
      | 步骤 | kbd_calls |
      |---|---|
      | 基线 | +4 ✅ |
      | + 全屏 hider 窗显示 | +4 ✅ |
      | + `SetActiveWindow` | +4 ✅ |
      | + **`SetForegroundWindow`** | **+0 ❌ 钩子当场死亡** |
      | + `SetCapture` | +4 ✅ |
      | + `ShowCursor(FALSE)` | +4 ✅ |
    * 因果链: 穿越 → `_acquire_control()` → `set_relay(True)` + `hider.hide()` → `hide()` 里抢前台 → **Windows 对前台窗口所属进程改变输入路由, 键盘不再经过本进程的 LL 钩子链** → `kbd_calls` 恒 0、键照常流入系统(RawInput 看到泄漏, 触发自愈)、重装无效(钩子没坏, 是路由变了)。
    * 与 Deskflow 对齐: Deskflow 的 primary 分支(`MSWindowsDesks.cpp` deskLeave)**只用 `EnableWindow` + `SetActiveWindow`**, secondary 用 `SetCapture`, 两边都**不抢前台**。本项目文件头注释早就写着"统一用 SetCapture: 它不抢前台", 但代码违背了它。
    * 修法: 删除 `_force_foreground()` 整个方法; `_do_hide` 只做 `EnableWindow` + `SetActiveWindow` + `SetCapture` + 抖动注入; `_activated` 判据改用 `GetActiveWindow`; `_do_show` 不再把前台抢回去(我们从没抢过, 抢回去会打断用户打字)。
    * 验证(`tools/verify_foreground_fix.py`): 穿越后 500ms/1.1s/2.1s `kbd_calls` 恒 +4 且 `kbd_eaten` +4; **光标仍隐藏**(`GetCursorInfo` 判据, BUG2 未回归); 退回后钩子仍活、光标恢复。
    * 回归锁: `tests/test_no_foreground_steal.py` + 重写 `test_hider_never_steals_foreground` / `test_show_does_not_steal_foreground_back`(替换了锁定旧"抢前台"设计的 4 条测试)。
43. **与 Deskflow 的其余实现对比结论**(用户要求"尽可能向 Deskflow 对齐"):
    * **已一致**: 只装一次钩子 + 纯布尔模式开关(`set_relay` ≙ `setMode(kHOOK_RELAY_EVENTS)`); 回调只入队 + `PostThreadMessage` 唤醒独立线程(≙ `DESKFLOW_MSG_KEY`); 穿越期间**无时间兜底**(空闲是合法状态); 边界基准用**整个虚拟桌面**(≙ 计算机 shape)。
    * **我们有而 Deskflow 没有的**: 钩子存活自愈(探针 + 重装)——因为 Python 回调做不到 C++ 的纳秒级, 必须带检测。
    * **Deskflow 有而我们没有的**: `g_fakeServerInput` 开关(本地合成事件不进入转发链)、`LLKHF_INJECTED` 标志位判据(我们改用 `dwExtraInfo == TAG`, 对"副机注入回来"的识别同样有效, 因为我们的注入全部带 TAG)。这两项目前不影响正确性, 记录备查。
    * `WH_GETMESSAGE` 钩子仅用于屏保检测, 与键鼠无关, 不需要移植。


### 开机自启(静默, 不需要管理员)

**要求**: 注册自启的整个过程必须静默, 不能出现需要用户确认的步骤。

**实现**: `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 注册表项
(`com_bridge/autostart.py`)。它属于当前用户, 读写**不需要管理员权限**,
也不弹任何窗口; Windows 在用户登录时自动执行。

操作(全程 GUI 内): 启动 `com-bridge-gui.exe` → 点状态横幅右侧的
**「开机自启」**按钮, **即时生效(实测 <50ms)、无任何弹窗**。再点即关闭。

**静默启动**: 登记的命令带 `--autostart` 标志, GUI 启动后**只创建托盘图标
并自动拉起上次的角色, 不显示主窗口**(`ComBridgeGui(start_hidden=True)` 在
`mainloop` 之前 `withdraw()`)。左键托盘图标可随时把窗口调出来。
顺带一个好处: 窗口不映射就不会抢前台 —— 而"本进程窗口是前台"恰好是键盘
低级钩子失效的唯一原因(见 `gui._release_focus_for_hook`)。

> 旧版本登记的是**裸 exe 路径**(与双击启动完全一样, 登录后窗口直接弹出)。
> `autostart.migrate_command()` 在每次 GUI 启动时检查并改写一次, 所以升级
> 后不需要用户手动关开自启开关 —— 否则会出现"改了但看起来没生效"。

**为什么不用计划任务**: 旧版本用「计划任务 + RunLevel Highest」追求与
Deskflow 同级提权, 但**创建最高权限计划任务在 Windows 上必然弹 UAC** ——
这是系统安全边界, 绕不过, 与"必须静默"的要求直接冲突, 因此已移除。

**代价(已知)**: HKCU Run 启动的是**普通权限**进程。本项目运行时并不
依赖管理员: 低级键鼠钩子、WASAPI 环回采集、光标控制、UDP/TCP 通信
在普通用户会话内都可用。唯一受限场景是"需要吞掉**其他提权窗口**
(如以管理员运行的程序、UAC 界面)的按键"时可能被 UIPI 拦截 ——
若确有该需求, 手动以管理员身份运行本程序即可。

> 旧版本注册过的计划任务 `ComBridgeElevatedAutostart` 若仍存在, 会在
> 登录时与 Run 项同时启动。本项目有**单实例互斥**, 第二个进程会立刻
> 退出并唤起第一个窗口, 不会双开。移除该任务需要一次管理员权限,
> 程序会在**已提权时自动顺手清理**, 未提权则静默跳过。

> 若主机上仍要保留 Deskflow: 两者抢同一个键盘钩子与右边线, 建议二选一
> (`Stop-Service Deskflow`; 或 Com Bridge 提权自启后, 至少不吃优先级亏)。

### 副机等待主电脑(开机自启的握手顺序)

两台机器都开机自启时, **副机几乎总是先到**(主板自检+登录 + 主机也要走一遍)。副机在冷启动路径上**必须等待**, 而不是一次连不上就报错。

| | 旧行为 | 现在 |
|---|---|---|
| 首次 `ControlClient.connect()` 失败 | `raise RuntimeError` → 界面"启动失败", 用户得手动再点一次启动 | 进入**等待**状态, 指数退避(1→2→4→5s 封顶)重试 |
| 界面 | 停在"启动失败" | `等待主电脑…(已等 Ns, 探测 M 次)`, 托盘提示同步 |
| 停止按钮 | 不可用(角色还没构造完) | = **取消**本次启动(归类"已取消", 不是失败) |
| 没填 IP 时 | 一次广播搜不到就错 | 反复搜索主电脑广播 |
| 上限 | —— | `wait_for_host_s`(默认 **0 = 一直等**; CLI `--wait-for-host <秒>`) |

实现要点:

* 等待发生在 `Agent._wait_control()` / `_wait_discover()`, 状态经 `Agent.host_wait_state()` 暴露给 GUI。
* **GUI 的 20s 启动超时看门狗必须让路**: 否则等待会被判成"启动超时(>20s), 已复位", 也就是用户看到的那句"直接报错"。`_check_watchdog` 先查等待状态, 命中则把计时基准往后挪并放开停止按钮。
* 取消用独立异常 `HostWaitCancelled`(继承 `RuntimeError`)区分"用户取消"与"真失败"; GUI 据此显示灰色"已取消"而不是红色"启动失败"。
* 取消/超时都会**清空等待状态**并收紧已构造的资源(半启动的角色由启动线程 `stop()` 收拾)。

实测(`tools/verify_startup_wait.py`, 主机延迟 2.5s 才起来): 副机等待 **3.1s 后自动完成握手**(版本协商 + 对时), 按停止后 **1.47s** 退出且归类为"取消"。

### 指针速度档位(SPI_SETMOUSESPEED)的实测结论

"副机鼠标明显比主机快"的排查中, 实测确认: **速度档位(设置里 1..20 的滑块)对注入位移毫无影响** —— speed=1/5/10/15/20 下同一注入的位移都是 1.000 倍; 只有**加速**会放大注入(2.29 倍), 已由决策 16 补偿。
因此若两台手感仍有差异, 主因通常是**两台的 DPI 缩放不同**(缩放更高的一侧, 同样的物理移动占更多逻辑像素, 观感即"更快"), 与代码无关, 需要在 Windows 显示设置里对齐; 可用 `tools/diag_secondary_speed.py` 在副机上复核(它会打印增益、DPI 缩放与加速度对照)。

### 穿越期间的逃生通道: Ctrl+Alt+Esc

穿越期间本机**几乎没有可用的输入通道** —— 这不是 bug, 而是"控制权交给副机"的必然结果:

| 通道 | 穿越期间的状态 |
|---|---|
| 键盘 | 低级钩子吞掉除 Caps/Num/Scroll 外的**所有**键(含 `Win`、`Alt+Tab`、`Ctrl+Shift+Esc`) |
| 鼠标 | hider 窗口 `SetCapture` 捕获, 点击**不落到任何应用**(含托盘图标) |
| 光标 | 隐藏 |

因此"退出穿越"唯一的正常依据是**副机回传的回程信号**。若触发条件出错(例如在远程桌面/全屏程序里把光标推到屏幕边), 而副机又活着(心跳正常 → 失联看门狗不触发), 用户就会**没有任何自救手段**。

为此增加两个保底机制:

1. **`Ctrl+Alt+Esc` = 强制收回控制权**(`hook.PanicCombo`)。判定发生在低级钩子回调里 —— 不能用 `RegisterHotKey`, 因为 relay 期间 `Ctrl`/`Alt` 本身也被我们吞掉, 系统输入状态里没有它们, 热键反而不会触发。该组合键只在**穿越态**下生效, 常态按到它没有任何副作用。
2. **失联/桌面切换/逃生键之后的冷静期**(`Server.ENTRY_LOCKOUT_S = 2s`): 强制交回后两秒内忽略越界判定。缺了它会出现"抢控制权抖动"——交回后 100ms 又被同一个贴边动作抓回去, 每秒一次地抢前台 + 建全屏窗 + 捕获 + 隐藏光标, 全屏程序被反复打断。

另外, hider 抢前台加了两道刹车(`CursorHider._steal_disabled`), 因为"永不放弃焦点"对全屏程序是灾难性的:

* 前台是**别的进程的全屏窗口**(游戏 / 远程桌面客户端 / 播放器)时**不抢**。代价是本轮光标可能保持可见(隐藏依赖前台), 收益是不会把别人的全屏画面打断成黑屏。
* 连续抢 3 次仍抢不到(有程序比我们更凶)就放弃本轮, 不做无限抖动。

> ⚠ `Ctrl+Alt+Del` 是 Windows 的 SAS, 不经过任何用户态钩子, 所以它**永远**能救场 —— 忘了组合键时按它即可。

### 交接区去抖: 一轮穿越只回传一次(2026-10-22)

用户报"副机指针靠近回主机的那条边时被吸过去、拉不出来"。真机日志给出了量化答案: 副机日志 886 行里有 **712 行**是同一句 `光标离开回传边`, 同一手势最多在 **42ms 内触发 145 次**。

原因是回传分支没有去抖。`_at_return_edge` 一旦成立就会持续成立(光标贴着边、状态机仍是 outside), 于是主机在收到第一个回程包之前发出的**每一个**事件都会再走一遍回传:

* 每个事件都回传一条位移 → 主机在绝对定位之后又被追加注入 145 条位移, 而它的物理鼠标早已实时走过同样的路程 → 回程瞬间指针猛窜;
* 每个事件都调 `hider.hide()`, 而单次 hide ≈ 50ms(全屏定位 + 抢前台 + 抓捕获 + 两次 ±1px 注入 + 两个 sleep)→ 145 次 ≈ **7 秒**。这期间 hider 线程一直在重设前台/捕获, 用户重新进入副机时 `show()` 排在几十个 hide 后面 → 光标迟迟不出现、出现时又落在离边 12px 处。

三处修正:

1. **回传去抖**(`Agent._returned` / `RETURN_STALE_DRAIN_S = 0.35s`): 同一轮穿越只回传一次, 之后积压的陈旧位移直接丢弃并计数(`events_dropped_stale`)。0.35s 之后自动解除 —— 万一回程包丢了、主机没接管, 副机必须能恢复注入, 否则会**永久吞掉**输入。
2. **`CursorHider._do_hide` 幂等**: 已隐藏就直接返回。维持隐藏本来就是保活定时器(100ms)的职责, 重复 hide 只有代价。
3. **键盘钩子自检搬到后台**: `probe_keyboard_hook()` 要轮询等最多 0.5s, 原先在 `_acquire_control` / `_release_control` 里**同步**执行, 而这两个方法分别跑在原始输入回调线程和输入接收线程上 —— 每次越界 / 交还都要把输入管线堵一次, 排队的事件随后一次性倒给副机(指针先僵住再猛窜)。改为 `_spawn_kbd_verify` 后台线程(单飞), 并且**先恢复光标再复核**(探针判据与"本进程是否前台"强相关, hider 还占着前台时问它必然假失败)。

### 交接几何: "一碰就走"是硬要求(2026-10-22 定稿)

* 进入落点固定为**离边 12px**(`input.ENTRY_JUMP_ZONE_PX`, 照搬 Deskflow 的 avoidJumpZone): 日志里是 `交接定位 -> (12, y)`, 回程是 `回程定位 -> (1907, y)`(1920-1-12)。
* 回传触发是"光标真的顶到最外几列(`EDGE_TOLERANCE = 2`)且方向朝主机", 副机侧 `confirm_frames = 1`(一击即走), 主机侧 2。
* **副机回主机必须一碰就走**。曾经试过"必须推过边界 24px 才回传"(想避免手一抖就弹回主机), 已按用户要求撤销 —— 那个方向把边缘变成了"要费力顶出去"的墙。误触改由**去抖**(`RETURN_STALE_DRAIN_S`, 一轮穿越只回传一次)承担。

### 边缘"慢移不动"的真正原因: 进入判定要求"连续两帧外向"(2026-10-22 结案)

用户报"光标在副机靠主机那一侧的边缘附近, 往副机里**慢慢**移动离不开边缘, 用力拉才行; 副机桌面左侧的快捷方式极难点"。主机日志给出了量化证据:

```
17:38:19.998 收到回程信号
17:38:20.001 回程定位 -> (1907,526)      ← 光标复位到离边 12px
17:38:20.255 光标越界 -> 交接 (1917,526)  ← 0.25 秒就重新进去了
...
17:39:11.273 回程定位 -> (1907,518)
17:40:07.097 光标越界 -> 交接 (1919,587)  ← 这次用了 55.8 秒
```

同一条边、同一个人、同样 12px: 有时 0.25 秒, 有时 55.8 秒。副机侧的逐事件记录显示注入完全正常(`inj dx=-8 (160,784) -> (152,783)`), 所以不是注入问题。

根因在主机侧进入判定的两个细节叠加:

1. `Server._start_input` 里 `EdgeDetector(confirm_frames=2)` —— 要"连续 2 帧抵边"才进穿越态;
2. `_on_input_event` 只在 `_is_outward(dx) > 0` 时才把位置喂给探测器, 而**任何 `dx <= 0` 的事件都会把 `_streak` 清零** —— 包括 `dx=0` 的纯竖直/静止报告。

鼠标按固定频率上报, **慢慢移动时绝大多数报告是 `dx=0`**(位移不够一个计数), 夹着偶尔的 `dx=+1`。于是:

| 手势 | 事件序列 | 结果 |
|---|---|---|
| 快甩 | `+30 +28 +31 ...` | 连续满足, 一两帧就过去 |
| 慢移 | `0 0 0 +1 0 0 0 0 +1 0 ...` | 每次 `dx=0` 都把 streak 归零, **永远凑不齐连续两帧** |

而副机回传侧一直是 `confirm_frames=1`(一击即走), 所以"往主机走正常, 往副机里走发涩" —— 与用户描述完全一致, 也解释了为什么"用力拉才行"。

修法: 主机侧 `confirm_frames=1`, 与回传侧对称 —— 光标真的抵到最外几列(`EDGE_TOLERANCE=2`) + 一次外向位移, 立刻过去。

> 排查过程中还顺手修了一处**原则性错误**(虽非本次根因): 副机交回控制权后, 它的"本机物理指针通道"仍会把自己的指针移动转发给主机, 而主机收到后是 `move_relative` 注入到**自己的光标**上 —— 等于用副机的设备去驱动主机光标, 与用户的物理鼠标对抗。现在用 `Agent._host_has_control` 在交回后一律丢弃(实测真机该通道事件数为 0, 所以它只是潜在的隐患, 不是本次现象的原因; 丢弃次数会打日志便于确认)。

### 边界现场取证(排障用, 平时零开销)

```powershell
# 开启(改完重启两台的程序生效)
New-Item "$env:LOCALAPPDATA\ComBridge\diag_edge.flag" -ItemType File -Force
# 关闭
Remove-Item "$env:LOCALAPPDATA\ComBridge\diag_edge.flag"
```

等价的环境变量是 `COM_BRIDGE_DIAG_EDGE=1`(文件开关是为了能远程给副机开 —— 它的程序由计划任务拉起, 改环境变量不方便)。

开启后, 光标距跨越边 60px 内时逐事件记录(上限 6000 行):

| 侧 | 日志 | 能证明什么 |
|---|---|---|
| 主机 | `[server][edge] dx=.. 光标=(x,y) 穿越=..` | 物理位移有没有到、判定处于什么状态 |
| 副机 | `[agent][edge] inj dx=.. (x0,y0) -> (x1,y1)` | 注入前后光标到底有没有动 |

## 已知限制

* 仅在**单机自环**下验证过全部纯逻辑单元; 双机端到端(真实网络延迟、晶振偏差、UIPI/权限差异)已在用户的两台真机上长期运行(音频到达率 48kHz、输入转发与会话锁均实测), **但"另一套 KVM/远控软件同装"的场景仍未覆盖** —— 见下条。
* 端口为**独占绑定**: 同一台机器上不能同时跑两个 server(或两个 agent)。控制/音频/输入端口被占用时会立刻报错提示"可能已有另一个 Com Bridge 实例在运行", 而不是静默失败。
* 键鼠共享假设键鼠物理接在主电脑上; 副电脑自己的键鼠仅在同时操作时生效, 没有做仲裁。
* 全链路要求两端采样率一致(通常都是 48kHz); 不一致时该路会被丢弃而非重采样(`peer_rejected` 计数)。
* 约 60ms 的固定延迟来自抖动缓冲, 对视频/音乐无感, **竞技游戏不可接受**; 游戏音频建议留在本机渲染。
* 注入提权窗口(UAC/管理员程序)需要本程序也以管理员运行。
* 未加密, 仅适合可信局域网。
* **对称 NAT 下打洞会失败**: 双向心跳能穿透常见的锥形 NAT(含家用路由器与 OpenWrt 默认 masquerade), 但若两端都在严格对称 NAT 之后则无法建立直连。此时应把两台机器放在同一网段(或让副电脑走桥接而非 NAT)。
* 网络诊断: 若鼠标过不去, 先看 GUI 的"控制权 / 转发 / 已发 / 收到 N 包"几项 —— 它们能把故障定位到"没收到物理输入"、"判定没触发"、"发不出去(地址/防火墙)"还是"收到了没解出来"。

## 测试

```powershell
python -m pytest tests/ -q      # 689 项: 协议/对时/缓冲/混音/越界几何/方位/位置映射/钉住公式/钩子控制/加速补偿/RAWMOUSE 布局/卡键防护/键盘钩子自产/钩子回调廉价性与重装自愈/键盘钩子单独死亡检测/交还复核(盲区)/链首抢占机制撤除锁定/GUI日志与自诊/GUI管理员自启(命令行tag/子进程分发/rc判定)/前台抢占(不信返回值+重试)/DPI 感知/注入时间戳判据/三项 BUG 回归/逃生通道(Ctrl+Alt+Esc)/强制交回冷静期/交接去抖(陈旧位移丢弃+hide幂等)/自检异步化/边界现场取证开关/本机漂移不驱动主机光标/慢推进入(confirm_frames=1)/全屏前台不抢焦点
python -m com_bridge.cli selfcheck
```

> ⚠ 活体验收(涉及真实光标的 `verify_pin.py` / `verify_hider.py`)**必须在没有其它 KVM 的桌面上跑**。
> 实测踩到: 开发机上跑着 Deskflow 本体时, 它会在屏幕边缘抢占光标并把它移到自己算的中心
> (Deskflow 的 `deskLeave` 调 `deskMouseMove(m_xCenter, m_yCenter)`), 于是"推到边缘再读回坐标"
> 这类测量全部失真(实测 `SetCursorPos(1919,540)` 之后读到 `(960,540)`)。
> `verify_pin.py` 自带环境探测: 检测到 Deskflow/synergy/barrier/input-leap 或边缘被拽走时,
> 纯数学层照常判定、活体层报 **SKIP**(失败的是测量环境, 不是代码)。

独立验证工具(`tools/`, 单机可跑, 双机部署前建议全部过一遍):

| 工具 | 验证什么 |
|---|---|
| `verify_audio.py` | 音频权威验收: spy write 流, 断言稳态零波形断裂、音调正确 |
| `verify_all_events.py` | **全事件类型贯通**: 协议往返 + 分发覆盖 + 真实注入(鼠标移动/五键/滚轮/键盘/扩展键/组合键) |
| `verify_button_forward.py` | **点击/滚轮/键盘能否产出并带标记转发**(用实测字节驱动生产代码, 不依赖物理设备) |
| `verify_hook_e2e.py` | **低级钩子端到端**: 吞掉本机物理点击、放行带标记的回程注入、`stop()` 后恢复 |
| `verify_capture_rawinput.py` | 捕获(hider)与 RawInput 互不干扰(证明 SetCapture 只影响窗口消息) |
| `verify_pin.py` | 钉住注入: 归一化公式对齐 Deskflow + 幂等 + 顶墙漂移对比(环境感知) |
| `verify_hider.py` | 光标隐藏: hider 线程**接管鼠标输入**且 `ShowCursor` 计数器为负 |
| `verify_bug2_cursor.py` | **BUG2 验收**: 用 `GetCursorInfo` 直接判定光标可见性 —— hide() 后、60 次注入移动期间都必须**不显示**, 且物理移动仍能恢复 |
| `verify_bug3_return.py` | **BUG3 验收**: 去程按比例进入(误差 ≤1px) + 回程落点**跟随副机实际位置**(而非停在钉住点) |
| `verify_hook_loop.py` | **BUG1 验收**: 钩子线程消息循环真的分派 `WM_APP_KEY`/`WM_APP_REINSTALL`(hwnd=NULL 的消息必须显式处理), 重装后钩子仍可用 |
| `diag_hider_state.py` | 诊断: hider 窗口矩形/可见性/**是否抢到前台**(BUG2 的决定性条件) |
| `diag_bug2_approach.py` | 对比 1x1 跟随窗 vs 全屏窗的隐藏效果 |
| `diag_accel.py` | 指针加速对注入位移的放大比例(未补偿 vs 已补偿) |
| `diag_secondary_speed.py` | **在副机上跑**: 注入增益 + 指针设置 + DPI 缩放(排查"副机更快") |
| `diag_repro.py` | 按生产顺序复现光标隐藏(含持续钉住注入), 直接读 `GetCursorInfo` |
| `diag_hook_rawinput.py` / `diag_hook_types.py` | 钩子吞事件后 RawInput 是否仍收到 / **各类型事件的原始字节**(布局证据) |
| `diag_occupier.py` | 枚举全屏覆盖窗口, 排查外部程序(如 Deskflow 的 `DeskflowDesk`)抢占光标 |
| `verify_wire.py` | 输入线路径: 心跳+事件混发不混淆、字段无损、序列连续 |
| `verify_punch_port.py` | NAT 打洞**端口**学习: 学到的端口必须是心跳源端口 |
| `verify_borrow_socket.py` | 回程包源端口必须 == 接收端口(端口限制型 NAT 的必要条件) |
| `verify_side_wire.py` | 方位下发: 主电脑 side 经心跳到达副电脑并正确解析 |
| `verify_side_logic.py` | 方位 -> 回传边映射(含副机配置填错时自动纠正) |
| `verify_placement.py` | 跨屏位置控制: 进入点在正确边缘、比例映射、整机桌面基准 |
| `verify_crossing_flow.py` | 双屏端到端场景: 进入定位→双屏移动→拉回→回传 |
| `verify_mapping.py` / `verify_side_exit.py` / `verify_holepunch.py` / `verify_handshake.py` | 比例映射 / 穿越退出 / 打洞 / 双端握手 |
