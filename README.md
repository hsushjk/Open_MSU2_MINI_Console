# Open_MSU2_MINI_Console

MSU2_MINI（CH32X033F8P6 + ST7735S 160×80）的开源上位机。程序在本地起一个 WebUI，用于管理插件页、合成并推送帧、写入开机与待机画面、通过 USB 完成固件升级，以及配置全局触控手势。支持 Windows 与 Linux。

> **该上位机必须搭配固件[`Open_MSU2_MINI_FW`](https://github.com/hsushjk/Open_MSU2_MINI_FW)使用**

<p>
  <img src="https://img.shields.io/badge/python%2B-blue" alt="python">
  <img src="https://img.shields.io/badge/platform-Windows%20%7C%20Linux-lightgrey" alt="platform">
</p>

## 功能

- 16 个内置插件页，覆盖时钟、日历、待办、音乐、系统资源、剪贴板、GIF 等；外挂插件放目录即被发现，改动后热重载。
- 每帧一次回执：设备回 `0x06` 表示帧正确，回 `0x15` 表示像素数不符，由上位机重发该帧。
- 分块签名校验：每帧结束后核对 50 个 16×16 块的校验和，只补画不一致的块。
- 串口断线后自动重连，设备更换端口号也能重新找回。
- 开机画面 1 槽、待机画面 16 槽，写入设备板载 NOR，脱离电脑后仍然生效；资产支持 RLE 压缩。
- 免拆机固件升级：写入 NOR 暂存区，校验 CRC 后由固件搬运到应用区。
- 触控手势：单击、双击、长按可分别绑定翻页、亮度、截屏、静音、键盘宏、启动程序、关闭上位机等动作。
- 命令行自检：`--selftest` 一次检查链路、触摸、整帧与分块链路、复位记录、NOR 内容、OTA 通道与资产 CRC。

## 安装

### 从源码运行

```bash
pip install -r requirements.txt
python msu2_mini.py
```

也可以安装成命令：

```bash
pip install .
msu2-console
```

### 预编译程序

`dist/MSU2_Console_Open.exe` 无需 Python，双击即用。配置、日志、上传文件与插件数据都保存在程序所在目录，整个目录复制走即可完整备份。

### Linux

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install pyserial pillow psutil
python msu2_mini.py --port /dev/serial/by-id/usb-MSU2_MSU2_MINI_*
```

设备重新枚举后节点号可能由 `/dev/ttyACM0` 变为 `/dev/ttyACM1`，因此推荐使用 `/dev/serial/by-id/` 下的固定链接，升级后的重连也依赖该路径。`--headless` 表示不启动 WebUI；需要在浏览器中操作时不要加这个参数。

### 依赖

| 类型 | 包 | 用途 |
| --- | --- | --- |
| 必需 | `pyserial>=3.5` | 串口通信 |
| 必需 | `pillow>=9.0.0` | 帧合成、图片解码、字体渲染 |
| 必需 | `psutil>=5.9.0` | CPU、内存、磁盘与进程类插件 |
| 可选 | `numpy>=1.21.0` | 加速 RLE 与差分编码，未安装时退回纯 Python 实现 |
| 可选 | `pynput>=1.7.6` | 键盘宏、静音、键鼠统计类插件 |
| 可选（Windows） | `pywin32>=306` | 前台窗口与系统接口 |

## 快速开始

1. 将设备接入 USB。配套固件以 `1A86:FE0D` 枚举，串口随即可用。
2. 运行 `python msu2_mini.py`，在浏览器打开 `http://127.0.0.1:8765`。
3. 在「设备」卡片选择串口，或等待自动重连；状态卡片出现刷新指标即表示链路正常。
4. 在「页面顺序」中调整页面顺序，双击页面名可立即切换。
5. 如需脱离电脑使用，在「开机 / 待机画面」中选择图片、写入槽位并保存设置。

## 命令行

| 参数 | 说明 |
| --- | --- |
| `--port PORT` | 指定串口（如 `COM7`、`/dev/ttyACM0`），缺省时自动扫描 |
| `--config PATH` | 使用指定配置文件 |
| `--init-config` | 生成默认配置文件；文件已存在时提示并退出 |
| `--headless` | 不启动 WebUI，仅运行调度与串口部分 |
| `--simulate` / `--offline` | 无硬件模式，额外打开模拟屏窗口（需要图形环境） |
| `--list` | 列出当前可用串口后退出 |
| `--plugins` | 列出已发现的插件及加载失败原因后退出 |
| `--devices` | 列出配置中的多台设备及其连接状态后退出 |
| `--new-widget NAME` | 生成外挂插件骨架后退出 |
| `--selftest` | 对设备执行完整自检；配合 `--all` 时逐台执行 |
| `--flash FILE` | 对 `.hex` / `.bin` 执行升级，完成后等待设备重新枚举，最多 15 秒 |
| `--log` | 同时把日志输出到控制台（默认只写文件） |
| `--log-level LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `--log-file PATH` | 日志文件路径，传 `none` 关闭文件日志 |
| `--version` | 打印版本号 |

`--selftest` 依次检查：握手与版本帧、触摸采样、局部变化帧与整屏噪点帧、50 块分块签名、复位记录、NOR 抽查、OTA 通道（写入暂存区并回读镜像头，不执行搬运）、资产 CRC、待机动画步进。

`--flash` 与网页版使用同一套升级流程：每 200 ms 尝试重连，成功时输出「升级完成：固件 x.y.z」，超时输出「设备没有回来」并返回非 0。

## WebUI 卡片

| 卡片 | 内容 |
| --- | --- |
| 设备 | 串口选择、连接状态、屏幕实时预览 |
| 显示控制 | 上一页 / 下一页 / 切换朝向 / 上下反转；亮度 100% 至 10%，步进 5% |
| 全局触控 | 单击、双击、长按分别绑定动作；键盘宏、启动程序路径；应用后下发 |
| 页面顺序 | 拖拽排序、双击跳页、刷新列表、重载插件 |
| 配置面板 | 当前插件的设置项，按 `config_schema.py` 的字段声明自动生成 |
| 通知 | 文本与停留时间（1 至 60 秒），推送到屏幕 |
| 背景图 | 单张或多图轮播（2 至 3600 秒）、适应 / 填充 / 拉伸、模糊 0 至 5、总开关；支持 png、jpg、bmp、webp、gif |
| 状态 | 帧率、字节数、当前页、重发次数等运行指标 |
| 开机 / 待机画面 | 开机模式与停留时间；待机模式与单张时长；16 个槽位的写入、预览、擦除、保存与读取设备自检结果 |
| 固件升级 | 选择 `.hex` / `.bin` 执行升级，显示进度与失败原因 |
| 网络 | 修改 WebUI 绑定地址与端口（`0.0.0.0` 允许局域网访问，`127.0.0.1` 仅本机） |
| 关闭程序 | 停止服务并释放串口 |

### 亮度说明

硬件背光为 5 V 直连，没有调光电路。界面中的「亮度」由上位机在合成后整体压暗像素实现，内部保存 0 至 90 的压暗量，因此不存在 100% 以上的取值。若需要真正的硬件调光，需要改动电路并同步修改固件。

## 内置插件

共 16 个：`clock`、`calendar`、`countdown`、`holiday`、`anniversary`、`todo`、`pcstats`、`network_detail`、`disk_io`、`music`、`clipboard`、`app_usage`、`gif`、`keyboard_stats`、`mouse_mileage`、`crypto`。

`--plugins` 可查看名称、来源、优先级与说明。

## 资产格式

槽头 20 字节：`magic('MSA1') / w / h / fmt / flags / len / crc32 / pad`。`fmt` 决定负载格式：

| fmt | 负载 | `len` 与 `crc32` 的取值对象 |
| --- | --- | --- |
| 0 | 裸 RGB565，160×80 为 25 600 字节 | 原始像素数据 |
| 1 | RLE，每 3 字节一组 `[cnt, hi, lo]`，`cnt` 为 1 至 255 个重复像素 | 压缩后的数据 |

上位机在 `Scheduler.encode_asset_image()` 中先尝试 RLE，只有结果小于裸数据时才使用 `fmt=1`。设备端 RAM 只有 20 KB，不缓存整幅图：`asset_draw` 与 `0A 18` 按两行一条带滚动解压，RLE 状态跨块保存，峰值占用为 640 字节行带与 64 字节 flash 读缓冲。网页槽位列表显示原始大小、压缩后大小与节省比例。

## 触控动作

| 动作 | 效果 |
| --- | --- |
| `passthrough` | 将原始触摸值交给当前插件处理（默认） |
| `next_page` / `prev_page` | 切换下一页 / 上一页 |
| `flip` | 上下翻转 |
| `toggle_orientation` | 切换横屏与竖屏 |
| `lock` | 锁定当前页，不再自动切页 |
| `screenshot` | 截取当前屏幕并保存 |
| `mute_toggle` | 切换系统静音 |
| `macro` | 发送键盘宏，例如 `ctrl+shift+s`、`alt+f4` |
| `launch` | 启动指定可执行文件，支持完整路径与环境变量 |
| `shutdown` | 关闭上位机 |
| `dim_down` / `dim_up` | 亮度降低 / 提高 15% |

三种手势独立配置，默认均为 `passthrough`。

## 诊断与日志

| 文件 | 位置 | 内容 |
| --- | --- | --- |
| `console.log` | 程序目录 | 轮转日志，单文件 2 MB，保留 3 份 |
| `crash.log` | 程序目录 | Python 未捕获异常与原生崩溃记录 |
| `config.json` | 程序目录（或 `--config` 指定） | 设备、页面顺序、触控映射、背景图、显示与开机待机设置 |
| `widget_config/` | 程序目录 | 每个插件的独立配置，插件名即文件名 |

程序目录不可写时依次退回用户目录与临时目录。已有实例在运行时启动会以退出码 2 结束。排查顺序为：`--selftest` 检查链路，状态卡片查看帧率与重发，`console.log` 查看串口异常，`crash.log` 查看崩溃记录。

## 稳定性设计

| 机制 | 作用 |
| --- | --- |
| 每帧回执 | 设备报 `0x15` 时整帧重发，避免长期运行累积错误 |
| 帧级串口锁 | 一帧像素流期间插入的命令排队执行，不会切断像素段 |
| 分块签名 | 每帧核对 50 块校验和，只重画不一致的区域 |
| 自动重连 | 拔插或更换端口后自动找回设备 |
| 单实例互斥 | 禁止两个进程同时占用同一串口 |
| 有界重连 | 升级后按固定间隔重试，超时后明确报错 |

## 打包

```powershell
python tools/build_exe.py --mode console --name MSU2_Console_Open
```

`--mode` 可选 `console` 或 `noconsole`；`--name` 决定产物名；`--no-numpy` 在不需要加速的平台上剔除 numpy。脚本固定以下选项：

- `--onefile`、`--optimize 1`、`--noconfirm --clean`；
- 图标同时作为运行时资源与可执行文件图标（`--add-data "icon.ico;."`、`--icon icon.ico`）；
- `--add-data` 携带 `msu2_core` 与 `widgets`，插件在运行时按目录发现，两者必须一起打包；
- 对 `msu2_core`、`widgets`、`PIL` 做 `--collect-submodules`；
- 主机上未安装的可选依赖（pynput、numpy、pywin32 等）自动排除。

产物在 `dist\`。

## 测试

```powershell
python -m unittest discover -s tests      # 134 个用例，不需要连接设备
python -X utf8 tools\lint.py              # 静态检查（优先 ruff，其次 pyflakes）
python msu2_mini.py --selftest            # 连接设备后的整机自检
```

测试覆盖整帧、差分与 RLE 编码，分块签名，触控状态机，协议字段，配置夹取，Intel HEX 解析，资产 RLE 往返，OTA 通道自检，`0A 18` 贴图的有符号坐标，关闭串口前的 flush，有界重连，以及坏块补画与整帧重发的分支。

## 插件开发

- 插件放在 `widgets/<name>/`，继承 `msu2_core.widget.Widget`，在 `msu2_core.registry` 注册，`__init__.py` 会被自动发现。
- 设置项通过 `msu2_core.config_schema.py` 的字段类型声明，WebUI 的配置面板据此自动生成。
- 外挂插件目录支持热重载，`--new-widget NAME` 生成骨架，`--plugins` 查看加载结果。
- 详细说明见仓库内 `widgets-dev.md`。

## 常见问题

**设备无法识别**

1. 确认设备管理器中显示为 `1A86:FE0D`；若为 `FE0C`，说明设备仍是原厂固件。
2. 更换数据线或 USB 端口。板边金手指直接插入 USB-A，接触不良较常见。
3. 关闭可能独占端口的串口工具。
4. 若仍无法识别，短接 `USB_D+` 与 `3.3V` 焊盘后插入 USB 进入 ROM ISP，用 WCHISPTool 重新烧写。

**固件版本不匹配**

界面显示「固件版本未知」时，说明设备固件没有返回版本帧。用 WCHISPTool 烧写配套固件后，即可使用网页或命令行的升级功能。

**升级失败**

| 现象 | 处理 |
| --- | --- |
| 提交后设备复位但版本未变 | 读取 `0A 0F` 获取结果码：1 头部错误、2 长度非法、3 校验失败、4 读回校验失败、5 整段 CRC 预检失败 |
| 返回 `0A 14 ... 15 FE` | 搬运动作被拒绝：缺少令牌、本次上电未完成提交，或仍在接收数据 |
| 传输中断 | 关闭其它串口工具，确认连接稳定后重试，升级会从分窗起点重新写入 |
| 无法开始 | 镜像不超过 48 000 字节，并补齐到 256 字节的整数倍；`.hex` 会先解析为二进制 |

**画面异常**

1. 先用 `--selftest` 确认局部变化帧与整屏噪点帧是否全部成功；出现 `0x15` 重发说明链路丢字节，优先更换线缆与端口。
2. 写入一张纯色图并预览：纯色正常而复杂图像异常，检查 SPI 时序与信号质量；纯色也异常，检查窗口坐标与行偏移。
3. 不同批次的 ST7735S 偏移与颜色顺序可能不同，表现为整体偏色或整屏位移；可改变朝向观察偏移是否固定。

## 许可

MIT。硬件资料与协议说明见 `Open_MSU2_MINI`，固件与协议表见 `Open_MSU2_MINI_FW`。
