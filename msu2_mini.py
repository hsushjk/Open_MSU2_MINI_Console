from __future__ import annotations

import argparse
import collections
import datetime
import faulthandler
import io
import json
import logging
import logging.handlers
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import urlparse, parse_qs

if getattr(sys, "frozen", False):
    _APP_DIR = Path(os.path.dirname(os.path.abspath(sys.executable)))
else:
    _APP_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
_HERE = str(_APP_DIR)
__version__ = "2.2.2"
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

def _console(text):
    try:
        print(text, file=sys.stderr)
    except Exception:
        pass

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    _console("缺少 pyserial: pip install pyserial"); sys.exit(1)

try:
    from PIL import Image, ImageDraw
except ImportError:
    _console("缺少 Pillow: pip install pillow"); sys.exit(1)

try:
    import psutil
except ImportError:
    _console("缺少 psutil: pip install psutil"); sys.exit(1)

try:
    import numpy as _np
except Exception:
    _np = None

try:
    from msu2_core import (
        WIDGET_REGISTRY, RenderContext, Widget,
        LANDSCAPE, PORTRAIT, LANDSCAPE_SIZE, PORTRAIT_SIZE,
        probe_fonts, set_user_font, get_font,
        merge_config,
    )
    from msu2_core.touch import TouchGestures
    import widgets as _widgets_pkg
except ImportError as e:
    _console(f"无法导入 msu2_core/widgets: {e}"); sys.exit(1)

SCREEN_W, SCREEN_H = LANDSCAPE_SIZE
SCREEN_BAUD = 19200
MANUAL_COOLDOWN = 0.8
try:
    import numpy as _np
except Exception:  # pragma: no cover - 没装 numpy 时退化为原样发送
    _np = None


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


def lcd_segment_payload(chunk):
    """选压缩或原样：压缩后更小就走 0x0D，否则按 0x0C 原样发。"""
    rle = _rle_payload(chunk)
    if rle and len(rle) < len(chunk):
        return MiniProtocol.lcd_rle_segment(rle)
    return MiniProtocol.lcd_raw_segment(chunk)


HEARTBEAT_INTERVAL = 2.0

TOUCH_CH = 9
TOUCH_SAMPLE_INTERVAL = 0.03
TOUCH_LONG_PRESS = 0.8
TOUCH_DOUBLE_WINDOW = 0.5
TOUCH_MIN_TAP_GAP = 0.012
TOUCH_RELEASE_RATIO = 0.3
TOUCH_DEBOUNCE = 2
TOUCH_RELEASE_DEBOUNCE = 1
TOUCH_BASE_TRACK = 0.002
TOUCH_DEFAULT_THRESHOLD = 60

def _asset_dir(name):
    for base in (_APP_DIR, _APP_DIR / "msu2_core", Path(__file__).resolve().parent,
                 Path(__file__).resolve().parent / "msu2_core"):
        p = base / "assets" / name
        if p.is_dir():
            return p
    return _APP_DIR / "assets" / name

SCREENSHOT_DIR = _APP_DIR / "screenshots"
DEFAULT_GIF_DIR = _asset_dir("gif")
MAX_REQUEST_BYTES = 32 * 1024
MAX_CONFIG_BYTES = 4 * 1024 * 1024
MAX_RESOURCE_BYTES = 8 * 1024 * 1024
MAX_TEXT_BYTES = 1024 * 1024
IMAGE_EXTS = (".gif", ".png", ".jpg", ".jpeg", ".webp", ".bmp")
TEXT_EXTS = (".md", ".txt")

log = logging.getLogger("msu2")

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
SYNC_FENCE = bytes((0xA5, 0x5A, 0xA5, 0x5A, 0x0E, 0x07))
END_FRAME = bytes((0x02, 0x03, 0x09, 0x00, 0x00, 0x00))
# v16 起：空闲探询。设备只有在“上一帧的字节全吃完、没有半开的段”时才能
# 解析到这条命令并回包，所以收到回包 = 设备确实空下来了。
QUERY_IDLE = bytes((0x00, 0x57, 0x00, 0x00, 0x00, 0x00))
IDLE_REPLY = b"\x57IDLE\xaa"


class SerialTransport:
    def __init__(self, port, baud=SCREEN_BAUD, timeout=0.05):
        self.port = port; self.baud = baud; self.timeout = timeout
        self._ser = None
        self._lock = threading.RLock()
        self.fw_version = None
        self._connected = False
        self._handshaked = False
        self.last_error = None
        self.bytes_written = 0
        self.version = ""
        self.raw_mode = False

    def open(self):
        with self._lock:
            if self._connected and self._handshaked:
                return True
            if self._ser is None:
                try:
                    self._ser = serial.Serial(self.port, self.baud,
                                              timeout=self.timeout,
                                              write_timeout=1.0)
                except PermissionError:
                    if sys.platform == "win32":
                        self.last_error = f"{self.port} 被其它程序占用"
                    else:
                        self.last_error = (
                            f"{self.port} 权限不足，"
                            f"试试 sudo usermod -aG dialout $USER")
                    return False
                except FileNotFoundError:
                    self.last_error = f"{self.port} 暂时不存在"
                    return False
                except Exception as e:
                    self.last_error = f"打开 {self.port} 失败: {e}"
                    return False
            self._connected = True
            if not self._handshaked and not self.handshake():
                self.close(); return False
            log.info("串口已打开: %s @ %d", self.port, self.baud)
            ver = self.query_version()
            if ver:
                major, minor, touch = ver
                self.fw_version = (major, minor)
                log.info("固件版本 %d.%d，触摸读数 %d", major, minor, touch)
                if touch == 0:
                    for _ in range(6):
                        time.sleep(0.08)
                        again = self.query_version(timeout=0.25)
                        if again and again[2] > 0:
                            touch = again[2]
                            log.info("触摸读数 %d（第一拍稍晚才有）", touch)
                            break
                    if touch == 0:
                        log.warning("固件上报的触摸读数是 0：ADC 还没有产出结果"
                                    "（没有触摸的老固件可以忽略这条）")
            else:
                log.warning("固件没有回应版本查询（可能是 v8 或更早的固件）")
            return True

    def close(self):
        with self._lock:
            if self._ser:
                try: self._ser.close()
                except Exception: pass
            self._ser = None
            self._connected = False; self._handshaked = False

    @property
    def connected(self):
        return self._connected and self._handshaked

    def write(self, data):
        with self._lock:
            if not self._connected or not self._ser:
                return False
            try:
                view = memoryview(bytes(data))
                total = len(view)
                sent = 0
                t0 = time.monotonic()
                while sent < total:
                    n = self._ser.write(view[sent:]) or 0
                    if n <= 0:
                        # 短写：给驱动喘口气，别把半帧当成整帧发出去
                        if time.monotonic() - t0 > 3.0:
                            raise IOError("写入卡住（3 秒没有进展）")
                        time.sleep(0.001)
                        continue
                    sent += n
                self.bytes_written += total
                return True
            except Exception as e:
                log.warning("写入失败: %s", e)
                self._connected = False; self._handshaked = False
                return False

    def resync(self, timeout=0.5):
        """把设备从“等像素”拉回“等命令”。

        v17 固件里已经没有栅栏：设备 400 ms 没有进展就会自己作废当前段。
        这里只做两件事——把半路的像素计数喂到 0（补足 2048 字节上限），
        再把迟到的回执清掉，免得被下一帧当成“收齐了”。
        """
        with self._lock:
            if not self._connected or not self._ser:
                return False
            try:
                blob = END_FRAME * 420          # 2520 B，足够填掉任何 raw_left
                self._ser.write(blob)
                time.sleep(max(0.05, len(blob) / 100000.0))
                self._ser.reset_input_buffer()
                return True
            except Exception:
                self._connected = False; self._handshaked = False
                return False

    def wait_idle(self, timeout=0.8):
        """等到设备把上一帧吃完（v16 的 0x57 空闲探询）。

        设备把一帧的字节从接收环里消费完才会回到命令解析，所以能回这个
        包就说明“现在发触摸/心跳是安全的”。收不到就整帧重发，绝不能把
        命令字节塞进还没吃完的像素流里——那会被当成像素吃掉，整条流错位。
        """
        with self._lock:
            if not self._connected or not self._ser:
                return False
            t_end = time.monotonic() + max(timeout, 0.05)
            while time.monotonic() < t_end:
                try:
                    self._ser.reset_input_buffer()
                    self._ser.write(QUERY_IDLE)
                except Exception:
                    self._connected = False; self._handshaked = False
                    return False
                deadline = time.monotonic() + 0.06
                buf = bytearray()
                while time.monotonic() < deadline:
                    n = self._ser.in_waiting
                    if n:
                        buf.extend(self._ser.read(n))
                        if IDLE_REPLY in bytes(buf):
                            return True
                    time.sleep(0.002)
            return False

    def read_verdicts(self, count, timeout=0.4):
        """收 count 个裁决字节（0x06 好 / 0x15 坏），跳过握手噪声。"""
        with self._lock:
            if not self._connected or not self._ser:
                return False, 0
            got = 0
            bad = False
            buf = bytearray()
            t0 = time.monotonic()
            while got < count and time.monotonic() - t0 < timeout:
                n = self._ser.in_waiting
                if n:
                    buf.extend(self._ser.read(n))
                while buf:
                    b = buf[0]
                    del buf[0]
                    if b == 0x06:
                        got += 1
                    elif b == 0x15:
                        got += 1
                        bad = True
                if got < count:
                    time.sleep(0.003)
            return (got >= count and not bad), got

    def read_all(self):
        with self._lock:
            if not self._connected or not self._ser:
                return b""
            try:
                n = self._ser.in_waiting
                return self._ser.read(n) if n else b""
            except Exception:
                self._connected = False; self._handshaked = False
                return b""

    def query_version(self, timeout=0.4):
        """问固件版本；返回 (major, minor, touch) 或 None。

        新固件（v9 起）会在同一个回包里带上当前触摸读数，所以这一条日志
        就能同时回答“烧的是哪一版”和“ADC 有没有在产出”。
        """
        with self._lock:
            if not self._connected or not self._ser:
                return None
            try:
                self._ser.reset_input_buffer()
                self._ser.write(bytes([0x00, 0x56, 0, 0, 0, 0]))
                t0 = time.monotonic()
                buf = bytearray()
                while time.monotonic() - t0 < timeout:
                    n = self._ser.in_waiting
                    if n:
                        buf.extend(self._ser.read(n))
                    while len(buf) >= 6:
                        if buf[0] == 0x56 and buf[5] == 0xAA:
                            major, minor = buf[1], buf[2]
                            touch = (buf[3] << 8) | buf[4]
                            del buf[:6]
                            return major, minor, touch
                        del buf[0]
                    time.sleep(0.005)
            except Exception as e:
                log.debug("查询固件版本失败: %s", e)
            return None

    def read_status(self, timeout=0.12):
        """收流后设备回的 16 字节状态帧（0x55 ... 0xAA）。

        返回 dict（done/drop/px/win/aborts/ms/ovf）或 None。px 是本帧实际
        画进屏的像素数，win 是这次开流声明的窗口像素数 —— 两者不等就说明
        中间丢过字节，必须整帧重发。
        """
        with self._lock:
            if not self._connected or not self._ser:
                return None
            t0 = time.monotonic()
            buf = bytearray()
            while time.monotonic() - t0 < timeout:
                try:
                    n = self._ser.in_waiting
                    if n:
                        buf.extend(self._ser.read(n))
                except Exception:
                    self._connected = False; self._handshaked = False
                    return None
                while len(buf) >= 1:
                    if buf[0] != 0x55:
                        del buf[0]
                        continue
                    if len(buf) < 18:
                        break
                    if buf[17] != 0xAA:
                        del buf[0]
                        continue
                    st = {
                        "done": (buf[2] << 8) | buf[1],
                        "drop": (buf[4] << 8) | buf[3],
                        "px":   (buf[6] << 8) | buf[5],
                        "win":  (buf[8] << 8) | buf[7],
                        "aborts": (buf[10] << 8) | buf[9],
                        "ms":   (buf[12] << 8) | buf[11],
                        "ovf":  (buf[14] << 8) | buf[13],
                        "tmo":  (buf[16] << 8) | buf[15],
                    }
                    return st
                time.sleep(0.004)
            return None

    def read_adc(self, ch, timeout=0.05):
        with self._lock:
            if not self._connected or not self._ser:
                return None
            t0 = time.monotonic()
            buf = bytearray()
            while time.monotonic() - t0 < timeout:
                try:
                    n = self._ser.in_waiting
                    if n:
                        buf.extend(self._ser.read(n))
                except Exception:
                    self._connected = False; self._handshaked = False
                    return None
                while len(buf) >= 6:
                    if buf[0] == Cmd.ADC and buf[1] == ch:
                        val = (buf[4] << 8) | buf[5]
                        del buf[:6]
                        return val
                    del buf[0]
                time.sleep(0.002)
            return None

    def handshake(self, timeout=2.0):
        if self._handshaked:
            return True
        t0 = time.monotonic(); buf = bytearray()
        while time.monotonic() - t0 < timeout:
            chunk = self.read_all()
            if chunk:
                buf.extend(chunk)
                idx = buf.find(b"\x00OPM")
                if idx >= 0 and len(buf) >= idx + 6:
                    self.version = "02"
                    self.raw_mode = True
                    log.info("检测到 OPMSU 设备（整段传输）")
                    self.write(b"\x00OPEND")
                    time.sleep(0.05); self.read_all()
                    self._handshaked = True; return True
                idx = buf.find(b"\x00MSN")
                if idx >= 0 and len(buf) >= idx + 6:
                    # 老固件（存档点/proto25）：整段传输协议一样，只是没有
                    # 版本查询、没有裁决字节、ADC 恒 0。
                    self.version = chr(buf[idx + 4]) + chr(buf[idx + 5])
                    self.raw_mode = (self.version == "02")
                    log.info("检测到 MSN 设备, 版本=%s%s", self.version,
                             "（整段传输）" if self.raw_mode else "")
                    self.write(b"\x00MSNCN")
                    time.sleep(0.05); self.read_all()
                    self._handshaked = True; return True
            time.sleep(0.01)
        return False

class FrameEncoder:
    SEG = 256
    TILE = 16                   # 高清模式的差分块，16×16 像素 = 512 字节 RGB565
    TILE_FULL_THRESHOLD = 32    # 变化块超过这个数就直接整帧发

    def __init__(self):
        self._last_raw = None
        self._last_tile_raw = None
        self.last_full = False

    def reset(self):
        self._last_raw = None
        self._last_tile_raw = None

    @staticmethod
    def _to_rgb565(img):
        data = img.tobytes()
        if _np is not None:
            a = _np.frombuffer(data, dtype=_np.uint8).reshape(-1, 3)
            a = a.astype(_np.uint16)
            v = ((a[:, 0] >> 3) << 11) | ((a[:, 1] >> 2) << 5) | (a[:, 2] >> 3)
            return v.astype(">u2").tobytes()
        r = data[0::3]
        g = data[1::3]
        b = data[2::3]
        n = len(r)
        raw = bytearray(n * 2)
        for k in range(n):
            v = ((r[k] >> 3) << 11) | ((g[k] >> 2) << 5) | (b[k] >> 3)
            raw[2 * k] = v >> 8
            raw[2 * k + 1] = v & 0xFF
        return bytes(raw)

    def encode(self, img, raw_mode=False):
        if img.size != (SCREEN_W, SCREEN_H):
            img = img.resize((SCREEN_W, SCREEN_H))
        if img.mode != "RGB":
            img = img.convert("RGB")
        raw = self._to_rgb565(img)
        self.last_full = False
        if raw == self._last_raw:
            return b""
        self._last_raw = raw

        out = bytearray()
        out += MiniProtocol.lcd_set_xy(0, 0)
        out += MiniProtocol.lcd_set_size(SCREEN_W, SCREEN_H)
        out += MiniProtocol.lcd_enter_direct_mode()

        if raw_mode:
            # v23: one full-screen 0x09 per frame, device answers one verdict
            # byte and we only send the next frame after reading it. Tile deltas
            # leave a broken tile broken forever - that is why tearing stuck.
            self._last_tile_raw = None
            self.last_full = True
            return bytes(out + self._encode_full(raw))

        total = len(raw)
        n_segs = (total + 255) // 256
        for si in range(n_segs):
            seg = bytearray(raw[si * 256:(si + 1) * 256])
            if len(seg) < 256:
                seg += b"\xff" * (256 - len(seg))
            units = [bytes(seg[k:k + 4]) for k in range(0, 256, 4)]
            counts = {}
            for u in units:
                counts[u] = counts.get(u, 0) + 1
            fill = max(counts, key=counts.get)
            out += MiniProtocol.lcd_set_fill(*fill)
            if sum(1 for u in units if u != fill) > 44:
                for idx, u in enumerate(units):
                    out += MiniProtocol.lcd_data_block(idx, *u)
            else:
                for idx, u in enumerate(units):
                    if u != fill:
                        out += MiniProtocol.lcd_data_block(idx, *u)
            out += MiniProtocol.lcd_segment_end_full()
        return bytes(out)

    # ---- v2 分块增量 -------------------------------------------------
    #
    # 160x80 切成 16x16 的块，共 10x5 = 50 块，每块 512 字节。
    # 与上一帧逐块比较，只发变化的块；每块自带窗口，独立成一次流。
    # 变化块超过 32 个时退回整帧传输，避免重复的窗口开销反而更慢。
    # ---- 4 位索引色 --------------------------------------------------
    #
    # 一帧 160x80 = 12800 像素。RGB565 是 25600 字节，索引色只要 6400 字节，
    # 加上每帧 32 字节调色板，整帧报文从 25.6 KB 降到约 13 KB。
    # 调色板用 PIL 的自适应量化（中位切分），16 色；解码在固件里做。
    # 固定 16 色调色板：4 级灰 + 6 个基色 + 2 个深色。
    # 顺序与取值都不随画面变化，抖动图案因此逐帧稳定，静态区域不会闪烁。
    # 有序抖动的 Bayer 8×8 矩阵（0..1）
    def _encode_tiles(self, raw):
        T = self.TILE
        prev = self._last_tile_raw
        raw = bytes(raw)

        if prev is None or len(prev) != len(raw):
            self._last_tile_raw = raw
            self.last_full = True
            return self._encode_full(raw)

        tiles = []
        for ty in range(0, SCREEN_H, T):
            for tx in range(0, SCREEN_W, T):
                for r in range(ty, min(ty + T, SCREEN_H)):
                    off = (r * SCREEN_W + tx) * 2
                    if raw[off:off + T * 2] != prev[off:off + T * 2]:
                        tiles.append((tx, ty))
                        break

        self._last_tile_raw = raw
        if not tiles:
            return b""
        if len(tiles) > self.TILE_FULL_THRESHOLD:
            self.last_full = True
            return self._encode_full(raw)

        out = bytearray()
        for tx, ty in tiles:
            out += MiniProtocol.lcd_set_xy(tx, ty)
            out += MiniProtocol.lcd_set_size(tx + T, ty + T)
            out += MiniProtocol.lcd_enter_direct_mode()
            pre = bytearray()
            for r in range(ty, ty + T):
                off = (r * SCREEN_W + tx) * 2
                pre += raw[off:off + T * 2]
            out += lcd_segment_payload(bytes(pre))
            out += MiniProtocol.lcd_raw_end()
        return bytes(out)

    def _encode_full(self, raw):
        out = bytearray()
        for si in range(0, len(raw), 512):
            out += lcd_segment_payload(raw[si:si + 512])
        out += MiniProtocol.lcd_raw_end()
        return bytes(out)

