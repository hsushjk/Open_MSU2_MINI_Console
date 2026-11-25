"""投屏类外部插件提示的单元测试。"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import msu2_mini as M


class PluginWarningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        for name in ("screen_cast4.py", "mycast.py", "clock.py",
                     "_private.py", "display_info.py", "viewer.py"):
            (root / name).write_text("", "utf-8")
        self.patch = mock.patch.object(
            M._widgets_pkg, "external_report",
            lambda: {"dirs": [str(root)]})
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_picks_keyword_names_only(self):
        self.assertEqual(M.risky_plugin_names({}),
                         ["display_info", "mycast", "screen_cast4", "viewer"])

    def test_whitelist_is_respected(self):
        cfg = {"plugin_ack": ["viewer", "mycast"]}
        self.assertEqual(M.risky_plugin_names(cfg),
                         ["display_info", "screen_cast4"])

    def test_private_and_plain_names_ignored(self):
        names = M.risky_plugin_names({})
        self.assertNotIn("_private", names)
        self.assertNotIn("clock", names)

    def test_sanitize_normalises_ack_list(self):
        cfg = M._sanitize_config({"plugin_ack": [" a ", "", 3, "a", "b" * 80]})
        self.assertEqual(cfg["plugin_ack"], ["a", "3"])

    def test_visible_config_keeps_ack(self):
        cfg = {"plugin_ack": ["x"], "_help": 1, "widget_config": {}}
        self.assertEqual(M._visible_config(cfg), {"plugin_ack": ["x"]})


if __name__ == "__main__":
    unittest.main()