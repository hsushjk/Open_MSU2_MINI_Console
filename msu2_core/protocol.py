"""固件协议：帧构造（Cmd/MiniProtocol）、同步栅栏、RLE 与分段载荷、屏幕与分块常量。

从 msu2_mini.py 拆出（行为不变）。只依赖 numpy（可选），不依赖其它新模块。
"""
from __future__ import annotations

from msu2_core import LANDSCAPE_SIZE

try:
    import numpy as _np
except Exception:  # pragma: no cover - 没装 numpy 时退化为原样发送
    _np = None

# 屏幕几何（横向 160x80）
SCREEN_W, SCREEN_H = LANDSCAPE_SIZE

# 160x80 屏 = 10x5 个 16x16 块（固件 v56 的 0A 11 核对用）
TILE_N = 50


def _rle_payload(chunk):
    """大端 RGB565 字节流做行程编码，返回 [像素数, 高, 低] 三元组序列。"""
    n = len(chunk) // 2
    if n <= 0 or _np is None:
        return b""
    arr = _np.frombuffer(chunk[:n * 2], dtype=">u2")
    if n > 1:
        cut = _np.flatnonzero(arr[1:] != arr[:-1]) + 1
        starts = _np.concatenate((_np.zeros(1, dtype=_np.int64), cut))
        lens = _np.diff(_np.concatenate((starts, _np.array([n], dtype=_np.int64))))
    else:
        starts = _np.zeros(1, dtype=_np.int64)
        lens = _np.array([n], dtype=_np.int64)
    counts = []
    vals = []
    for st, ln in zip(starts.tolist(), lens.tolist()):
        v = int(arr[st])
        while ln > 255:
            counts.append(255)
            vals.append(v)
            ln -= 255
        counts.append(ln)
        vals.append(v)
    if not counts:
        return b""
    cnt = _np.array(counts, dtype=_np.uint8).tobytes()
    val = _np.array(vals, dtype=">u2").tobytes()
    out = bytearray(len(counts) * 3)
    out[0::3] = cnt
    out[1::3] = val[0::2]
    out[2::3] = val[1::2]
    return bytes(out)


def rle_encode_rgb565(data):
    """把 RGB565 字节流（高字节在前）压成资产 fmt=1 的 `[cnt, hi, lo]` 流。

    语义与 `_rle_payload()` / 固件 `rle_feed()` 完全一致：`cnt` 是 1..255 的
    重复像素数（0 表示这一包含 0 个像素，编码器不会产生），长行程按 255 拆包。
    纯 Python 实现，不依赖 numpy（资产压缩在没装 numpy 的机器上也要能用）。
    """
    n = len(data) // 2
    out = bytearray()
    i = 0
    while i < n:
        hi = data[2 * i]
        lo = data[2 * i + 1]
        run = 1
        while i + run < n and run < 255:
            j = 2 * (i + run)
            if data[j] != hi or data[j + 1] != lo:
                break
            run += 1
        out += bytes((run, hi, lo))
        i += run
    return bytes(out)


def rle_decode_rgb565(payload):
    """`rle_encode_rgb565()` 的逆过程；测试与离线工具用。"""
    out = bytearray()
    i = 0
    while i + 3 <= len(payload):
        cnt = payload[i]
        hi = payload[i + 1]
        lo = payload[i + 2]
        i += 3
        if cnt:
            out += bytes((hi, lo)) * cnt
    return bytes(out)


def compress_asset_payload(raw):
    """资产载荷选型：RLE 比原样小就用 fmt=1，否则原样 fmt=0。

    返回 `(payload, fmt)`。纯色/渐变的压缩收益很大，照片类几乎压不动，
    这时保持 fmt=0（设备端零解码开销，也没有“压缩反而更大”的风险）。
    """
    rle = rle_encode_rgb565(raw)
    if rle and len(rle) < len(raw):
        return rle, 1
    return raw, 0


def lcd_segment_payload(chunk):
    """选压缩或原样：压缩后更小就走 0x0D，否则按 0x0C 原样发。"""
    rle = _rle_payload(chunk)
    if rle and len(rle) < len(chunk):
        return MiniProtocol.lcd_rle_segment(rle)
    return MiniProtocol.lcd_raw_segment(chunk)


