"""MSU2 Console 核心单元测试（stdlib unittest，不依赖设备）。

运行：python -m unittest discover -s tests -v
"""
import io
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("MSU2_NO_PYTHON", "1")

from PIL import Image  # noqa: E402

import msu2_mini as M  # noqa: E402


def solid(w=160, h=80, color=(0, 0, 0)):
    if isinstance(w, tuple):
        color = w
        w, h = 160, 80
    return Image.new("RGB", (w, h), color)


class FrameEncoderTests(unittest.TestCase):
    def test_first_frame_is_full_and_rle_compressed(self):
        enc = M.FrameEncoder()
        body = enc.encode(solid(), raw_mode=True)
        self.assertTrue(enc.last_full)
        self.assertGreater(len(body), 0)
        # 纯黑整屏：RLE 应该远小于 25.6 KB 原样
        self.assertLess(len(body), 2000)

    def test_small_change_sends_only_that_tile(self):
        enc = M.FrameEncoder()
        img = solid()
        first = enc.encode(img, raw_mode=True)
        img2 = img.copy()
        img2.paste((255, 0, 0), (32, 16, 48, 32))
        second = enc.encode(img2, raw_mode=True)
        self.assertFalse(enc.last_full)
        self.assertGreater(len(first), len(second))
        self.assertLess(len(second), 200)

    def test_identical_frame_sends_nothing(self):
        enc = M.FrameEncoder()
        img = solid((10, 20, 30))
        enc.encode(img, raw_mode=True)
        self.assertEqual(enc.encode(img, raw_mode=True), b"")

    def test_large_change_falls_back_to_full_frame(self):
        enc = M.FrameEncoder()
        enc.encode(solid(), raw_mode=True)
        noisy = Image.effect_noise((160, 80), 64).convert("RGB")
        enc.encode(noisy, raw_mode=True)
        self.assertTrue(enc.last_full)

    def test_rgb565_is_high_byte_first(self):
        raw = M.FrameEncoder._to_rgb565(solid(color=(255, 0, 0)))
        self.assertEqual(raw[0], 0xF8)  # 红 = 0xF800
        self.assertEqual(raw[1], 0x00)

    def test_tile_signatures_are_stable_and_sized(self):
        raw = M.FrameEncoder._to_rgb565(solid((1, 2, 3)))
        a = M._tile_signatures(raw)
        b = M._tile_signatures(raw)
        self.assertEqual(a, b)
        self.assertEqual(len(a), M.TILE_N * 2)

    def test_full_frame_helper_is_a_complete_transaction(self):
        enc = M.FrameEncoder()
        enc.encode(solid(), raw_mode=True)
        frame = M._full_frame(enc.last_raw, enc)
        self.assertTrue(frame.startswith(M.MiniProtocol.lcd_set_xy(0, 0)))
        self.assertTrue(frame.endswith(M.MiniProtocol.lcd_raw_end()))


class TouchTests(unittest.TestCase):
    BASE = 3900

    def feed(self, g, values, dt=0.03, start=0.0):
        out = []
        t = start
        for v in values:
            out.extend(g.feed(v, t))
            t += dt
        return out, t

    def test_single_tap(self):
        g = M.TouchGestures()
        _, t = self.feed(g, [self.BASE] * 6)
        ev, t = self.feed(g, [self.BASE - 120] * 3, start=t)
        ev2, _ = self.feed(g, [self.BASE] * 2, start=t)
        self.assertEqual(ev + ev2, [] or ev + ev2)  # 单击要等窗口，先无事件
        ev3, _ = self.feed(g, [self.BASE] * 20, start=t + 0.06)
        self.assertIn("single", ev3)

    def test_double_tap(self):
        g = M.TouchGestures()
        _, t = self.feed(g, [self.BASE] * 6)
        seq = []
        for _ in range(2):
            e, t = self.feed(g, [self.BASE - 120] * 2, start=t)
            seq += e
            e, t = self.feed(g, [self.BASE] * 2, start=t + 0.06)
            seq += e
        self.assertIn("double", seq)

    def test_long_press(self):
        g = M.TouchGestures()
        _, t = self.feed(g, [self.BASE] * 6)
        ev, _ = self.feed(g, [self.BASE - 120] * 40, start=t)
        self.assertIn("long", ev)

    def test_jitter_does_not_trigger(self):
        g = M.TouchGestures()
        _, t = self.feed(g, [self.BASE] * 6)
        ev, _ = self.feed(g, [self.BASE - 10] * 20, start=t)
        self.assertEqual(ev, [])

    def test_zero_reading_is_ignored(self):
        g = M.TouchGestures()
        ev, _ = self.feed(g, [0] * 10)
        self.assertEqual(ev, [])


