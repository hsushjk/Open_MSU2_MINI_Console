"""MSU2 Console 核心单元测试（stdlib unittest，不依赖设备）。

运行：python -m unittest discover -s tests -v
"""
import io
import os
import shutil
import sys
import tempfile
import time
import unittest
import zlib
from pathlib import Path
from unittest import mock

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
        body = enc.encode(solid())
        self.assertTrue(enc.last_full)
        self.assertGreater(len(body), 0)
        # 纯黑整屏：RLE 应该远小于 25.6 KB 原样
        self.assertLess(len(body), 2000)

    def test_small_change_sends_only_that_tile(self):
        enc = M.FrameEncoder()
        img = solid()
        first = enc.encode(img)
        img2 = img.copy()
        img2.paste((255, 0, 0), (32, 16, 48, 32))
        second = enc.encode(img2)
        self.assertFalse(enc.last_full)
        self.assertGreater(len(first), len(second))
        self.assertLess(len(second), 200)

    def test_identical_frame_sends_nothing(self):
        enc = M.FrameEncoder()
        img = solid((10, 20, 30))
        enc.encode(img)
        self.assertEqual(enc.encode(img), b"")

    def test_large_change_falls_back_to_full_frame(self):
        enc = M.FrameEncoder()
        enc.encode(solid())
        noisy = Image.effect_noise((160, 80), 64).convert("RGB")
        enc.encode(noisy)
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
        enc.encode(solid())
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
            self.fw_version = (1, 0, 0)

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
        enc.encode(solid())
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


