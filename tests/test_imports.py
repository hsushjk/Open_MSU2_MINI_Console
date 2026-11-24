"""纯 import 冒烟测试：拆包（msu2_mini.py -> msu2_core/*.py）后的回归护栏。

背景：2026-11 拆分时 `zlib` 的使用搬进了 msu2_core/transport.py
（`crc = zlib.crc32(data)`），但 import 语句没跟着搬。主文件照样能 import，
旧的单元测试也全过，直到真机上传资产 / OTA 时才炸（NameError/ModuleNotFoundError
只在执行到那一行时出现）。所以这组测试除了 import，还对协议帧和 CRC 做一次
纯函数级冒烟——不碰串口、不碰 UI、不依赖设备。

运行：`python -X utf8 -m unittest discover -s tests -v`
"""
from __future__ import annotations

import os
import sys
import unittest
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 与 test_core.py 保持一致：单元测试不拉起外部 python 设备进程。
os.environ.setdefault("MSU2_NO_PYTHON", "1")

CORE_MODULES = (
    "msu2_core",
    "msu2_core.protocol",
    "msu2_core.transport",
    "msu2_core.device",
    "msu2_core.branding",
)


class TestModuleImports(unittest.TestCase):
    """每个模块单独 import 一次：拆包后任何一个模块拉不起来都要立刻可见。"""

    def test_import_main(self):
        import msu2_mini
        self.assertTrue(msu2_mini.__version__)
        self.assertTrue(callable(msu2_mini.main))

    def test_import_core_package(self):
        import msu2_core
        self.assertTrue(hasattr(msu2_core, "__path__"))

    def test_import_core_protocol(self):
        import msu2_core.protocol as p
        self.assertTrue(callable(p.MiniProtocol._frame))

    def test_import_core_transport(self):
        import msu2_core.transport as t
        self.assertTrue(callable(t.SerialTransport))

    def test_import_core_device(self):
        import msu2_core.device as d
        self.assertTrue(callable(d.MiniDevice))
        self.assertTrue(callable(d.FrameEncoder))

    def test_import_core_branding(self):
        import msu2_core.branding as b
        self.assertTrue(callable(b._asset_dir))

    def test_import_widgets(self):
        import widgets
        self.assertTrue(hasattr(widgets, "__path__"))
        import msu2_mini
        self.assertIs(msu2_mini._widgets_pkg, widgets)


class TestPublicApi(unittest.TestCase):
    """拆出去的符号必须还在原来被调用的地方（主文件重新绑定 + core 自己的定义）。"""

    def test_protocol_callables(self):
        from msu2_core.protocol import MiniProtocol as P
        for name in ("flash_begin", "flash_window", "flash_commit",
                     "tile_check", "asset_begin", "asset_commit"):
            self.assertTrue(callable(getattr(P, name, None)), "MiniProtocol.%s 不见了" % name)
        # main 里引用的兼容别名必须仍指向同一个类
        import msu2_mini
        self.assertIs(msu2_mini.MiniProtocol, P)

    def test_transport_callables(self):
        from msu2_core.transport import SerialTransport as T
        for name in ("asset_write", "flash_firmware", "tile_check"):
            self.assertTrue(callable(getattr(T, name, None)), "SerialTransport.%s 不见了" % name)
        import msu2_mini
        self.assertIs(msu2_mini.SerialTransport, T)

    def test_device_callables(self):
        from msu2_core.device import FrameEncoder, MiniDevice
        self.assertTrue(callable(getattr(MiniDevice, "show_image", None)))
        self.assertTrue(callable(getattr(FrameEncoder, "encode", None)))
        import msu2_mini
        self.assertIs(msu2_mini.MiniDevice, MiniDevice)
        self.assertIs(msu2_mini.FrameEncoder, FrameEncoder)

    def test_constants(self):
        import msu2_core
        import msu2_mini
        from msu2_core.protocol import TILE_N
        self.assertEqual(msu2_mini.TILE_N, TILE_N)
        self.assertEqual(TILE_N, 50)
        self.assertEqual((msu2_mini.SCREEN_W, msu2_mini.SCREEN_H), msu2_core.LANDSCAPE_SIZE)
        self.assertIsInstance(msu2_mini.SCREEN_BAUD, int)
        self.assertGreater(msu2_mini.SCREEN_BAUD, 0)

    def test_main_compat_helpers(self):
        import msu2_mini
        from msu2_core import device
        for name in ("_load_firmware_image", "_tile_frame", "_full_frame", "_tile_signatures"):
            self.assertTrue(callable(getattr(msu2_mini, name, None)), "msu2_mini.%s 不见了" % name)
        self.assertIs(msu2_mini._tile_frame, device._tile_frame)
        self.assertIs(msu2_mini._full_frame, device._full_frame)
        self.assertIs(msu2_mini._tile_signatures, device._tile_signatures)


class TestSmokeCalls(unittest.TestCase):
    """不碰串口的“调用级”冒烟：正是这一类（zlib）以前只能在真机上暴露。"""

    def test_flash_window_frame(self):
        from msu2_core.protocol import MiniProtocol
        frame = MiniProtocol.flash_window(1024)
        self.assertEqual(len(frame), 6)
        self.assertEqual(frame[:4], bytes((0x0A, 0x12, 0x02, 0x00)))
        self.assertEqual(int.from_bytes(frame[4:], "big"), 1024)

    def test_asset_commit_frame(self):
        from msu2_core.protocol import MiniProtocol
        frame = MiniProtocol.asset_commit(0x12345678)
        self.assertEqual(frame, bytes((0x0A, 0x03, 0x12, 0x34, 0x56, 0x78)))
        self.assertEqual(int.from_bytes(frame[2:], "big"), 0x12345678)

    def test_zlib_is_bound_in_transport(self):
        # 拆包时丢的就是这一行：transport.py 里 crc = zlib.crc32(...)
        from msu2_core import transport
        self.assertIs(transport.zlib, zlib)
        self.assertEqual(transport.zlib.crc32(b"abc") & 0xFFFFFFFF, 0x352441C2)
        self.assertIs(transport.zlib.crc32, zlib.crc32)

    def test_flash_commit_carries_crc32_bytes(self):
        # flash_firmware 用 zlib.crc32(data) 算校验，再按大端塞进 0x13 帧
        from msu2_core.protocol import MiniProtocol
        crc = zlib.crc32(b"abc") & 0xFFFFFFFF
        frame = MiniProtocol.flash_commit(crc)
        self.assertEqual(len(frame), 6)
        self.assertEqual(frame[0], 0x13)
        self.assertEqual(frame[2:], crc.to_bytes(4, "big"))
        self.assertEqual(frame[2:], bytes((0x35, 0x24, 0x41, 0xC2)))


if __name__ == "__main__":
    unittest.main()