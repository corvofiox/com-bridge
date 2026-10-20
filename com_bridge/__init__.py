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

__version__ = "0.3.0"
