"""离线测试：握手、串口同步、RLE 待机淡入与定界填充规则。

运行：python -X utf8 -m unittest discover -s tests -v

这里全部用假串口/纯 Python 参考模型，不依赖真机；固件源码检查在找不到
固件仓库时自动跳过（设置 MSU2_FW_DIR 可指定路径）。
"""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("MSU2_NO_PYTHON", "1")

import msu2_mini as M  # noqa: E402
from msu2_core.protocol import (  # noqa: E402
    END_FRAME, rle_decode_rgb565, rle_encode_rgb565)

FW_DIR = os.environ.get(
    "MSU2_FW_DIR",
    os.path.join(os.path.dirname(ROOT), "Open_MSU2_MINI_FW"))


class ScriptSerial:
    """假串口：可以预置接收字节，并按写入内容自动补版本回包。

    只实现 transport.py 用到的接口：write/read/in_waiting/reset_input_buffer/
    flush/close。所有写入按顺序记进 writes，方便断言“谁是第一笔”。
    """

    def __init__(self, rx=b"", answer_version=True):
        self.rx = bytearray(rx)
        self.writes = []
        self.flushed = False
        self.closed = False
        self.answer_version = answer_version

    @property
    def in_waiting(self):
        return len(self.rx)

    def read(self, n=1):
        take = min(int(n or 0), len(self.rx))
        out = bytes(self.rx[:take])
        del self.rx[:take]
        return out

    def write(self, data):
        if self.closed:
            raise OSError("port is closed")
        data = bytes(data)
        self.writes.append(data)
        if self.answer_version and data.startswith(b"\x00\x56"):
            # 7 字节版本帧：56 01 00 05 00 1E AA（版本 1.0.5，触摸读数 0x001E）
            self.rx.extend(bytes([0x56, 1, 0, 5, 0x00, 0x1E, 0xAA]))
        return len(data)

    def reset_input_buffer(self):
        self.rx.clear()

    def flush(self):
        self.flushed = True

    def close(self):
        self.closed = True


def make_transport(rx=b"", answer_version=True):
    tr = M.SerialTransport("COM_fake")
    ser = ScriptSerial(rx=rx, answer_version=answer_version)
    tr._ser = ser
    tr._connected = True
    return tr, ser


class OpmsuHandshakeTests(unittest.TestCase):
    """握手：设备发 \0OPMSU、主机回 \0OKMSU，版本只从 0x56 帧取。"""

    def test_opmsu_replies_okmsu_and_leaves_version_empty(self):
        tr, ser = make_transport(rx=b"\x00OPMSU")
        self.assertTrue(tr.handshake(timeout=0.3))
        self.assertEqual(ser.writes, [b"\x00OKMSU"])
        self.assertTrue(tr.raw_mode)
        self.assertEqual(tr.version, "", "OPMSU 里没有版本数字，version 必须留空")
        self.assertTrue(tr._handshaked)

    def test_version_comes_from_056_frame_after_handshake(self):
        tr, ser = make_transport(rx=b"\x00OPMSU")
        self.assertTrue(tr.open())
        self.assertEqual(tr.version, "")
        self.assertEqual(tr.fw_version, (1, 0, 5))
        # 打开端口后先只听问候，第一笔写必须是握手回复，然后才是版本查询
        self.assertEqual(ser.writes[0], b"\x00OKMSU")
        self.assertEqual(ser.writes[1], bytes([0x00, 0x56, 0, 0, 0, 0]))

    def test_half_frame_before_hello_does_not_break_handshake(self):
        # 上一次写到一半留下的 3 个残字节，后面跟着设备真正打招呼：按子串
        # 查找照样能认出来，不用拔插。
        tr, ser = make_transport(rx=b"\x00\x56\x00" + b"\x00OPMSU")
        self.assertTrue(tr.handshake(timeout=0.3))
        self.assertEqual(ser.writes, [b"\x00OKMSU"])

    def test_no_greeting_times_out_without_writing(self):
        tr, ser = make_transport(rx=b"\x00\x56\x00")
        self.assertFalse(tr.handshake(timeout=0.1))
        self.assertEqual(ser.writes, [])


