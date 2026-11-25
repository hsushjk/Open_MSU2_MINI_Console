"""设备端直接改屏之后，上位机必须整屏重画。

设备收到「预览 / 擦除 / 存设置」这类命令会立刻改屏，而上位机的编码器还记着
上一帧的内容；如果下一帧只补差异，没变化的地方就会留着上一张图的像素。
这里保证这些路径都会把编码器的上一帧丢掉并强制重发。
"""

import os
import tempfile
import unittest

from PIL import Image

import msu2_mini as M


class FakeEncoder:
    def __init__(self):
        self.resets = 0

    def reset(self):
        self.resets += 1


class FakeDevice:
    def __init__(self):
        self.encoder = FakeEncoder()
        self.transport = type("T", (), {"_lock": None})()
        self.writes = []
        self.drawn = []
        self.erased = []

    def asset_write(self, kind, slot, payload, fmt=0):
        self.writes.append((kind, slot, len(payload), fmt))
        return True, "ok"

    def asset_draw(self, kind, slot):
        self.drawn.append((kind, slot))
        return True, "ok"

    def asset_erase(self, kind, slot):
        self.erased.append((kind, slot))
        return True, "ok"


def make_sched():
    sched = M.Scheduler(M.OfflineDevice(), {"pages": []})
    sched.device = FakeDevice()
    # 跳过真实的 60 ms 停顿，测试不关心时序
    sched._asset_idle = lambda fn, pause=0.8: fn()
    return sched


class ScreenInvalidationTests(unittest.TestCase):
    def test_invalidate_resets_encoder_and_forces_send(self):
        sched = make_sched()
        sched._force_send = False
        sched.invalidate_screen()
        self.assertEqual(sched.device.encoder.resets, 1)
        self.assertTrue(sched._force_send)

    def test_preview_resets_encoder(self):
        sched = make_sched()
        ok, why = sched.asset_draw_safe(1, 0)
        self.assertTrue(ok, why)
        self.assertEqual(sched.device.encoder.resets, 1)
        self.assertTrue(sched._force_send)

    def test_failed_preview_keeps_cache(self):
        sched = make_sched()
        sched.device.asset_draw = lambda kind, slot: (False, "busy")
        ok, _ = sched.asset_draw_safe(1, 0)
        self.assertFalse(ok)
        self.assertEqual(sched.device.encoder.resets, 0)

    def test_erase_resets_encoder(self):
        sched = make_sched()
        ok, why = sched.asset_erase_safe(1, 2)
        self.assertTrue(ok, why)
        self.assertEqual(sched.device.encoder.resets, 1)

    def test_invalidate_covers_every_channel(self):
        sched = make_sched()
        other = FakeDevice()
        sched.channels = [type("Ch", (), {"id": "dev2", "device": other})()]
        sched.invalidate_screen()
        self.assertEqual(sched.device.encoder.resets, 1)
        self.assertEqual(other.encoder.resets, 1)

    def test_upload_does_not_preview(self):
        """写槽不再自动预览：预览图会留在屏上，用户要求去掉。"""
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "solid.png")
            Image.new("RGB", (160, 80), (30, 120, 200)).save(src)
            sched = make_sched()
            ok, why, info = sched.upload_asset(1, 0, src)
        self.assertTrue(ok, why)
        self.assertEqual(info["fmt"], 1)
        self.assertEqual(sched.device.drawn, [])
        self.assertEqual(sched.device.encoder.resets, 0)


if __name__ == "__main__":
    unittest.main()