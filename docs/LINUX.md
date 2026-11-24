# Linux 兼容性测试报告

对象：`Open_MSU2_MINI_Console` v1.0.13 + 固件 v1.0.13，真机 MSU2_MINI（CH32X033 + P25D80 + ST7735S 160×80）。
测试机：Debian GNU/Linux 13 (trixie)，内核 6.12.113，Python 3.13.5，USB CDC `1a86:fe0d`。
测试方式：SSH 远程跑源码（venv：pyserial 3.5 / Pillow 13.0.0 / psutil 7.2.2），屏幕插在测试机上。

## 结论速览

| 项目 | 结果 |
| --- | --- |
| 枚举 / 握手 / 版本 | 通过（`1a86:fe0d`，`/dev/serial/by-id/…-if00`） |
| `--selftest` | **PASS**（帧、分块签名、NOR、OTA 通道、资产 CRC、动画探针） |
| 整帧 25 924 B 耗时 | **104–123 ms**（修复前 759 / 1803 / 2748 ms） |
| 分块签名坏块 | **0/50**（修复前 30/50、40/50） |
| 原始 25 920 B 写 | **50 ms ≈ 500 KB/s**（修复前 701–719 ms，含 520 ms 卡顿） |
| CLI OTA | 通过（写 19 968 B → 重启 → 按 by-id 找回 → 版本 1.0.13） |
| WebUI | 通过（页面、favicon、GET/POST API、插件配置、背景上传） |
| 16 个内置插件轮询 | 全部渲染并上屏，无异常栈 |
| 数据目录 | 全部在程序同目录，退出后可完整搬走 |

## 一、发现并修复的问题

### 1. 整帧传输慢 3–37 倍，且大量块坏（三个原因叠加）

**现象**：`--selftest` 整屏噪点 2 748 ms / 坏块 30/50，随后 1 803 ms / 40/50；串口写单笔随机卡 505–535 ms。

**排查过程（每一步都排除了一个假设）**
1. 设备侧 `0A 1A` 诊断回 `g_dma_to == 0`：SPI DMA 一次都没超时 → “DMA 等超时”不成立。
2. 发 25 920 B 未知命令 `0xEE`（完全不进像素/LCD 路径）照样卡 → 与像素路径无关。
3. 绕过 pyserial 直接 `os.write` 照样卡 → 与 pyserial 无关。
4. `usbmon` 抓 `1u`：最大静默 **519.7 ms** 落在设备 OUT 完成之后，期间主机**一笔 OUT URB 都没提交**，恢复时先出现 IN URB 重提 → 是 **tty/cdc_acm 写路径在缓冲满时丢了 wakeup**。
5. `_dbg_nb.py` 用 `O_NONBLOCK` + `select` 手写写循环：同样 25 920 B 只要 **50 ms**，零卡顿。

**修复**
- `msu2_core/transport.py`：POSIX 下所有串口写走 `_write_raw()`（`os.set_blocking(fd, False)` + 2048 B 分片 + `select` 等待可写 + 3 s 兜底），Windows 保持原路径。26 KB 整帧从 700–1600 ms 降到 ~50 ms。
- 固件 `main_proto.c`：像素流“断流放弃”阈值 4 000 圈 → **400 000 圈**。旧值约几毫秒，Linux 的成串 USB 传输块间有十几毫秒空档，会被误判成断流、整帧作废。
- 主机限速下限 40 KB/s → **120 KB/s**，块间隔封顶 10 ms（`msu2_core/device.py`）。
- 固件 `usb.c`：去掉“接收环余量 < 2 KB 就提前 NAK”的粗节流；改为「这一包（≤64 B）放得下就 ACK，真放不下才 NAK」。提前 NAK 会让主机 OUT URB 部分完成，Linux 的 cdc_acm 为此要退避几百毫秒。

**修复前后**

| 指标 | 修复前 | 修复后 |
| --- | --- | --- |
| 整帧 25 924 B | 759 / 1 803 / 2 748 ms | **104 / 122 / 123 ms** |
| 分块签名坏块 | 30/50、40/50 | **0/50（50 块全对上）** |
| 原始 25 920 B 写 | 701–719 ms（含 520 ms 卡顿） | **50 ms（≈500 KB/s，零卡顿）** |
| 局部增量 1 084 B | 3 ms | **0–3 ms** |

> 整帧 104 ms 是固件主循环按 64 字节/包消费的速度上限（约 290 KB/s 设备侧），与主机无关；日常界面用的是增量帧（~1 KB / 0–3 ms），所以观感是流畅的。

### 2. 启动第一笔写撞 1 秒 `Write timeout`

**原因**：探测会自动打开**所有**串口（含蓝牙 COM7/COM10），每个都走完整握手 + 灌 32 KB，而且握手前还先写 6 字节“对齐垫”；那笔垫字节落到没准备好的口上就撞 1 s 写超时。另外 `read_all()` 会因为写超时把 `_connected` 置 False，之后连问候都读不到。

**修复**：只优先探测 `hwid` 含 `1A86`/`USB` 的口、探测超时 0.35 s 且不灌字节；打开端口后先只听问候、绝不先写；写超时不再让读路径失效。

### 3. OTA 之后报「设备没有回来」