def _normalize_port(port):
    if sys.platform == "win32":
        return str(port)
    p = str(port)
    if p.startswith("/"):
        return p
    if p.startswith(("tty", "cu.", "serial")):
        return "/dev/" + p
    return p

class MiniDevice:
    def __init__(self, port):
        port = _normalize_port(port)
        self.port = port
        self.transport = SerialTransport(port)
        self.encoder = FrameEncoder()
        self.screen_reversed = False

    def open(self):
        ok = self.transport.open()
        if ok:
            self.encoder.reset()
            self._verify_acks = None          # 重新连上就重新相信设备
            self._ack_fails = 0
            self._zero_acks = 0
            fwv = getattr(self.transport, "fw_version", None)
            if fwv is None:
                log.info("固件没有回版本号（proto25 或更早），按老固件处理")
            elif fwv < (13, 0):
                log.warning("固件版本 %d.%d 太旧，请烧 MSU2_open_v17.hex", fwv[0], fwv[1])
        return ok

    def close(self):  self.transport.close()

    @property
    def connected(self): return self.transport.connected

    def show_image(self, img):
        ok = self._show_image_once(img)
        if ok:
            return True
        # 设备报告缺像素（或整段作废）：缓存已清，立刻整帧补一次，
        # 用户看到的是“偶尔卡一下”，而不是半屏花掉的内容。
        log.debug("整帧校验失败，重发一次")
        return self._show_image_once(img)

    def _show_image_once(self, img):
        t0 = time.perf_counter()
        frame = self.encoder.encode(
            img, raw_mode=getattr(self.transport, "raw_mode", False))
        self.enc_ms = (time.perf_counter() - t0) * 1000.0
        if not frame:
            return True
        # 整帧一次写：分段写会让别的线程（触摸轮询）把请求字节插进帧中间，
        # 设备按“上一段的剩余字节数”消费，从此整条流错位。
        payload = frame
        # 按 8192 字节分块写（与存档点一致）：一旦某一块写不进去，
        # 立即停下来，不会出现“写了一半、剩下的在超时后才报错”。
        # Write in 2 KB pieces with a tiny gap: the device ring is 8 KB and its
        # consumer is the main loop, so never push a whole 25 KB frame in one
        # burst. This costs ~25 ms per frame and removes the overflow entirely.
        ok = True
        seg_write = 2048
        for i in range(0, len(payload), seg_write):
            if not self.transport.write(payload[i:i + seg_write]):
                ok = False
                break
            if i + seg_write < len(payload):
                time.sleep(0.002)
        if not ok:
            # 写失败/截断：设备可能停在“等像素”中间，此时再发任何命令都会被
            # 当作像素吃掉。用同步栅栏把它拉回命令模式，再整帧重发。
            self.encoder.reset()
            try:
                if getattr(self.transport, "connected", False):
                    self.transport.resync()
            except Exception:
                pass
            return False
        fw = getattr(self.transport, "fw_version", None)
        raw = bool(getattr(self.transport, "raw_mode", False))
        if raw and fw is not None and fw >= (23, 0):
            # Full frame written; wait for the device verdict byte. 0x06 = all
            # 12800 pixels arrived, 0x15 = short. We never send the next frame
            # before the ack, so the receive ring can never overflow.
            ok_ack, got = self.transport.read_verdicts(1, 1.5)
            if ok_ack:
                self.self_paced = True
                self.tx_ms = (time.perf_counter() - t0) * 1000.0 - self.enc_ms
                self.frame_bytes = len(frame)
                return True
            if got == 0:
                # No verdict byte at all: the device was too busy to answer in
                # time. That is not proof the frame was bad, so keep going
                # (the next frame is a full redraw anyway).
                self.self_paced = False
                self.tx_ms = (time.perf_counter() - t0) * 1000.0 - self.enc_ms
                self.frame_bytes = len(frame)
                self._quiet_frames = int(getattr(self, "_quiet_frames", 0)) + 1
                if self._quiet_frames in (3, 30):
                    log.warning("device answered no verdict for %d frames "
                                "(firmware without the queued verdict?)",
                                self._quiet_frames)
                if self._quiet_frames >= 8:
                    # The device stopped answering: it is either wedged or has
                    # just been reset by its own watchdog. Drop the link so the
                    # scheduler reconnects (that also re-runs the handshake).
                    log.error("device stopped answering; reconnecting")
                    self._quiet_frames = 0
                    try:
                        self.transport.close()
                    except Exception:
                        pass
                    self.encoder.reset()
                return True
            self._ack_fails = int(getattr(self, "_ack_fails", 0)) + 1
            if self._ack_fails in (1, 3, 10, 30):
                log.warning("device did not confirm the frame (attempt %d, got %d "
                            "verdict bytes), resending the full frame",
                            self._ack_fails, got)
            self.encoder.reset()
            try:
                self.transport.resync()
            except Exception:
                pass
            return False
        # 裁决字节默认关闭：v13~v19 的事实是，任何往像素流附近加的东西
        # （栅栏前缀、空闲探询、逐帧回执）都会引入新的错帧，而它对可见
        # 故障的贡献是零。需要定位问题时再设 MSU2_VERDICT=1 打开。
        if os.environ.get("MSU2_VERDICT", "0") != "1":
            fw = None
        if (not raw) or fw is None or fw < (13, 0):
            # 老固件（proto25 及更早）不回裁决字节，只能靠发送间隔兜底。
            self.self_paced = False
            self.transport.read_all()
            self.tx_ms = (time.perf_counter() - t0) * 1000.0 - self.enc_ms
            self.frame_bytes = len(frame)
            return True
        # v13 起，设备每收完一个收流命令就回一个裁决字节（0x06 收齐 /
        # 0x15 丢了）。读到最后一个就等于“这一帧已经整个进设备了”。
        # 关键：这是唯一不往像素流里插字节的同步方式。以前那套“栅栏前缀”
        # 和“0x57 空闲探询”都会在设备还没吃完时把上一帧打断/把命令塞进
        # 像素里，正是花屏的来源，已经全部删掉。
        want = frame.count(END_FRAME)
        ok_ack, got = (True, want) if want == 0 else self.transport.read_verdicts(want, 0.6)
        if not ok_ack:
            self._ack_fails = int(getattr(self, "_ack_fails", 0)) + 1
            if self._ack_fails in (1, 3, 10, 30):
                log.warning("这一帧设备没收全（%d/%d 个回执），重发", got, want)
            self.encoder.reset()
            self._recover()
            return False
        self._ack_fails = 0
        self.self_paced = True
        self.tx_ms = (time.perf_counter() - t0) * 1000.0 - self.enc_ms
        self.frame_bytes = len(frame)
        return True

    def _recover(self):
        """设备可能停在“等像素”里：等它自己的 400 ms 看门狗作废该段，
        再把像素计数喂到 0。"""
        try:
            if not getattr(self.transport, "connected", False):
                return
            time.sleep(0.45)
            self.transport.resync()
        except Exception:
            pass

    def set_reverse(self, reverse):
        self.screen_reversed = bool(reverse)

    def heartbeat(self):
        ok = self.transport.write(MiniProtocol.read_adc(15))
        time.sleep(0.01)
        self.transport.read_all()
        return ok

    def read_touch(self):
        if not self.transport.write(MiniProtocol.read_adc(TOUCH_CH)):
            return None
        return self.transport.read_adc(TOUCH_CH, timeout=0.008)

