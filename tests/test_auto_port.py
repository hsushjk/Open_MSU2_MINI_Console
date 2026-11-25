"""自动端口（auto）找不到设备时的占位与重新发现测试。"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("MSU2_NO_PYTHON", "1")

import msu2_mini as M  # noqa: E402


class DummyScheduler:
    def __init__(self, *args, **kwargs):
        pass


class FakeDevice:
    """最小可用设备替身：只实现通道会碰到的接口。"""

    def __init__(self, port="COM9"):
        self.port = port
        self.connected = True
        self.closed = False

    def set_reverse(self, value):
        pass

    def close(self):
        self.closed = True
        self.connected = False


class PendingDeviceTests(unittest.TestCase):
    def test_never_opens_a_port(self):
        dev = M.PendingDevice()
        self.assertEqual(dev.port, "")
        self.assertFalse(dev.connected)
        self.assertFalse(dev.open())

    def test_send_calls_are_harmless(self):
        dev = M.PendingDevice()
        self.assertFalse(dev.show_image(object()))
        self.assertIsNone(dev.read_touch())
        self.assertTrue(dev.heartbeat())

    def test_suspend_roundtrip(self):
        dev = M.PendingDevice()
        dev.suspend()
        self.assertTrue(dev.suspended)
        dev.resume()
        self.assertFalse(dev.suspended)


class AutoFallbackTests(unittest.TestCase):
    def test_auto_without_device_uses_pending(self):
        cfg = {"devices": [{"id": "dev1", "port": "auto"}]}
        with mock.patch.object(M, "_pick_device", lambda used=(): None), \
                mock.patch.object(M, "Scheduler", DummyScheduler):
            mgr = M.DeviceManager(cfg, Path("unused.json"))
        ch = mgr.channels["dev1"]
        self.assertIsInstance(ch.device, M.PendingDevice)
        self.assertEqual(ch.port, "")
        self.assertFalse(ch.connected)

    def test_explicit_port_still_errored_cleanly(self):
        # 显式端口没插设备时仍是 MiniDevice（占位只用于 auto）
        cfg = {"devices": [{"id": "dev1", "port": "COM99"}]}
        with mock.patch.object(M, "Scheduler", DummyScheduler):
            mgr = M.DeviceManager(cfg, Path("unused.json"))
        self.assertIsInstance(mgr.channels["dev1"].device, M.MiniDevice)


class RediscoverTests(unittest.TestCase):
    def test_pending_device_is_replaced_by_discovery(self):
        found = FakeDevice()
        ch = M.DeviceChannel("dev1", {}, M.PendingDevice(), sync=True,
                             discover=lambda: found)
        ch._last_scan = -1e9          # 绕过重扫间隔
        self.assertTrue(ch.try_rediscover())
        self.assertIs(ch.device, found)
        self.assertTrue(ch.connected)
        self.assertEqual(ch.port, "COM9")

    def test_pick_device_prefers_probed_connection(self):
        found = FakeDevice()
        with mock.patch.object(M, "_pick_free_port", lambda used: "COM9"), \
                mock.patch.dict(M._PROBED, {"COM9": found}, clear=False):
            self.assertIs(M._pick_device(set()), found)


if __name__ == "__main__":
    unittest.main()