**原因**：设备重启后 Linux 重新枚举，节点从 `/dev/ttyACM0` 变成 `ttyACM1`，而上位机按原来那个固定名字等。
**修复/用法**：统一用 `/dev/serial/by-id/usb-MSU2_MSU2_MINI_MSU2M24Q300-if00`（软链接在重枚举后自动指向新节点）。实测 Linux 上 `--flash` 全流程通过：`[OK] uploaded 19968 bytes, device is rebooting` → `升级完成：固件 1.0.13`。

### 4. `--simulate` 在没有图形环境时甩 tkinter 堆栈

无 `DISPLAY` 时原来直接 `_tkinter.TclError` 崩掉。现在会打印一句人话（需要图形环境 / 请用 WebUI）后正常退出。

### 5. 其它

- `--selftest` 不再去戳 `/dev/ttyS0..3`（会白等 2 s 并打 `Could not configure port` 噪音），只挑 USB CDC 口。
- `widgets/mouse_mileage.py` docstring 加 `r` 前缀，消掉 `SyntaxWarning: invalid escape sequence '\M'`。

## 二、逐项验证

**命令行**
- `--version` → `MSU2_MINI 1.0.13`；`--plugins` 16 个；`--list` 能认出 `/dev/ttyACM0 MSU2_MINI`；从别的目录（`/tmp`）启动也正确找到程序目录里的 `config.json`。
- `--devices` → `dev1 … 固件 1.0.13 亮度 80% 翻转否 landscape 页 countdown`。
- `--selftest` → 局部变化 4/4（0 ms / 1 084 B）、整屏噪点 3/3（123 ms / 25 924 B）、分块签名 50/50、复位面包屑、NOR 抽查 7 点、OTA 通道 1 024 B、待机槽 1–4 CRC（68D9/5627/DDFD/70A7）、资产回读 4 张、动画探针 10 步 7 ms → **[PASS]**。

**WebUI（`http://<机器>:8765`）**
- `GET /` 200 / 67 069 B，`/favicon.png?v=2` 200 / 2 879 B。
- `GET /api/dim` → `{"ok":true,"dim":0}`；`GET /api/touch_config` → 13 种动作 + 阈值；`GET /api/widget_config?name=todo` → 配置 schema。
- `POST /api/next` → 设备翻页（日志 `翻页 -> calendar`）；`POST /api/dim {"dim":20}` → `亮度 -> 80%`；`POST /api/widget_config` → 落盘到 `widget_config/<插件>.json`。
- 背景图上传（`X-Filename` + `X-Purpose: background`）→ 自动规范成 160×80 存进 `backgrounds/`。

**插件**
- 16 个内置页面各切一次（共 17 次翻页）全部正常上屏，无异常栈。
- 依赖缺失时优雅降级（只有 WARNING）：`music` 缺 dbus-python、`keyboard_stats` / `mouse_mileage` 缺 pynput。

**文件布局**（全部在程序目录）
`config.json`（顶层不含 `widget_config`）、`widget_config/<插件>.json`、`console.log`、`crash.log`、`uploads/`、`backgrounds/`、`todo.md`。

## 三、未覆盖 / 已知限制

- **多设备同时推流**：测试机只有一块屏，未实测；代码路径与单设备共用（`DeviceChannel`，队列深度 2）。
- **物理触摸**：只验证了 ADC 有读数（3 927–3 946 随按压变化），手势需要手指按住实测。
- **WebUI 里的固件上传**（`/api/flash` 表单）与浏览器端文件选择：未逐项点过；命令行 OTA 与原始上传接口已验。
- **Linux 下的插件细节**：剪贴板（X11/xclip 路径）、GIF、应用使用统计只在轮询里渲染过，未做长时观察。
- **无 GUI 窗口是设计如此**：Linux 上没有本地窗口，界面就是 WebUI；`--headless` 表示连 WebUI 都不起（纯设备端）。没有 `DISPLAY` 时 `--simulate` 会给提示退出。
- **整帧上限约 9.6 fps**：设备按 64 字节/包消费，属固件侧节奏；日常增量帧 ~0–3 ms，不受影响。
- `--list` 仍会把 `/dev/ttyS*` 列出来（只是展示，不探测）。

## 四、复现步骤

```bash
# 1) 部署
tar czf host.tgz msu2_mini.py msu2_core widgets tests tools pyproject.toml README.md
scp host.tgz user@host:~/ && ssh user@host 'mkdir -p ~/msu2 && tar xzf host.tgz -C ~/msu2'
ssh user@host 'cd ~/msu2 && python3 -m venv .venv && .venv/bin/pip install pyserial pillow psutil'

# 2) 找口（推荐 by-id）
ls /dev/serial/by-id/ | grep -i msu2

# 3) 自检 + 真机跑
cd ~/msu2 && .venv/bin/python msu2_mini.py --selftest --port /dev/serial/by-id/usb-MSU2_MSU2_MINI_*
.venv/bin/python msu2_mini.py --port /dev/serial/by-id/usb-MSU2_MSU2_MINI_*   # 然后浏览器开 http://host:8765

# 4) OTA
.venv/bin/python msu2_mini.py --port /dev/serial/by-id/usb-MSU2_MSU2_MINI_* --flash ~/MSU2_open_v1.0.13.hex
```