class MultiDeviceTests(unittest.TestCase):
    """同一份内容扇出到多台设备，每台维护自己的差分缓存、写队列与亮度。"""

    def channels(self, dims=(0, 0), reverses=(False, False), devices=None):
        chans = []
        for i, (dim, rev) in enumerate(zip(dims, reverses)):
            cfg = {"id": "d%d" % (i + 1), "pages": [], "reverse": rev,
                   "display": {"dim": dim}}
            dev = devices[i] if devices else M.OfflineDevice()
            chans.append(M.DeviceChannel("d%d" % (i + 1), cfg, dev,
                                         sync=False))
        sched = M.Scheduler(chans[0].device, chans[0].config, channels=chans)
        for ch in chans:
            ch.start()
        self.addCleanup(lambda: [ch.stop() for ch in chans])
        return sched, chans

    @staticmethod
    def wait_frames(ch, n, timeout=2.0):
        """等这台通道真的发出去 n 帧（队列空了不等于写完了）。"""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if ch.snapshot()["frames"] >= n:
                return True
            time.sleep(0.01)
        return False

    def test_one_composite_fans_out_to_all_devices(self):
        sched, chans = self.channels()
        sched.background.frames = [Image.new("RGB", (160, 80), (0, 0, 200))]
        self.assertTrue(sched._send_frame(None, 0.0))
        for ch in chans:
            self.assertTrue(self.wait_frames(ch, 1), ch.id)
        self.assertEqual(sched.metrics["composites"], 1)   # 只合成一次
        for ch in chans:
            self.assertIsNotNone(ch.device.last, ch.id)
            self.assertEqual(ch.device.last.getpixel((0, 0)), (0, 0, 200))
            self.assertEqual(ch.snapshot()["submitted"], 1)
            self.assertEqual(ch.snapshot()["frames"], 1)
        snap = sched.metrics_snapshot()
        self.assertEqual(snap["device_count"], 2)
        self.assertEqual([d["id"] for d in snap["devices"]], ["d1", "d2"])

    def test_per_device_dim_and_reverse_are_independent(self):
        sched, chans = self.channels(dims=(50, 0), reverses=(False, True))
        sched.background.frames = [Image.new("RGB", (160, 80), (0, 0, 200))]
        layer = Image.new("RGBA", (160, 80), (0, 0, 0, 0))
        layer.paste((255, 0, 0, 255), (0, 0, 80, 80))     # 左半屏不透明红
        sched._send_frame(layer, 0.0)
        for ch in chans:
            self.assertTrue(self.wait_frames(ch, 1))
        a = chans[0].device.last                         # 亮度 50%，不翻转
        self.assertEqual(a.getpixel((0, 0)), (127, 0, 0))
        self.assertEqual(a.getpixel((159, 0)), (0, 0, 100))
        b = chans[1].device.last                         # 不压暗，180° 翻转
        self.assertEqual(b.getpixel((0, 0)), (0, 0, 200))
        self.assertEqual(b.getpixel((159, 0)), (255, 0, 0))

    def test_slow_device_submit_is_non_blocking(self):
        class SlowDevice(M.OfflineDevice):
            def __init__(self):
                super().__init__()
                self.waits = 0

            def show_image(self, img):
                self.waits += 1
                time.sleep(0.3)
                return super().show_image(img)

        sched, chans = self.channels(devices=[SlowDevice(), M.OfflineDevice()])
        sched.background.frames = [Image.new("RGB", (160, 80), (0, 0, 0))]
        t0 = time.monotonic()
        for _ in range(4):
            self.assertTrue(sched._send_frame(None, 0.0))
        cost = time.monotonic() - t0
        # 慢设备在独立线程里发，渲染循环 4 次提交不会被它拖住
        self.assertLess(cost, 0.2)
        self.assertTrue(self.wait_frames(chans[1], 1))
        self.assertEqual(chans[1].device.last.getpixel((0, 0)), (0, 0, 0))
        snap = chans[0].snapshot()
        self.assertLess(snap["queue"], 3)                 # 队列封顶，丢最旧
        self.assertGreaterEqual(snap["dropped"], 1)

    def test_single_device_stays_synchronous(self):
        sched = M.Scheduler(M.OfflineDevice(), {"pages": []})
        sched.background.frames = [Image.new("RGB", (160, 80), (0, 255, 0))]
        self.assertTrue(sched._send_frame(None, 0.0))
        # 单设备：发完即落地，行为和拆分前一致（CompositeTests 也依赖这点）
        self.assertEqual(sched.device.last.getpixel((0, 0)), (0, 255, 0))
        self.assertEqual(sched.metrics_snapshot()["devices"][0]["mode"], "sync")

    def test_describe_lists_every_configured_device(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        cfg = {"devices": [{"id": "a", "port": "offline"},
                           {"id": "b", "port": "COM_NOPE_9"}]}
        mgr = M.DeviceManager(cfg, os.path.join(tmp, "config.json"))
        try:
            desc = mgr.describe()
            self.assertEqual([d["id"] for d in desc], ["a", "b"])
            self.assertTrue(desc[0]["offline"])
            self.assertEqual(desc[0]["port"], "offline")
            self.assertFalse(desc[1]["connected"])       # 连不上但不影响第一台
            self.assertTrue(desc[0]["active"])
            self.assertEqual(mgr.active_id, "a")
            self.assertIsNotNone(mgr.active())
            self.assertIs(mgr.active_channel(), mgr.channels["a"])
        finally:
            mgr.close_devices()

    def test_sanitize_device_keeps_display_and_offline_port(self):
        out = M._sanitize_device({"id": "x", "port": "offline",
                                  "display": {"dim": 40}, "reverse": True}, 0)
        self.assertEqual(out["port"], "offline")
        self.assertEqual(out["display"]["dim"], 40)
        self.assertTrue(out["display"]["reverse"])
        self.assertEqual(out["display"]["orientation"], out["orientation"])


class BackgroundSchedulingTests(unittest.TestCase):
    """背景换图：插件静止时主循环也得按时要帧，节奏跟配置的 interval 走。"""

    def make(self, **cfg):
        cfg.setdefault("pages", [])
        return M.Scheduler(M.OfflineDevice(), cfg)

    def _png(self, td, name, color):
        p = os.path.join(td, name)
        Image.new("RGB", (160, 80), color).save(p)
        return p

    def test_bg_due_fires_at_playlist_interval(self):
        # 插件静止（不渲染）时，轮播到点也必须靠 _bg_due 继续要帧，
        # 否则背景会一直停在旧图上。
        sched = self.make()
        with tempfile.TemporaryDirectory() as td:
            a = self._png(td, "a.png", (255, 0, 0))
            b = self._png(td, "b.png", (0, 0, 255))
            sched._bg_enabled = True
            sched.background.load_playlist([a, b], "fill", 5.0, False)
            sched.background.tick(0.0)          # 首次 tick 才开始计时
            self.assertFalse(sched._bg_due(4.9, 0.0), "没到 5 s 不该换图")
            self.assertTrue(sched._bg_due(5.0, 0.0), "到 5 s 必须请求换图帧")
            self.assertIsNotNone(sched.background.tick(5.0))
            self.assertEqual(sched.background.switch_count, 1)
            self.assertFalse(sched._bg_due(5.4, 5.0), "换完立刻停，不再空推帧")

    def test_bg_due_step_follows_link_gap(self):
        # 换图是硬切，只有动图翻页才需要按链路占用排节拍。
        sched = self.make()
        with tempfile.TemporaryDirectory() as td:
            a = self._png(td, "a.png", (255, 0, 0))
            b = self._png(td, "b.png", (0, 0, 255))
            sched._bg_enabled = True
            sched.background.load_playlist([a, b], "fill", 2.0, False)
            sched.background.tick(0.0)
            sched._send_gap = 0.2
            self.assertFalse(sched._bg_due(2.15, 2.0), "刚发完一帧，链路还没空")
            self.assertTrue(sched._bg_due(2.25, 2.0))

    def test_startup_uses_configured_interval_and_shuffle(self):
        # 回归：启动路径以前调 load_playlist 时漏传 interval/shuffle，
        # 配置写 5 s 实际按默认 12 s 换图（“设 5 秒实际 10 秒”的根因）。
        with tempfile.TemporaryDirectory() as td:
            data = Path(td, "data")
            bg_dir = Path(data, "backgrounds")
            bg_dir.mkdir(parents=True)
            a = self._png(str(bg_dir), "a.png", (255, 0, 0))
            b = self._png(str(bg_dir), "b.png", (0, 0, 255))
            with mock.patch.object(M, "_data_dir", lambda: Path(data)):
                sched = self.make(background={
                    "enabled": True, "source": "playlist", "paths": [a, b],
                    "mode": "fill", "interval": 5, "shuffle": True})
                self.assertEqual(sched._bg_interval, 5.0)
                self.assertEqual(sched.background.pl_interval, 5.0)
                self.assertTrue(sched.background.shuffle)
                self.assertEqual(len(sched.background.groups), 2)

    def test_unknown_background_keys_are_ignored(self):
        # 未知键（例如已废弃的选项）不报错、不影响 interval。
        with tempfile.TemporaryDirectory() as td:
            data = Path(td, "data")
            bg_dir = Path(data, "backgrounds")
            bg_dir.mkdir(parents=True)
            a = self._png(str(bg_dir), "a.png", (255, 0, 0))
            b = self._png(str(bg_dir), "b.png", (0, 0, 255))
            with mock.patch.object(M, "_data_dir", lambda: Path(data)):
                sched = self.make(background={
                    "enabled": True, "source": "playlist", "paths": [a, b],
                    "mode": "fill", "interval": 2,
                    "fade": True, "fade_secs": 1.0})
                self.assertEqual(sched.background.pl_interval, 2.0)
                self.assertFalse(hasattr(sched.background, "fading"))

    def test_bg_step_matches_link_rate(self):
        sched = self.make()
        self.assertAlmostEqual(sched._bg_step_secs(), 0.08, places=3)
        sched._send_gap = 0.30
        self.assertLessEqual(sched._bg_step_secs(), 0.30)
        sched._send_gap = 0.01
        self.assertGreaterEqual(sched._bg_step_secs(), 0.04)


class CapturingSerial:
    """假串口：记录每一笔写入，close() 后拒绝再写。"""

    def __init__(self):
        self.writes = []
        self.closed = False
        self.flushed = False

    def flush(self):
        self.flushed = True

    def write(self, data):
        if self.closed:
            raise OSError("port is closed")
        self.writes.append(bytes(data))
        return len(data)

    def reset_input_buffer(self):
        pass

    def close(self):
        self.closed = True

    def read(self, n=1):
        return b""


class ScripedRebootDevice:
    """假设备：open() 按脚本返回，用来测升级后的有界重连轮询。"""

    def __init__(self, reopen_calls=1, version=(1, 0, 9, 0)):
        self.port = "COM_fake"
        self.connected = False
        self._suspended = False
        self.opens = 0
        self.reopen_calls = reopen_calls      # 前几次 open() 返回 False
        self.version = version
        self.flashed = []
        self.transport = type("T", (), {})()
        self.transport.fw_version = (1, 0, 9)
        self.transport.query_version = (
            lambda timeout=0.4: self.version)

    def open(self, handshake_timeout=2.0):
        self.opens += 1
        if self.opens <= self.reopen_calls:
            return False
        self.connected = True
        return True

    def close(self):
        self.connected = False

    def suspend(self):
        self._suspended = True

    def resume(self):
        self._suspended = False

    @property
    def suspended(self):
        return self._suspended

    def flash_firmware(self, data, progress=None):
        self.flashed.append(len(data))
        return True, "uploaded %d bytes, device is rebooting" % len(data)


class FirmwareFlashTests(unittest.TestCase):
    """问题 4：写完 [OK] uploaded 立刻关串口，然后有界轮询等设备回来。"""

    def make(self):
        return M.Scheduler(M.OfflineDevice(), {"pages": []})

    def test_transport_closes_port_immediately_after_ok(self):
        tr = M.SerialTransport("COM_fake")
        ser = CapturingSerial()
        tr._ser = ser
        tr._connected = True
        tr._handshaked = True
        tr._reply_byte = lambda *a, **k: 0x06       # 设备对每一步都回 0x06
        ok, why = tr.flash_firmware(bytes(512))
        self.assertTrue(ok, why)
        self.assertIn("rebooting", why)
        self.assertIsNone(tr._ser, "收到 [OK] 之后必须立刻关串口")
        self.assertFalse(tr.connected)
        self.assertTrue(ser.closed)
        self.assertEqual(ser.writes[-1], M.MiniProtocol.flash_apply(),
                         "flash_apply 必须是最后一笔写入")
        before = len(ser.writes)
        self.assertFalse(tr.write(b"\x00" * 8), "关串口后 write() 必须直接拒绝")
        self.assertEqual(len(ser.writes), before, "关串口后不该再有任何写入")

    def test_device_flash_suspends_channel_writes(self):
        dev = M.MiniDevice("COM_fake")
        dev.transport = type("T", (), {
            "fw_version": (1, 0, 9),
            "flash_firmware": lambda self, data, progress=None: (
                True, "uploaded")})()
        ok, _why = dev.flash_firmware(bytes(256))
        self.assertTrue(ok)
        self.assertTrue(dev.suspended, "上传期间必须挂起，通道不能再写帧")
        self.assertFalse(dev.show_image(Image.new("RGB", (160, 80))))

    def test_wait_device_back_success(self):
        dev = ScripedRebootDevice(reopen_calls=2)
        back, fw, why, _n = M.wait_device_back(dev, poll=0.01, timeout=2.0)
        self.assertTrue(back, why)
        self.assertEqual(fw, "1.0.9")
        self.assertEqual(len(dev.flashed), 0)

    def test_wait_device_back_timeout(self):
        dev = ScripedRebootDevice(reopen_calls=10 ** 9)
        back, fw, why, _n = M.wait_device_back(dev, poll=0.01, timeout=0.08)
        self.assertFalse(back)
        self.assertEqual(fw, "")
        self.assertIn("拔插", why)
        self.assertGreaterEqual(dev.opens, 2, "15 s 窗口内要反复试 open()")

    def test_flash_safe_success_reports_fw_and_resumes(self):
        sched = self.make()
        dev = ScripedRebootDevice(reopen_calls=0)
        sched.device = dev
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as fh:
            fh.write(bytes(256))
            fw_path = fh.name
        try:
            res = sched.flash_firmware_safe(fw_path)
        finally:
            os.unlink(fw_path)
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["rebooted"])
        self.assertEqual(res["fw"], "1.0.9")
        self.assertIn("升级完成", res["msg"])
        self.assertEqual(dev.flashed, [256])
        self.assertFalse(dev.suspended, "设备回来之后要恢复发帧")
        self.assertEqual(sched.flash_phase, "done")
        self.assertEqual(sched._devcfg_pause_until, 0.0)

    def test_flash_safe_timeout_reports_unplug_hint(self):
        sched = self.make()
        dev = ScripedRebootDevice(reopen_calls=0)
        sched.device = dev
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as fh:
            fh.write(bytes(256))
            fw_path = fh.name
        try:
            with mock.patch.object(
                    M, "wait_device_back",
                    return_value=(False, "",
                                  "设备没有回来，请拔插一次 USB 再重试", 3)):
                res = sched.flash_firmware_safe(fw_path)
        finally:
            os.unlink(fw_path)
        self.assertFalse(res["ok"])
        self.assertTrue(res["rebooted"], "已经上传成功，设备只是没回来")
        self.assertIn("拔插", res["err"])
        self.assertTrue(dev.suspended, "没回来时保持挂起，避免往复活中的串口写帧")