class SyncPadTests(unittest.TestCase):
    """打开端口/重同步：先垫 6 个 0x00，让定界重新对齐。"""

    def test_open_writes_nothing_before_the_greeting(self):
        # 打开端口后只听不说：问候来了才回 OKMSU，不先写对齐垫。
        tr, ser = make_transport(rx=b"\x00OPMSU")
        self.assertTrue(tr.open())
        self.assertEqual(ser.writes[0], b"\x00OKMSU",
                         "第一笔必须是握手回复，不能先灌对齐垫")

    def test_resync_pads_before_empty_frame(self):
        tr, ser = make_transport()
        self.assertTrue(tr._connected)
        tr._handshaked = True
        tr.resync(timeout=0.05)
        self.assertEqual(ser.writes, [b"\x00" * 6, END_FRAME],
                         "重同步顺序：先 6 个 0x00，再 0 像素空帧")

    def test_close_still_flushes(self):
        # 回归：close() 前 flush 的老行为不能因为改动丢掉
        tr, ser = make_transport()
        tr._handshaked = True
        tr.close()
        self.assertTrue(ser.flushed)
        self.assertTrue(ser.closed)


class FwRleReader:
    """固件 rle_next_byte/rle_next_pixel 的逐字节参考模型。

    特意按 64 字节一块从 payload 里取数，模拟 assets.c 的 RLE_CHUNK 行为，
    这样“跨带子保留解码游标”才有意义：如果每带子重新从头解，结果会错位。
    """

    def __init__(self, payload):
        self.payload = bytes(payload)
        self.addr = 0
        self.left = len(self.payload)
        self.buf = b""
        self.pos = 0
        self.cnt = 0
        self.hi = 0
        self.lo = 0

    def _next_byte(self):
        if self.pos >= len(self.buf):
            if self.left <= 0:
                return None
            n = min(self.left, 64)
            self.buf = self.payload[self.addr:self.addr + n]
            self.addr += n
            self.left -= n
            self.pos = 0
        val = self.buf[self.pos]
        self.pos += 1
        return val

    def next_pixel(self):
        while self.cnt == 0:
            cnt = self._next_byte()
            if cnt is None:
                return None
            hi = self._next_byte()
            lo = self._next_byte()
            if hi is None or lo is None:
                return None
            self.cnt, self.hi, self.lo = cnt, hi, lo
        self.cnt -= 1
        return self.hi, self.lo

    def band(self, pixels=320):
        out = bytearray()
        for _ in range(pixels):
            px = self.next_pixel()
            if px is None:
                return None
            out += bytes(px)
        return bytes(out)


def gradient_rgb565(w=160, h=80):
    out = bytearray()
    for y in range(h):
        for x in range(w):
            val = (((x * 4 + y * 3) & 0xFF) << 8) | ((x * 2 + y * 5) & 0xFF)
            out += bytes([val >> 8, val & 0xFF])
    return bytes(out)


def mix_rows(a, b, wa, wb):
    """复刻固件 fade_rows()：每像素 RGB565 分量加权 (wa+wb)==8。"""
    out = bytearray()
    for i in range(0, len(a), 2):
        pa = (a[i] << 8) | a[i + 1]
        pb = (b[i] << 8) | b[i + 1]
        r = (((pa >> 11) & 0x1F) * wa + ((pb >> 11) & 0x1F) * wb) >> 3
        g = (((pa >> 5) & 0x3F) * wa + ((pb >> 5) & 0x3F) * wb) >> 3
        bl = ((pa & 0x1F) * wa + (pb & 0x1F) * wb) >> 3
        out += bytes([((r << 3) | (g >> 3)) & 0xFF,
                      ((g << 5) | bl) & 0xFF])
    return bytes(out)


