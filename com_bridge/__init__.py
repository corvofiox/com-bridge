"""Com Bridge — 双机(Windows)键鼠越界 + 音频混流同步。

模块划分:
  com_bridge.protocol    音频帧头/输入事件编解码 + 常量
  com_bridge.discovery   UDP 广播信标(局域网发现)
  com_bridge.clock       NTP 四时间戳对时 + PI 抖动缓冲
  com_bridge.audio       WASAPI loopback 采集 / 渲染 / 混音
  com_bridge.input       鼠标键盘捕获(原始输入) + SendInput 注入 + 越界几何判定
  com_bridge.transport   UDP 传输层(音频/输入/控制)
  com_bridge.server      主电脑角色
  com_bridge.agent       副电脑角色
  com_bridge.cli         命令行入口
"""

# 0.4.0: 界面破坏性重设计(白纸 + 细线 + 芯片式选项 + 内容自适应窗口高度)
# 0.4.1: 键鼠跨屏手感修复(注入坐标归一化精确化/回传去抖) + 全屏程序时禁止穿越
# 0.4.2: 剪贴板支持图片(裸 DIB)与文件/文件夹; 剪贴板改走 TCP + 512KB 分块(对齐 Deskflow)
__version__ = "0.4.2"