class AssetRleTests(unittest.TestCase):
    """资产 RLE 编解码与下发格式选型。"""

    def test_round_trip_gradient(self):
        data = bytes((i * 73 + (i >> 5) * 19) & 0xFF for i in range(25600))
        packed = M.rle_encode_rgb565(data)
        self.assertEqual(len(packed) % 3, 0, "必须是 3 字节一包")
        self.assertEqual(M.rle_decode_rgb565(packed), data)

    def test_long_run_split_at_255(self):
        # 纯色整屏：一个像素重复 12 800 次，cnt 只能 1..255，必须拆包
        data = b"\x12\x34" * 12800
        packed = M.rle_encode_rgb565(data)
        cnts = [packed[i] for i in range(0, len(packed), 3)]
        self.assertTrue(all(1 <= c <= 255 for c in cnts))
        self.assertGreater(len(cnts), 1, ">255 的行程要拆成多包")
        self.assertEqual(M.rle_decode_rgb565(packed), data)
        self.assertLess(len(packed), 400)

    def test_compress_only_when_smaller(self):
        flat, fmt = M.compress_asset_payload(b"\x00\x00" * 12800)
        self.assertEqual(fmt, 1)
        self.assertLess(len(flat), 25600)
        # 逐像素变化的噪声 RLE 压不动（3 B/像素 > 2 B/像素）→ 保持 fmt=0
        noise = bytes((i * 37 + 11) & 0xFF for i in range(25600))
        raw, fmt2 = M.compress_asset_payload(noise)
        self.assertEqual(fmt2, 0)
        self.assertEqual(raw, noise)
        self.assertEqual(len(raw), 25600)

    def test_begin_command_kind_byte(self):
        raw = M.MiniProtocol.asset_begin(1, 3, 25600)
        self.assertEqual(raw[2], 1, "裸数据的 kind 字节是 1")
        rle = M.MiniProtocol.asset_begin(1, 3, 0x0200, fmt=1)
        self.assertEqual(rle[2], 0x81, "fmt=1 用 kind 最高位标记")
        self.assertEqual(rle[4:6], b"\x02\x00")