def component_distance(a, b):
    """按 RGB565 三个分量算距离；不能直接比字节，字节低位不是线性亮度。"""
    total = 0
    for i in range(0, len(a), 2):
        pa = (a[i] << 8) | a[i + 1]
        pb = (b[i] << 8) | b[i + 1]
        total += abs(((pa >> 11) & 0x1F) - ((pb >> 11) & 0x1F))
        total += abs(((pa >> 5) & 0x3F) - ((pb >> 5) & 0x3F))
        total += abs((pa & 0x1F) - (pb & 0x1F))
    return total


class RleFadeTests(unittest.TestCase):
    """fmt=1 待机图按 2 行带子流式解压再混色。"""

    def setUp(self):
        self.raw = gradient_rgb565()
        self.payload = rle_encode_rgb565(self.raw)
        self.assertEqual(rle_decode_rgb565(self.payload), self.raw,
                         "参考模型自身先要能还原整图")

    def test_bandwise_decode_keeps_rle_cursor(self):
        reader = FwRleReader(self.payload)
        bands = []
        while True:
            band = reader.band()
            if band is None:
                break
            bands.append(band)
        self.assertEqual(len(bands), 40, "160x80 每带 2 行，共 40 带")
        self.assertEqual(b"".join(bands), self.raw,
                         "跨带子保留游标时，逐带拼接必须等于整图")
        self.assertEqual(reader.left, 0)

    def test_restarting_decode_each_band_would_be_wrong(self):
        # 反证：每带子都从头解，第 2 带起就不是原图（说明游标必须跨带保留）
        reader = FwRleReader(self.payload)
        first = reader.band()
        again = FwRleReader(self.payload).band()
        self.assertEqual(first, again)
        self.assertNotEqual(again, self.raw[640:1280])

    def test_black_fade_darkens_monotonically_to_target(self):
        reader = FwRleReader(self.payload)
        target = reader.band()
        black = bytes(640)
        dist = []
        for step in range(1, 9):
            mixed = mix_rows(black, target, 8 - step, step)
            dist.append(component_distance(mixed, target))
        self.assertEqual(mix_rows(black, target, 0, 8), target,
                         "第 8 步必须完全等于目标帧（淡入收尾）")
        self.assertGreater(dist[0], 0, "第一步还没混色就说明是硬切")
        self.assertEqual(dist[-1], 0)
        for prev, cur in zip(dist, dist[1:]):
            self.assertLessEqual(cur, prev, "每步都应该不比上一步更远（混色而不是硬切）")
        self.assertGreater(len(set(dist)), 4, "8 步应有多个不同亮度，不能是两级硬切")

    def test_truncated_payload_reports_band_failure(self):
        reader = FwRleReader(self.payload[:len(self.payload) // 3])
        ok = 0
        while reader.band() is not None:
            ok += 1
        self.assertLess(ok, 40, "数据不够时 asset_fade_band() 必须返回 0 触发回退")


class FwFrameParser:
    """固件主循环里“空闲跳填充 + 6 字节定界”的参考模型。

    只实现本测试需要的两条：完全空闲时连续 ≥8 个 0x00 按 6 的整数倍跳过
    （多余的 0 留给下一帧），其它字节每 6 个一帧。
    """

    def __init__(self):
        self.rx = bytearray()

    def feed(self, data):
        self.rx.extend(data)

    def frames(self):
        out = []
        pos = 0
        while pos < len(self.rx):
            if self.rx[pos] == 0x00:
                z = 0
                while pos + z < len(self.rx) and self.rx[pos + z] == 0x00:
                    z += 1
                if z >= 8:
                    pos += z - (z % 6)
                    continue
            if len(self.rx) - pos >= 6:
                out.append(bytes(self.rx[pos:pos + 6]))
                pos += 6
                continue
            break
        del self.rx[:pos]
        return out


class FwPadRuleTests(unittest.TestCase):
    """固件侧填充规则：怎么垫都不能把 6 字节定界推错位。"""

    QUERY = bytes([0x00, 0x56, 0, 0, 0, 0])

    def test_single_pad_then_query_is_aligned(self):
        p = FwFrameParser()
        p.feed(b"\x00" * 6 + self.QUERY)
        frames = p.frames()
        self.assertEqual(frames[-1], self.QUERY, "垫 6 个 0 之后查询帧必须对齐")
        self.assertEqual(len(frames), 2)

    def test_double_pad_then_query_is_aligned(self):
        # 两次开端口各垫 6 个 0（12 个），再紧跟以 0x00 开头的查询帧：
        # 总共 13 个 0 ≥ 8，按 6 的倍数丢掉 12 个填充，剩下那个 0 属于
        # 查询帧，所以查询帧仍然完整对齐。
        p = FwFrameParser()
        p.feed(b"\x00" * 12 + self.QUERY)
        self.assertEqual(p.frames(), [self.QUERY])

    def test_partial_frame_waits_for_more_bytes(self):
        p = FwFrameParser()
        p.feed(b"\x00\x56\x00")
        self.assertEqual(p.frames(), [])
        p.feed(bytes([0, 0, 0]))
        self.assertEqual(p.frames(), [self.QUERY])


class FirmwareSourceTests(unittest.TestCase):
    """固件侧改动的静态守门（找不到固件仓库时跳过）。"""

    def setUp(self):
        if not os.path.isdir(FW_DIR):
            self.skipTest("找不到固件仓库：%s" % FW_DIR)
        self.fw = FW_DIR

    def read(self, rel):
        with open(os.path.join(self.fw, rel), encoding="utf-8") as fh:
            return fh.read()

    def test_hello_is_opmsu(self):
        src = self.read(os.path.join("src", "main_proto.c"))
        self.assertIn("static const uint8_t hello[6] = "
                      "{ 0x00, 0x4F, 0x50, 0x4D, 0x53, 0x55 };", src)
        self.assertIn("设备发 OPMSU，主机回 OKMSU", src)
        self.assertNotIn("0x00, 0x4D, 0x53, 0x4E, 0x30, 0x32", src)

    def test_version_matches_console(self):
        # 固件 patch 号必须和上位机 __version__ 的 patch 号一致
        import msu2_mini as _m
        src = self.read(os.path.join("src", "fw.h"))
        m = re.search(r"#define\s+FW_VER_PATCH\s+(\d+)u", src)
        self.assertIsNotNone(m, "fw.h 里找不到 FW_VER_PATCH")
        want = int(_m.__version__.split(".")[2])
        self.assertEqual(int(m.group(1)), want,
                         "固件 FW_VER_PATCH 与上位机版本不一致")

    def test_resync_guards_exist(self):
        src = self.read(os.path.join("src", "main_proto.c"))
        self.assertTrue(re.search(r"#define\s+RX_IDLE_FLUSH_MS\s+100u", src))
        self.assertTrue(re.search(r"#define\s+RX_PAD_RUN\s+8\b", src))
        self.assertIn("if (usb_reset_count != rx_reset_seen)", src)
        self.assertIn("RX_IDLE_FLUSH_MS * 48000u", src)
        self.assertIn("z >= RX_PAD_RUN", src)
        self.assertIn("pos += z - (z % 6);", src,
                      "跳填充只能按 6 的整数倍，否则会把边界推错一位")

    def test_fmt1_fade_path_exists(self):
        src = self.read(os.path.join("src", "main_proto.c"))
        assets = self.read(os.path.join("src", "assets.c"))
        self.assertIn("asset_fade_open(0u, ASSET_STANDBY, from", src)
        self.assertIn("asset_fade_band(0u, fade_a)", src)
        self.assertIn("asset_fade_band(1u, fade_b)", src)
        self.assertIn("void asset_fade_open(", assets)
        self.assertIn("int asset_fade_band(", assets)
        self.assertIn("fade_lane[2]", assets)


if __name__ == "__main__":
    unittest.main(verbosity=2)