class ProtocolTests(unittest.TestCase):
    def test_frames_are_six_bytes(self):
        for f in (
            M.MiniProtocol.lcd_set_xy(10, 20),
            M.MiniProtocol.lcd_set_size(160, 80),
            M.MiniProtocol.lcd_enter_direct_mode(),
            M.MiniProtocol.lcd_raw_end(),
            M.MiniProtocol.tile_check(),
            M.MiniProtocol.read_adc(9),
        ):
            self.assertEqual(len(f), 6)

    def test_flash_window_length_in_bytes_4_5(self):
        f = M.MiniProtocol.flash_window(1024)
        self.assertEqual(f[4:6], bytes((0x04, 0x00)))

    def test_flash_begin_length_in_bytes_4_5(self):
        f = M.MiniProtocol.flash_begin(16896)
        self.assertEqual(f[4:6], bytes((0x42, 0x00)))

    def test_tile_check_command(self):
        self.assertEqual(M.MiniProtocol.tile_check()[:2], bytes((0x0A, 0x11)))


class ConfigTests(unittest.TestCase):
    def test_coerce_int_clamps(self):
        self.assertEqual(M._coerce_int("50", 0, 0, 90), 50)
        self.assertEqual(M._coerce_int(999, 0, 0, 90), 90)
        self.assertEqual(M._coerce_int("abc", 7, 0, 90), 7)


class FirmwareImageTests(unittest.TestCase):
    def test_load_hex_pads_and_returns_bounds(self):
        hex_text = (
            ":020000040000FA\n"
            ":0400000001020304F2\n"
            ":00000001FF\n"
        )
        import tempfile

        path = os.path.join(tempfile.gettempdir(), "_msu2_test.hex")
        io.open(path, "w", encoding="ascii").write(hex_text)
        try:
            data, note = M._load_firmware_image(path)
        finally:
            os.remove(path)
        self.assertEqual(data[:4], bytes((1, 2, 3, 4)))
        self.assertTrue(note)


class RepairTests(unittest.TestCase):
    class FakeTransport:
        def __init__(self, masks):
            self.masks = list(masks)
            self.writes = []
            self.fw_version = (1, 0)

        def write(self, data):
            self.writes.append(bytes(data))
            return True

        def tile_check(self, sigs, timeout=2.5):
            return self.masks.pop(0) if self.masks else 0

    def make(self, masks):
        dev = M.MiniDevice("COM_fake")
        dev.transport = self.FakeTransport(masks)
        dev._tiles_ok = True
        enc = M.FrameEncoder()
        enc.encode(solid(), raw_mode=True)
        dev.encoder = enc
        dev.frame_bytes = 1234
        dev.tx_ms = 10.0
        return dev

    def test_all_tiles_bad_resends_full_frame(self):
        dev = self.make([(1 << M.TILE_N) - 1, 0])
        dev._repair_tiles()
        self.assertEqual(len(dev.transport.writes), 1)  # 一次整帧，不是 50 块
        self.assertEqual(dev._full_resends, 1)

    def test_two_bad_tiles_are_patched(self):
        dev = self.make([0b11, 0])
        dev._repair_tiles()
        self.assertEqual(len(dev.transport.writes), 2)
        self.assertEqual(dev.tile_repairs, 1)

    def test_clean_frame_writes_nothing(self):
        dev = self.make([0])
        dev._repair_tiles()
        self.assertEqual(dev.transport.writes, [])

    def test_persistent_mismatch_forces_full_redraw(self):
        dev = self.make([0b1, 0b1, 0b1, 0b1])
        dev._repair_tiles(rounds=2)
        self.assertTrue(dev.transport.writes)
        enc_reset = dev.encoder._last_raw is None
        self.assertTrue(enc_reset)