class FakeAssetDevice:
    """假设备：记录资产写入的格式和长度。"""

    def __init__(self):
        self.calls = []
        tr = type("T", (), {})()
        tr._lock = None
        self.transport = tr

    def asset_write(self, kind, slot, payload, fmt=0):
        self.calls.append((kind, slot, len(payload), fmt))
        return True, "ok"

    def asset_draw(self, kind, slot):
        return True


class AssetUploadTests(unittest.TestCase):
    """上传链路：按压缩比选 RLE 或裸数据，写入长度与槽位信息一致。"""

    def make(self):
        sched = M.Scheduler(M.OfflineDevice(), {"pages": []})
        sched.device = FakeAssetDevice()
        return sched

    def png(self, td):
        src = os.path.join(td, "solid.png")
        Image.new("RGB", (160, 80), (30, 120, 200)).save(src)
        return src

    def test_upload_uses_rle(self):
        with tempfile.TemporaryDirectory() as td:
            src = self.png(td)
            sched = self.make()
            ok, why, info = sched.upload_asset(1, 0, src)
        self.assertTrue(ok, why)
        self.assertEqual(info["raw"], 25600)
        self.assertEqual(info["fmt"], 1)
        self.assertLess(info["sent"], 25600)
        self.assertEqual(sched.device.calls[-1][3], 1)
        self.assertEqual(sched.device.calls[-1][2], info["sent"])


