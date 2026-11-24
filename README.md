# Open_MSU2_MINI_Console

MSU2_MINI 小屏的上位机 **v1.0.13**（Windows / Linux）：一个本地 WebUI（默认 `http://0.0.0.0:8765`）把插件页、截屏、背景图、开机/待机画面、OTA 升级和全局触控手势全放在一个页面里，另带 `--selftest` / `--flash` / `--simulate` 等命令行入口。

## 关键特性

- **16 个内置插件页**（时钟、日历、待办、音乐、CPU/网络/磁盘、剪贴板、GIF…），外挂插件目录即插即用。
- **每帧 1 字节裁决**：设备回 `0x06` 才认为这帧画对了，回 `0x15` 就整帧重发；帧级串口锁保证别处插进来的命令不会把像素流切坏。
- **拔插自恢复**：串口掉线自动重连，换 COM 号也能找回来；单实例互斥防止两个进程抢同一个口。
- **开机 / 待机画面都写进板载 NOR**：开机图 1 槽 + 待机图 16 槽，写完不连电脑也生效；固件 v1.0.4 起支持 **RLE 压缩**，网页会显示每张图「原始 / 压缩后 / 省百分比」。
- **免拆 OTA**：`.hex` / `.bin` 直接在页面上点，先写 NOR 暂存区、CRC 提交、再让固件自己搬运。
- **全局触控手势**：单击 / 双击 / 长按映射到透传、亮度、截屏、静音、键盘宏、启动程序、关掉上位机。
- **可诊断**：`--selftest` 一次跑完链路、触摸、帧、分块签名、复位面包屑、NOR 抽查、**OTA 通道自检**（写暂存区 + 回读头，不 apply）、资产 CRC；日志与崩溃记录落盘。

## 安装

### 方式一：pip（开发 / 改代码）

```powershell
cd Open_MSU2_MINI_Console
pip install .
msu2-console                 # 安装后的命令
```

也可以不安装，直接跑源码：

```powershell
pip install -r requirements.txt
python msu2_mini.py
```

### 方式二：预编译 exe

`dist/MSU2_Console.exe`（或 `MSU2_Console_Open.exe`）双击即用，无需 Python。配置、日志与插件数据都在程序同目录（`config.json`、`console.log`、`widget_config\`、`uploads\`…），整个目录拷走就是完整备份。

### 方式三：Linux

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install pyserial pillow psutil      # 可选：pynput（键鼠统计）、dbus-python（音乐）
python msu2_mini.py --port /dev/serial/by-id/usb-MSU2_MSU2_MINI_*
```

设备每次重启后节点号可能从 `/dev/ttyACM0` 变成 `ttyACM1`，所以推荐用 `/dev/serial/by-id/` 下的软链接（OTA 之后上位机也按这个路径等设备回来）。`--headless` 是「不启动 WebUI」的意思；Linux 服务器上要看界面就不要加它。实测数据、已修问题与未覆盖项见 [docs/LINUX.md](docs/LINUX.md)。

### 依赖

| 类型 | 包 | 用途 |
| --- | --- | --- |
| 必需 | `pyserial>=3.5` | 串口链路 |
| 必需 | `pillow>=9.0.0` | 帧合成、图片解码、字体渲染 |
| 必需 | `psutil>=5.9.0` | CPU / 内存 / 磁盘 / 进程插件 |
| 可选 | `pynput>=1.7.6` | 键盘宏、静音、鼠标里程等系统动作 |
| 可选 | `mss>=9.0` | 截屏 / 投屏类插件 |

## 快速开始

1. 板子插进 USB（自研固件会以 `1A86:FE0D` 出现，端口即时可用）。
2. `python msu2_mini.py`，浏览器打开 `http://127.0.0.1:8765`。
3. 左上「设备」卡片下拉选串口（或让自动重连自己找），状态卡片出现刷新指标即通。
4. 「页面顺序」里拖拽排好页，双击页面名可立即切到该页。
5. 想让它脱离电脑也好看：在「开机 / 待机画面」里选图、写入槽位、保存设置。

命令行一次性动作：

```powershell
python msu2_mini.py --list                      # 列出插件（名称/来源/优先级/说明）
python msu2_mini.py --selftest                  # 全链路体检，逐串口跑
python msu2_mini.py --flash build/msu2_proto_fw.hex   # OTA 升级
python msu2_mini.py --simulate                  # 无硬件，开模拟屏窗口
python msu2_mini.py --headless                  # 只跑服务，不开浏览器
```

## WebUI 卡片