class Cmd:
    SFR = 0x00; LCD = 0x02; FLASH = 0x03; BULK = 0x04; ADC = 0x08
    LCD_SET_XY = 0x00; LCD_SET_SIZE = 0x01; LCD_SET_COLOR = 0x02
    LCD_DRAW = 0x03
    DRAW_LOAD_ADDR = 0x07; DRAW_DATA = 0x08
    DRAW_DIRECTION = 0x0A
    FILL_COLOR = 0x04


class MiniProtocol:
    @staticmethod
    def _frame(*vals):
        return bytes(v & 0xFF for v in vals)

    # --- 设备端配置存档（v30+）：亮/朝向/翻转/页，存在板载 NOR 里 ---
    @staticmethod
    def devcfg_info():
        return MiniProtocol._frame(0x09, 0x01, 0, 0, 0, 0)

    @staticmethod
    def devcfg_write(dim, orient, reverse, page):
        return MiniProtocol._frame(0x09, 0x02, dim, orient, reverse, page)


    # --- 资产：开机/待机画面存在板载 NOR，class 0x0A（16 槽） ---
    ASSET_BOOT = 0x00
    ASSET_STANDBY = 0x01

    @staticmethod
    def asset_info():
        return MiniProtocol._frame(0x0A, 0x00, 0, 0, 0, 0)

    @staticmethod
    def asset_begin(kind, slot, length, fmt=0):
        """`0A 01`：开始写图。kind 最高位携带数据格式。

        fmt=0 是裸 RGB565（25 600 B），fmt=1 是 RLE，此时 length 是压缩后
        长度；低 7 位仍是 kind（0 开机 / 1 待机）。
        """
        if fmt:
            kind |= 0x80
        return MiniProtocol._frame(0x0A, 0x01, kind, slot,
                                   (length >> 8) & 0xFF, length & 0xFF)

    @staticmethod
    def asset_window(n):
        """送一段数据：`0A 08 b2 b3 len_hi len_lo`，紧跟 n 个字节，设备收满回 ack。

        注意长度放在**第 5、6 字节**（和 `asset_begin` 一致）：固件读 f[4]:f[5]。
        之前放在 b2/b3，设备读成 0 就退化成 4096，永远等不满，把板子卡住。
        """
        return MiniProtocol._frame(0x0A, 0x08, 0, 0, (n >> 8) & 0xFF, n & 0xFF)

    @staticmethod
    def asset_commit(crc32):
        return MiniProtocol._frame(0x0A, 0x03,
                                   (crc32 >> 24) & 0xFF, (crc32 >> 16) & 0xFF,
                                   (crc32 >> 8) & 0xFF, crc32 & 0xFF)

    @staticmethod
    def asset_abort():
        return MiniProtocol._frame(0x0A, 0x02, 0, 0, 0, 0)

    @staticmethod
    def asset_draw(kind, slot):
        return MiniProtocol._frame(0x0A, 0x04, kind, slot, 0, 0)

    @staticmethod
    def asset_erase(kind, slot):
        return MiniProtocol._frame(0x0A, 0x05, kind, slot, 0, 0)

    @staticmethod
    def asset_settings(boot_mode, standby_mode, count, dwell):
        return MiniProtocol._frame(0x0A, 0x06, boot_mode, standby_mode,
                                   count, dwell)

    @staticmethod
    def boot_report():
        return MiniProtocol._frame(0x0A, 0x0B, 0, 0, 0, 0)

    @staticmethod
    def asset_wipe():
        """抹掉全部资产与设置：`0A 0A`"""
        return MiniProtocol._frame(0x0A, 0x0A, 0, 0, 0, 0)

    @staticmethod
    def boot_trace():
        return MiniProtocol._frame(0x0A, 0x0D, 0, 0, 0, 0)

    @staticmethod
    def asset_verify(kind, slot):
        return MiniProtocol._frame(0x0A, 0x0E, kind, slot, 0, 0)

    @staticmethod
    def anim_probe(n):
        return MiniProtocol._frame(0x0A, 0x10, n, 0, 0, 0)

    @staticmethod
    def tile_check():
        return MiniProtocol._frame(0x0A, 0x11, 0, 0, 0, 0)

    @staticmethod
    def flash_begin(total):
        return MiniProtocol._frame(0x0A, 0x12, 0x01, 0, (total >> 8) & 0xFF, total & 0xFF)

    @staticmethod
    def flash_window(n):
        return MiniProtocol._frame(0x0A, 0x12, 0x02, 0, (n >> 8) & 0xFF, n & 0xFF)

    @staticmethod
    def flash_commit(crc):
        return MiniProtocol._frame(0x13, 0, (crc >> 24) & 0xFF, (crc >> 16) & 0xFF,
                                   (crc >> 8) & 0xFF, crc & 0xFF)

    @staticmethod
    def flash_apply():
        # f[2]/f[3] 是确认令牌 A5 5A：固件只会执行带令牌的搬运命令，
        # 防止流错位时像素字节凑出 0A 14 误触内部 flash 擦写。
        return MiniProtocol._frame(0x14, 0, 0xA5, 0x5A, 0, 0)

    @staticmethod
    def asset_boot_hold(secs):
        """开机画面停留秒数：`0A 09 secs`"""
        return MiniProtocol._frame(0x0A, 0x09, int(secs) & 0xFF, 0, 0, 0)

    @staticmethod
    def lcd_set_xy(x, y):
        return MiniProtocol._frame(Cmd.LCD, Cmd.LCD_SET_XY,
                                   (x >> 8) & 0xFF, x & 0xFF,
                                   (y >> 8) & 0xFF, y & 0xFF)

    @staticmethod
    def lcd_set_size(w, h):
        return MiniProtocol._frame(Cmd.LCD, Cmd.LCD_SET_SIZE,
                                   (w >> 8) & 0xFF, w & 0xFF,
                                   (h >> 8) & 0xFF, h & 0xFF)

    @staticmethod
    def lcd_enter_direct_mode():
        return MiniProtocol._frame(Cmd.LCD, Cmd.LCD_DRAW,
                                   Cmd.DRAW_LOAD_ADDR, 0, 0, 0)

    @staticmethod
    def lcd_set_fill(b0, b1, b2, b3):
        return MiniProtocol._frame(Cmd.LCD, Cmd.FILL_COLOR, b0, b1, b2, b3)

    @staticmethod
    def lcd_data_block(idx, b0, b1, b2, b3):
        return MiniProtocol._frame(Cmd.BULK, idx, b0, b1, b2, b3)

    @staticmethod
    def lcd_segment_end_full():
        return MiniProtocol._frame(Cmd.LCD, Cmd.LCD_DRAW,
                                   Cmd.DRAW_DATA, 0x01, 0x00, 0x00)

    @staticmethod
    def lcd_segment_end_tail(tail):
        return MiniProtocol._frame(Cmd.LCD, Cmd.LCD_DRAW,
                                   Cmd.DRAW_DATA, 0x00, tail & 0xFF, 0x00)

    @staticmethod
    def lcd_raw_segment(data):
        n = len(data)
        return (MiniProtocol._frame(Cmd.LCD, Cmd.LCD_DRAW, 0x0C,
                                    (n >> 8) & 0xFF, n & 0xFF, 0x00)
                + bytes(data))

    @staticmethod
    def lcd_rle_segment(payload):
        n = len(payload)
        return (MiniProtocol._frame(Cmd.LCD, Cmd.LCD_DRAW, 0x0D,
                                    (n >> 8) & 0xFF, n & 0xFF, 0x01)
                + bytes(payload))

    @staticmethod
    def lcd_raw_end():
        return MiniProtocol._frame(Cmd.LCD, Cmd.LCD_DRAW, 0x09, 0, 0, 0)

    @staticmethod
    def lcd_direction(reverse):
        return MiniProtocol._frame(Cmd.LCD, Cmd.LCD_DRAW,
                                   Cmd.DRAW_DIRECTION, reverse & 0x01, 0, 0)

    @staticmethod
    def read_adc(ch):
        return MiniProtocol._frame(Cmd.ADC, ch, 0, 0, 0, 0)


# 同步栅栏：坏帧之后连发这几个字节，设备无论停在收像素还是等命令，
# 都会认出来、把现场清干净并回一个 0x06。像素里出现它的概率是 2^-48。
END_FRAME = bytes((0x02, 0x03, 0x09, 0x00, 0x00, 0x00))
