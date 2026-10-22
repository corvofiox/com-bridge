# Com Bridge v0.4.4

修 v0.4.2 里**图片跨机粘贴不工作**的问题。文字和文件一直是好的，只有图片。

## 根因：抄了 Deskflow 的一半逻辑

真发现（在真机剪贴板上直接 dump 出来的）：

```
  fmt  name            size
    8  CF_DIB          256052 字节      biSize=40  biWidth=320  biBitCount=32
                                        biCompression=3 (BI_BITFIELDS)
```

图片确实在剪贴板里，但它的像素头是 **BI_BITFIELDS**（40 字节头 + **12 字节颜色掩码** + BGRA 像素），
不是 BI_RGB。

我的采样逻辑照着 Deskflow 写了"**只接受 BI_RGB**"这条判据，却漏掉了它的另一半：
Deskflow 的 `MSWindowsClipboardBitmapConverter::normaliseMalformedMacDib()` 对这种情况是
**转换**而不是丢弃 —— 把 `biCompression` 改写回 `BI_RGB`、并把紧跟头部的 12 字节掩码块
丢掉（因为 INFOHEADER 本身没有掩码字段，留着会被 Windows 当成像素读）。

结果就是：截图类应用（截图工具、画图、浏览器）复制出来的图**全部被静默丢弃**，
日志里一个字都没有 —— 所以看起来像"根本没人实现图片"。

## 修

新增 `clipboard.normalise_dib()`，按 Deskflow 的规则处理：

| 输入 | 处理 |
|---|---|
| BI_RGB | 原样接受 |
| `biSize=40 / planes=1 / 32bpp / BI_BITFIELDS / 长度 = 40+12+w*h*4` | 改写成 BI_RGB 并丢掉掩码块 |
| BI_JPEG / BI_PNG（内容不是像素） | 拒绝（没法当位图用） |

## 顺带：日志现在能自己说话

之前"图片传没传"完全看不出来。现在两端各有一行：

```
[clip] 发送 图片 800x500x32 (1600040 字节)            ← 发送侧
[clip] 收到 图片 800x500x32 (1600040 字节)            ← 接收侧
[agent] 剪贴板已写入本机: 图片 800x500x32             ← 确认本机剪贴板真的写进去了
```

最后那行是关键判据：**有它** = 对端剪贴板已经拿到图，粘不出来就是目标程序的问题；
**没它** = 写剪贴板失败（一般是被别的程序占用）。

## 真机验证

```
主机 04:24:33  [clip] 发送 图片 800x500x32 (1600040 字节)
副机 04:24:34  [clip] 收到 图片 800x500x32 (1600040 字节)
副机 04:24:34  [agent] 剪贴板已写入本机: 图片 800x500x32
```

## 测试

753 项通过。新增 6 项覆盖 DIB 规范化：BI_RGB 直通、**BI_BITFIELDS 必须被转换而不是丢弃**
（含"掩码块必须丢掉""像素必须原样保留"）、BI_JPEG/BI_PNG 拒绝、垃圾数据拒绝、
日志摘要含尺寸。
