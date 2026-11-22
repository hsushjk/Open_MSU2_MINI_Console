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

```bash
pip install -r requirements.txt
python msu2_mini.py
```

打包：

```bash
build.bat
```

## 许可

MIT，见 `LICENSE`。上游的界面、插件框架与大部分 widget 由原作者编写，本仓库在其上做了协议层与发送路径的修改。

## 相关仓库

- [Open_MSU2_MINI](https://github.com/hsushjk/Open_MSU2_MINI) —— 硬件：引脚、网表、协议说明
- [MSU2_MINI_FW_Reverse](https://github.com/hsushjk/MSU2_MINI_FW_Reverse) —— 原厂固件反编译与分析
- [MSU2_MINI_Console](https://github.com/hsushjk/MSU2_MINI_Console) —— 原厂固件的上位机（本仓库的上游）