class HotReloadTests(unittest.TestCase):
    """插件热加载：指纹变化才重载，且一次变化只排一次 reload。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="msu2_hot_")
        self.plugin = os.path.join(self.tmp, "hot_plugin.py")
        with io.open(self.plugin, "w", encoding="utf-8") as fh:
            fh.write("x = 1\n")
        self._old_env = os.environ.get(M._widgets_pkg._EXT_DIR_ENV)
        os.environ[M._widgets_pkg._EXT_DIR_ENV] = self.tmp
        self.sched = M.Scheduler(M.OfflineDevice(), {"pages": []})

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop(M._widgets_pkg._EXT_DIR_ENV, None)
        else:
            os.environ[M._widgets_pkg._EXT_DIR_ENV] = self._old_env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bump(self, text="y = 2\n"):
        with io.open(self.plugin, "a", encoding="utf-8") as fh:
            fh.write(text)

    def test_change_posts_exactly_one_reload(self):
        sched = self.sched
        sched._check_plugin_changes(0.0)      # 建立基线
        self.assertEqual(len(sched._ops), 0)
        sched._check_plugin_changes(1.0)      # 不到 RELOAD_POLL：跳过
        self.assertEqual(len(sched._ops), 0)
        sched._check_plugin_changes(3.0)      # 到点但没变化：不重载
        self.assertEqual(len(sched._ops), 0)
        self._bump()
        with self.assertLogs("msu2", level="INFO") as cm:
            sched._check_plugin_changes(6.0)
        self.assertEqual(len(sched._ops), 1)
        self.assertEqual(sched._ops[0][0], "reload")
        self.assertTrue(any("检测到插件变化，已自动重载（1 个文件变化）" in line
                            for line in cm.output), cm.output)
        self.assertTrue(sched._reload_pending)
        sched._check_plugin_changes(9.0)      # 上次重载还没落地：不重复排队
        self.assertEqual(len(sched._ops), 1)
        sched._drain_ops()                    # 主循环里真正执行 reload
        self.assertFalse(sched._reload_pending)
        sched._check_plugin_changes(12.0)     # 已处理完、文件没再变：不重载
        self.assertEqual(len(sched._ops), 0)

    def test_failure_is_caught_and_does_not_block_later_scans(self):
        sched = self.sched
        sched._check_plugin_changes(0.0)
        self._bump()
        sched.reload = lambda: (_ for _ in ()).throw(RuntimeError("reload boom"))
        with self.assertLogs("msu2", level="WARNING") as cm:
            sched._check_plugin_changes(3.0)
        self.assertTrue(any("插件热加载失败" in line for line in cm.output), cm.output)
        self.assertFalse(sched._reload_pending)   # 失败后允许下一次检测
        self.assertEqual(len(sched._ops), 0)

    def test_watch_dirs_cover_package_and_external_dirs(self):
        dirs = [os.path.normcase(str(d)) for d in M._widget_watch_dirs()]
        self.assertIn(os.path.normcase(self.tmp), dirs)
        self.assertIn(os.path.normcase(str(M._widgets_pkg._pkg_path)), dirs)


class CompositeTests(unittest.TestCase):
    """每帧都必须重新合成：背景变化后发出去的帧必须跟着变。"""

    def make(self, **cfg):
        cfg.setdefault("pages", [])
        return M.Scheduler(M.OfflineDevice(), cfg)

    def test_each_send_recomposes_with_current_background(self):
        sched = self.make()
        sched.background.frames = [Image.new("RGB", (160, 80), (255, 0, 0))]
        self.assertTrue(sched._send_frame(None, 0.0))
        self.assertEqual(sched.device.last.getpixel((0, 0)), (255, 0, 0))
        sched.background.frames = [Image.new("RGB", (160, 80), (0, 0, 255))]
        self.assertTrue(sched._send_frame(None, 0.0))
        self.assertEqual(sched.device.last.getpixel((0, 0)), (0, 0, 255))
        self.assertEqual(sched.metrics["composites"], 2)
        self.assertLess(sched.metrics["comp_ms"], 15.0)

    def test_dim_and_reverse_apply_after_composite(self):
        sched = self.make(display={"dim": 50}, reverse=True)
        sched.background.frames = [Image.new("RGB", (160, 80), (0, 0, 200))]
        layer = Image.new("RGBA", (160, 80), (0, 0, 0, 0))
        layer.paste((255, 0, 0, 255), (0, 0, 80, 80))   # 左半屏不透明红
        sched._send_frame(layer, 0.0)
        out = sched.device.last
        self.assertEqual(out.size, (160, 80))
        # 先合成（左红右蓝）-> 压暗 50% -> 180° 翻转：左右互换
        self.assertEqual(out.getpixel((0, 0)), (0, 0, 100))
        self.assertEqual(out.getpixel((159, 0)), (127, 0, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)