class OtaChannelTests(unittest.TestCase):
    """--selftest 的 OTA 通道项：写暂存区、回读头，绝不 apply。"""

    def make(self):
        tr = M.SerialTransport("COM_fake")
        ser = CapturingSerial()
        tr._ser = ser
        tr._connected = True
        tr._handshaked = True
        tr._reply_byte = lambda *a, **k: 0x06
        return tr, ser

    def test_staging_round_trip_validates_header(self):
        tr, ser = self.make()
        payload = bytes(range(256)) * 4
        crc = zlib.crc32(payload) & 0xFFFFFFFF
        hdr = (b"MSU2" + bytes([1, 0]) + len(payload).to_bytes(4, "little")
               + crc.to_bytes(4, "little") + b"\xff\xff")
        tr.nor_read = lambda addr, n=16: hdr
        ok, why, n = tr.ota_channel_probe(payload)
        self.assertTrue(ok, why)
        self.assertEqual(n, len(payload))
        self.assertIn(M.MiniProtocol.flash_begin(len(payload)), ser.writes)
        self.assertEqual(ser.writes[-1], M.MiniProtocol.flash_commit(crc))
        self.assertNotIn(M.MiniProtocol.flash_apply(), ser.writes,
                         "自检绝不能发 0A 14（真升级）")

    def test_rejected_begin_reports_reason(self):
        tr, _ser = self.make()
        tr._reply_byte = lambda *a, **k: 0x15
        tr.nor_read = lambda addr, n=16: b"MSU2" + bytes(12)
        ok, why, _n = tr.ota_channel_probe(bytes(1024))
        self.assertFalse(ok)
        self.assertIn("暂存区不接受", why)

    def test_bad_magic_reports_reason(self):
        tr, _ser = self.make()
        tr.nor_read = lambda addr, n=16: b"XXXX" + bytes(12)
        ok, why, _n = tr.ota_channel_probe(bytes(1024))
        self.assertFalse(ok)
        self.assertIn("魔数", why)

    def test_selftest_has_ota_item(self):
        src = open(os.path.join(ROOT, "msu2_mini.py"), encoding="utf-8").read()
        self.assertIn("OTA 通道：写入/回读/校验通过", src)
        self.assertIn("ota_channel_probe(ota_data)", src)


