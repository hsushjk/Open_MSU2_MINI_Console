# Open_MSU2_MINI_Console

> **Open_MSU2_MINI 自研固件的上位机**（MIT）

本仓库派生自 [MSU2_MINI_Console](https://github.com/hsushjk/MSU2_MINI_Console)，面向我们自己写的固件，不是原厂固件的上位机。

## 与原版上位机的区别

| 项 | MSU2_MINI_Console | 本仓库 |
|---|---|---|
| 目标固件 | 原厂固件 | 自研固件（CH32X033 从零写的那套，`firmware_bare` v28） |
| 握手 | 设备发 `\x00MSN` + 版本，主机回 `\x00MSNCN` | 设备发 `\x00MSN02`（也认旧的 `\x00OPMSU`），主机对应回 `\x00MSNCN` / `\x00OPEND` |
| 像素传输 | `fill` 4 字节 + `class 4` 差异块 | `class 0/3` 开流收流 + `op 0x0C` 原样段 / `op 0x0D` 行程编码 |
| 分块差分 | 无 | 16×16 分块，只发变化的块 |
| 流控 | 无 | 每帧读设备回执：`0x06` 收齐 / `0x15` 少了 → 整帧重发；无回执 → 重连 |
| 计时指标 | 无 | `enc_ms` / `tx_ms` / `composite_ms` / `frame_bytes` |

两边**协议不兼容**：原厂固件不认识 `0x0C` / `0x0D`，本版上位机也只认 `OPMSU` 握手。想换固件必须连上位机一起换。

## 使用

推荐用 pip 安装（跑在正常解释器里，标准库和依赖都没有限制，插件用什么都行）：

```bash
pip install .
msu2-console          # 或 python -m msu2_mini
```

可选依赖：`pip install ".[extras]"`（pynput + mss），或 `pip install ".[full]"`（再加 numpy、pywin32）。

`pip install .` 之后（不是打 exe）：

- 插件放在 `%LOCALAPPDATA%\MSU2_MINI\widgets`，或用 `MSU2_WIDGETS_DIR` 指定；`msu2-console --new-widget foo` 会直接把骨架生成到这里
- 插件里 `import` 任何标准库、任何第三方包都行，缺什么自己 `pip install` 即可
- 配置默认写到 `~\.msu2_mini\config.json`（源码模式下仍在仓库根目录）

从源码直接跑：

```bash
pip install -r requirements.txt
python msu2_mini.py
```

打包成单文件 exe（给没有 Python 的机器用，解释器只带主程序用到的标准库）：

```bash
build.bat
```

## 插件依赖

外部插件可以自带 `requirements.txt`，缺包时控制台会自动补上，不用重新打包：

- 单文件插件 `widgets\foo.py` → 依赖写 `widgets\foo.requirements.txt`，或同目录的 `requirements.txt`
- 目录插件 `widgets\foo\` → 依赖写 `widgets\foo\requirements.txt`

装到哪里、怎么装：

1. 有系统 Python 时用 `pip install --target widgets\_deps -r ...`
2. 没有 Python 时直接从 PyPI 下载 wheel（优先同一解释器 ABI，其次纯 Python 包）解压到 `widgets\_deps`
3. `widgets\_deps` 与每个插件目录下的 `_deps` 会自动加入 `sys.path`

注意：打包成 exe 的解释器只带主程序用到的标准库。插件如果用到冷门标准库模块（例如 `http.cookies`、`tkinter`），打包版会报缺模块并给出提示，这种情况请用源码方式运行：

```bash
pip install -r requirements.txt
python msu2_mini.py
```

## 显示与画质

**只有一种画质：真彩 RGB565（65536 色）**，走 16×16 分块差分 —— 文字、图标、照片、背景图都按原样显示。
8 位 216 色那条「省流量」分支已删除：它在 160×80 上只能看出抖动网点，投屏也被它拉低画质，实际没有意义。

160×80 一共 12 800 像素，整帧 25.6 KB；平时只发变化的块，静态页面几乎不发数据。

### 暗化（伪亮度）

「显示控制」里的**暗化**是全局的：作用在最终合成帧上，插件和背景图一起变暗，0%–90% 连续可调。
它不是改变屏幕背光（硬件没有背光控制），而是把像素按比例压暗，所以看起来像亮度旋钮。

### 背景模糊

模糊上限是 **5**：屏幕只有 160×80，超过 5 之后整张图就糊成色块了，再往上调没有意义。

### 传输

整帧 25.6 KB，走链路实测约 200 KB/s ⇒ 全屏刷新约 8–10 fps，局部刷新快得多。
右下角 8×8 方块是**整帧完整性指示**（绿=收齐、红=少了）；分块局部刷新时它不更新，
老固件更是“只有整 12800 像素才绿”，所以**别只看角标判断花屏**。

## 稳定性与排查

需要固件 **v28 或更新**（`MSU2_open_v28.hex`）+ 配套上位机。v28 的三道保险：

1. 设备端接收环满了**只 NAK 不丢包**（丢一包 = 少 32 像素，而分块差分会把这一帧当成“已发”，残影永久留下）；
2. 流式接收按“400 ms 没有新字节 / 整段 2 s”判超时，作废当前段并回 `0x15`；
3. 独立看门狗约 1.6 s：主循环万一卡死，整机复位重画，不会停在花屏上不动。

上位机侧对应行为：收到 `0x15`、或连续多帧没有回执 → 清编码缓存 + 整帧重发一次；
连续 8 帧完全没回执 → 关串口重连（日志 `device stopped answering; reconnecting`）。

常见日志含义：

| 日志 | 含义 |
|---|---|
| `device did not confirm the frame … resending the full frame` | 设备回 `0x15`：那一帧少了像素，已自动整帧重发（偶发正常） |
| `device answered no verdict for N frames` | 设备没回执。N 小是抖动，持续增大说明设备卡了，会触发重连 |
| `固件版本 28.0，触摸读数 3925` | 版本帧。触摸读数是首拍，晚几毫秒才会更新，不要拿它判断 ADC 坏没坏 |
| `固件没有回应版本查询` | 固件太老（proto25 及更早）没有版本帧，属正常 |

固件与上位机**必须成对升级**：老固件没有回执字节，新版上位机会自动退回“只写不等回执”的老路径；
新固件配老上位机也能点亮，但拿不到重发保护。

## 许可

MIT，见 `LICENSE`。上游的界面、插件框架与大部分 widget 由原作者编写，本仓库在其上做了协议层与发送路径的修改。

**禁止倒卖**：本项目仅供个人学习与非商业使用，禁止任何形式的转售、收费分发或捆绑销售。项目地址：<https://github.com/hsushjk/Open_MSU2_MINI_Console>。如果你为它付过钱，那是别人在倒卖，请退款并举报。

## 相关仓库

- [Open_MSU2_MINI](https://github.com/hsushjk/Open_MSU2_MINI) —— 硬件：引脚、网表、协议说明
- [MSU2_MINI_FW_Reverse](https://github.com/hsushjk/MSU2_MINI_FW_Reverse) —— 原厂固件反编译与分析
- [MSU2_MINI_Console](https://github.com/hsushjk/MSU2_MINI_Console) —— 原厂固件的上位机（本仓库的上游）