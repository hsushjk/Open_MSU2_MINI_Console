# Linux 兼容性说明

适用版本：上位机 1.0.9，配套固件 1.0.9。

测试环境：

- 主机：Debian GNU/Linux 13 (trixie)，内核 6.12.113，Python 3.13.5；
- 设备：MSU2_MINI（CH32X033F8P6 + P25D80 + ST7735S 160×80），USB CDC `1a86:fe0d`；
- 运行方式：源码模式，venv 内安装 pyserial 3.5、Pillow 13.0.0、psutil 7.2.2；
- 图形环境：无本地窗口，界面经 WebUI 访问。

## 安装与启动

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install pyserial pillow psutil            # 可选：pynput、dbus-python
python msu2_mini.py --port /dev/serial/by-id/usb-MSU2_MSU2_MINI_*
```

浏览器打开 `http://<主机地址>:8765`。`--headless` 表示不启动 WebUI，仅运行调度与串口部分；需要界面时不要加该参数。无 `DISPLAY` 的环境下 `--simulate` 会提示缺少图形环境后退出。

## 串口设备选择

设备重新枚举后节点号可能由 `/dev/ttyACM0` 变为 `/dev/ttyACM1`，因此应使用 `/dev/serial/by-id/` 下的固定链接：

```
/dev/serial/by-id/usb-MSU2_MSU2_MINI_MSU2M24Q300-if00
```

该链接在重新枚举后自动指向新节点，升级后的重连也依赖它。串口探测只优先尝试 USB CDC 端口，避免打开蓝牙或虚拟串口时长时间等待。

权限方面，当前用户需要在 `dialout` 组中，或对设备节点有读写权限。

## 性能数据

| 项目 | 实测值 |
| --- | --- |
| 整帧 25 924 字节 | 104 至 123 ms |
| 分块签名坏块 | 0/50 |
| 原始 25 920 字节连续写 | 约 50 ms，约 500 KB/s |
| 局部增量帧 1 084 字节 | 0 至 3 ms |
| 待机动画步进 | 每步约 0.7 ms |

整帧 104 ms 为设备侧主循环按 64 字节每包消费的上限，约 9.6 fps；界面使用增量帧，日常刷新不依赖整帧耗时。

## 平台相关实现

- POSIX 平台下串口写使用非阻塞循环：`os.set_blocking(fd, False)` 配合 2048 字节分片与 `select` 等待可写。Linux 的 `cdc_acm` 驱动在写缓冲满时可能出现数百毫秒的静默，阻塞式写入会直接暴露为卡顿；非阻塞循环可避免该问题。Windows 仍使用串口库的阻塞写。
- 串口探测使用 0.35 秒短超时，探测阶段不发送任何数据，并优先选择 `hwid` 含 `1A86` 或 `USB` 的端口。
- `--selftest` 与多设备自检同样只遍历 USB CDC 端口。
- `--simulate` 在缺少图形环境时给出提示并正常退出。

## 验证清单

命令行：

- `--version` 输出 `MSU2_MINI 1.0.9`；
- `--list` 列出 `/dev/ttyACM0` 并标识为 MSU2_MINI；`/dev/ttyS*` 仅展示，不参与探测；
- `--plugins` 列出 16 个内置插件；
- `--devices` 显示设备固件版本、亮度、朝向与当前页；
- `--selftest` 通过：局部变化帧 4/4、整屏噪点帧 3/3、分块签名 50/50、复位记录、NOR 抽查 7 处、OTA 通道 1 024 字节、待机槽 CRC、资产回读、动画步进。

WebUI：

- `GET /` 返回 200；`/favicon.png` 与 `/favicon.ico` 返回正确图标格式；
- `GET /api/dim`、`/api/touch_config`、`/api/widget_config` 返回预期结构；
- `POST /api/next` 触发设备翻页，`POST /api/dim` 改变亮度，`POST /api/widget_config` 写入插件配置文件；
- 背景图上传后自动规范为 160×80 并保存到 `backgrounds/`；
- 16 个内置页面逐页切换正常，插件依赖缺失时仅记录 WARNING（`music` 缺 dbus-python，`keyboard_stats` 与 `mouse_mileage` 缺 pynput）。

文件布局：配置、日志、上传文件与插件配置全部位于程序目录，`config.json` 顶层不包含插件配置（各插件配置在 `widget_config/<插件名>.json`）。

## 已知限制

- 多设备同时推流未实测（测试机仅一块屏）；代码路径与单设备共用，队列深度为 2。
- 物理触摸手势未逐项验证，仅确认 ADC 读数随按压变化（约 3 927 至 3 946）。
- WebUI 中的固件上传表单与浏览器文件选择未逐项操作；命令行升级与上传接口已验证。
- 剪贴板、GIF、应用使用统计等插件仅确认渲染，未做长时间观察。
- Linux 平台没有本地窗口，界面为 WebUI；`--headless` 不启动 WebUI。
- 整帧刷新上限约 9.6 fps，由固件消费节奏决定；增量帧不受影响。

## 复现步骤

```bash
# 部署
tar czf host.tgz msu2_mini.py msu2_core widgets tests tools pyproject.toml README.md
scp host.tgz user@host:~/
ssh user@host 'mkdir -p ~/msu2 && tar xzf host.tgz -C ~/msu2'
ssh user@host 'cd ~/msu2 && python3 -m venv .venv && .venv/bin/pip install pyserial pillow psutil'

# 定位串口
ls /dev/serial/by-id/ | grep -i msu2

# 自检与运行
cd ~/msu2
.venv/bin/python msu2_mini.py --selftest --port /dev/serial/by-id/usb-MSU2_MSU2_MINI_*
.venv/bin/python msu2_mini.py --port /dev/serial/by-id/usb-MSU2_MSU2_MINI_*

# 升级
.venv/bin/python msu2_mini.py --port /dev/serial/by-id/usb-MSU2_MSU2_MINI_* --flash msu2_proto_fw.hex
```