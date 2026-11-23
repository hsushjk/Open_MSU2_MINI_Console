# Open_MSU2_MINI_Console

> **Open_MSU2_MINI 自研固件的上位机**（MIT）

本仓库派生自 [MSU2_MINI_Console](https://github.com/hsushjk/MSU2_MINI_Console)，面向我们自己写的固件，不是原厂固件的上位机。

## 与原版上位机的区别

| 项 | MSU2_MINI_Console | 本仓库 |
|---|---|---|
| 目标固件 | 原厂固件 | 自研固件（CH32X033 从零写的那套） |
| 握手 | 设备发 `\x00MSN` + 版本，主机回 `\x00MSNCN` | 设备发 `\x00OPMSU`，主机回 `\x00OPEND` |
| 像素传输 | `fill` 4 字节 + `class 4` 差异块 | `op 0x0C` 整段（512 字节）+ `op 0x0D` 行程编码 |
| 分块差分 | 无 | 16×16 分块，只发变化的块 |
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

## 画质模式

160×80 的屏只有 12 800 像素，但链路上每帧的字节数直接决定帧率，所以发送端有两种编码：

| 模式 | 编码 | 每帧 | 适用 |
|---|---|---|---|
| **高清**（默认） | RGB565 真彩，16×16 分块差分 | 整帧约 25 KB，平时只发变化的块 | 文字、图标、**背景图/照片** |
| 流畅 | 8 位索引色（216 色）＋可选 Bayer 抖动 | 整帧约 13 KB | 投屏、动画（插件声明 `FAST_FRAME = True` 即自动用） |

背景图在**高清**下是真彩直出，不会出现色带或亮度断层；换成「流畅」才走 216 色量化。
投屏类插件自己声明 `FAST_FRAME = True`，所以不受全局设置影响 —— 这是有意保留的取舍，
README 与插件注释里都写清楚了：**想要照片级背景就待在默认的高清模式，别为了投屏把全局画质拉低**。

两点注意：

- 以前所有插件都被强制走 8 位索引，所以第三方插件默认是「高清」。如果你的插件画面小、刷新快（比如自己抓屏、放动画），
  在类里加一行 `FAST_FRAME = True` 就会走省流的 8 位编码。
- 切换画质会让发送端清空差分缓存并**整帧重发一次**，画面会闪一下，属正常现象。

## 触摸灵敏度

WebUI「全局触控」里的**灵敏度**滑块就是判定门限（默认 60，范围 5–300）：

- 数越小越灵敏：读数只要比基准低这么多就算按下；
- 贴膜、隔着壳、手指偏干导致点不动 → 往下调（20–40）；
- 环境干扰大、经常自己跳页 → 往上调（100–150）。

## 许可

MIT，见 `LICENSE`。上游的界面、插件框架与大部分 widget 由原作者编写，本仓库在其上做了协议层与发送路径的修改。

**禁止倒卖**：本项目仅供个人学习与非商业使用，禁止任何形式的转售、收费分发或捆绑销售。项目地址：<https://github.com/hsushjk/Open_MSU2_MINI_Console>。如果你为它付过钱，那是别人在倒卖，请退款并举报。

## 相关仓库

- [Open_MSU2_MINI](https://github.com/hsushjk/Open_MSU2_MINI) —— 硬件：引脚、网表、协议说明
- [MSU2_MINI_FW_Reverse](https://github.com/hsushjk/MSU2_MINI_FW_Reverse) —— 原厂固件反编译与分析
- [MSU2_MINI_Console](https://github.com/hsushjk/MSU2_MINI_Console) —— 原厂固件的上位机（本仓库的上游）