| 卡片 | 能做什么 |
| --- | --- |
| 设备 | 串口下拉、连接状态、320×160 实时预览（2× 放大板面） |
| 显示控制 | 上一页 / 下一页 / 切换朝向 / 上下反转；亮度下拉 100%→10%（步进 5%） |
| 全局触控 | 单击 / 双击 / 长按分别绑定动作；键盘宏输入框、启动程序路径、「应用触控」下发生效 |
| 页面顺序 | 拖拽排序、双击跳页、刷新列表、重载插件（改完插件不用重启） |
| 配置面板 | 当前插件自己的设置项，按 `config_schema.py` 声明的字段类型自动生成 |
| 通知 | 文本 + 停留 1..60 秒，推送到屏上 |
| 背景图 | 单张 / 多图轮播（2..3600 s、可无序，到点直接硬切换图）、适应 / 填充 / 拉伸、模糊 0..5、总开关；支持 gif/png/jpg/jpeg/bmp/webp（动图按每帧时长播放）。旧配置里的 `fade` 键会被静默忽略 |
| 状态 | 帧率、字节数、当前页、重发次数等运行指标 |
| 开机 / 待机画面 | 开机模式 + 停留秒数；待机模式 + 每张秒数；选图即写、16 槽列表、预览、擦除单槽、保存设置、抹掉板载 NOR、读回设备侧自检结果 |
| 固件升级 | 选 `.hex` / `.bin` 直接 OTA，带进度与失败原因 |
| 网络 | 改 WebUI 绑定地址与端口（0.0.0.0 = 局域网可访问，127.0.0.1 = 仅本机），应用后服务自动重开 |
| 关闭程序 | 停服务、释放串口 |

> 亮度说明：硬件背光是 5 V 直连、没有调光电路，所以 100% 以上不存在、“亮度”实际是上位机把合成后的像素整体压暗（内部存 0..90 的压暗量）。要真正调光得改板（把 R1 换成低边 N-MOSFET，栅极接 PA9）。

### 内置插件（17 个）

| 插件 | 内容 |
| --- | --- |
| `clock` | 时钟 / 日期 |
| `calendar` | 月历 |
| `countdown` | 倒计时 |
| `anniversary` | 纪念日 |
| `holiday` | 节假日 |
| `todo` | 待办清单 |
| `music` | 正在播放 |
| `gif` | GIF 播放 |
| `clipboard` | 剪贴板内容 |
| `app_usage` | 前台应用时长统计 |
| `process_top` | 进程占用排行 |
| `pcstats` | CPU / 内存 / 温度总览 |
| `network_detail` | 网络流量与速率 |
| `disk_io` | 磁盘读写 |
| `keyboard_stats` | 键鼠按键统计 |
| `mouse_mileage` | 鼠标里程 |
| `crypto` | 加密货币行情 |

`msu2-console --list` 可看完整表（名称 / 来源 / 优先级 / 说明），`--plugins` 只看外挂目录与加载失败原因。

## CLI 参数