class Background:
    MAX_FRAMES = 300
    MODES = ("fit", "fill", "stretch")
    PLAYLIST_SECS = 12.0

    def __init__(self):
        self.path = ""
        self.mode = "fit"
        self.dim = 0
        self.blur = 0
        self.raw = []
        self.frames = []
        self.durations = []
        self.groups = []
        self.group_idx = 0
        self.group_ts = None
        self.idx = 0
        self.t_last = None

    def configure(self, dim=0, blur=0):
        self.dim = 0            # 暗化搬到全局显示设置，背景不再压暗
        self.blur = _coerce_int(blur, 0, 0, 5)
        self.frames = [self._decorate(f) for f in self.raw]

    def _decorate(self, img):
        if not self.raw:
            return img
        out = img
        if self.blur > 0:
            try:
                from PIL import ImageFilter
                out = out.filter(ImageFilter.GaussianBlur(self.blur))
            except Exception:
                pass
        return out

    def load_playlist(self, paths, mode=None, interval=None, shuffle=None):
        if mode is not None:
            self.mode = mode if mode in self.MODES else "fit"
        self.path = ";".join(paths)
        self.pl_interval = float(interval or self.PLAYLIST_SECS)
        self.shuffle = bool(shuffle)
        self.groups = []
        self.raw = []
        self.frames = []
        self.durations = []
        self.idx = 0
        self.t_last = None
        self.group_idx = 0
        self.group_ts = None
        for i, path in enumerate(paths[:32]):
            if not path:
                continue
            self.raw = []
            self.durations = []
            self._read_path(Path(path))
            if self.raw:
                self.groups.append((list(self.raw), list(self.durations)))
        self.raw = []
        self.durations = []
        if self.groups:
            self.raw, self.durations = list(self.groups[0][0]), list(self.groups[0][1])
            self.frames = [self._decorate(f) for f in self.raw]
        log.info("背景轮播已加载: %d 组 (%s, 模式 %s)", len(self.groups),
                 self.path, self.mode)

    def _read_path(self, p):
        if not p.exists():
            log.warning("背景图不存在: %s", p)
            return
        if p.is_dir():
            try:
                files = sorted(x for x in p.iterdir()
                               if x.is_file() and x.suffix.lower() in IMAGE_EXTS)
            except Exception as e:
                log.warning("扫描图片目录失败: %s", e)
                return
            for f in files[:self.MAX_FRAMES]:
                try:
                    with Image.open(f) as src:
                        self.raw.append(self._frame(src.convert("RGB")))
                    self.durations.append(0.2)
                except Exception as e:
                    log.warning("跳过图片 %s: %s", f.name, e)
            return
        try:
            with Image.open(p) as src:
                if getattr(src, "is_animated", False):
                    for i in range(min(src.n_frames, self.MAX_FRAMES)):
                        src.seek(i)
                        self.raw.append(self._frame(src.convert("RGB")))
                        self.durations.append(
                            max(0.05, src.info.get("duration", 100) / 1000.0))
                else:
                    self.raw.append(self._frame(src.convert("RGB")))
                    self.durations.append(0.0)
        except Exception as e:
            log.warning("背景图处理失败: %s", e)
            self.raw = []
            self.durations = []

    def load(self, path, mode=None):
        self.path = path
        if mode is not None:
            self.mode = mode if mode in self.MODES else "fit"
        self.groups = []
        self.raw = []
        self.frames = []
        self.durations = []
        self.idx = 0
        self.t_last = None
        self.group_ts = None
        if not path:
            return
        self._read_path(Path(path))
        self.frames = [self._decorate(f) for f in self.raw]
        log.info("背景图已加载: %s (%d 帧, 模式 %s)", path, len(self.frames), self.mode)

    def _frame(self, img):
        tw, th = SCREEN_W, SCREEN_H
        mode = self.mode if self.mode in self.MODES else "fit"
        try:
            if mode == "stretch":
                return img.resize((tw, th), Image.LANCZOS)
            scale = (max(tw / img.width, th / img.height) if mode == "fill"
                     else min(tw / img.width, th / img.height))
            nw = max(1, int(img.width * scale))
            nh = max(1, int(img.height * scale))
            img = img.resize((nw, nh), Image.LANCZOS)
            if mode == "fill":
                x = max(0, (nw - tw) // 2)
                y = max(0, (nh - th) // 2)
                return img.crop((x, y, x + tw, y + th))
            canvas = Image.new("RGB", (tw, th), "black")
            canvas.paste(img, ((tw - nw) // 2, (th - nh) // 2))
            return canvas
        except Exception as e:
            log.warning("背景变换失败: %s", e)
            return Image.new("RGB", (tw, th), "black")

    def tick(self, now):
        if len(self.groups) > 1:
            if self.group_ts is None:
                self.group_ts = now
            elif now - self.group_ts >= self.pl_interval:
                self.group_ts = now
                if self.shuffle and len(self.groups) > 1:
                    nxt = self.group_idx
                    while nxt == self.group_idx:
                        nxt = random.randrange(len(self.groups))
                    self.group_idx = nxt
                else:
                    self.group_idx = (self.group_idx + 1) % len(self.groups)
                self.raw, self.durations = list(self.groups[self.group_idx][0]), list(self.groups[self.group_idx][1])
                self.frames = [self._decorate(f) for f in self.raw]
                self.idx = 0
                self.t_last = None
                log.debug("背景切换到第 %d 组", self.group_idx + 1)
        if not self.frames:
            return None
        if len(self.frames) == 1:
            return self.frames[0]
        if self.t_last is None:
            self.t_last = now
            return self.frames[0]
        if now - self.t_last >= self.durations[self.idx]:
            self.idx = (self.idx + 1) % len(self.frames)
            self.t_last = now
        return self.frames[self.idx]

def _act_screenshot():
    try:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = Path(SCREENSHOT_DIR)
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / f"shot_{ts}.png"
        try:
            from PIL import ImageGrab
            img = ImageGrab.grab()
            img.save(p)
            log.info("截屏已保存: %s", p)
            return True
        except Exception:
            pass
        if sys.platform == "darwin":
            if shutil.which("screencapture"):
                subprocess.run(["screencapture", str(p)], check=False)
                return p.exists()
        if sys.platform.startswith("linux"):
            for cmd in (["gnome-screenshot", "-f", str(p)],
                        ["scrot", str(p)],
                        ["import", "-window", "root", str(p)]):
                if shutil.which(cmd[0]):
                    subprocess.run(cmd, check=False)
                    if p.exists():
                        log.info("截屏已保存: %s", p)
                        return True
    except Exception as e:
        log.warning("截屏失败: %s", e)
    return False

def _act_mute_toggle():
    try:
        if sys.platform == "win32":
            import ctypes
            VK_VOLUME_MUTE = 0xAD
            KEYEVENTF_KEYUP = 0x0002
            ctypes.windll.user32.keybd_event(VK_VOLUME_MUTE, 0, 0, 0)
            ctypes.windll.user32.keybd_event(VK_VOLUME_MUTE, 0, KEYEVENTF_KEYUP, 0)
            return True
        if sys.platform == "darwin":
            subprocess.run(
                ["osascript", "-e",
                 "set volume output muted not (output muted of (get volume settings))"],
                check=False, timeout=2)
            return True
        if sys.platform.startswith("linux"):
            for cmd in (["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"],
                        ["amixer", "-q", "set", "Master", "toggle"]):
                if shutil.which(cmd[0]):
                    subprocess.run(cmd, check=False, timeout=2)
                    return True
            log.warning("未找到 pactl 或 amixer，无法切换静音")
            return False
    except Exception as e:
        log.debug("静音切换失败: %s", e)
    return False

def _act_send_macro(combo):
    combo = (combo or "").strip()
    if not combo:
        return False
    try:
        from pynput.keyboard import Controller, Key
    except ImportError:
        log.warning("未安装 pynput，无法发送键盘宏")
        return False

    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if not parts:
        return False

    key_map = {
        "ctrl": Key.ctrl, "control": Key.ctrl,
        "shift": Key.shift, "alt": Key.alt,
        "win": Key.cmd, "cmd": Key.cmd, "super": Key.cmd,
    }
    special = {
        "enter": Key.enter, "return": Key.enter,
        "esc": Key.esc, "escape": Key.esc,
        "tab": Key.tab, "space": Key.space,
        "backspace": Key.backspace, "delete": Key.delete,
        "up": Key.up, "down": Key.down, "left": Key.left, "right": Key.right,
        "home": Key.home, "end": Key.end,
        "pageup": Key.page_up, "pagedown": Key.page_down,
    }

    mods = []
    main_key = None
    for p in parts:
        if p in key_map:
            mods.append(key_map[p])
        else:
            main_key = p

    ctrl = Controller()
    pressed = []
    try:
        for m in mods:
            ctrl.press(m)
            pressed.append(m)
        if main_key:
            k = None
            if len(main_key) == 1:
                k = main_key
            elif main_key in special:
                k = special[main_key]
            elif main_key.startswith("f") and main_key[1:].isdigit():
                k = getattr(Key, main_key, None)
            if k is not None:
                ctrl.press(k); ctrl.release(k)
        log.info("宏已发送: %s", combo)
        return True
    except Exception as e:
        log.warning("宏发送失败: %s", e)
        return False
    finally:
        for m in reversed(pressed):
            try: ctrl.release(m)
            except Exception: pass

def _act_launch_program(cmd):
    cmd = (cmd or "").strip()
    if not cmd:
        log.warning("启动自定义程序：路径为空")
        return False
    try:
        if sys.platform == "win32":
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            subprocess.Popen(
                cmd, shell=True, close_fds=True,
                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            )
        else:
            subprocess.Popen(
                cmd, shell=True, close_fds=True,
                start_new_session=True,
            )
        log.info("已启动程序: %s", cmd)
        return True
    except Exception as e:
        log.warning("启动程序失败: %s", e)
        return False

def _coerce_int(value, default, lo=None, hi=None):
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = default
    if lo is not None and v < lo:
        v = lo
    if hi is not None and v > hi:
        v = hi
    return v

def _widget_watch_dirs():
    dirs = []
    try:
        base = getattr(_widgets_pkg, "_pkg_path", None)
        if base is not None:
            dirs.append(Path(base))
        for item in list(getattr(_widgets_pkg, "_ext_dirs", []) or []):
            dirs.append(Path(item))
    except Exception:
        pass
    return dirs

def _build_overlay(text, orientation):
    w, h = (PORTRAIT_SIZE if orientation == PORTRAIT else LANDSCAPE_SIZE)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = 6
    font = get_font(12, bold=True, cjk=True)
    max_w = w - pad * 2 - 12
    lines = []
    cur = ""
    for ch in text:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        if cur and d.textlength(cur + ch, font=font) > max_w:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    lines = lines[:6]
    line_h = 16
    box_h = len(lines) * line_h + 12
    box_y = max(3, (h - box_h) // 2)
    d.rounded_rectangle([pad, box_y, w - pad, box_y + box_h], radius=6,
                        fill=(18, 22, 30, 235), outline=(90, 120, 200, 255))
    y = box_y + 6
    for line in lines:
        d.text((pad + 6, y), line, font=font, fill=(240, 244, 255, 255))
        y += line_h
    return img

class Scheduler(threading.Thread):
    def __init__(self, device, config, on_config_changed=None, on_shutdown=None,
                 shared=None):
        super().__init__(daemon=True, name="Scheduler")
        self.device = device
        self.config = config
        self.shared = shared if isinstance(shared, dict) else {}
        self._on_config_changed = on_config_changed
        self._on_shutdown = on_shutdown
        self._save_timer = None
        self._save_timer_lock = threading.Lock()

        self.orientation = config.get("orientation", LANDSCAPE)
        self.reverse = bool(config.get("reverse", False))
        self.portrait_rotate = _coerce_int(config.get("portrait_rotate", -90), -90)
        disp = config.get("display")
        if not isinstance(disp, dict):
            disp = {}
        self._dim = _coerce_int(disp.get("dim"), 0, 0, 90)

        bg = config.get("background")
        if not isinstance(bg, dict):
            bg = {}
        self._bg_enabled = bool(bg.get("enabled", False))
        self._bg_path = str(bg.get("path", "") or "")
        self._bg_paths = [p for p in (bg.get("paths") or []) if isinstance(p, str)]
        self._bg_source = bg.get("source") if bg.get("source") in ("single", "playlist") else "single"
        self._bg_interval = float(_coerce_int(bg.get("interval"), 12, 2, 3600))
        self._bg_shuffle = bool(bg.get("shuffle"))
        self._bg_mode = bg.get("mode")
        if self._bg_mode not in Background.MODES:
            self._bg_mode = "fit"
        self._bg_dim = 0          # 暗化已独立成全局设置，这里保留字段只为兼容旧配置
        self._bg_blur = _coerce_int(bg.get("blur"), 0, 0, 5)
        self.background = Background()
        self.background.configure(self._bg_dim, self._bg_blur)
        if self._bg_enabled:
            if self._bg_paths:
                self.background.load_playlist(self._bg_paths, self._bg_mode)
            elif self._bg_path:
                self.background.load(self._bg_path, self._bg_mode)

        self._overlay = None
        self._overlay_until = 0.0
        self._overlay_seen = None
        self._frames_window = collections.deque()
        self.metrics = {"frames": 0, "bytes": 0, "reconnects": 0,
                        "render_ms": {}, "touch": 0}
        self._watch_mtime = 0.0
        self._watch_ts = 0.0

        self._touch_baseline = None
        self._touch_last_sample_ts = 0.0
        self._touch = TouchGestures(
            threshold=TOUCH_DEFAULT_THRESHOLD,
            long_press=TOUCH_LONG_PRESS,
            double_window=TOUCH_DOUBLE_WINDOW,
            min_gap=TOUCH_MIN_TAP_GAP,
            release_ratio=TOUCH_RELEASE_RATIO,
            debounce=TOUCH_DEBOUNCE,
            release_debounce=TOUCH_RELEASE_DEBOUNCE,
            base_track=TOUCH_BASE_TRACK,
        )
        self._locked = False
        self._load_touch_cfg(config)

        self.ctx = RenderContext(
            device=device, now=time.time(),
            orientation=self.orientation, config=self._cfg_view(),
        )
        self._ctxs = {}
        self._last_entered = None
        self._last_imgs = {}
        self._force_send = False
        self._send_gap = 0.0
        self._last_frame_mono = 0.0
        self._stop = threading.Event()
        self._switch = threading.Event()
        self._ops = collections.deque()
        self._ops_lock = threading.Lock()
        self._widgets = []
        self._current = 0
        self._cooldown_until = 0.0
        self._last_send_mono = 0.0

        self._reload_widgets()

    def _load_touch_cfg(self, config):
        tk = config.get("touch")
        if not isinstance(tk, dict):
            tk = {}
        self._touch_cfg = {
            "single": tk.get("single", "next_page"),
            "double": tk.get("double", "toggle_orientation"),
            "long":   tk.get("long",   "toggle_reverse"),
            "macro_single": str(tk.get("macro_single", "") or ""),
            "macro_double": str(tk.get("macro_double", "") or ""),
            "macro_long":   str(tk.get("macro_long", "") or ""),
            "program_single": str(tk.get("program_single", "") or ""),
            "program_double": str(tk.get("program_double", "") or ""),
            "program_long":   str(tk.get("program_long", "") or ""),
            "threshold": _coerce_int(tk.get("threshold", TOUCH_DEFAULT_THRESHOLD),
                                     TOUCH_DEFAULT_THRESHOLD, 5, 2000),
        }
        self._touch_baseline = None
        self._touch_last_sample_ts = 0.0
        self._touch = TouchGestures(
            threshold=self._touch_cfg["threshold"],
            long_press=TOUCH_LONG_PRESS,
            double_window=TOUCH_DOUBLE_WINDOW,
            min_gap=TOUCH_MIN_TAP_GAP,
            release_ratio=TOUCH_RELEASE_RATIO,
            debounce=TOUCH_DEBOUNCE,
            release_debounce=TOUCH_RELEASE_DEBOUNCE,
            base_track=TOUCH_BASE_TRACK,
        )

    def set_touch_config(self, new_cfg):
        if not isinstance(new_cfg, dict):
            new_cfg = {}
        tk = self.config.get("touch")
        if not isinstance(tk, dict):
            tk = {}
            self.config["touch"] = tk
        for k in ("single", "double", "long",
                  "macro_single", "macro_double", "macro_long",
                  "program_single", "program_double", "program_long"):
            if k not in new_cfg:
                continue
            v = new_cfg[k]
            if v is None:
                v = ""
            elif not isinstance(v, str):
                v = str(v)
            tk[k] = v[:512].strip()
            if k in ("single", "double", "long") and tk[k] not in _TOUCH_ACTIONS:
                tk[k] = "passthrough"
        if "threshold" in new_cfg:
            tk["threshold"] = _coerce_int(new_cfg["threshold"],
                                          TOUCH_DEFAULT_THRESHOLD, 5, 2000)
        self._load_touch_cfg(self.config)
        log.info("触摸配置已更新: %s", self._touch_cfg)

    def _allow(self, name="操作", from_touch=False):
        if not from_touch:
            self._cooldown_until = time.monotonic() + MANUAL_COOLDOWN
            return True
        now = time.monotonic()
        if now < self._cooldown_until:
            log.info("%s 被冷却拒绝 (还剩 %.2fs)",
                     name, self._cooldown_until - now)
            return False
        self._cooldown_until = now + MANUAL_COOLDOWN
        return True

    def _cfg_view(self):
        view = dict(self.shared) if self.shared else {}
        view.update(self.config)
        if self.shared:
            view["widget_config"] = self.shared.get("widget_config") or {}
            view["gif_dir"] = self.shared.get("gif_dir") or ""
            view["font"] = self.shared.get("font") or ""
        return view

    def _ctx_for(self, w):
        ctx = self._ctxs.get(w.name)
        if ctx is None:
            ctx = RenderContext(device=self.device, now=time.time(),
                                orientation=self.orientation,
                                state={}, config=self._cfg_view())
            self._ctxs[w.name] = ctx
        return ctx

    def post_op(self, op, *args):
        with self._ops_lock:
            self._ops.append((op, args))

    def _drain_ops(self):
        while True:
            with self._ops_lock:
                if not self._ops:
                    return
                op, args = self._ops.popleft()
            try:
                if op == "reload":
                    self._reload_widgets()
                elif op == "restart":
                    self._restart_widget(*args)
                elif op == "background":
                    self._apply_background()
            except Exception as e:
                log.warning("后台任务 %s 失败: %s", op, e)

    def _teardown_all(self):
        for w in list(self._widgets):
            try: w.teardown(self._ctx_for(w))
            except Exception as e:
                log.warning("插件 %s teardown 失败: %s", w.name, e)
        self._ctxs.clear()
        self._last_imgs.clear()
        self._last_entered = None

    def _reload_widgets(self):
        self._teardown_all()

        names = []
        for name in self.config.get("pages") or []:
            if name in names:
                log.warning("重复的页面已忽略: %s", name)
                continue
            names.append(name)

        new_widgets = []
        for name in names:
            cls = WIDGET_REGISTRY.get(name)
            if not cls:
                log.warning("未知插件: %s", name); continue
            w = cls()
            try: w.setup(self._ctx_for(w))
            except Exception as e:
                log.warning("插件 %s setup 失败: %s", name, e)
            new_widgets.append(w)

        saved = self.config.get("current_page", "")
        new_current = 0
        if saved:
            for i, w in enumerate(new_widgets):
                if w.name == saved:
                    new_current = i
                    break
        new_current = max(0, min(new_current, max(0, len(new_widgets) - 1)))

        self._widgets = new_widgets
        self._current = new_current
        self._switch.set()

        log.info("已加载 %d 个插件: %s (当前: %s)",
                 len(self._widgets),
                 [w.name for w in self._widgets],
                 self._widgets[self._current].name if self._widgets else "-")
        self._ensure_supported()

    def _save_config(self, delay=0.0):
        """写配置。拖滑块这类高频调用传 delay>0，合并成一次落盘。"""
        if delay <= 0.0:
            self._save_flush()
            return
        with self._save_timer_lock:
            if self._save_timer is not None:
                self._save_timer.cancel()
            t = threading.Timer(delay, self._save_flush)
            t.daemon = True
            self._save_timer = t
            t.start()

    def _save_flush(self):
        with self._save_timer_lock:
            self._save_timer = None
        if self._on_config_changed:
            try: self._on_config_changed()
            except Exception as e:
                log.warning("保存配置失败: %s", e)

    def _persist_current(self):
        if not self._widgets:
            return
        name = self._widgets[self._current].name
        if self.config.get("current_page") != name:
            self.config["current_page"] = name
            self._save_config()

    def find_widget(self, name):
        for w in self._widgets:
            if w.name == name:
                return w
        return None

    def _ensure_supported(self):
        if not self._widgets:
            return False
        if self.orientation in self._widgets[self._current].orientations:
            return True
        if not self._find_supported(self._current, +1):
            log.warning("没有插件支持 %s 朝向", self.orientation)
            return False
        return True

    def _find_supported(self, start, direction):
        n = len(self._widgets)
        if n == 0:
            return False
        for i in range(n):
            idx = (start + i * direction) % n
            if self.orientation in self._widgets[idx].orientations:
                if idx != self._current:
                    self._current = idx
                    self._persist_current()
                return True
        return False

    def reload(self):
        self.post_op("reload")

    def next_page(self, from_touch=False):
        if from_touch and self._locked:
            log.info("已锁定，忽略触摸翻页")
            return False
        if not self._allow("next_page", from_touch): return False
        if not self._widgets: return False
        self._find_supported(self._current + 1, +1)
        self._switch.set()
        log.info("切页 -> %s", self._widgets[self._current].name)
        return True

    def prev_page(self, from_touch=False):
        if from_touch and self._locked:
            log.info("已锁定，忽略触摸翻页")
            return False
        if not self._allow("prev_page", from_touch): return False
        if not self._widgets: return False
        self._find_supported(self._current - 1, -1)
        self._switch.set()
        log.info("切页 -> %s", self._widgets[self._current].name)
        return True

    def toggle_reverse(self, from_touch=False):
        if not self._allow("toggle_reverse", from_touch): return False
        self.reverse = not self.reverse
        self.device.set_reverse(self.reverse)
        self.config["reverse"] = self.reverse
        self._save_config()
        self._switch.set()
        log.info("翻转 -> %s", self.reverse)
        return True

    def toggle_orientation(self, from_touch=False):
        if not self._allow("toggle_orientation", from_touch): return False
        self.orientation = (PORTRAIT if self.orientation == LANDSCAPE
                            else LANDSCAPE)
        self.ctx.orientation = self.orientation
        self.config["orientation"] = self.orientation
        self._save_config()
        self._ensure_supported()
        self._switch.set()
        log.info("朝向 -> %s", self.orientation)
        return True

    def set_dim(self, value):
        """整屏暗化 0-90：作用在最终合成帧上，等于一个假亮度旋钮。"""
        v = _coerce_int(value, self._dim, 0, 90)
        if v == self._dim:
            return True
        self._dim = v
        self.config.setdefault("display", {})["dim"] = v
        self._save_config(delay=0.8)     # 拖动时每格都来一次，合并写盘
        self._force_send = True
        log.info("暗化 -> %d%%", v)
        return True

    def toggle_lock(self, from_touch=False):
        self._locked = not self._locked
        log.info("锁定 -> %s", self._locked)
        return True

    def set_pages(self, page_names):
        seen = set()
        pages = []
        for name in page_names:
            if not isinstance(name, str) or name in seen:
                continue
            if name not in WIDGET_REGISTRY:
                continue
            seen.add(name)
            pages.append(name)
        self.config["pages"] = pages
        self.post_op("reload")
        log.info("页面已更新: %s", pages)
        return pages

    def set_background(self, enabled, path="", mode=None, paths=None, dim=None,
                       blur=None, source=None, interval=None, shuffle=None):
        self._bg_enabled = bool(enabled)
        self._bg_path = str(path or "")
        if paths is not None:
            self._bg_paths = [p for p in paths if isinstance(p, str)][:32]
        if mode is not None:
            self._bg_mode = mode if mode in Background.MODES else "fit"
        if dim is not None:
            self._bg_dim = _coerce_int(dim, self._bg_dim, 0, 90)
        if blur is not None:
            self._bg_blur = _coerce_int(blur, self._bg_blur, 0, 5)
        if source is not None:
            self._bg_source = source if source in ("single", "playlist") else "single"
        if interval is not None:
            self._bg_interval = float(_coerce_int(interval, int(self._bg_interval), 2, 3600))
        if shuffle is not None:
            self._bg_shuffle = bool(shuffle)
        self.post_op("background")
        self._save_config(delay=0.8)     # 拖模糊/暗化时合并写盘
        return True

    def _apply_background(self):
        # 拖模糊/暗化时只有修饰变了，图不用重新解码：按关键字段判断
        key = (self._bg_enabled, self._bg_source, self._bg_path,
               tuple(self._bg_paths), self._bg_mode,
               self._bg_interval, self._bg_shuffle)
        same = key == getattr(self, "_bg_loaded_key", None)
        self.background.configure(self._bg_dim, self._bg_blur)
        if self._bg_enabled and self._bg_source == "playlist" and self._bg_paths:
            if not same or not self.background.groups:
                self.background.load_playlist(self._bg_paths, self._bg_mode,
                                              self._bg_interval, self._bg_shuffle)
        elif self._bg_enabled and self._bg_path:
            if not same or not self.background.raw:
                self.background.load(self._bg_path, self._bg_mode)
        elif not same:
            self.background.load("", self._bg_mode)
        self._bg_loaded_key = key
        self._switch.set()
        log.info("背景 -> enabled=%s mode=%s dim=%d blur=%d path=%s",
                 self._bg_enabled, self._bg_mode, self._bg_dim, self._bg_blur,
                 self._bg_path)

    def notify(self, text, secs=5):
        text = str(text or "").strip()
        if not text:
            return False
        try:
            secs = _coerce_int(secs, 5, 1, 60)
            img = _build_overlay(text, self.orientation)
        except Exception as e:
            log.warning("通知生成失败: %s", e)
            return False
        self._overlay = img
        self._overlay_until = time.monotonic() + secs
        self._overlay_seen = None
        self._force_send = True
        log.info("通知: %s (%ds)", text, secs)
        return True

    def _count_frame(self, mono):
        self.metrics["frames"] = int(self.metrics.get("frames", 0)) + 1
        # 链路预算：按上一帧的实际传输时间给下一帧留出间隔。高清整帧有 25 KB，
        # 每秒能发的帧数是有上限的；连续排帧会把数据压在设备侧，帧中间断流
        # 就表现为花屏、错位（尤其是动画背景图这种每帧全屏都变的场景）。
        # v16 起 show_image 自己会等设备回空闲，链路已经不会堆积，再加间隔
        # 只会白白拖低帧率。
        if getattr(self.device, "self_paced", False):
            self._send_gap = 0.0
            self._last_frame_mono = mono
        else:
            try:
                tx = float(getattr(self.device, "tx_ms", 0.0) or 0.0)
            except Exception:
                tx = 0.0
            if tx > 0.0:
                self._send_gap = min(0.6, tx / 1000.0 * 1.15)
            self._last_frame_mono = mono
        total = 0
        try:
            total = int(getattr(self.device.transport, "bytes_written", 0))
        except Exception:
            total = 0
        last = int(self.metrics.get("_bytes_last", 0))
        delta = total - last if total >= last else 0
        self.metrics["_bytes_last"] = total
        self.metrics["bytes"] = int(self.metrics.get("bytes", 0)) + delta
        self._frames_window.append((mono, delta))
    def _watch_widgets(self, mono):
        if mono - self._watch_ts < 1.5:
            return
        self._watch_ts = mono
        stamp = 0.0
        for d in _widget_watch_dirs():
            try:
                entries = list(d.iterdir())
            except Exception:
                continue
            for f in entries:
                try:
                    if f.is_dir():
                        target = f / "__init__.py"
                        if not target.exists():
                            continue
                    elif f.suffix == ".py":
                        target = f
                    else:
                        continue
                    m = target.stat().st_mtime
                except Exception:
                    continue
                if m > stamp:
                    stamp = m
        if self._watch_mtime == 0.0:
            self._watch_mtime = stamp
            return
        if stamp > self._watch_mtime:
            self._watch_mtime = stamp
            log.info("检测到插件文件变化，正在重新加载")
            self.post_op("reload")

    def metrics_snapshot(self):
        now = time.monotonic()
        while self._frames_window and now - self._frames_window[0][0] > 10.0:
            self._frames_window.popleft()
        span = 0.0
        if self._frames_window:
            span = max(0.001, now - self._frames_window[0][0])
        fps = len(self._frames_window) / span if span > 0.001 else 0.0
        bps = sum(b for _, b in self._frames_window) / span if span > 0.001 else 0.0
        render = dict(self.metrics.get("render_ms") or {})
        top = sorted(render.items(), key=lambda kv: kv[1], reverse=True)[:6]
        return {
            "fps": round(fps, 2),
            "bytes_per_sec": int(bps),
            "frames": int(self.metrics.get("frames", 0)),
            "bytes": int(self.metrics.get("bytes", 0)),
            "reconnects": int(self.metrics.get("reconnects", 0)),
            "touch": int(self.metrics.get("touch", 0)),
            "render_ms": {k: round(v, 2) for k, v in top},
            "enc_ms": round(getattr(self.device, "enc_ms", 0.0), 2),
            "tx_ms": round(getattr(self.device, "tx_ms", 0.0), 2),
            "frame_bytes": int(getattr(self.device, "frame_bytes", 0)),
            "composite_ms": round(self.metrics.get("comp_ms", 0.0), 2),
            "background": {"enabled": self._bg_enabled, "mode": self._bg_mode,
                           "dim": self._bg_dim, "blur": self._bg_blur,
                           "frames": len(self.background.frames)},
        }

    def switch_to(self, name):
        for i, w in enumerate(self._widgets):
            if w.name == name:
                if i != self._current:
                    self._current = i
                    self._persist_current()
                    self._switch.set()
                    log.info("切页 -> %s", name)
                return True
        return False

    def restart_widget(self, name):
        self.post_op("restart", name)
        return True

    def _restart_widget(self, name):
        w = self.find_widget(name)
        if not w:
            log.warning("重启失败，未找到插件: %s", name)
            return False
        try: w.teardown(self._ctx_for(w))
        except Exception: pass
        self._ctxs.pop(name, None)
        self._last_imgs.pop(name, None)
        try:
            w.setup(self._ctx_for(w))
            self._switch.set()
            return True
        except Exception as e:
            log.warning("插件 %s 重启失败: %s", name, e)
            return False

    def stop(self):
        self._stop.set()
        self._save_flush()      # 退出前把还没落盘的改动补上

    def _poll_touch(self, now):
        v = self.device.read_touch()
        if v is None:
            self._touch_zero_streak = 0
            return

        # 设备端的转换是异步的，开机头一两拍可能还没有结果（返回 0）；
        # 如果一直只拿到 0，说明 ADC 根本没在转换，这里明确报出来而不是静默失效。
        if v <= 0:
            self._touch_zero_streak = getattr(self, "_touch_zero_streak", 0) + 1
            if self._touch_zero_streak == 25:
                log.warning("触摸通道读数一直为 0：设备 ADC 没有产出结果"
                            "（确认固件是 MSU2_open_v8 或更新）")
            return
        self._touch_zero_streak = 0

        had_base = self._touch.baseline is not None
        events = self._touch.feed(v, now)
        self._touch_baseline = self._touch.baseline
        if not had_base and self._touch.baseline is not None:
            log.info("触摸基准 = %d", int(self._touch.baseline))
        if self._touch.pressed:
            log.debug("touch adc=%d base=%d thr=%d pressed",
                      v, int(self._touch.baseline), self._touch_cfg["threshold"])

        for gesture in events:
            log.info("触摸: %s", {"single": "单击", "double": "双击",
                                 "long": "长按"}.get(gesture, gesture))
            self._dispatch_touch(gesture)

    def _dispatch_touch(self, gesture):
        action = self._touch_cfg.get(gesture, "passthrough")
        if action not in _TOUCH_ACTIONS:
            action = "passthrough"
        macro = self._touch_cfg.get(f"macro_{gesture}", "")
        program = self._touch_cfg.get(f"program_{gesture}", "")
        if action == "passthrough":
            w = self._widgets[self._current] if self._widgets else None
            if w is None:
                return
            try:
                r = w.on_action(f"touch_{gesture}", {}, self._ctx_for(w))
                if isinstance(r, dict) and r.get("ok"):
                    self._switch.set()
            except Exception as e:
                log.warning("插件 %s touch_%s 失败: %s", w.name, gesture, e)
            return
        try:
            self._exec_action(action, macro, program, from_touch=True)
        except Exception as e:
            log.exception("执行触摸动作 %s/%s 失败: %s", gesture, action, e)

    def _exec_action(self, action, macro="", program="", from_touch=False):
        if action == "next_page":
            self.next_page(from_touch=from_touch)
        elif action == "prev_page":
            self.prev_page(from_touch=from_touch)
        elif action == "flip":
            self.toggle_reverse(from_touch=from_touch)
        elif action == "toggle_orientation":
            self.toggle_orientation(from_touch=from_touch)
        elif action == "lock":
            self.toggle_lock(from_touch=from_touch)
        elif action == "screenshot":
            threading.Thread(target=_act_screenshot, daemon=True).start()
        elif action == "mute_toggle":
            threading.Thread(target=_act_mute_toggle, daemon=True).start()
        elif action == "macro":
            threading.Thread(target=_act_send_macro, args=(macro,),
                             daemon=True).start()
        elif action == "launch":
            threading.Thread(target=_act_launch_program, args=(program,),
                             daemon=True).start()
        elif action == "shutdown":
            log.info("触摸触发关闭程序")
            if callable(self._on_shutdown):
                self._on_shutdown()
        elif action == "blank":
            size = PORTRAIT_SIZE if self.orientation == PORTRAIT else LANDSCAPE_SIZE
            self._overlay = Image.new("RGBA", size, (0, 0, 0, 255))
            self._overlay_until = time.monotonic() + 5.0
            self._overlay_seen = None
            self._force_send = True
            log.info("屏幕已清空 5 秒")
        else:
            log.warning("未知触摸动作: %s", action)

    def _composite(self, widget_img, mono):
        t0 = time.perf_counter()
        img = self._composite_impl(widget_img, mono)
        cost = (time.perf_counter() - t0) * 1000.0
        prev = self.metrics.get("comp_ms", cost)
        self.metrics["comp_ms"] = prev * 0.8 + cost * 0.2
        return img

    def _composite_impl(self, widget_img, mono):
        bg = self.background.tick(mono)
        if bg is None:
            canvas = Image.new("RGBA", (SCREEN_W, SCREEN_H), "black")
        else:
            canvas = bg.convert("RGBA")
        if widget_img is None:
            out = canvas.convert("RGB")
        else:
            if self.orientation == PORTRAIT:
                widget_img = widget_img.rotate(self.portrait_rotate, expand=True)
            if widget_img.size != (SCREEN_W, SCREEN_H):
                widget_img = widget_img.resize((SCREEN_W, SCREEN_H))
            if widget_img.mode != "RGBA":
                widget_img = widget_img.convert("RGBA")
            out = Image.alpha_composite(canvas, widget_img).convert("RGB")
        if self._dim > 0:
            # 伪亮度：整屏按比例压暗，对所有插件和背景一起生效。
            k = 100 - self._dim
            lut = [(i * k) // 100 for i in range(256)] * 3
            out = out.point(lut)
        if self.reverse:
            out = out.rotate(180)
        return out

    def _handle_switch(self):
        widgets = self._widgets
        current = self._current
        cur = widgets[current] if 0 <= current < len(widgets) else None
        prev = self._last_entered
        if prev is not None and prev is not cur:
            try: prev.on_leave(self._ctx_for(prev))
            except Exception as e:
                log.warning("on_leave %s 失败: %s", prev.name, e)
            if all(x is not prev for x in widgets):
                self._ctxs.pop(prev.name, None)
        if cur is not None:
            self._last_entered = cur
            try: cur.on_enter(self._ctx_for(cur))
            except Exception as e:
                log.warning("on_enter %s 失败: %s", cur.name, e)
        self._force_send = True

    def run(self):
        log.info("Scheduler 启动, %d 页, 朝向 %s, 翻转 %s",
                 len(self._widgets), self.orientation, self.reverse)
        last_mono = 0.0
        last_bg_ts = 0.0
        try:
            while not self._stop.is_set():
                if not self.device.connected:
                    if not self.device.open():
                        time.sleep(1.0); continue
                    self.device.set_reverse(self.reverse)
                    self.metrics["reconnects"] = int(self.metrics.get("reconnects", 0)) + 1
                    last_mono = 0.0
                    last_bg_ts = 0.0

                self._drain_ops()
                self._watch_widgets(time.monotonic())

                widgets = self._widgets
                current = self._current
                if not widgets or current >= len(widgets):
                    time.sleep(0.5); continue

                if self._switch.is_set():
                    self._switch.clear()
                    last_mono = 0.0
                    last_bg_ts = 0.0
                    self._handle_switch()

                w = widgets[current]
                now = time.time()
                mono = time.monotonic()
                self.ctx.now = now
                self.ctx.orientation = self.orientation
                wctx = self._ctx_for(w)
                wctx.now = now
                wctx.orientation = self.orientation

                need_render = (mono - last_mono >= w.interval)
                need_bg = (self._bg_enabled and
                           len(self.background.frames) > 1 and
                           mono - last_bg_ts >= 0.1)
                need_heartbeat = (not need_render and not need_bg and
                                  mono - self._last_send_mono >= HEARTBEAT_INTERVAL)

                # 上一帧还在链路上就不要排下一帧。设备侧环缓冲只有 8 KB，
                # 25 KB 的高清整帧必须让它在两帧之间排空，否则帧中间断流
                # 会留下半帧像素，表现为错位花屏。
                if self._send_gap > 0.0 and mono - self._last_frame_mono < self._send_gap:
                    need_render = False
                    need_bg = False
                    need_heartbeat = False

                if self._overlay is not None:
                    if mono >= self._overlay_until:
                        self._overlay = None
                        self._overlay_seen = None
                        self._force_send = True
                        overlay = None
                    else:
                        overlay = self._overlay
                else:
                    overlay = None

                if overlay is not None:
                    key = id(overlay)
                    if key != self._overlay_seen or self._force_send:
                        self._overlay_seen = key
                        if self.device.show_image(self._composite(overlay, mono)):
                            self._last_send_mono = mono
                            self._count_frame(mono)
                            self._force_send = False
                        else:
                            self._force_send = True      # ???????????
                elif need_render:
                    last_mono = mono
                    t0 = time.perf_counter()
                    try:
                        img = w.render(wctx)
                    except Exception as e:
                        log.exception("插件 %s 渲染异常: %s", w.name, e)
                        img = None
                        time.sleep(0.3)
                    t1 = time.perf_counter()
                    cost = (t1 - t0) * 1000.0
                    old = self.metrics["render_ms"].get(w.name, cost)
                    self.metrics["render_ms"][w.name] = old * 0.8 + cost * 0.2
                    if img is not None:
                        self._last_imgs[w.name] = img
                    if need_bg:
                        last_bg_ts = mono
                    if img is not None or need_bg or self._force_send:
                        frame = img if img is not None else self._last_imgs.get(w.name)
                        if frame is not None or need_bg:
                            if self.device.show_image(self._composite(frame, mono)):
                                self._last_send_mono = mono
                                self._count_frame(mono)
                                self._force_send = False
                            else:
                                self._force_send = True
                    log.debug("渲染 %s: render=%.1fms total=%.1fms",
                              w.name, cost, (time.perf_counter() - t0) * 1000.0)
                elif need_bg:
                    last_bg_ts = mono
                    if self.device.show_image(
                            self._composite(self._last_imgs.get(w.name), mono)):
                        self._last_send_mono = mono
                        self._count_frame(mono)
                    else:
                        self._force_send = True
                elif need_heartbeat:
                    if self.device.heartbeat():
                        self._last_send_mono = mono

                if mono - self._touch_last_sample_ts >= TOUCH_SAMPLE_INTERVAL:
                    self._touch_last_sample_ts = mono
                    try:
                        self._poll_touch(mono)
                    except Exception as e:
                        log.debug("触摸采样异常: %s", e)

                time.sleep(0.005)
        finally:
            self._teardown_all()
            log.info("Scheduler 退出")

def _default_config_path():
    local = _APP_DIR / "config.json"
    installed = "site-packages" in _APP_DIR.parts or "dist-packages" in _APP_DIR.parts
    if not installed:
        try:
            probe = _APP_DIR / ".write_test"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
            return str(local)
        except Exception:
            pass
    try:
        home = Path.home() / ".msu2_mini"
        home.mkdir(parents=True, exist_ok=True)
        return str(home / "config.json")
    except Exception:
        return str(local)

DEFAULT_CONFIG_PATH = _default_config_path()

_CONFIG_LOCK = threading.RLock()

_TOUCH_ACTIONS = ("passthrough", "next_page", "prev_page", "flip",
                  "toggle_orientation", "lock", "screenshot",
                  "mute_toggle", "macro", "launch", "shutdown")

_TOUCH_DEFAULTS = {
    "single": "passthrough",
    "double": "passthrough",
    "long":   "passthrough",
    "macro_single": "",
    "macro_double": "",
    "macro_long":   "",
    "program_single": "",
    "program_double": "",
    "program_long":   "",
    "threshold": TOUCH_DEFAULT_THRESHOLD,
}

def _default_pages():
    names = list(WIDGET_REGISTRY.keys())
    names.sort(key=lambda n: (WIDGET_REGISTRY[n].priority, n))
    return names

def _device_default(dev_id="dev1", port="auto"):
    return {
        "id": dev_id,
        "port": port,
        "pages": _default_pages(),
        "current_page": "",
        "orientation": "landscape",
        "reverse": False,
        "portrait_rotate": -90,
        "background": {"enabled": False, "path": "", "mode": "fit",
                       "dim": 0, "blur": 0, "paths": []},
        "touch": dict(_TOUCH_DEFAULTS),
    }


def _base_default_config():
    return {
        "devices": [_device_default()],
        "gif_dir": str(DEFAULT_GIF_DIR),
        "widget_config": {},
        "font": "",
        "web": {"host": "0.0.0.0", "port": 8765},
        "log_level": "INFO",
    }

def _widget_meta():
    meta = {}
    for name, cls in sorted(WIDGET_REGISTRY.items()):
        try:
            doc = (cls.__doc__ or "").strip().splitlines()
            desc = doc[0].strip() if doc else ""
            meta[name] = {
                "description": desc,
                "orientations": list(cls.orientations),
                "interval": cls.interval,
                "priority": cls.priority,
                "has_config": bool(getattr(cls, "config_schema", [])),
                "actions": dict(getattr(cls, "actions", {}) or {}),
                "source": "external" if str(getattr(cls, "__module__", "")).startswith("msu2_ext_widgets") else "builtin",
            }
        except Exception:
            meta[name] = {}
    return meta

def _deep_fill(default, user):
    out = dict(user) if isinstance(user, dict) else {}
    for k, dv in default.items():
        if k not in out:
            out[k] = dv
            continue
        uv = out[k]
        if isinstance(dv, dict):
            out[k] = _deep_fill(dv, uv) if isinstance(uv, dict) else dict(dv)
    return out

def _sanitize_touch(tk):
    if not isinstance(tk, dict):
        tk = {}
    touch = {}
    for k, dv in _TOUCH_DEFAULTS.items():
        v = tk.get(k, dv)
        if k == "threshold":
            v = _coerce_int(v, TOUCH_DEFAULT_THRESHOLD, 5, 2000)
        else:
            v = v if isinstance(v, str) else ("" if v is None else str(v))
            v = v.strip()
        if k in ("single", "double", "long") and v not in _TOUCH_ACTIONS:
            v = "passthrough"
        touch[k] = v
    return touch


def _sanitize_device(dev, index):
    if not isinstance(dev, dict):
        dev = {}
    out = _device_default("dev%d" % (index + 1))
    dev_id = dev.get("id")
    if isinstance(dev_id, str) and dev_id.strip():
        out["id"] = dev_id.strip()[:32]
    out["port"] = str(dev.get("port") or "auto")
    raw_pages = dev.get("pages")
    pages = []
    for name in raw_pages if isinstance(raw_pages, list) else []:
        if isinstance(name, str) and name in WIDGET_REGISTRY and name not in pages:
            pages.append(name)
    out["pages"] = pages or _default_pages()
    cur = dev.get("current_page") or ""
    out["current_page"] = cur if isinstance(cur, str) and cur in out["pages"] else ""
    if dev.get("orientation") in (LANDSCAPE, PORTRAIT):
        out["orientation"] = dev["orientation"]
    out["reverse"] = bool(dev.get("reverse", False))
    rotate = _coerce_int(dev.get("portrait_rotate"), -90)
    out["portrait_rotate"] = rotate if rotate in (90, -90) else -90
    bg = dev.get("background")
    if not isinstance(bg, dict):
        bg = {}
    paths = []
    for p in bg.get("paths") if isinstance(bg.get("paths"), list) else []:
        if isinstance(p, str) and p.strip():
            paths.append(p.strip())
    out["background"] = {
        "enabled": bool(bg.get("enabled", False)),
        "path": str(bg.get("path") or ""),
        "paths": paths,
        "mode": bg.get("mode") if bg.get("mode") in Background.MODES else "fit",
        "source": bg.get("source") if bg.get("source") in ("single", "playlist") else "single",
        "interval": _coerce_int(bg.get("interval"), 12, 2, 3600),
        "shuffle": bool(bg.get("shuffle")),
        "blur": _coerce_int(bg.get("blur"), 0, 0, 5),
    }
    out["touch"] = _sanitize_touch(dev.get("touch"))
    return out


def _sanitize_devices(cfg):
    raw = cfg.get("devices")
    if not isinstance(raw, list) or not raw:
        legacy = _device_default()
        for key in ("port", "pages", "current_page", "orientation", "reverse",
                    "portrait_rotate", "background", "touch"):
            if key in cfg:
                legacy[key] = cfg[key]
        raw = [legacy]
    devices = []
    seen_ids = set()
    for i, dev in enumerate(raw[:8]):
        item = _sanitize_device(dev, i)
        base_id = item["id"]
        n = 2
        while item["id"] in seen_ids:
            item["id"] = "%s-%d" % (base_id, n)
            n += 1
        seen_ids.add(item["id"])
        devices.append(item)
    for key in ("port", "pages", "current_page", "orientation", "reverse",
                "portrait_rotate", "background", "touch"):
        cfg.pop(key, None)
    cfg["devices"] = devices
    return devices

def _sanitize_config(cfg):
    gif_dir = cfg.get("gif_dir")
    cfg["gif_dir"] = gif_dir if isinstance(gif_dir, str) and gif_dir else str(DEFAULT_GIF_DIR)
    _sanitize_devices(cfg)
    wc = cfg.get("widget_config")
    if not isinstance(wc, dict):
        wc = {}
    cfg["widget_config"] = {k: dict(v) for k, v in wc.items()
                            if k in WIDGET_REGISTRY and isinstance(v, dict)}
    font = cfg.get("font")
    cfg["font"] = font if isinstance(font, str) else ""
    web = cfg.get("web")
    if not isinstance(web, dict):
        web = {}
    host = web.get("host")
    cfg["web"] = {"host": host if isinstance(host, str) and host else "0.0.0.0",
                  "port": _coerce_int(web.get("port"), 8765, 1, 65535)}
    lvl = str(cfg.get("log_level") or "INFO").upper()
    cfg["log_level"] = lvl if lvl in ("DEBUG", "INFO", "WARNING", "ERROR",
                                      "CRITICAL") else "INFO"
    return cfg

def _write_config_raw(cfg, path):
    p = Path(path)
    tmp = p.with_name(p.name + ".tmp")
    data = json.dumps(cfg, indent=2, ensure_ascii=False)
    with _CONFIG_LOCK:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(data, "utf-8")
            os.replace(tmp, p)
            return True
        except Exception as e:
            log.warning("写入配置失败 %s: %s", p, e)
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
            return False

def _default_config_path():
    p = Path(DEFAULT_CONFIG_PATH)
    if p.exists():
        return str(p)
    legacy = Path("./config.json")
    try:
        if legacy.exists() and legacy.resolve() != p.resolve():
            log.info("使用当前目录的配置文件: %s", legacy)
            return str(legacy)
    except Exception:
        pass
    return str(p)

def ensure_config_file(path=None):
    p = Path(path or DEFAULT_CONFIG_PATH)
    meta = _widget_meta()
    if not p.exists():
        cfg = _base_default_config()
        cfg["_help"] = {
            "port": "串口号。auto=自动扫描；也可指定 COM15 / /dev/ttyUSB0 / ttyACM0",
            "pages": "启用的插件及显示顺序，可在 Web 里调整",
            "current_page": "上次停留的插件名，启动时自动恢复",
            "orientation": "landscape / portrait",
            "reverse": "true=上下翻转（180°）",
            "portrait_rotate": "竖屏转横屏时的旋转角，-90 或 90",
            "gif_dir": "读取图片序列的目录",
            "background": {"enabled": "背景图开关",
                           "path": "背景图路径（图片、GIF 或图片序列目录）",
                           "mode": "显示方式：fit=适应 / fill=填充 / stretch=拉伸"},
            "widget_config": "每个插件自己的配置（Web 页面该行的 ⋯ 菜单里点「设置」修改）",
            "font": "自定义字体路径",
            "web": {"host": "WebUI 监听地址", "port": "WebUI 端口"},
            "log_level": "DEBUG / INFO / WARNING / ERROR",
            "touch": "全局触控动作。选 passthrough 的手势会透传给插件",
        }
        cfg["_available_widgets"] = meta
        _write_config_raw(cfg, p)
        log.info("已生成默认配置: %s", p)
        log.info("  可用插件: %s", ", ".join(meta.keys()))
        return cfg
    try:
        if p.stat().st_size > MAX_CONFIG_BYTES:
            raise ValueError("文件过大")
        user_cfg = json.loads(p.read_text("utf-8"))
        if not isinstance(user_cfg, dict):
            raise ValueError("顶层必须是对象")
    except Exception as e:
        backup = p.with_name(p.name + ".bad")
        try:
            shutil.copy2(p, backup)
            log.warning("配置解析失败 (%s)，已备份到 %s", e, backup)
        except Exception:
            log.warning("配置解析失败 (%s)，无法备份", e)
        cfg = _base_default_config(); cfg["_available_widgets"] = meta
        _write_config_raw(cfg, p)
        return cfg

    merged = _sanitize_config(_deep_fill(_base_default_config(), user_cfg))
    merged["_available_widgets"] = meta
    if merged != user_cfg:
        _write_config_raw(merged, p)
        log.info("配置已规范化: %s", p)
    return merged

def save_config(cfg, path=None):
    _write_config_raw(cfg, path or DEFAULT_CONFIG_PATH)


_save_timers = {}
_save_timers_lock = threading.Lock()


def save_config_soon(cfg, path=None, delay=0.8):
    """合并高频写盘：滑块拖动每挪一格都会 POST 一次，没必要每次都落盘。
    延迟 delay 秒后写一次最新内容；期间再有调用就把计时器重置。"""
    key = str(path or DEFAULT_CONFIG_PATH)

    def _flush():
        with _save_timers_lock:
            _save_timers.pop(key, None)
        try:
            save_config(cfg, path)
        except Exception as e:
            log.warning("保存配置失败: %s", e)

    with _save_timers_lock:
        old = _save_timers.get(key)
        if old is not None:
            old.cancel()
        t = threading.Timer(delay, _flush)
        t.daemon = True
        _save_timers[key] = t
        t.start()
    return True


def flush_config_saves():
    """退出前把挂着的延迟写盘立刻执行掉。"""
    with _save_timers_lock:
        timers = list(_save_timers.items())
        _save_timers.clear()
    for _, t in timers:
        t.cancel()
        try:
            t.function()
        except Exception:
            pass

def discover_and_open():
    ports = list(serial.tools.list_ports.comports())
    log.info("发现 %d 个串口: %s", len(ports), [p.device for p in ports])
    if not ports:
        return None
    for p in ports:
        dev = MiniDevice(p.device)
        if dev.open():
            return dev
        if dev.transport.last_error:
            log.info("  %s 不可用: %s", p.device, dev.transport.last_error)
        dev.close()
    log.error("未找到 MSU2_MINI。可能原因: 被其它程序占用 / 需提权")
    return None

INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Open_MSU2_MINI_Console</title>
<style>
body { font-family: Consolas, Menlo, monospace; max-width: 820px;
       margin: 2em auto; padding: 0 1em; color: #222; font-size: 14px;
       background: #fafafa; }
h1 { font-size: 1.15em; margin: 0 0 .8em; }
h2 { font-size: 1em; margin: 1.2em 0 .5em; color: #444;
     border-bottom: 1px solid #ddd; padding-bottom: 4px; }
button { font-family: inherit; font-size: 13px;
         padding: 3px 10px; margin: 2px 4px 2px 0;
         background: #eee; border: 1px solid #aaa;
         border-radius: 3px; cursor: pointer; }
button:hover { background: #ddd; }
button:disabled { color: #999; cursor: not-allowed; }
button.active { background: #cde; border-color: #678; }
button.mini { padding: 1px 7px; font-size: 12px; }
button.danger { background: #d33; color: #fff; border-color: #a11;
                padding: 6px 18px; font-size: 14px; }
button.danger:hover { background: #b22; }
input[type=text], input[type=number], select, textarea {
    font-family: inherit; font-size: 13px;
    padding: 3px 6px; border: 1px solid #aaa;
    border-radius: 3px; box-sizing: border-box;
}
textarea { font-size: 12px; width: 22em; }
label { display: inline-block; margin: 2px 8px 2px 0; }
pre { background: #fff; border: 1px solid #ccc; padding: .6em;
      font-size: 12px; overflow: auto; max-height: 15em;
      white-space: pre-wrap; }
.msg { font-size: 12px; color: #080; margin-left: .6em; }
.hint { font-size: 12px; color: #444; background: #f2f5fa; border: 1px solid #dde3ee;
        border-radius: 6px; padding: 6px 8px; margin: 0 0 10px; display: none; }
.tiny { font-size: 11px; color: #999; margin: 4px 0 0; }
.msg.err { color: #c00; }
.foot { max-width: 900px; margin: 18px auto 30px; padding: 10px 12px;
        border-top: 1px solid #ddd; color: #777; font-size: 12px;
        line-height: 1.7; text-align: center; }
.foot-warn { color: #a33; background: #fff6f6; border: 1px solid #f0d5d5;
             border-radius: 6px; padding: 6px 10px; margin-bottom: 6px; }
.foot b { color: #c0392b; }
.foot a { color: #2b5fd9; text-decoration: none; }
.foot a:hover { text-decoration: underline; }
.plist { border: 1px solid #ddd; background: #fff; border-radius: 3px;
         padding: 4px 6px; }
.pitem { padding: 3px 2px; border-bottom: 1px solid #eee;
         display: flex; align-items: center; flex-wrap: wrap; }
.pitem:last-child { border-bottom: 0; }
.pitem .name { flex: 1; padding-left: 6px; }
.pitem .tags { color: #888; font-size: 11px; margin-left: 6px; }
.pitem .tag-ext { background: #eef3ff; color: #2b5fd9; border: 1px solid #b9cdf5;
  border-radius: 3px; font-size: 10px; padding: 0 4px; margin-left: 4px; }
.pitem.empty { color: #999; font-style: italic; padding: 6px 8px; }
.pitem.current { background: #e8f0ff; }
.pitem.current .name { font-weight: bold; color: #2b5fd9; }
.pitem.current .name::before { content: '▶ '; color: #2b5fd9; }
.pitem .actbtn { margin-left: 4px; padding: 0 6px; line-height: 17px; }
.actpop { position: absolute; z-index: 90; min-width: 130px;
          background: #fff; border: 1px solid #aaa; border-radius: 4px;
          box-shadow: 0 4px 14px rgba(0,0,0,.18); padding: 4px; }
.actpop .ttl { font-size: 11px; color: #888; padding: 2px 6px 4px;
               border-bottom: 1px solid #eee; margin-bottom: 3px; }
.actpop button { display: block; width: 100%; text-align: left;
                 margin: 2px 0; padding: 4px 10px; font-size: 13px;
                 border: 0; background: #f4f4f4; }
.actpop button:hover { background: #e4e4e4; }
.card { background: #fff; border: 1px solid #ddd; border-radius: 4px;
        padding: 10px 12px; margin-bottom: 12px; }
.field { margin: 8px 0; }
.field label { display: block; font-size: 12px; color: #666;
               margin-bottom: 3px; }
.field .help { color: #999; font-size: 11px; margin-left: 6px; }
.shutdown-card { border-color: #f0b0b0; background: #fff8f8; }
.shutdown-card h2 { color: #a11; border-color: #f0c0c0; }
.touch-row { display: grid; grid-template-columns: 50px 200px 1fr 1fr;
             gap: 8px; align-items: center; margin: 6px 0; }
.touch-row select { width: 200px; }
.touch-row input[type=text] { width: 100%; }
.touch-row .lbl { font-size: 13px; color: #444; }
.hint { font-size: 11px; color: #888; margin: 4px 0 8px; }
</style>
</head>
<body>

<h1>Open_MSU2_MINI_Console</h1>

<div class="card">
  <h2>设备</h2>
  <label style="font-size:13px">当前设备
    <select id="dev-select" onchange="selectDevice()"></select>
  </label>
  <span class="msg" id="msg-dev"></span>
  <div style="margin-top:6px">
    <img id="dev-preview" alt="" style="width:320px;height:160px;object-fit:contain;image-rendering:pixelated;background:#000;border:1px solid #ccc;border-radius:4px">
  </div>
</div>

<div class="card">
  <h2>显示控制</h2>
  <button onclick="post('/api/prev')">上一页</button>
  <button onclick="post('/api/next')">下一页</button>
  <button id="btn-orient" onclick="post('/api/orientation')">切换朝向</button>
  <button id="btn-rev" onclick="post('/api/reverse')">上下翻转</button>
  <span class="msg" id="msg-display"></span>
  <div style="margin-top:8px;font-size:13px">
    <label>暗化（伪亮度）
      <input type="range" id="disp-dim" min="0" max="90" step="5" value="0"
             oninput="setDimLabel()" onchange="applyDim()"
             style="width:170px;vertical-align:middle">
      <span id="disp-dim-v" class="tiny">0%</span>
    </label>
    <span class="tiny" id="dim-hint">整屏压暗，插件和背景一起生效；0% = 原亮度。</span>
  </div>
</div>

<div class="card">
  <h2>全局触控</h2>
  <div class="hint">
    选"未定义（透传给插件）"的手势会直接交给当前插件处理，各插件支持的手势见它的设置面板<br>
    自定义程序：支持完整路径和环境变量<br>
    键盘宏示例：<code>ctrl+shift+s</code>、<code>alt+f4</code>、<code>win+r</code>
  </div>
  <div class="touch-row">
    <span class="lbl">单击</span>
    <select id="tk-single" onchange="updateArgVisibility()"></select>
    <input type="text" id="tk-macro-single" placeholder="宏，例: ctrl+shift+s">
    <input type="text" id="tk-program-single" placeholder="程序路径">
  </div>
  <div class="touch-row">
    <span class="lbl">双击</span>
    <select id="tk-double" onchange="updateArgVisibility()"></select>
    <input type="text" id="tk-macro-double" placeholder="宏，例: ctrl+shift+s">
    <input type="text" id="tk-program-double" placeholder="程序路径">
  </div>
  <div class="touch-row">
    <span class="lbl">长按</span>
    <select id="tk-long" onchange="updateArgVisibility()"></select>
    <input type="text" id="tk-macro-long" placeholder="宏，例: ctrl+shift+s">
    <input type="text" id="tk-program-long" placeholder="程序路径">
  </div>
  <div style="margin-top:8px">
    <button onclick="saveTouch()">应用触控</button>
    <span class="msg" id="msg-touch"></span>
  </div>
</div>

<div class="card">
  <h2>页面顺序</h2>
  <div class="plist" id="pages-list"></div>
  <div class="tiny">拖拽页面行可排序；双击页面名可立即切换到该页。</div>
  <div style="margin-top:.4em">
    <button onclick="loadPages()">刷新列表</button>
    <button onclick="post('/api/reload')">重载插件</button>
    <span class="msg" id="msg-pages"></span>
  </div>
</div>

<div class="card" id="config-panel" style="display:none">
  <h2 id="config-title">配置</h2>
  <div id="config-hint" class="hint"></div>
  <div id="config-body"></div>
  <div style="margin-top:.8em">
    <button onclick="saveConfig()">保存</button>
    <button onclick="closeConfig()">关闭</button>
    <span class="msg" id="msg-config"></span>
  </div>
</div>

<div class="card">
  <h2>通知</h2>
  <input type="text" id="notify-text" placeholder="推送到屏幕上的文字" style="width:22em">
  <input type="number" id="notify-secs" value="5" min="1" max="60" style="width:4em"> 秒
  <button onclick="sendNotify()">推送</button>
  <span class="msg" id="msg-notify"></span>
</div>

<div class="card">
  <h2>背景图</h2>
  <div style="margin-top:4px">
    <label style="font-size:12px;color:#666">模式
      <select id="bg-source" onchange="setBgSource()">
        <option value="single">单张</option>
        <option value="playlist">多张轮播</option>
      </select>
    </label>
    <label style="font-size:12px;color:#666;margin-left:12px" id="bg-iv-wrap">轮播秒数
      <input type="number" id="bg-interval" value="12" min="2" max="3600" style="width:5em">
    </label>
    <label style="font-size:12px;color:#666;margin-left:12px" id="bg-sh-wrap">
      <input type="checkbox" id="bg-shuffle"> 无序轮播
    </label>
  </div>
  <div style="margin-top:4px" id="bg-single-wrap">
    <input type="text" id="bg-path" placeholder="path/to/image.gif|png|jpg" style="width:22em">
  </div>
  <div style="margin-top:4px" id="bg-multi-wrap">
    <textarea id="bg-paths" rows="3" style="width:100%"
      placeholder="多张背景：一行一个路径"></textarea>
  </div>
  <div style="margin-top:4px">
    <label style="font-size:12px;color:#666">显示方式
      <select id="bg-mode" onchange="setBackgroundMode()">
        <option value="fit">适应</option>
        <option value="fill">填充</option>
        <option value="stretch">拉伸</option>
      </select>
    </label>
    <label style="font-size:12px;color:#666;margin-left:12px">模糊
      <input type="range" id="bg-blur" min="0" max="5" step="1" value="0"
             oninput="setBgBlurLabel()" onchange="applyBgLook()">
      <span id="bg-blur-v" class="tiny">0</span>
    </label>
  </div>
  <div style="margin-top:4px">
    <button id="btn-bg" onclick="toggleBackground()">启用背景</button>
    <span class="msg" id="msg-bg"></span>
  </div>
</div>

<div class="card">
  <h2>状态</h2>
  <div class="tiny" id="metrics-line"></div>
  <div class="tiny" id="ext-line" style="color:#c0392b"></div>
  <pre id="status">加载中...</pre>
</div>

<div class="card shutdown-card">
  <h2>关闭程序</h2>
  <div style="color:#666; font-size:12px; margin-bottom:.6em">
    关闭后上位机会退出，WebUI 会断开。需要重新启动程序才能继续使用。
  </div>
  <button class="danger" onclick="shutdownApp()">关闭程序</button>
  <span class="msg" id="msg-shutdown"></span>
</div>

<footer class="foot">
  <div class="foot-warn">
    禁止倒卖：本项目仅供个人学习与非商业使用，<b>禁止任何形式的转售、收费分发或捆绑销售</b>。
    如果你为它付过钱，那是有人在倒卖，请退款并向平台举报。
  </div>
  <div>
    项目地址：<a href="https://github.com/hsushjk/Open_MSU2_MINI_Console"
      target="_blank" rel="noopener">https://github.com/hsushjk/Open_MSU2_MINI_Console</a>
  </div>
</footer>

<script>
let currentPages = [];
let allWidgets = {};
let allBgEnabled = false;
let configWidgetName = '';
let currentWidgetName = '';

const TOUCH_ACTIONS = [
  ["passthrough", "未定义（透传给插件）"],
  ["next_page", "下一页"],
  ["prev_page", "上一页"],
  ["flip", "上下翻转"],
  ["toggle_orientation", "横竖屏切换"],
  ["lock", "锁定/解锁当前页"],
  ["screenshot", "截屏"],
  ["mute_toggle", "静音 / 解除"],
  ["macro", "自定义键盘宏"],
  ["launch", "自定义程序"],
  ["shutdown", "关闭程序"],
];

function fillTouchSelect(sel) {
  sel.innerHTML = '';
  TOUCH_ACTIONS.forEach(([v, l]) => {
    const o = document.createElement('option');
    o.value = v; o.textContent = l;
    sel.appendChild(o);
  });
}

function syncTouchSelects(t) {
  ['single', 'double', 'long'].forEach(k => {
    const sel = document.getElementById('tk-' + k);
    if (!sel || !sel.options.length) return;
    if (document.activeElement === sel) return;
    const v = t[k];
    if (v && sel.value !== v) sel.value = v;
  });
}

function updateArgVisibility() {
  const triples = [
    ['tk-single', 'tk-macro-single', 'tk-program-single'],
    ['tk-double', 'tk-macro-double', 'tk-program-double'],
    ['tk-long',   'tk-macro-long',   'tk-program-long'],
  ];
  triples.forEach(([sid, mid, pid]) => {
    const v = document.getElementById(sid).value;
    document.getElementById(mid).style.visibility =
      (v === 'macro') ? 'visible' : 'hidden';
    document.getElementById(pid).style.visibility =
      (v === 'launch') ? 'visible' : 'hidden';
  });
}

async function loadTouch() {
  try {
    const j = await getJSON('/api/touch_config');
    if (!j.ok) return;
    fillTouchSelect(document.getElementById('tk-single'));
    fillTouchSelect(document.getElementById('tk-double'));
    fillTouchSelect(document.getElementById('tk-long'));
    document.getElementById('tk-single').value = j.values.single;
    document.getElementById('tk-double').value = j.values.double;
    document.getElementById('tk-long').value = j.values.long;
    document.getElementById('tk-macro-single').value = j.values.macro_single || '';
    document.getElementById('tk-macro-double').value = j.values.macro_double || '';
    document.getElementById('tk-macro-long').value = j.values.macro_long || '';
    document.getElementById('tk-program-single').value = j.values.program_single || '';
    document.getElementById('tk-program-double').value = j.values.program_double || '';
    document.getElementById('tk-program-long').value = j.values.program_long || '';
    updateArgVisibility();
  } catch(e) {
    console.error(e);
  }
}

async function saveTouch() {
  const values = {
    single: document.getElementById('tk-single').value,
    double: document.getElementById('tk-double').value,
    long: document.getElementById('tk-long').value,
    macro_single: document.getElementById('tk-macro-single').value.trim(),
    macro_double: document.getElementById('tk-macro-double').value.trim(),
    macro_long: document.getElementById('tk-macro-long').value.trim(),
    program_single: document.getElementById('tk-program-single').value.trim(),
    program_double: document.getElementById('tk-program-double').value.trim(),
    program_long: document.getElementById('tk-program-long').value.trim(),
  };
  setMsg('msg-touch', '保存中...', false);
  try {
    const j = await getJSON('/api/touch_config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({values})
    });
    if (j.ok) setMsg('msg-touch', '已应用', false);
    else setMsg('msg-touch', j.err || '保存失败', true);
  } catch(e) {
    setMsg('msg-touch', '失败: ' + e, true);
  }
}


async function loadDim() {
  try {
    const j = await getJSON('/api/dim');
    const el = document.getElementById('disp-dim');
    if (el && document.activeElement !== el) {
      el.value = j.dim || 0;
      setDimLabel();
    }
  } catch(e) {}
}

function setDimLabel() {
  const el = document.getElementById('disp-dim');
  const lab = document.getElementById('disp-dim-v');
  if (el && lab) lab.textContent = el.value + '%';
}

async function applyDim() {
  const el = document.getElementById('disp-dim');
  if (!el) return;
  const v = parseInt(el.value, 10);
  try {
    const j = await postJSON('/api/dim', { dim: isNaN(v) ? 0 : v });
    if (j.ok) {
      setDimLabel();
      setMsg('msg-display', '暗化 ' + j.dim + '%', false);
    } else {
      setMsg('msg-display', j.err || '设置失败', true);
    }
  } catch(e) {
    setMsg('msg-display', '失败: ' + e, true);
  }
}

async function getJSON(url, opts) {
  const r = await fetch(url, Object.assign({cache:'no-store'}, opts||{}));
  if(!r.ok) throw new Error(r.status + ' ' + r.statusText);
  return r.json();
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
  }[c]));
}

function setMsg(id, text, isErr) {
  const el = document.getElementById(id);
  el.textContent = text;
  el.classList.toggle('err', !!isErr);
}

async function loadDevices() {
  try {
    const j = await getJSON('/api/devices');
    const sel = document.getElementById('dev-select');
    const active = j.active || '';
    sel.innerHTML = '';
    (j.devices || []).forEach(d => {
      const o = document.createElement('option');
      o.value = d.id;
      o.textContent = `${d.id}  ${d.port || 'auto'}`;
      sel.appendChild(o);
    });
    if (active) sel.value = active;
  } catch(e) {}
}

async function selectDevice() {
  const id = document.getElementById('dev-select').value;
  const j = await postJSON('/api/select', { id });
  document.getElementById('msg-dev').textContent = j.ok ? '已切换' : (j.err || '失败');
  if (j.ok) {
    await refresh();
    await loadPages();
  }
}

async function sendNotify() {
  const text = document.getElementById('notify-text').value;
  const secs = parseInt(document.getElementById('notify-secs').value || '5', 10);
  const j = await postJSON('/api/notify', { text, secs });
  document.getElementById('msg-notify').textContent =
    j.ok ? '已推送' : (j.err || '失败');
}

function setBgBlurLabel() {
  document.getElementById('bg-blur-v').textContent =
    document.getElementById('bg-blur').value;
}

async function applyBgLook() {
  const blur = parseInt(document.getElementById('bg-blur').value, 10);
  const j = await postJSON('/api/background', {
    enabled: allBgEnabled,
    path: document.getElementById('bg-path').value.trim(),
    paths: bgPaths(),
    mode: document.getElementById('bg-mode').value,
    blur,
  });
  document.getElementById('msg-bg').textContent =
    j.ok ? `模糊 ${j.blur}` : (j.err || '失败');
}

function bgSource() {
  const el = document.getElementById('bg-source');
  return el ? el.value : 'single';
}

function setBgSource() {
  const pl = bgSource() === 'playlist';
  const pw = document.getElementById('bg-iv-wrap');
  const sw = document.getElementById('bg-sh-wrap');
  const single = document.getElementById('bg-single-wrap');
  const multi = document.getElementById('bg-multi-wrap');
  if (pw) pw.style.display = pl ? '' : 'none';
  if (sw) sw.style.display = pl ? '' : 'none';
  if (single) single.style.display = pl ? 'none' : '';
  if (multi) multi.style.display = pl ? '' : 'none';
}

function bgInterval() {
  const el = document.getElementById('bg-interval');
  return el ? parseInt(el.value || '12', 10) : 12;
}

function bgShuffle() {
  const el = document.getElementById('bg-shuffle');
  return !!(el && el.checked);
}

function bgPaths() {
  return document.getElementById('bg-paths').value
    .split('\n').map(s => s.trim()).filter(s => s.length > 0);
}

async function refresh() {
  try {
    const j = await getJSON('/api/status');
    document.getElementById('status').textContent =
      JSON.stringify(j, null, 2);
    document.getElementById('btn-orient').textContent =
      j.orientation === 'portrait' ? '切换到横屏' : '切换到竖屏';
    document.getElementById('btn-rev').textContent =
      j.reverse ? '取消翻转' : '上下翻转';
    if (document.activeElement !== document.getElementById('bg-path')) {
      document.getElementById('bg-path').value = j.background_path || '';
    }
    const bgBtn = document.getElementById('btn-bg');
    bgBtn.textContent = j.background_enabled ? '关闭背景' : '启用背景';
    bgBtn.classList.toggle('active', !!j.background_enabled);
    allBgEnabled = !!j.background_enabled;
    const bgModeEl = document.getElementById('bg-mode');
    if (bgModeEl && document.activeElement !== bgModeEl) {
      bgModeEl.value = j.background_mode || 'fit';
    }
    const dimEl = document.getElementById('disp-dim');
    if (dimEl && document.activeElement !== dimEl) {
      dimEl.value = j.dim || 0;
      setDimLabel();
    }
    const blurEl = document.getElementById('bg-blur');
    if (blurEl && document.activeElement !== blurEl) {
      blurEl.value = j.background_blur || 0;
      setBgBlurLabel();
    }
    const pathsEl = document.getElementById('bg-paths');
    if (pathsEl && document.activeElement !== pathsEl) {
      const cur = (j.background_paths || []).join('\n');
      if (pathsEl.value.trim() !== cur.trim()) pathsEl.value = cur;
    }
    const sel = document.getElementById('dev-select');
    if (sel && j.active && document.activeElement !== sel) {
      sel.value = j.active;
    }
    const m = j.metrics || {};
    if (m.fps !== undefined) {
      const rk = Object.keys(m.render_ms || {})
        .map(k => `${k} ${m.render_ms[k]}ms`).join('  ');
      document.getElementById('metrics-line').textContent =
        `FPS ${m.fps}   吞吐 ${m.bytes_per_sec} B/s   帧 ${m.frames}   ` +
        `重连 ${m.reconnects}   渲染 ${rk}`;
    }
    const extEl = document.getElementById('ext-line');
    if (extEl) {
      const ef = j.ext_failed || [];
      extEl.textContent = ef.length
        ? `外部插件加载失败 ${ef.length} 个：` + ef.map(function (x) {
            const p = String(x[0] || '').split(/[\\/]/).pop();
            return p + '（' + (x[1] || '') + '）';
          }).join('   ')
        : '';
    }
    syncTouchSelects(j.touch || {});
    const pv = document.getElementById('dev-preview');
    if (pv && j.current_widget) {
      const want = '/api/preview?name=' + encodeURIComponent(j.current_widget) + '&t=' + Date.now();
      if (pv.dataset.want !== want) { pv.dataset.want = want; pv.src = want; }
    }
    const srcEl = document.getElementById('bg-source');
    if (srcEl && document.activeElement !== srcEl) {
      srcEl.value = j.background_source || 'single';
      setBgSource();
    }
    const pthEl = document.getElementById('bg-paths');
    if (pthEl && document.activeElement !== pthEl) {
      pthEl.value = (j.background_paths || []).join('\n');
    }
    const ivEl = document.getElementById('bg-interval');
    if (ivEl && document.activeElement !== ivEl) {
      ivEl.value = j.background_interval || 12;
    }
    const shEl = document.getElementById('bg-shuffle');
    if (shEl && document.activeElement !== shEl) {
      shEl.checked = !!j.background_shuffle;
    }

    currentWidgetName = j.current_widget || '';
    document.querySelectorAll('#pages-list .pitem').forEach(el => {
      el.classList.toggle('current', el.dataset.widget === currentWidgetName);
    });
  } catch(e) {
    document.getElementById('status').textContent = '状态获取失败: ' + e;
  }
}

async function loadPages() {
  const j = await getJSON('/api/widgets');
  allWidgets = {};
  (j.widgets || []).forEach(w => { allWidgets[w.name] = w; });
  currentPages = (j.active || []).slice();
  renderPages();
}

function renderPages() {
  const box = document.getElementById('pages-list');
  closeActMenu();
  box.innerHTML = '';

  if (currentPages.length === 0) {
    const e = document.createElement('div');
    e.className = 'pitem empty';
    e.textContent = '未启用任何页面';
    box.appendChild(e);
  }

  currentPages.forEach((name, i) => {
    const w = allWidgets[name] || { orientations: [], has_config: false, actions: {} };
    const row = document.createElement('div');
    row.className = 'pitem';
    row.dataset.widget = name;
    row.draggable = true;
    if (name === currentWidgetName) row.classList.add('current');

    row.addEventListener('dragstart', ev => {
      dragIndex = i;
      ev.dataTransfer.effectAllowed = 'move';
      try { ev.dataTransfer.setData('text/plain', name); } catch(e) {}
    });
    row.addEventListener('dragend', () => { dragIndex = null; });
    row.addEventListener('dragover', ev => {
      ev.preventDefault();
      ev.dataTransfer.dropEffect = 'move';
      if (dragIndex !== null && dragIndex !== i) row.style.borderTop = '2px solid #2b5fd9';
    });
    row.addEventListener('dragleave', () => { row.style.borderTop = ''; });
    row.addEventListener('drop', ev => {
      ev.preventDefault();
      row.style.borderTop = '';
      dragMove(i);
    });
    row.addEventListener('dblclick', ev => {
      if (ev.target.closest('button')) return;
      switchTo(name);
    });

    const cfgBtn = '';
    const actCount = (w.has_config ? 1 : 0) +
                     Object.keys(w.actions || {}).length;
    const actBtn = actCount > 0
      ? `<button class="mini actbtn" title="插件动作" ` +
        `onclick="toggleActMenu(event,'${esc(name)}')">⋯</button>`
      : '';

    const nameTag = w.source === 'external'
      ? `<span class="name" title="双击切换到该页">${esc(name)}</span>` +
        `<span class="tag-ext" title="外挂插件">外挂</span>`
      : `<span class="name" title="双击切换到该页">${esc(name)}</span>`;

    row.innerHTML =
      `<button class="mini" onclick="moveUp(${i})" ${i===0?'disabled':''}>↑</button>` +
      `<button class="mini" onclick="moveDown(${i})" ${i===currentPages.length-1?'disabled':''}>↓</button>` +
      `<button class="mini" onclick="removeAt(${i})">×</button>` +
      cfgBtn +
      nameTag +
      actBtn +
      `<span class="tags">[${(w.orientations||[]).join('/')}]</span>`;
    box.appendChild(row);
  });

  const disabled = Object.keys(allWidgets).filter(n => !currentPages.includes(n));
  if (disabled.length > 0) {
    const sep = document.createElement('div');
    sep.className = 'pitem';
    sep.style.borderTop = '1px dashed #ccc';
    sep.style.color = '#999';
    sep.style.fontSize = '11px';
    sep.textContent = '已停用';
    box.appendChild(sep);
    disabled.forEach(name => {
      const w = allWidgets[name] || { orientations: [] };
      const row = document.createElement('div');
      row.className = 'pitem';
      row.style.opacity = '0.6';
      row.innerHTML =
        `<button class="mini" onclick="addWidget('${esc(name)}')">+</button>` +
        `<span class="name">${esc(name)}</span>` +
        `<span class="tags">[${(w.orientations||[]).join('/')}]</span>`;
      box.appendChild(row);
    });
  }
}

let actMenu = null;
let dragIndex = null;

function closeActMenu() {
  if (!actMenu) return;
  actMenu.remove();
  actMenu = null;
}

function placeActMenu(box, btn) {
  const r = btn.getBoundingClientRect();
  const bw = box.offsetWidth, bh = box.offsetHeight;
  const sx = window.scrollX || window.pageXOffset || 0;
  const sy = window.scrollY || window.pageYOffset || 0;
  let left = r.right + sx + 6;
  if (left + bw > sx + window.innerWidth - 8) left = r.left + sx - bw - 6;
  if (left < sx + 8) left = sx + Math.max(8, window.innerWidth - bw - 8);
  let top = r.top + sy + r.height / 2 - bh / 2;
  if (top < sy + 8) top = sy + 8;
  box.style.left = left + 'px';
  box.style.top = top + 'px';
}

function toggleActMenu(ev, name) {
  ev.stopPropagation();
  if (ev.preventDefault) ev.preventDefault();
  const btn = ev.currentTarget;
  const same = actMenu && actMenu.dataset.widget === name;
  closeActMenu();
  if (same) return;
  const w = allWidgets[name] || {};
  const entries = [];
  if (w.has_config) entries.push(['__cfg__', '设置']);
  Object.entries(w.actions || {}).forEach(([a, label]) => entries.push([a, label]));
  if (!entries.length) return;
  const box = document.createElement('div');
  box.className = 'actpop';
  box.dataset.widget = name;
  const ttl = document.createElement('div');
  ttl.className = 'ttl';
  ttl.textContent = name;
  box.appendChild(ttl);
  entries.forEach(([act, label]) => {
    const b = document.createElement('button');
    b.textContent = label;
    b.onclick = () => {
      if (act === '__cfg__') openConfig(name); else doAction(name, act);
    };
    box.appendChild(b);
  });
  document.body.appendChild(box);
  placeActMenu(box, btn);
  actMenu = box;
}

async function pushPages(newPages) {
  try {
    const j = await getJSON('/api/pages', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({pages: newPages})
    });
    if (j.ok) {
      currentPages = j.pages.slice();
      renderPages();
      setMsg('msg-pages', '已更新', false);
    } else {
      setMsg('msg-pages', j.err || '被拒绝', true);
    }
  } catch(e) {
    setMsg('msg-pages', '失败: ' + e, true);
  }
  setTimeout(refresh, 200);
}

function moveUp(i) {
  if (i <= 0) return;
  const p = currentPages.slice();
  [p[i-1], p[i]] = [p[i], p[i-1]];
  pushPages(p);
}
function moveDown(i) {
  if (i >= currentPages.length - 1) return;
  const p = currentPages.slice();
  [p[i+1], p[i]] = [p[i], p[i+1]];
  pushPages(p);
}
function removeAt(i) {
  const p = currentPages.slice();
  p.splice(i, 1);
  pushPages(p);
}
function addWidget(name) {
  const p = currentPages.slice();
  p.push(name);
  pushPages(p);
}
function dragMove(target) {
  if (dragIndex === null || target === null) return;
  if (dragIndex === target) return;
  if (dragIndex < 0 || dragIndex >= currentPages.length) return;
  const p = currentPages.slice();
  const [item] = p.splice(dragIndex, 1);
  p.splice(target, 0, item);
  dragIndex = null;
  pushPages(p);
}
async function switchTo(name) {
  try {
    const j = await getJSON('/api/switch', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name})
    });
    if (j.ok) setMsg('msg-pages', '已切换到 ' + name, false);
    else setMsg('msg-pages', j.err || '切换失败', true);
  } catch(e) {
    setMsg('msg-pages', '切换失败: ' + e, true);
  }
  setTimeout(refresh, 200);
}
async function setBackgroundMode() {
  const modeEl = document.getElementById('bg-mode');
  const mode = modeEl ? modeEl.value : 'fit';
  const path = (document.getElementById('bg-path') || {}).value || '';
  const enabled = !!allBgEnabled;
  try {
    const j = await getJSON('/api/background', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled: enabled, path: path.trim(), mode: mode})
    });
    if (j.ok) setMsg('msg-bg', '显示方式已更新', false);
    else setMsg('msg-bg', j.err || '被拒绝', true);
  } catch(e) {
    setMsg('msg-bg', '失败: ' + e, true);
  }
  setTimeout(refresh, 200);
}

async function doAction(name, action) {
  setMsg('msg-pages', '执行中...', false);
  try {
    const j = await getJSON('/api/widget_action', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name, action, payload: {}})
    });
    if (j.ok) setMsg('msg-pages', '已执行', false);
    else setMsg('msg-pages', j.err || '失败', true);
  } catch(e) {
    setMsg('msg-pages', '失败: ' + e, true);
  }
  setTimeout(refresh, 200);
}

async function openConfig(name) {
  try {
    const j = await getJSON(`/api/widget_config?name=${encodeURIComponent(name)}`);
    if (!j.ok) { alert(j.err || '加载失败'); return; }
    configWidgetName = name;

    document.getElementById('config-title').textContent = `配置: ${name}`;
    const hint = document.getElementById('config-hint');
    if (hint) {
      const parts = [];
      if (j.touch_help) parts.push('透传触控：' + j.touch_help);
      hint.textContent = parts.length ? parts.join('　|　') : '';
      hint.style.display = parts.length ? 'block' : 'none';
    }
    const box = document.getElementById('config-body');
    box.innerHTML = '';

    if (!j.schema || j.schema.length === 0) {
      box.innerHTML = '<div style="color:#999">此插件暂无配置项</div>';
      document.getElementById('config-panel').style.display = 'block';
      return;
    }

    j.schema.forEach(f => {
      const v = j.values[f.key];
      const div = document.createElement('div');
      div.className = 'field';

      const lbl = document.createElement('label');
      lbl.textContent = f.label;
      if (f.help) {
        const h = document.createElement('span');
        h.className = 'help';
        h.textContent = '(' + f.help + ')';
        lbl.appendChild(h);
      }
      div.appendChild(lbl);

      let input;
      if (f.type === 'bool') {
        input = document.createElement('input');
        input.type = 'checkbox'; input.checked = !!v;
      } else if (f.type === 'select') {
        input = document.createElement('select');
        (f.options || []).forEach(o => {
          const opt = document.createElement('option');
          opt.value = o; opt.textContent = o;
          if (o === v) opt.selected = true;
          input.appendChild(opt);
        });
      } else if (f.type === 'list_str') {
        input = document.createElement('textarea');
        input.rows = 4;
        input.value = (Array.isArray(v) ? v : []).join('\n');
      } else if (f.type === 'int' || f.type === 'float') {
        input = document.createElement('input');
        input.type = 'number';
        input.value = v ?? '';
        if (f.min != null) input.min = f.min;
        if (f.max != null) input.max = f.max;
        if (f.step != null) input.step = f.step;
      } else {
        input = document.createElement('input');
        input.type = 'text';
        input.value = v ?? '';
      }
      input.dataset.key = f.key;
      input.dataset.type = f.type;
      div.appendChild(input);
      box.appendChild(div);
    });

    document.getElementById('config-panel').style.display = 'block';
    document.getElementById('config-panel').scrollIntoView({
      behavior: 'smooth', block: 'start' });
    setMsg('msg-config', '', false);
  } catch(e) {
    alert('加载配置失败: ' + e);
  }
}

async function saveConfig() {
  if (!configWidgetName) return;
  const box = document.getElementById('config-body');
  const values = {};
  box.querySelectorAll('input,select,textarea').forEach(el => {
    const k = el.dataset.key;
    const t = el.dataset.type;
    if (t === 'bool') values[k] = el.checked;
    else if (t === 'int') values[k] = parseInt(el.value || 0, 10);
    else if (t === 'float') values[k] = parseFloat(el.value || 0);
    else if (t === 'list_str') {
      values[k] = el.value.split('\n').map(s => s.trim()).filter(Boolean);
    } else {
      values[k] = el.value;
    }
  });
  setMsg('msg-config', '保存中...', false);
  try {
    const j = await getJSON('/api/widget_config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: configWidgetName, values})
    });
    if (j.ok) setMsg('msg-config', '已保存', false);
    else setMsg('msg-config', j.err || '保存失败', true);
  } catch(e) {
    setMsg('msg-config', '失败: ' + e, true);
  }
}

function closeConfig() {
  document.getElementById('config-panel').style.display = 'none';
}

async function postJSON(path, body) {
  try {
    return await getJSON(path, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body || {}),
    });
  } catch(e) {
    return {ok: false, err: String(e)};
  }
}

async function post(path) {
  try {
    const j = await getJSON(path, {method:'POST'});
    if (j.ok === false) setMsg('msg-display', j.err || '拒绝', true);
    else setMsg('msg-display', '已应用', false);
  } catch(e) {
    setMsg('msg-display', '失败: ' + e, true);
  }
  setTimeout(refresh, 250);
}

async function toggleBackground() {
  const btn = document.getElementById('btn-bg');
  const currentlyOn = btn.classList.contains('active');
  const next = !currentlyOn;
  const path = document.getElementById('bg-path').value.trim();
  const modeEl = document.getElementById('bg-mode');
  const mode = modeEl ? modeEl.value : 'fit';
  const paths = bgPaths();
  const blur = parseInt(document.getElementById('bg-blur').value, 10);
  const source = bgSource();
  const interval = bgInterval();
  const shuffle = bgShuffle();

  if (next && source === 'single' && !path) {
    setMsg('msg-bg', '请先填写图片路径', true);
    return;
  }
  if (next && source === 'playlist' && paths.length === 0) {
    setMsg('msg-bg', '请先填写图片路径', true);
    return;
  }
  setMsg('msg-bg', '应用中...', false);
  try {
    const j = await getJSON('/api/background', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled: next, path, paths, mode, blur,
                           source, interval, shuffle})
    });
    if (j.ok) setMsg('msg-bg', next ? '已启用' : '已关闭', false);
    else setMsg('msg-bg', j.err || '被拒绝', true);
  } catch(e) {
    setMsg('msg-bg', '失败: ' + e, true);
  }
  setTimeout(refresh, 250);
}

async function shutdownApp() {
  if (!confirm('确定要关闭 MSU2_MINI 上位机吗？\n关闭后需要重新启动才能继续使用。')) {
    return;
  }
  setMsg('msg-shutdown', '正在关闭...', false);
  try {
    const r = await fetch('/api/shutdown', {method:'POST'});
    const j = await r.json();
    if (j.ok) {
      setMsg('msg-shutdown', '已发送关闭指令，窗口即将失效', false);
      setTimeout(() => {
        document.body.innerHTML =
          '<div style="text-align:center; margin-top:8em; color:#666;' +
          ' font-family:Consolas,monospace">' +
          '<h1 style="color:#a11">程序已关闭</h1>' +
          '<p>可以关闭此页面，或重新启动 MSU2_MINI 后再刷新。</p></div>';
      }, 800);
    } else {
      setMsg('msg-shutdown', j.err || '失败', true);
    }
  } catch(e) {
    setMsg('msg-shutdown', '程序已关闭', false);
    setTimeout(() => {
      document.body.innerHTML =
        '<div style="text-align:center; margin-top:8em; color:#666;' +
        ' font-family:Consolas,monospace">' +
        '<h1 style="color:#a11">程序已关闭</h1>' +
        '<p>可以关闭此页面，或重新启动 MSU2_MINI 后再刷新。</p></div>';
    }, 800);
  }
}

loadTouch();
loadDim();
loadDevices();
setBgSource();
loadPages();
refresh();
setInterval(refresh, 1000);
setInterval(loadDevices, 5000);
</script>
</body>
</html>
"""

def _check_path(path, exts, max_bytes):
    try:
        p = Path(str(path)).expanduser()
    except Exception:
        return False, "路径无效"
    try:
        if not p.exists():
            return True, ""
        if p.is_dir():
            for f in list(p.iterdir())[:200]:
                if f.is_file() and f.suffix.lower() not in exts:
                    return False, "目录含不支持的文件: %s" % f.name
            return True, ""
        if p.suffix.lower() not in exts:
            return False, "不支持的扩展名: %s" % (p.suffix or "无")
        if p.stat().st_size > max_bytes:
            return False, "文件过大: %s" % p.name
    except Exception:
        return False, "无法读取文件"
    return True, ""

def _check_widget_values(schema, values):
    for f in schema:
        key = getattr(f, "key", None)
        if not isinstance(key, str) or not isinstance(values, dict):
            continue
        if not (key == "path" or key.endswith("_path") or key.endswith("_dir")):
            continue
        v = values.get(key)
        if not isinstance(v, str) or not v:
            continue
        if key.endswith("_dir") or Path(v).suffix.lower() in IMAGE_EXTS:
            ok, err = _check_path(v, IMAGE_EXTS, MAX_RESOURCE_BYTES)
        else:
            ok, err = _check_path(v, TEXT_EXTS + IMAGE_EXTS, MAX_TEXT_BYTES)
        if not ok:
            label = getattr(f, "label", "") or key
            return False, "%s: %s" % (label, err)
    return True, ""

_PROBED = {}


def _pick_free_port(used):
    """自动挑一个能握手的串口。

    握手成功的那条连接**不关**，留在 _PROBED 里交给 DeviceManager 直接复用：
    否则探测时打一遍“串口已打开 / 固件版本”，_build 里再开一次又打一遍。
    """
    try:
        ports = [p.device for p in serial.tools.list_ports.comports()]
    except Exception:
        return ""
    for name in ports:
        if name in used:
            continue
        probe = MiniDevice(name)
        ok = False
        try:
            ok = probe.open()
        except Exception:
            ok = False
        if ok:
            _PROBED[name] = probe
            return name
        probe.close()
    return ""


class OfflineDevice:
    def __init__(self):
        self.port = "offline"
        self.connected = True
        self.last = None
        self.frames = 0
        self.transport = type("T", (), {"bytes_written": 0})()

    def open(self):
        self.connected = True
        return True

    def close(self):
        self.connected = False

    def show_image(self, img):
        self.last = img.copy()
        self.frames += 1
        self.transport.bytes_written += img.size[0] * img.size[1] * 2
        return True

    def set_reverse(self, reverse):
        pass

    def read_touch(self):
        return 0

    def heartbeat(self):
        return True

class DeviceManager:
    def __init__(self, cfg, cfg_path, simulate=False):
        self.cfg = cfg
        self.cfg_path = cfg_path
        self.simulate = bool(simulate)
        self.lock = threading.RLock()
        self.schedulers = {}
        self.order = []
        self.active_id = None
        self._build()

    def _build(self):
        items = list(self.cfg.get("devices") or [])
        if self.simulate:
            ids = {str(d.get("id")) for d in items if isinstance(d, dict)}
            sid = "sim"
            n = 1
            while sid in ids:
                n += 1
                sid = "sim%d" % n
            sim_dev = _device_default(sid, "offline")
            items.append(sim_dev)
        used = set()
        for i, dev in enumerate(items):
            if not isinstance(dev, dict):
                continue
            did = str(dev.get("id") or ("dev%d" % (i + 1)))
            port = str(dev.get("port") or "auto")
            reused = None
            if port.lower() in ("auto", ""):
                port = _pick_free_port(used) or ""
                reused = _PROBED.pop(port, None)
            if port:
                used.add(port)
            if reused is not None:
                device = reused
            else:
                device = (OfflineDevice() if port.lower() == "offline"
                          else MiniDevice(port or "auto"))
            sched = Scheduler(device, dev, on_config_changed=self._persist,
                              shared=self.cfg)
            self.schedulers[did] = sched
            self.order.append(did)
        if self.order:
            self.active_id = self.order[0]

    def _persist(self):
        try:
            save_config(self.cfg, self.cfg_path)
        except Exception as e:
            log.warning("保存配置失败: %s", e)

    def start(self):
        for did in self.order:
            self.schedulers[did].start()

    def stop(self):
        for did in self.order:
            self.schedulers[did].stop()
        for did in self.order:
            try:
                self.schedulers[did].join(timeout=3.0)
            except Exception:
                pass

    def active(self):
        with self.lock:
            return self.schedulers.get(self.active_id)

    def select(self, did):
        with self.lock:
            if did not in self.schedulers:
                return False
            self.active_id = did
            return True

    def describe(self):
        out = []
        for did in self.order:
            s = self.schedulers[did]
            widgets = s._widgets
            cur = s._current
            name = widgets[cur].name if 0 <= cur < len(widgets) else None
            out.append({"id": did, "port": s.device.port,
                        "connected": s.device.connected,
                        "current": name, "pages": len(widgets)})
        return out

class WebUI(threading.Thread):
    def __init__(self, manager, cfg, cfg_path, on_shutdown=None):
        super().__init__(daemon=True, name="WebUI")
        self.manager = manager
        self.cfg = cfg
        self.cfg_path = cfg_path
        self.on_shutdown = on_shutdown
        web = cfg.get("web")
        if not isinstance(web, dict):
            web = {}
        self.host = web.get("host") or "0.0.0.0"
        self.port = _coerce_int(web.get("port"), 8765, 1, 65535)
        self._stop = threading.Event()
        self._pv = {}
        self._pv_lock = threading.RLock()

    @property
    def scheduler(self):
        return self.manager.active() if self.manager else None

    def stop(self):
        self._stop.set()
        with self._pv_lock:
            for entry in self._pv.values():
                inst = entry.get("inst")
                ctx = entry.get("ctx")
                if inst is None or ctx is None:
                    continue
                try:
                    inst.teardown(ctx)
                except Exception:
                    pass
            self._pv.clear()

    def run(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        cfg_path = self.cfg_path
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def _send(self, body, ctype):
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (ConnectionError, OSError):
                    pass        # the tab was closed mid-request

            def _json(self, obj):
                self._send(json.dumps(obj, ensure_ascii=False).encode(),
                           "application/json")

            def _html(self, text):
                self._send(text.encode(), "text/html; charset=utf-8")

            def _read_json(self):
                n = _coerce_int(self.headers.get("Content-Length"), 0, 0)
                if n <= 0: return {}
                if n > MAX_REQUEST_BYTES:
                    raise ValueError("请求体过大: %d 字节" % n)
                try:
                    self.connection.settimeout(5.0)
                    raw = self.rfile.read(n)
                except Exception:
                    return {}
                if len(raw) != n: return {}
                try:
                    obj = json.loads(raw.decode("utf-8"))
                except Exception:
                    return {}
                return obj if isinstance(obj, dict) else {}

            def _dev(self):
                sched = outer.manager.active()
                if sched is None:
                    return None, {}
                return sched, sched.config

            def _preview(self, name):
                cls = WIDGET_REGISTRY.get(name)
                if not cls:
                    return None
                sched, dcfg = self._dev()
                orientation = sched.orientation if sched else LANDSCAPE
                size = PORTRAIT_SIZE if orientation == PORTRAIT else LANDSCAPE_SIZE
                key = (name, orientation)
                img = None
                with outer._pv_lock:
                    entry = outer._pv.get(key)
                    if entry is None:
                        ctx = RenderContext(device=None, now=time.time(),
                                            orientation=orientation, state={},
                                            config=dict(outer.cfg or {}))
                        inst = None
                        try:
                            inst = cls()
                            inst.setup(ctx)
                        except Exception as e:
                            log.warning("缩略图初始化失败 %s: %s", name, e)
                        entry = {"inst": inst, "ctx": ctx, "last": None}
                        outer._pv[key] = entry
                    inst = entry["inst"]
                    ctx = entry["ctx"]
                    ctx.config = dict(outer.cfg or {})
                    ctx.now = time.time()
                    ctx.orientation = orientation
                    if inst is not None:
                        try:
                            img = inst.render(ctx)
                        except Exception as e:
                            log.warning("缩略图渲染失败 %s: %s", name, e)
                            img = None
                    if img is not None:
                        entry["last"] = img
                    else:
                        img = entry["last"]
                if img is None:
                    img = Image.new("RGBA", size, (0, 0, 0, 255))
                if img.size != size:
                    img = img.resize(size, Image.LANCZOS)
                if img.mode != "RGBA":
                    img = img.convert("RGBA")
                bg = Image.new("RGBA", size, (0, 0, 0, 255))
                bg.alpha_composite(img)
                buf = io.BytesIO()
                bg.convert("RGB").save(buf, format="PNG")
                return buf.getvalue()

            def do_GET(self):
                parsed = urlparse(self.path)
                path = parsed.path

                if path in ("/", "/index.html"):
                    self._html(INDEX_HTML); return

                if path == "/api/devices":
                    self._json({"ok": True,
                                "active": outer.manager.active_id,
                                "devices": outer.manager.describe()}); return

                sched, dcfg = self._dev()

                if path == "/api/status":
                    if sched is None:
                        self._json({"connected": False, "port": "",
                                    "current_widget": None, "widgets": [],
                                    "orientation": LANDSCAPE, "reverse": False,
                                    "locked": False, "active": None,
                                    "devices": [], "touch": dict(_TOUCH_DEFAULTS)})
                        return
                    widgets = sched._widgets
                    cur = sched._current
                    cur_name = widgets[cur].name if 0 <= cur < len(widgets) else None
                    try:
                        _ext = _widgets_pkg.external_report()
                    except Exception:
                        _ext = {}
                    self._json({
                        "ext_dirs": list(_ext.get("dirs") or []),
                        "ext_failed": [list(x) for x in (_ext.get("failed") or [])],
                        "connected": sched.device.connected,
                        "port": sched.device.port,
                        "device": str(dcfg.get("id") or ""),
                        "active": outer.manager.active_id,
                        "devices": outer.manager.describe(),
                        "current_widget": cur_name,
                        "widgets": [w.name for w in widgets],
                        "orientation": sched.orientation,
                        "reverse": sched.reverse,
                        "dim": getattr(sched, "_dim", 0),
                        "locked": sched._locked,
                        "background_enabled": sched._bg_enabled,
                        "background_path": sched._bg_path,
                        "background_paths": list(sched._bg_paths),
                        "background_mode": sched._bg_mode,
                        "background_dim": sched._bg_dim,
                        "background_blur": sched._bg_blur,
                        "background_source": sched._bg_source,
                        "background_interval": int(sched._bg_interval),
                        "background_shuffle": bool(sched._bg_shuffle),
                        "touch": dict(sched._touch_cfg),
                        "metrics": sched.metrics_snapshot(),
                    }); return

                if path == "/api/metrics":
                    if sched is None:
                        self._json({"ok": False, "err": "没有可用设备"}); return
                    data = sched.metrics_snapshot()
                    data["ok"] = True
                    data["port"] = sched.device.port
                    self._json(data); return
                if path == "/api/preview":
                    q = parse_qs(parsed.query)
                    name = (q.get("name") or [""])[0]
                    data = self._preview(name)
                    if data is None:
                        self.send_error(404); return
                    self._send(data, "image/png"); return

                if path == "/api/widgets":
                    self._json({
                        "ok": True,
                        "widgets": [
                            {"name": n,
                             "orientations": list(c.orientations),
                             "interval": c.interval,
                             "priority": c.priority,
                             "has_config": bool(getattr(c, "config_schema", [])),
                             "actions": dict(getattr(c, "actions", {}) or {}),
                             "touch_help": str(getattr(c, "touch_help", "") or ""),
                             "source": "external" if str(getattr(c, "__module__", "")).startswith("msu2_ext_widgets") else "builtin"}
                            for n, c in sorted(WIDGET_REGISTRY.items())
                        ],
                        "active": list(dcfg.get("pages") or []),
                    }); return

                if path == "/api/widget_config":
                    q = parse_qs(parsed.query)
                    name = (q.get("name") or [""])[0]
                    cls = WIDGET_REGISTRY.get(name)
                    if not cls:
                        self._json({"ok": False, "err": "未知插件"}); return
                    obj = cls
                    for w in (sched._widgets if sched else []):
                        if w.name == name:
                            obj = w
                            break
                    schema = getattr(obj, "config_schema", [])
                    user = outer.cfg.get("widget_config", {}).get(name, {})
                    values = merge_config(schema, user)
                    self._json({
                        "ok": True, "name": name,
                        "schema": [f.to_dict() for f in schema],
                        "values": values,
                        "touch_help": str(getattr(obj, "touch_help", "") or ""),
                        "actions": dict(getattr(obj, "actions", {}) or {}),
                    }); return

                if path == "/api/touch_config":
                    tk = dcfg.get("touch", {}) or {}
                    vals = {}
                    for k, dv in _TOUCH_DEFAULTS.items():
                        vals[k] = tk.get(k, dv)
                    self._json({"ok": True, "values": vals}); return

                if path == "/api/dim":
                    self._json({"ok": True,
                                "dim": getattr(sched, "_dim", 0) if sched else 0}); return

                self.send_error(404)

            def do_POST(self):
                try:
                    sched, dcfg = self._dev()
                    if self.path == "/api/select":
                        body = self._read_json()
                        did = str(body.get("id", ""))
                        ok = outer.manager.select(did)
                        self._json({"ok": ok, "active": outer.manager.active_id,
                                    "err": "" if ok else "设备不存在"})
                    elif sched is None:
                        self._json({"ok": False, "err": "没有可用设备"})
                    elif self.path == "/api/next":
                        ok = sched.next_page()
                        self._json({"ok": ok, "err": "" if ok else "冷却中"})
                    elif self.path == "/api/prev":
                        ok = sched.prev_page()
                        self._json({"ok": ok, "err": "" if ok else "冷却中"})
                    elif self.path == "/api/reload":
                        sched.reload(); self._json({"ok": True})
                    elif self.path == "/api/notify":
                        body = self._read_json()
                        text = str(body.get("text", ""))
                        secs = body.get("secs", 5)
                        ok = sched.notify(text, secs)
                        self._json({"ok": ok, "err": "" if ok else "内容为空"})
                    elif self.path == "/api/reverse":
                        ok = sched.toggle_reverse()
                        if ok:
                            dcfg["reverse"] = sched.reverse
                            save_config(outer.cfg, cfg_path)
                        self._json({"ok": ok, "reverse": sched.reverse,
                                    "err": "" if ok else "冷却中"})
                    elif self.path == "/api/orientation":
                        ok = sched.toggle_orientation()
                        if ok:
                            dcfg["orientation"] = sched.orientation
                            save_config(outer.cfg, cfg_path)
                        self._json({"ok": ok, "orientation": sched.orientation,
                                    "err": "" if ok else "冷却中"})
                    elif self.path == "/api/dim":
                        body = self._read_json()
                        v = body.get("dim", 0)
                        sched.set_dim(v)
                        disp = dcfg.get("display")
                        if not isinstance(disp, dict):
                            disp = {}
                            dcfg["display"] = disp
                        disp["dim"] = sched._dim
                        save_config_soon(outer.cfg, cfg_path)   # 拖动时合并写盘
                        self._json({"ok": True, "dim": sched._dim})
                    elif self.path == "/api/pages":
                        body = self._read_json()
                        pages = [p for p in body.get("pages", [])
                                 if p in WIDGET_REGISTRY]
                        applied = sched.set_pages(pages)
                        dcfg["pages"] = applied
                        save_config(outer.cfg, cfg_path)
                        self._json({"ok": True, "pages": applied, "err": ""})
                    elif self.path == "/api/background":
                        body = self._read_json()
                        enabled = bool(body.get("enabled", False))
                        path = str(body.get("path", "") or "")
                        mode = body.get("mode")
                        if mode not in Background.MODES:
                            mode = None
                        paths = body.get("paths")
                        if paths is not None and not isinstance(paths, list):
                            paths = None
                        if paths is not None:
                            paths = [str(p) for p in paths if isinstance(p, str)]
                        dim = body.get("dim")
                        blur = body.get("blur")
                        source = body.get("source")
                        if source not in ("single", "playlist"):
                            source = None
                        interval = body.get("interval")
                        shuffle = body.get("shuffle")
                        bg = dcfg.get("background")
                        if not isinstance(bg, dict):
                            bg = {}
                            dcfg["background"] = bg
                        if not enabled and not path and not paths:
                            path = str(bg.get("path", "") or "")
                        if path:
                            vok, verr = _check_path(path, IMAGE_EXTS,
                                                    MAX_RESOURCE_BYTES)
                            if not vok:
                                self._json({"ok": False, "err": verr}); return
                        for item in paths or []:
                            vok, verr = _check_path(item, IMAGE_EXTS,
                                                    MAX_RESOURCE_BYTES)
                            if not vok:
                                self._json({"ok": False, "err": verr}); return
                        sched.set_background(enabled, path, mode, paths, dim,
                                             blur, source, interval, shuffle)
                        bg["enabled"] = enabled
                        bg["path"] = path
                        if paths is not None:
                            bg["paths"] = paths
                        if mode:
                            bg["mode"] = mode
                        bg["dim"] = sched._bg_dim
                        bg["blur"] = sched._bg_blur
                        bg["source"] = sched._bg_source
                        bg["interval"] = int(sched._bg_interval)
                        bg["shuffle"] = bool(sched._bg_shuffle)
                        save_config_soon(outer.cfg, cfg_path)   # 拖动模糊时合并写盘
                        self._json({"ok": True, "enabled": enabled, "path": path,
                                    "paths": list(sched._bg_paths),
                                    "mode": bg.get("mode", "fit"),
                                    "dim": sched._bg_dim,
                                    "blur": sched._bg_blur,
                                    "source": sched._bg_source,
                                    "interval": int(sched._bg_interval),
                                    "shuffle": bool(sched._bg_shuffle)})
                    elif self.path == "/api/switch":
                        body = self._read_json()
                        name = str(body.get("name", ""))
                        ok = sched.switch_to(name)
                        self._json({"ok": ok, "current": name,
                                    "err": "" if ok else "插件未启用"})
                    elif self.path == "/api/widget_config":
                        body = self._read_json()
                        name = str(body.get("name", ""))
                        values = body.get("values", {}) or {}
                        cls = WIDGET_REGISTRY.get(name)
                        if not cls:
                            self._json({"ok": False, "err": "未知插件"}); return
                        live = sched.find_widget(name)
                        schema = getattr(live or cls, "config_schema", [])
                        if not isinstance(values, dict):
                            values = {}
                        merged = merge_config(schema, values)
                        vok, verr = _check_widget_values(schema, merged)
                        if not vok:
                            self._json({"ok": False, "err": verr}); return
                        outer.cfg.setdefault("widget_config", {})
                        outer.cfg["widget_config"][name] = merged
                        save_config(outer.cfg, cfg_path)
                        for s in outer.manager.schedulers.values():
                            s.restart_widget(name)
                        self._json({"ok": True, "values": merged})
                    elif self.path == "/api/widget_action":
                        body = self._read_json()
                        name = str(body.get("name", ""))
                        action = str(body.get("action", ""))
                        payload = body.get("payload", {}) or {}
                        w = sched.find_widget(name)
                        if not w:
                            self._json({"ok": False,
                                        "err": "插件未启用"}); return
                        try:
                            result = w.on_action(action, payload, sched._ctx_for(w))
                            if not isinstance(result, dict):
                                result = {"ok": True}
                            if result.get("ok"):
                                sched._switch.set()
                            self._json(result)
                        except Exception as e:
                            log.exception("插件 %s on_action 异常", name)
                            self._json({"ok": False, "err": str(e)})
                    elif self.path == "/api/touch_config":
                        body = self._read_json()
                        values = body.get("values", {}) or {}
                        try:
                            sched.set_touch_config(values)
                            save_config(outer.cfg, cfg_path)
                            self._json({"ok": True, "values": sched._touch_cfg})
                        except Exception as e:
                            log.exception("保存触控配置失败")
                            self._json({"ok": False, "err": str(e)})
                    elif self.path == "/api/shutdown":
                        self._json({"ok": True})
                        log.info("收到 Web 关闭指令")
                        if callable(outer.on_shutdown):
                            threading.Timer(
                                0.3, outer.on_shutdown
                            ).start()
                    else:
                        self.send_error(404)
                except Exception as e:
                    log.exception("POST %s 异常", self.path)
                    self._json({"ok": False, "err": str(e)})

        try:
            srv = ThreadingHTTPServer((self.host, self.port), Handler)
            srv.daemon_threads = True
            log.info("Web UI 已启动: http://%s:%d", self.host, self.port)

            def watch():
                while not self._stop.is_set():
                    time.sleep(0.2)
                srv.shutdown()

            threading.Thread(target=watch, daemon=True).start()
            srv.serve_forever(poll_interval=0.3)
            srv.server_close()
        except Exception as e:
            log.warning("Web UI 启动失败: %s", e)

_NEW_WIDGET_TEMPLATE = '''from PIL import Image, ImageDraw

from msu2_core import BOTH, Field, Widget, get_font


class {cls}(Widget):
    name = "{name}"
    interval = 1.0
    orientations = BOTH
    priority = 200

    actions = {{}}
    touch_help = ""

    config_schema = [
        Field("title", "str", "标题", default="{name}"),
    ]

    def render(self, ctx):
        cfg = self.get_config(ctx)
        w, h = ctx.size
        img = Image.new("RGBA", (w, h), (18, 20, 26, 255))
        d = ImageDraw.Draw(img)
        f1 = get_font(12, bold=True, cjk=True)
        f2 = get_font(9, cjk=True)
        d.text((8, h // 2 - 12), str(cfg.get("title") or self.name),
               font=f1, fill=(240, 244, 255, 255))
        d.text((8, h // 2 + 4), "%.1f" % ctx.now, font=f2,
               fill=(140, 160, 200, 255))
        return img
'''


def _widget_class_name(name):
    parts = [p for p in str(name).replace("-", "_").split("_") if p]
    base = "".join(p[:1].upper() + p[1:] for p in parts) or "New"
    if base.endswith("Widget"):
        return base
    return base + "Widget"


def _user_widgets_dir():
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = os.environ.get("XDG_DATA_HOME")
    if not base:
        try:
            base = str(Path.home() / ".local" / "share")
        except Exception:
            return None
    try:
        p = Path(base) / "MSU2_MINI" / "widgets"
        p.mkdir(parents=True, exist_ok=True)
        return p
    except Exception:
        return None


def cmd_new_widget(name):
    raw = str(name or "").strip()
    if not raw:
        _console("请提供一个插件名，例如 --new-widget my_clock")
        return 1
    slug = "".join(c if (c.isalnum() or c == "_") else "_" for c in raw).strip("_")
    if not slug:
        _console("插件名只能包含字母、数字和下划线")
        return 1
    if slug[0].isdigit():
        slug = "w_" + slug
    ext_dirs = []
    try:
        ext_dirs = list(getattr(_widgets_pkg, "_ext_dirs", []) or [])
    except Exception:
        ext_dirs = []
    target_dir = Path(ext_dirs[0]) if ext_dirs else (Path(_APP_DIR) / "widgets_ext")
    if not getattr(sys, "frozen", False) and ("site-packages" in _APP_DIR.parts
                                              or "dist-packages" in _APP_DIR.parts):
        target_dir = _user_widgets_dir() or target_dir
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        _console("无法创建目录 %s: %s" % (target_dir, e))
        return 1
    path = target_dir / (slug + ".py")
    if path.exists():
        _console("文件已存在: %s" % path)
        return 1
    body = _NEW_WIDGET_TEMPLATE.format(cls=_widget_class_name(slug), name=slug)
    try:
        path.write_text(body, encoding="utf-8")
    except Exception as e:
        _console("写入失败: %s" % e)
        return 1
    _console("已生成插件: %s" % path)
    _console("放到该目录后重启程序（或等 2 秒热重载），即可在 Web 页面的“已停用”里找到它")
    return 0

def cmd_list():
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        print("未发现串口。"); return
    for p in ports:
        print(f"{p.device:<30} {p.description}")

def cmd_plugins():
    if not WIDGET_REGISTRY:
        print("未发现任何插件。")
        return
    try:
        ext = _widgets_pkg.external_report()
    except Exception:
        ext = {}
    for folder in ext.get("dirs") or []:
        print(f"外挂插件目录: {folder}")
    if ext.get("failed"):
        print(f"外挂插件加载失败 {len(ext['failed'])} 个: {ext['failed']}")
    print(f"{'NAME':<18} {'ORI':<22} {'PRI':<5} {'SRC':<6} {'DESC'}")
    print("-" * 86)
    rows = sorted(WIDGET_REGISTRY.items(),
                  key=lambda kv: (kv[1].priority, kv[0]))
    for name, cls in rows:
        ori = "/".join(cls.orientations)
        doc = (cls.__doc__ or "").strip().splitlines()
        desc = doc[0].strip() if doc else ""
        src = "外挂" if str(getattr(cls, "__module__", "")).startswith("msu2_ext_widgets") else "内置"
        print(f"{name:<18} {ori:<22} {cls.priority:<5} {src:<6} {desc}")

def _setup_stdio():
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def run_offline_window(manager, stop_event, device_id=None):
    try:
        import tkinter as tk
        from PIL import ImageTk
    except Exception as e:
        _console("模拟屏不可用（需要 tkinter 与 Pillow）: %s" % e)
        return 1
    root = tk.Tk()
    root.title("MSU2_MINI 模拟屏")
    root.resizable(False, False)
    canvas = tk.Canvas(root, width=SCREEN_W * 3, height=SCREEN_H * 3,
                       bg="black", highlightthickness=0, bd=0)
    canvas.pack()
    bar = tk.Frame(root)
    bar.pack(fill="x")
    holder = {"photo": None}

    def pick():
        if device_id:
            return manager.schedulers.get(device_id) or manager.active()
        return manager.schedulers.get('sim') or manager.active()

    def act(fn):
        def _run():
            s = pick()
            if s is not None:
                fn(s)
        return _run

    tk.Button(bar, text="单击",
              command=act(lambda s: s._dispatch_touch("single"))).pack(side="left")
    tk.Button(bar, text="双击",
              command=act(lambda s: s._dispatch_touch("double"))).pack(side="left")
    tk.Button(bar, text="长按",
              command=act(lambda s: s._dispatch_touch("long"))).pack(side="left")
    tk.Button(bar, text="关闭", command=stop_event.set).pack(side="right")

    def tick():
        sched = pick()
        dev = getattr(sched, "device", None) if sched is not None else None
        img = getattr(dev, "last", None) if dev is not None else None
        if img is not None:
            img = img.convert("RGB")
            w, h = img.size
            scale = 3
            if (canvas.winfo_width(), canvas.winfo_height()) != (w * scale, h * scale):
                canvas.config(width=w * scale, height=h * scale)
            photo = ImageTk.PhotoImage(
                img.resize((w * scale, h * scale), Image.NEAREST))
            holder["photo"] = photo
            canvas.delete("all")
            canvas.create_image(0, 0, anchor="nw", image=photo)
        if stop_event.is_set():
            try:
                root.destroy()
            except Exception:
                pass
            return
        root.after(60, tick)

    root.protocol("WM_DELETE_WINDOW", stop_event.set)
    root.after(60, tick)
    root.mainloop()
    return 0


def cmd_selftest(port=None):
    """--selftest：不开界面，直接体检设备（握手 / 版本 / 触摸 / 整帧链路 / 逐帧回执）。"""
    names = [port] if port else [p.device for p in serial.tools.list_ports.comports()]
    if not names:
        _console("没有找到串口设备")
        return 2
    bad = 0
    for name in names:
        _console("== %s ==" % name)
        dev = MiniDevice(name)
        if not dev.open():
            _console("  [FAIL] 打不开或握手失败: %s"
                     % (dev.transport.last_error or "设备没有回问候包"))
            bad += 1
            continue
        tr = dev.transport
        fw = tr.fw_version
        _console("  握手 %s，raw_mode=%s" % (tr.version or "?", tr.raw_mode))
        if fw:
            _console("  固件 %d.%d" % fw)
        else:
            _console("  固件版本未知（v8 及更早没有版本帧）")

        # 1) 触摸：连读 10 次，看有没有非 0 值
        vals = []
        for _ in range(10):
            v = dev.read_touch()
            if v is not None:
                vals.append(v)
            time.sleep(0.03)
        vals = [v for v in vals if v]
        if vals:
            _console("  触摸 %d..%d（%d 次采样）" % (min(vals), max(vals), len(vals)))
        else:
            _console("  [WARN] 触摸读不到有效值")

        # 2) 链路：静态帧、局部变化、整屏噪点各发几帧
        img = Image.new("RGB", (SCREEN_W, SCREEN_H), (16, 20, 28))
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, SCREEN_W - 1, SCREEN_H - 1], outline=(0, 255, 0))
        d.line([0, 0, SCREEN_W - 1, SCREEN_H - 1], fill=(0, 120, 255))
        ok = 0
        for i in range(4):
            d.rectangle([8 + i * 6, 20, 20 + i * 6, 32], outline=(255, 255, 0))
            if dev.show_image(img):
                ok += 1
        _console("  局部变化 4 帧：成功 %d，最近一次 %d ms / %d B"
                 % (ok, int(getattr(dev, "tx_ms", 0) or 0),
                    int(getattr(dev, "frame_bytes", 0) or 0)))

        noise = Image.new("RGB", (SCREEN_W, SCREEN_H))
        npx = noise.load()
        rnd = random.Random(7)
        for y in range(SCREEN_H):
            for x in range(SCREEN_W):
                npx[x, y] = (rnd.randrange(256), rnd.randrange(256),
                             rnd.randrange(256))
        ok_full = sum(1 for _ in range(3) if dev.show_image(noise))
        _console("  整屏噪点 3 帧：成功 %d，最近一次 %d ms / %d B"
                 % (ok_full, int(getattr(dev, "tx_ms", 0) or 0),
                    int(getattr(dev, "frame_bytes", 0) or 0)))

        if ok == 4 and ok_full == 3:
            _console("  [PASS] 链路与回执正常")
        else:
            _console("  [FAIL] 有帧没有确认——检查 USB 线、或换短一点的线")
            bad += 1
        dev.close()
    return 0 if bad == 0 else 1


def _data_dir():
    """放日志与崩溃记录的地方；装到只读目录时退回用户目录。"""
    bases = []
    if os.name == "nt":
        la = os.environ.get("LOCALAPPDATA")
        if la:
            bases.append(Path(la) / "MSU2_MINI")
    bases.append(Path.home() / ".msu2_mini")
    for p in bases:
        try:
            p.mkdir(parents=True, exist_ok=True)
            probe = p / ".writable"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
            return p
        except Exception:
            continue
    return Path(tempfile.gettempdir())


_FAULT_FILE = None


def _install_crash_handlers(data_dir):
    """Python 未捕获异常 + 原生崩溃（0xc0000374 这种）都落盘，别让窗口一关什么都不剩。"""
    global _FAULT_FILE
    try:
        _FAULT_FILE = open(data_dir / "crash.log", "a", buffering=1,
                           encoding="utf-8", errors="replace")
        _FAULT_FILE.write("\n===== %s 启动 pid=%d =====\n"
                          % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid()))
        faulthandler.enable(file=_FAULT_FILE, all_threads=True)
    except Exception:
        _FAULT_FILE = None

    def _hook(exc_type, exc, tb):
        try:
            log.critical("未捕获异常", exc_info=(exc_type, exc, tb))
        except Exception:
            pass
        try:
            if _FAULT_FILE:
                _FAULT_FILE.write("\n--- 未捕获异常 ---\n")
                traceback.print_exception(exc_type, exc, tb, file=_FAULT_FILE)
                _FAULT_FILE.flush()
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _hook

    def _thook(a):
        try:
            log.error("线程 %s 抛出未捕获异常",
                      getattr(a.thread, "name", "?"),
                      exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
        except Exception:
            pass

    threading.excepthook = _thook


def main(argv=None):
    _setup_stdio()
    data_dir = _data_dir()
    _install_crash_handlers(data_dir)
    ap = argparse.ArgumentParser(description="MSU2_MINI_Console")
    ap.add_argument("--config")
    ap.add_argument("--init-config", action="store_true")
    ap.add_argument("--port")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--new-widget", default=None,
                    help="生成一个外挂插件骨架后退出")
    ap.add_argument("--version", action="version",
                    version="MSU2_MINI %s" % __version__)
    ap.add_argument("--simulate", "--offline", dest="simulate",
                    action="store_true", help="额外开一个模拟屏窗口")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--selftest", action="store_true",
                    help="连上设备做自检（握手/版本/触摸/整帧链路/回执）后退出")
    ap.add_argument("--plugins", action="store_true",
                    help="列出所有已发现的插件后退出")
    ap.add_argument("--log", default=None)
    ap.add_argument("--log-level", default=None)
    ap.add_argument("--log-file", default=str(data_dir / "console.log"),
                    help="日志文件（默认 %s；传 none 关闭）" % (data_dir / "console.log"))
    args = ap.parse_args(argv)

    if args.list:
        cmd_list(); return 0
    if args.plugins:
        cmd_plugins(); return 0
    if args.new_widget:
        return cmd_new_widget(args.new_widget)
    if args.selftest:
        return cmd_selftest(args.port)

    stream = sys.stderr
    if stream is None:
        stream = sys.stdout
    if stream is None:
        try:
            stream = open(os.devnull, "w", encoding="utf-8")
        except Exception:
            stream = None
    handlers = [logging.StreamHandler(stream)]
    if args.log_file and str(args.log_file).lower() not in ("none", "off", "-"):
        try:
            handlers.append(logging.handlers.RotatingFileHandler(
                args.log_file, maxBytes=2_000_000, backupCount=3,
                encoding="utf-8"))
        except Exception:
            _console("无法写入日志文件: %s" % args.log_file)

    logging.basicConfig(
        level=getattr(logging, (args.log or args.log_level or "INFO").upper(),
                      logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )

    cfg_path = args.config or _default_config_path()
    if args.init_config:
        p = Path(cfg_path)
        if p.exists():
            try: p.unlink()
            except Exception: pass

    cfg = ensure_config_file(cfg_path)
    level_name = (args.log or args.log_level
                  or cfg.get("log_level", "INFO")).upper()
    logging.getLogger().setLevel(getattr(logging, level_name, logging.INFO))

    fi = probe_fonts()
    log.info("字体目录: %s", fi["font_dirs"])
    log.info("常规字重: %s", fi["cjk"])
    log.info("粗体字重: %s", fi["latin"])
    if cfg.get("font"):
        set_user_font(cfg["font"])

    log.info("已注册插件: %s", list(WIDGET_REGISTRY.keys()))
    try:
        ext = _widgets_pkg.external_report()
    except Exception:
        ext = {}
    for folder in ext.get("dirs") or []:
        log.info("外部插件目录: %s", folder)
    if ext.get("loaded"):
        log.info("外部插件加载 %d 个: %s", len(ext["loaded"]),
                 ", ".join(ext["loaded"]))
    if ext.get("failed"):
        log.warning("外部插件加载失败 %d 个: %s", len(ext["failed"]),
                    ext["failed"])

    if args.port:
        devs = cfg.get("devices") or []
        if devs and isinstance(devs[0], dict):
            devs[0]["port"] = args.port

    stop_event = threading.Event()

    def _save():
        save_config(cfg, cfg_path)

    def _request_shutdown():
        log.info("收到关闭请求，准备退出…")
        try:
            _save()
        except Exception:
            pass
        stop_event.set()

    manager = DeviceManager(cfg, cfg_path, simulate=bool(args.simulate))
    if not manager.order:
        log.error("配置里没有任何设备")
        return 2
    for did in manager.order:
        s = manager.schedulers[did]
        s._on_shutdown = _request_shutdown
        log.info("设备 %s -> %s", did, s.device.port or "auto")
    manager.start()
    sim_id = None
    if args.simulate:
        for did in manager.order:
            if getattr(manager.schedulers[did].device, 'port', '') == 'offline':
                sim_id = did
                break

    web = None
    if not args.headless:
        web = WebUI(manager, cfg, cfg_path, on_shutdown=_request_shutdown)
        web.start()
    else:
        log.info("无头模式: Ctrl+C 退出")

    def _sig(*_):
        stop_event.set()

    signal.signal(signal.SIGINT, _sig)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _sig)

    try:
        if sim_id:
            run_offline_window(manager, stop_event, sim_id)
        while not stop_event.is_set():
            time.sleep(0.2)
    finally:
        log.info("正在退出…")
        try: flush_config_saves()
        except Exception: pass
        try: _save()
        except Exception: pass
        manager.stop()
        if web: web.stop()
        for did in manager.order:
            try:
                manager.schedulers[did].device.close()
            except Exception:
                pass
    return 0

if __name__ == "__main__":
    sys.exit(main())