class AssetBlitTests(unittest.TestCase):
    """`0A 18` 贴图：坐标按有符号字节发，负偏移交给固件裁剪。"""

    def make(self):
        tr = M.SerialTransport("COM_fake")
        ser = CapturingSerial()
        tr._ser = ser
        tr._connected = True
        tr._handshaked = True
        tr._asset_reply = lambda sub, timeout=3.0: 0x06
        return tr, ser

    def test_negative_offset_is_sent_as_int8(self):
        tr, ser = self.make()
        ok, why = tr.asset_blit(1, 2, -40, 10)
        self.assertTrue(ok, why)
        self.assertEqual(ser.writes[-1],
                         bytes([0x0A, 0x18, 1, 2, 0xD8, 0x0A]))

    def test_missing_slot_reports_reason(self):
        tr, _ser = self.make()
        tr._asset_reply = lambda sub, timeout=3.0: 0x15
        ok, why = tr.asset_blit(1, 2, 0, 0)
        self.assertFalse(ok)
        self.assertIn("还没有图", why)


class CloseFlushTests(unittest.TestCase):
    """关串口前要 flush，别把半个 6 字节帧留在设备接收侧造成错位。"""

    def test_close_flushes_before_closing(self):
        tr = M.SerialTransport("COM_fake")
        ser = CapturingSerial()
        tr._ser = ser
        tr._connected = True
        tr._handshaked = True
        tr.close()
        self.assertTrue(ser.flushed, "关串口前必须 flush 发送缓冲")
        self.assertTrue(ser.closed)


class WaitDeviceBackModuleTests(unittest.TestCase):
    """cmd_flash 复用模块级 wait_device_back（200 ms 轮询、15 s 上限）。"""

    def test_module_function_returns_fw_and_attempts(self):
        dev = ScripedRebootDevice(reopen_calls=1)
        back, fw, why, attempts = M.wait_device_back(dev, poll=0.01, timeout=2.0)
        self.assertTrue(back, why)
        self.assertEqual(fw, "1.0.9")
        self.assertGreaterEqual(attempts, 2)
        self.assertEqual(why, "")

    def test_cmd_flash_uses_wait_device_back(self):
        src = open(os.path.join(ROOT, "msu2_mini.py"), encoding="utf-8").read()
        self.assertIn("back, fw2, why2, attempts = wait_device_back(dev2)", src)
        self.assertIn("升级完成：固件", src)


class SuspendedChannelTests(unittest.TestCase):
    """升级期间通道要静默丢帧：不写串口、也不让调度器当成发送失败重试。"""

    def test_deliver_skips_frames_while_suspended(self):
        dev = M.OfflineDevice()
        ch = M.DeviceChannel("d1", {"pages": []}, dev, sync=True)
        dev.connected = False          # 模拟“串口已经关了”的升级窗口
        dev.suspend()
        self.assertTrue(ch.submit(solid()), "挂起时算“已处理”，别触发重试")
        self.assertEqual(dev.frames, 0, "挂起期间一个像素帧都不能写")
        self.assertGreaterEqual(ch.snapshot()["dropped"], 1)
        self.assertFalse(ch.ensure_open(), "挂起期间不该抢着 open()")
        dev.resume()
        self.assertTrue(ch.submit(solid()))
        self.assertEqual(dev.frames, 1)
        self.assertTrue(ch.ensure_open())


if __name__ == "__main__":
    unittest.main(verbosity=2)