| 参数 | 说明 |
| --- | --- |
| `--port PORT` | 指定串口（如 `COM7`）；不指定则自动扫描 |
| `--config PATH` | 用指定配置文件 |
| `--init-config` | 生成一份默认配置文件后退出 |
| `--headless` | 不开浏览器窗口，只起 Web 服务 |
| `--simulate` / `--offline` | 无硬件模式，额外开一个模拟屏窗口 |
| `--list` | 打印插件表后退出 |
| `--plugins` | 打印外挂插件目录与加载失败原因后退出 |
| `--new-widget NAME` | 生成一个外挂插件骨架（目录 + 入口 + 注册代码）后退出 |
| `--selftest` | 连每一路串口跑完整自检（含 OTA 通道项） |
| `--flash FILE` | 对 `.hex` / `.bin` 执行 OTA 升级；写完最多等 15 s 设备重启回来 |
| `--log` | 同时把日志打到控制台（默认也写文件） |
| `--log-level LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `--log-file PATH` | 日志文件路径，传 `none` 关闭文件日志（默认 `%LOCALAPPDATA%\MSU2_MINI\console.log`） |
| `--version` | 打印版本（`1.0.2`） |

`--selftest` 的报告结构：握手与 `raw_mode` → 固件版本 → 触摸 10 次采样 → 局部变化帧 / 整屏噪点帧的成功数与耗时 → 50 块分块签名核对（没对上会自动补画）→ 复位面包屑（上次阶段 + 复位次数）→ NOR 抽查 7 处（看是否已抹干净）→ **OTA 通道自检**（真的写一遍 NOR 暂存区并回读 16 B 镜像头核对 magic/len/crc32，但**不 apply**，设备不重启）→ 资产 CRC 回读 → 待机动画步进。

`--flash` 写完后不再只等 2 秒：和网页版共用同一套逻辑，每 200 ms 试一次重连、最多 15 s，成功打印「升级完成：固件 x.y.z」，超时打印「设备没有回来，请拔插一次 USB 再重试」并返回非 0。

## 开机 / 待机图压缩（固件 v1.0.4 起）

资产槽头 20 B：`magic('MSA1') / w / h / fmt / flags / len / crc32 / pad`。`fmt` 决定负载格式：

| fmt | 负载 | `len` / `crc32` 按什么算 |
| --- | --- | --- |
| 0 | 裸 RGB565（160×80 = 25 600 B） | 原始像素数据 |
| 1 | RLE：3 字节一包 `[cnt, hi, lo]`，`cnt` = 1..255 个重复像素 | 压缩后的数据 |

- **上位机自动选**：`Scheduler.encode_asset_image()` 先编码 RLE，只有比裸数据更小才用 `fmt=1`，否则保持 `fmt=0`（照片类常常压不动，压缩反而更大时绝不发）。
- **设备端不整图缓存**（RAM 只有 20 KB）：`asset_draw` / `0A 18` 贴图按 2 行一条带滚动解压，RLE 状态跨块保持，峰值只需要 640 B 行带 + 64 B flash 读缓冲，所以 17 张图不用多占一个字节 RAM。
- **兼容**：`fmt=0` 的老图照旧能画；老固件（< v1.0.4）也能用——上位机发现版本不支持就自动退回裸 RGB565 重写。
- **界面**：网页「资产」卡片槽位列表显示 `原始 25.0 KB` / `RLE 8.2 KB（省 68%）`，上传提示里显示 `原始 25 600 B → 压缩 N B（省 x%）`。
- **实测压缩比**（v1.0.4，同一张图上传前后）：渐变图 25 600 → 11 787 B（省 54.0%）、照片 25 600 → 21 438 B（省 16.3%）、纯色 25 600 → 153 B（省 99.4%）。17 张图裸数据约 435 KB，压缩后通常只有几十 KB。

## 全局触控手势

| 动作 | 效果 |
| --- | --- |
| `passthrough`（未定义） | 把原始触摸值交给当前插件处理（默认） |
| `next_page` / `prev_page` | 切到下一页 / 上一页 |
| `flip` | 翻转显示 |
| `toggle_orientation` | 切换朝向（横竖） |
| `lock` | 锁定当前页，不再自动切页 |
| `screenshot` | 截下当前屏并保存 |
| `mute_toggle` | 系统静音开关 |
| `macro` | 发送键盘宏，例如 `ctrl+shift+s`、`alt+f4`、`win+r` |
| `launch` | 启动指定可执行文件（支持完整路径与环境变量） |
| `shutdown` | 关闭上位机 |
| `dim_down` / `dim_up` | 亮度 −15% / +15% |

三种手势（单击 / 双击 / 长按）各自独立配置，默认全部为 `passthrough`。

## 诊断与日志

| 文件 | 位置 | 内容 |
| --- | --- | --- |
| `console.log` | `%LOCALAPPDATA%\MSU2_MINI\` | 轮转日志，单个 2 MB，保留 3 份 |
| `crash.log` | 同上 | Python 未捕获异常 + 原生崩溃；每次启动写一行 `===== 时间 启动 pid=N =====` |
| `config.json` | 同上（或 `--config` 指定） | 插件顺序、触控映射、背景图、显示参数、开待机设置 |

- 目录不可写（受限环境）时回退到 `~/.msu2_mini`，再不行退到临时目录。
- 单实例互斥：已有实例在跑时退出码为 `2` 并提示“已经有一个 MSU2 上位机实例在运行了”。
- 排障顺序：`--selftest` 看链路 → 状态卡片看帧率与重发 → `console.log` 看串口异常 → `crash.log` 看崩溃。

## 稳定性设计

| 机制 | 解决什么 |
| --- | --- |
| 每帧 1 字节裁决 | 设备报 `0x15`（像素数不对）就整帧重发，长线不累积花屏 |
| 1.5 秒“不用时”整帧 | 停止差分、直接整帧重写，防止长时间静止后残影/错位的累积误差 |
| 帧级串口锁 | 一帧像素流期间别处插进来的命令会排队，不会把 `02 03 08` 段切坏 |
| 自动重连 | 拔插、换 COM 号后自动找回设备，不要求重启上位机 |
| 分块签名补画 | 每帧结束核对 50 块校验和，只重画没对上的区域 |
| 单实例互斥 | 两个进程抢同一串口会互相踩字节，直接禁止 |

## 打包

`powershell
# 一条命令，用仓库里的 spec（已剔除用不到的 PIL AVIF 插件，省约 4.3 MB）
python -m PyInstaller --noconfirm --clean MSU2_Console_Open.spec
# 产物：dist\\MSU2_Console_Open.exe
`

spec 里已经固定了这些设置，手工改命令时别漏：

- --onefile --console --icon icon.ico
- --add-data "icon.ico;." --add-data "msu2_core;msu2_core" --add-data "widgets;widgets"
  （msu2_core/ 与 widgets/ 必须一起打进去，插件是运行时按目录发现的）
- --collect-submodules msu2_core/widgets/mss/pynput/PIL
- --optimize 1
- spec 末尾把 vif 从 inaries/datas 里过滤掉：PIL\\_avif.pyd 一个就 7.5 MB，只用于 AVIF 图片，界面与插件都用不到

可选依赖 pynput / mss 不装也能启动，只是对应动作/插件提示不可用。

## 测试

`powershell
python -m unittest discover -s tests -v   # 104 个用例，不接设备
`

覆盖编码器（整帧 / 差分 / RLE / 分块签名）、触控状态机、协议帧字段、配置夹取、
`--flash` 的 Intel HEX 解析、资产 RLE 往返与「压缩不比原始小就保持 fmt=0」、
老固件回退、OTA 通道自检（只写暂存区不 apply）、`0A 18` 贴图的有符号坐标、
关串口前 flush（半个帧会把设备的接收侧顶错位）、`wait_device_back` 有界重连，
以及「坏块补画 / 整帧重发 / 仍不同步则整屏重画」的分支。

提交前再跑一遍未定义名门禁（优先 ruff，没装就退回 pyflakes，两者都没有则跳过）：

`powershell
python -X utf8 tools\lint.py
`

真机自检（接上设备）：

`powershell
python msu2_mini.py --selftest
`

## 插件开发

- 插件放 `widgets/<name>/`，继承 `msu2_core.widget.Widget`，在 `msu2_core.registry` 注册；`__init__.py` 会被自动发现。
- 设置项用 `msu2_core.config_schema.py` 的字段类型声明，WebUI 的「配置面板」会自动渲染。
- 外挂插件目录支持热重载（页面「重载插件」），`--new-widget NAME` 生成骨架，`--list` 看加载结果。
- 详细手册见仓库内 `widgets-dev.md`。

## 常见问题

**设备不认**

1. 设备管理器看是不是 `1A86:FE0D`；显示 `FE0C` 说明板子上还是原厂固件。
2. 换一根数据线 / 换 USB 口（板边金手指直接插 USB-A，接触不良很常见）。
3. 装了 CH340/串口助手类软件时先关掉，它们会独占端口。
4. 还不行就短接 `USB_D+` 与 `3.3V` 焊盘、插 USB 进 ROM ISP，用 WCHISPTool 重烧（见固件仓库 README）。

**卡在旧固件**

- 旧版本（v8 及更早）没有版本帧，界面会显示“固件版本未知”；v1.0.0 起统一版本号，v1.0.2 起固件版本按三段式显示（正常应显示 `1.0.2`）。更老的 6 字节版本帧仍被兼容：只有 major/minor 时 patch 补 0（例如旧 v1.0.1 固件显示 `1.1.0`）。
- 用 WCHISPTool 走一次 ISP 烧 `build/msu2_proto_fw.hex`，之后就能用页面上的 OTA 免拆升级。

**OTA 失败**

| 现象 | 处理 |
| --- | --- |
| 提交后设备复位但版本没变 | 读 `0A 0F` 看 `g_up_result`：1 头不对 / 2 长度非法 / 3 体检失败 / 4 读回校验失败 |
| 报“体检失败” | 内部 flash 写序列在这颗片上不成立，内部 flash 未被改动，仍可用 ISP 正常烧录 |
| 传到一半失败 | 关掉其它串口工具；确认线不松动；重试时上位机会重新 `0A 12 01` 从头写 |
| 一直不开始 | 镜像要 ≤48000 B 且补齐成 256 的整数倍；`.hex` 会让上位机先解析成 bin |

**花屏排查三条**

1. **先看是不是偶发**：`--selftest` 的“局部变化 / 整屏噪点”成功数是否为 4/4、3/3；有 `0x15` 重发说明链路在丢字节，优先换线换口。
2. **再定位是哪一层**：用「开机 / 待机画面」写一张纯色图并预览——纯色对、复杂图糊是 SPI 速率/信号问题（24 MHz 超 ST7735S 规格，可试降分频）；纯色也错则是窗口坐标或 RASET 24 行偏移问题。
3. **最后确认是不是屏参数不同**：不同批次 ST7735S 的 offset/颜色顺序不一样，表现是整体偏色或整屏移位；同一张图换个朝向看是否固定偏移。

## 许可与声明

MIT。硬件与协议见 `Open_MSU2_MINI`；固件与协议表见 `Open_MSU2_MINI_FW`。本项目禁止倒卖。