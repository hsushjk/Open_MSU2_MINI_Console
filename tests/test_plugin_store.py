"""插件私有配置（widget_config/<插件名>.json）的单元测试。

覆盖：
  · 文件名清洗、保存/读取往返
  · 内容没变不重复写盘、空配置不建空文件
  · 老 config.json 的 widget_config 字段迁移
  · 主配置写盘时剔除 widget_config，并同步写插件文件
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from msu2_core import plugin_store as ps  # noqa: E402


class PluginStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._patch = mock.patch.object(ps, "store_dir", lambda: self.dir)
        self._patch.start()
        ps._WRITTEN.clear()

    def tearDown(self):
        self._patch.stop()
        ps._WRITTEN.clear()
        self.tmp.cleanup()

    def test_path_for_sanitizes_names(self):
        self.assertEqual(ps.path_for("todo").name, "todo.json")
        self.assertEqual(ps.path_for("my plug/v2").name, "my_plug_v2.json")
        self.assertEqual(ps.path_for("").name, "plugin.json")
        self.assertEqual(ps.path_for("../../evil").name, "evil.json")

    def test_save_load_roundtrip(self):
        data = {"items": ["a", "b"], "n": 3}
        self.assertTrue(ps.save_one("todo", data))
        self.assertEqual(ps.load_one("todo"), data)
        on_disk = json.loads((self.dir / "todo.json").read_text("utf-8"))
        self.assertEqual(on_disk, data)

    def test_identical_content_is_not_rewritten(self):
        self.assertTrue(ps.save_one("clock", {"a": 1}))
        self.assertFalse(ps.save_one("clock", {"a": 1}))

    def test_empty_config_does_not_create_file(self):
        # 真正空的 dict 才算空；{"items": []} 有键，仍算用户配置，要写
        self.assertEqual(ps.save_all({"clock": {}, "gif": {}}), 0)
        self.assertFalse((self.dir / "clock.json").exists())
        self.assertFalse((self.dir / "gif.json").exists())
        self.assertEqual(ps.save_all({"todo": {"items": []}}), 1)
        self.assertEqual(ps.load_one("todo"), {"items": []})

    def test_empty_config_clears_existing_file(self):
        ps.save_one("todo", {"items": ["x"]})
        self.assertEqual(ps.save_all({"todo": {}}), 1)
        self.assertEqual(ps.load_one("todo"), {})

    def test_broken_json_falls_back_to_empty(self):
        (self.dir / "bad.json").write_text("{not json", "utf-8")
        self.assertEqual(ps.load_one("bad"), {})

    def test_migrate_legacy_writes_only_missing_non_empty(self):
        ps.save_one("todo", {"items": ["existing"]})
        moved = ps.migrate_legacy({
            "todo": {"items": ["legacy"]},   # 已有文件 → 不覆盖
            "clock": {},                      # 空 → 不建文件
            "gif": {"dir": "D:/gif"},         # 没有文件 → 迁移
        })
        self.assertEqual(moved, 1)
        self.assertEqual(ps.load_one("todo"), {"items": ["existing"]})
        self.assertEqual(ps.load_one("gif"), {"dir": "D:/gif"})
        self.assertFalse((self.dir / "clock.json").exists())

    def test_load_all_covers_every_name(self):
        ps.save_one("todo", {"items": ["x"]})
        got = ps.load_all(["todo", "clock"])
        self.assertEqual(got, {"todo": {"items": ["x"]}, "clock": {}})


class MainConfigIntegrationTests(unittest.TestCase):
    """config.json 里不该再出现 widget_config，插件配置写到独立文件。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._patch = mock.patch.object(ps, "store_dir", lambda: self.dir)
        self._patch.start()
        ps._WRITTEN.clear()

    def tearDown(self):
        self._patch.stop()
        ps._WRITTEN.clear()
        self.tmp.cleanup()

    def test_write_config_raw_strips_widget_config(self):
        import msu2_mini as M

        cfg = {"devices": [], "widget_config": {"todo": {"items": ["x"]}},
               "_help": {"a": 1}, "gif_dir": "X"}
        out = self.dir / "config.json"
        self.assertTrue(M._write_config_raw(cfg, str(out)))
        written = json.loads(out.read_text("utf-8"))
        self.assertNotIn("widget_config", written)
        self.assertNotIn("_help", written)
        self.assertEqual(written["gif_dir"], "X")
        self.assertEqual(ps.load_one("todo"), {"items": ["x"]})

    def test_sanitize_migrates_legacy_field(self):
        import msu2_mini as M

        cfg = M._base_default_config()
        cfg["widget_config"] = {"todo": {"items": ["老数据"]}}
        M._sanitize_config(cfg)
        self.assertEqual(ps.load_one("todo"), {"items": ["老数据"]})
        self.assertIsInstance(cfg["widget_config"], dict)
        self.assertEqual(cfg["widget_config"].get("todo"), {"items": ["老数据"]})


if __name__ == "__main__":
    unittest.main()