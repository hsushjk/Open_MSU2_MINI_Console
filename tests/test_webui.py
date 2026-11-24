"""WebUI / 背景图 / 时钟颜色的回归测试。

这里覆盖用户实际踩过的坑：
- 表单改完没点应用，被 1 秒一次的状态轮询冲掉（dirty 标记 + node 实跑）；
- 上传的背景图必须统一成 160×80、存进数据目录 backgrounds/，旧路径要懒迁移；
- 开机/待机画面按模式显隐控件（JS 规则和 Python 规则必须一致）；
- 固件升级上传的返回值结构（不再把字符串当对象用）；
- favicon 指向 /favicon.ico；
- 时钟插件三个颜色字段的校验与回退。
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

os.environ.setdefault("MSU2_NO_PYTHON", "1")

from PIL import Image, ImageDraw  # noqa: E402

import msu2_mini as M  # noqa: E402
from widgets import clock as clock_mod  # noqa: E402

NODE = shutil.which("node")


def _script_text():
    m = re.search(r"<script>(.*)</script>", M.INDEX_HTML, re.S)
    return m.group(1) if m else ""


def _between(text, start, end):
    i = text.index(start)
    j = text.index(end, i)
    return text[i:j + len(end)]


def _run_node(harness, source):
    """把 source 存成文件、用 node 跑 harness（harness 里 eval 它），返回 JSON。"""
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "source.js"
        src.write_text(source, encoding="utf-8")
        run = Path(td) / "run.js"
        run.write_text(harness, encoding="utf-8")
        proc = subprocess.run([NODE, str(run), str(src)], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=60)
        if proc.returncode != 0:
            raise AssertionError("node 执行失败：\n%s\n%s"
                                 % (proc.stdout, proc.stderr))
        return json.loads(proc.stdout.strip().splitlines()[-1])


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class DirtyPollingTests(unittest.TestCase):
    """轮询只能刷新“没在编辑、也没改过”的控件。"""

    def _core(self):
        return _between(_script_text(),
                        "/* ---- dirty tracking core",
                        "/* ---- dirty tracking core end ---- */")

    @unittest.skipUnless(NODE, "没有 node，跳过 JS 行为测试")
    def test_dirty_blocks_polling_and_clear_restores(self):
        harness = r"""
const fs = require('fs');
eval(fs.readFileSync(process.argv[2], 'utf8'));
const document = { activeElement: null, getElementById: () => null };
const out = {};
out.initial = shouldSync('bg-path');
markDirty('bg-path');
out.dirty = shouldSync('bg-path');
out.isDirty = isDirty('bg-path');
clearDirty(['bg-path']);
out.cleared = shouldSync('bg-path');
const focused = { id: 'bg-path' };
document.activeElement = focused;
document.getElementById = id => (id === 'bg-path' ? focused : null);
out.focused = shouldSync('bg-path');
document.activeElement = null;
out.blurred = shouldSync('bg-path');
console.log(JSON.stringify(out));
"""
        got = _run_node(harness, self._core())
        self.assertTrue(got["initial"], "没改过的字段应该允许轮询刷新")
        self.assertFalse(got["dirty"], "改过还没应用的字段不能被轮询覆盖")
        self.assertTrue(got["isDirty"])
        self.assertTrue(got["cleared"], "应用成功后要恢复允许轮询")
        self.assertFalse(got["focused"], "正在编辑的控件不能被轮询覆盖")
        self.assertTrue(got["blurred"], "焦点离开且没改动时应恢复轮询")

    def test_all_controls_have_dirty_listeners(self):
        script = _script_text()
        self.assertIn("setupDirtyListeners()", script)
        for el_id in ("tk-single", "tk-double", "tk-long",
                      "tk-macro-single", "tk-program-long",
                      "asset-boot", "asset-standby", "bg-path"):
            self.assertIn("'%s'" % el_id, script, "%s 没被 dirty 机制覆盖" % el_id)
        self.assertIn("shouldSync('disp-dim')", script)
        self.assertIn("clearTouchDirty()", script)
        self.assertIn("clearBgDirty()", script)
        self.assertIn("clearAssetDirty()", script)

    def test_no_stale_loadstatus_call(self):
        self.assertNotIn("loadStatus", M.INDEX_HTML)
        self.assertIn("refresh();", M.INDEX_HTML)


class AssetUiStateTests(unittest.TestCase):
    """开机/待机画面控件显隐规则（JS 与 Python 必须一致）。"""

    def test_python_rule(self):
        st = M.asset_ui_state(1, 2)
        self.assertTrue(st["bootFile"] and st["bootHold"] and st["dwell"]
                        and st["slots"] and st["standbyFile"])
        self.assertFalse(st["bootHoldHint"])

    @unittest.skipUnless(NODE, "没有 node，跳过 JS 行为测试")
    def test_js_matches_python(self):
        js = _between(_script_text(), "/* ---- assetUiState 规则",
                      "/* ---- assetUiState 规则结束 ---- */")
        harness = r"""
const fs = require('fs');
eval(fs.readFileSync(process.argv[2], 'utf8'));
const out = [];
for (let boot = 0; boot <= 2; boot++) {
  for (let standby = 0; standby <= 3; standby++) {
    out.push([boot, standby, assetUiState(boot, standby)]);
  }
}
console.log(JSON.stringify(out));
"""
        got = _run_node(harness, js)
        self.assertEqual(len(got), 12)
        for boot, standby, state in got:
            self.assertEqual(state, M.asset_ui_state(boot, standby),
                             "boot=%s standby=%s 的显隐规则 JS 与 Python 不一致"
                             % (boot, standby))

    def test_dom_wiring(self):
        html = M.INDEX_HTML
        for el_id in ("asset-boot-file-wrap", "asset-standby-file-wrap",
                      "asset-boot-hold-wrap", "asset-dwell-wrap",
                      "asset-slot-list"):
            self.assertIn('id="%s"' % el_id, html)
        self.assertIn('<option value="0">关闭</option>', html)
        self.assertNotIn('<option value="0">不画</option>', html)
        self.assertIn("applyAssetVisibility()", _script_text())


class BackgroundNormalizeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._patch = mock.patch.object(M, "_data_dir", lambda: self.root)
        self._patch.start()
        M._BG_NORM_CACHE.clear()
        M._BG_THUMB_CACHE.clear()

    def tearDown(self):
        self._patch.stop()
        self.tmp.cleanup()

    def _make_big_image(self):
        src = self.root / "big.png"
        img = Image.new("RGB", (320, 200), (255, 0, 0))
        d = ImageDraw.Draw(img)
        d.rectangle([100, 50, 219, 149], fill=(0, 0, 255))
        img.save(src)
        return src

    def test_static_image_normalized_to_160x80(self):
        src = self._make_big_image()
        out = Path(M._normalize_bg_image(str(src)))
        self.assertTrue(out.is_file())
        self.assertEqual(out.parent, (self.root / "backgrounds").resolve())
        with Image.open(out) as im:
            self.assertEqual(im.size, (160, 80))
            # fill 语义：居中裁剪，不是拉伸变形；中间那块蓝色必须还在
            self.assertEqual(im.convert("RGB").getpixel((80, 40)), (0, 0, 255))
        # 幂等：同一张图再处理一次返回同一个文件
        self.assertEqual(M._normalize_bg_image(str(src)), str(out))
        # 已经在 backgrounds/ 且尺寸正确的路径直接原样返回
        self.assertEqual(M._normalize_bg_image(str(out)), str(out))

    def test_animated_gif_keeps_animation(self):
        frames = [Image.new("RGB", (64, 64), (255, 0, 0)),
                  Image.new("RGB", (64, 64), (0, 255, 0))]
        src = self.root / "anim.gif"
        frames[0].save(src, save_all=True, append_images=frames[1:],
                       duration=100, loop=0)
        out = Path(M._normalize_bg_image(str(src)))
        self.assertEqual(out.suffix, ".gif")
        with Image.open(out) as im:
            self.assertEqual(im.size, (160, 80))
            self.assertGreaterEqual(im.n_frames, 2)

    def test_bad_input_raises_valueerror(self):
        bad = self.root / "note.txt"
        bad.write_text("不是图片", encoding="utf-8")
        with self.assertRaises(ValueError):
            M._normalize_bg_image(str(bad))
        with self.assertRaises(ValueError):
            M._normalize_bg_image(str(self.root / "nope.png"))

    def test_thumbnail_only_for_managed_dir(self):
        src = self._make_big_image()
        self.assertIsNone(M._background_thumb(str(src)))
        out = M._normalize_bg_image(str(src))
        data = M._background_thumb(out)
        self.assertIsNotNone(data)
        with Image.open(__import__("io").BytesIO(data)) as im:
            self.assertEqual(im.size, (96, 48))

    def test_old_config_path_lazily_migrated_by_scheduler(self):
        src = self._make_big_image()
        cfg = {"pages": [], "background": {"enabled": False,
                                           "path": str(src)}}
        sched = M.Scheduler(M.OfflineDevice(), cfg)
        new_path = cfg["background"]["path"]
        self.assertEqual(new_path, sched._bg_path)
        self.assertNotEqual(new_path, str(src))
        with Image.open(new_path) as im:
            self.assertEqual(im.size, (160, 80))
        self.assertEqual(Path(new_path).parent,
                         (self.root / "backgrounds").resolve())


class BackgroundHardCutTests(unittest.TestCase):
    """背景换图是硬切：一帧就是新图，没有任何淡入/过渡状态。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _png(self, name, color):
        p = self.root / name
        Image.new("RGB", (160, 80), color).save(p)
        return str(p)

    def test_load_paints_full_image_on_first_frame(self):
        bg = M.Background()
        bg.load(self._png("white.png", (255, 255, 255)))
        self.assertFalse(hasattr(bg, "CROSSFADE_SECS"), "淡入常量应该删掉了")
        self.assertFalse(hasattr(bg, "fading"), "淡入状态应该删掉了")
        self.assertEqual(bg.tick(0.0).convert("RGB").getpixel((80, 40)),
                         (255, 255, 255), "装载后第一帧就是完整新图")
        self.assertEqual(bg.tick(0.01).convert("RGB").getpixel((80, 40)),
                         (255, 255, 255))

    def test_direct_frames_assignment_stays_sharp(self):
        # 旧的 CompositeTests 直接塞 frames，必须原样返回（像素精确）
        bg = M.Background()
        bg.frames = [Image.new("RGB", (160, 80), (7, 8, 9))]
        self.assertEqual(bg.tick(0.0).getpixel((0, 0)), (7, 8, 9))

    def test_playlist_switch_is_hard_cut(self):
        a = self._png("a.png", (255, 0, 0))
        b = self._png("b.png", (0, 0, 255))
        bg = M.Background()
        bg.load_playlist([a, b], "fill", 5.0, False)
        bg.tick(0.0)                       # 首次 tick 才开始计时
        self.assertFalse(bg.needs_frames(4.9), "没到 5 s 不该换图")
        self.assertEqual(bg.tick(4.9).convert("RGB").getpixel((80, 40)),
                         (255, 0, 0))
        self.assertTrue(bg.needs_frames(5.0), "到点就该请求一帧")
        # 到点这一帧直接是新图：没有 0<x<255 的混色中间态
        self.assertEqual(bg.tick(5.0).convert("RGB").getpixel((80, 40)),
                         (0, 0, 255))
        self.assertEqual(bg.switch_count, 1)
        self.assertFalse(bg.needs_frames(5.4), "换完图不该再推淡入帧")

    def test_playlist_interval_matches_configured_seconds(self):
        a = self._png("a.png", (255, 0, 0))
        b = self._png("b.png", (0, 0, 255))
        bg = M.Background()
        bg.load_playlist([a, b], "fill", 2.0, False)
        bg.tick(0.0)
        stamps = []
        last = 0
        for i in range(0, 1000):           # 0 … 9.99 s，步进 0.01 s
            t = i / 100.0
            if bg.needs_frames(t):
                bg.tick(t)
                if bg.switch_count != last:
                    last = bg.switch_count
                    stamps.append(t)
        self.assertEqual(stamps, [2.0, 4.0, 6.0, 8.0],
                         "interval=2 的换图时刻：%s" % (stamps,))

    def test_playlist_cadence_does_not_drift_when_late(self):
        a = self._png("a.png", (255, 0, 0))
        b = self._png("b.png", (0, 0, 255))
        bg = M.Background()
        bg.load_playlist([a, b], "fill", 2.0, False)
        bg.tick(0.0)
        bg.tick(2.3)                       # 主循环晚 0.3 s 才轮到换图
        self.assertEqual(bg.switch_count, 1)
        stamps = []
        for i in range(230, 650):
            t = i / 100.0
            if bg.needs_frames(t):
                bg.tick(t)
                stamps.append(t)
        self.assertEqual(stamps, [4.0, 6.0],
                         "节拍按计划时刻推进，迟到不能累加成漂移：%s" % (stamps,))

    def test_needs_frames_for_animated_background(self):
        src = self.root / "anim.gif"
        a = Image.new("RGB", (160, 80), (255, 0, 0))
        b = Image.new("RGB", (160, 80), (0, 0, 255))
        a.save(src, save_all=True, append_images=[b], duration=80, loop=0)
        bg = M.Background()
        bg.load(str(src))
        self.assertGreaterEqual(len(bg.frames), 2)
        bg.tick(0.0)
        self.assertFalse(bg.needs_frames(0.01), "还没到帧时长，不用推帧")
        self.assertTrue(bg.needs_frames(0.2), "动图翻页必须靠主循环持续要帧")
        self.assertEqual(bg.tick(0.2).convert("RGB").getpixel((80, 40)),
                         (0, 0, 255))


class ClockColorTests(unittest.TestCase):
    def test_parse_color_accepts_hex_with_or_without_hash(self):
        self.assertEqual(clock_mod._parse_color("#ffcc00", "#FFFFFF"), "#FFCC00")
        self.assertEqual(clock_mod._parse_color("00FF00", "#FFFFFF"), "#00FF00")
        self.assertEqual(clock_mod._parse_color(" #AbCdEf ", "#FFFFFF"), "#ABCDEF")

    def test_parse_color_falls_back(self):
        self.assertEqual(clock_mod._parse_color("nope", "#B0B0B0"), "#B0B0B0")
        self.assertEqual(clock_mod._parse_color("", "#B0B0B0"), "#B0B0B0")
        self.assertEqual(clock_mod._parse_color(None, "#B0B0B0"), "#B0B0B0")
        self.assertEqual(clock_mod._parse_color("#12345", "#B0B0B0"), "#B0B0B0")

    def test_schema_has_three_color_fields(self):
        keys = [f.key for f in clock_mod.ClockWidget.config_schema]
        self.assertEqual(keys, ["time_color", "date_color", "week_color"])
        for f in clock_mod.ClockWidget.config_schema:
            self.assertEqual(f.type, "text")

    def test_render_uses_configured_colors(self):
        def render(color):
            ctx = M.RenderContext(device=None, now=1700000000.0,
                                  orientation=M.LANDSCAPE, state={},
                                  config={"widget_config": {"clock": {
                                      "time_color": color}}})
            return clock_mod.ClockWidget().render(ctx)

        white = render("#FFFFFF")
        yellow = render("#FFCC00")
        self.assertIsNotNone(white)
        self.assertIsNotNone(yellow)
        self.assertNotEqual(white.tobytes(), yellow.tobytes(),
                            "改时间颜色后渲染结果应该变化（缓存键要带上颜色）")


class IndexHtmlTests(unittest.TestCase):
    def test_favicon_wired(self):
        html = M.INDEX_HTML
        # PNG 排前面：部分浏览器/反代不认 ICO；两个都带 ?v= 版本串，
        # 这样换图标时不会被旧的 404/旧图标缓存卡住。
        self.assertIn('<link rel="icon" type="image/png" sizes="32x32" '
                      'href="/favicon.png?v=2">', html)
        self.assertIn('<link rel="icon" type="image/x-icon" '
                      'href="/favicon.ico?v=2">', html)
        self.assertIn('href="/favicon.png?v=2"', html)
        self.assertNotIn('<link rel="icon" href="/favicon.ico" '
                         'type="image/x-icon">', html)

    def test_multi_background_ui_replaced_textarea(self):
        self.assertIn('id="bg-list"', M.INDEX_HTML)
        self.assertIn('id="bg-count"', M.INDEX_HTML)
        self.assertNotIn('id="bg-paths"', M.INDEX_HTML)
        script = _script_text()
        self.assertIn("renderBgList()", script)
        self.assertIn("moveBg(i, -1)", script)
        self.assertIn("function removeBg(i)", script)
        self.assertNotIn("function bgPaths()", script)

    def test_firmware_upload_shows_size_and_progress(self):
        html = M.INDEX_HTML
        self.assertIn('id="fw-pick"', html)
        script = _script_text()
        self.assertIn("formatBytes", script)
        self.assertIn("xhr.upload.onprogress", script)
        self.assertIn("X-Purpose", script)
        # 升级收尾：前端轮询 /api/flash/phase，设备重启期间显示「设备重启中…」，
        # 拿到 {"ok":true,"fw":"x.y.z"} 后显示升级完成，不再僵在“已写入”。
        self.assertIn("/api/flash/phase", script)
        self.assertIn("设备重启中", script)
        self.assertIn("升级完成", script)

    def test_single_default_config_path(self):
        src_text = Path(M.__file__).read_text(encoding="utf-8")
        self.assertEqual(src_text.count("def _default_config_path("), 1)


class FakeManager:
    """最小 manager 替身：WebUI 只用到 active()/active_id/describe()。"""

    def __init__(self, sched, dev_id="dev1"):
        self._sched = sched
        self.active_id = dev_id

    def active(self):
        return self._sched

    def describe(self):
        return [{"id": self.active_id, "port": "", "connected": False,
                 "offline": True, "fps": 0, "dropped": 0, "err": ""}]

    def select(self, did):
        return did == self.active_id


class WebUIHttpTests(unittest.TestCase):
    """真起一个 WebUI 线程，用 urllib 把接口和页面元素都抓一遍。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls._data_patch = mock.patch.object(M, "_data_dir", lambda: cls.root)
        cls._data_patch.start()
        M._BG_NORM_CACHE.clear()
        M._BG_THUMB_CACHE.clear()
        cls.cfg = {"web": {"host": "127.0.0.1", "port": _free_port()},
                   "pages": []}
        cls.sched = M.Scheduler(M.OfflineDevice(), cls.cfg)
        cls.ui = M.WebUI(FakeManager(cls.sched), cls.cfg,
                         str(cls.root / "config.json"))
        cls.ui.start()
        cls.base = "http://127.0.0.1:%d" % cls.cfg["web"]["port"]
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(cls.base + "/", timeout=0.5) as r:
                    r.read()
                break
            except Exception:
                time.sleep(0.05)
        else:
            cls.ui.stop()
            raise unittest.SkipTest("WebUI 没能在 10 秒内起来")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.ui.stop()
            cls.ui.join(5)
        finally:
            cls._data_patch.stop()
            cls.tmp.cleanup()

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()

    def _fetch(self, path):
        """带响应头抓一次（favicon 要看 Cache-Control / Content-Type）。"""
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return r.status, r.headers, r.read()

    def _post(self, path, body, headers=None, raw=None):
        data = raw if raw is not None else json.dumps(body).encode()
        hdrs = {"Content-Type": "application/json"}
        hdrs.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data,
                                     headers=hdrs, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8"))

    def test_index_page_and_favicon(self):
        status, ctype, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        text = body.decode("utf-8")
        for el_id in ("bg-list", "bg-count", "asset-boot-file-wrap",
                      "asset-standby-file-wrap", "fw-pick", "fw-name",
                      "msg-flash"):
            self.assertIn('id="%s"' % el_id, text)
        status, ctype, ico = self._get("/favicon.ico")
        self.assertEqual(status, 200)
        self.assertIn("image/x-icon", ctype)
        self.assertTrue(ico.startswith(b"\x00\x00\x01\x00"),
                        "favicon 应该是真正的 ICO 内容")

    def test_favicon_formats_and_cache_headers(self):
        # 浏览器实测“请求 200 但图标不显示”：旧 404 被缓存 + 只给 ICO。
        # 现在 .ico 与 .png 都带 ?v=2，PNG 由 Pillow 从 ico 里抽最大尺寸转，
        # 两个都允许缓存 1 小时（版本串变了才会重新拉）。
        status, hdrs, ico = self._fetch("/favicon.ico?v=2")
        self.assertEqual(status, 200)
        self.assertIn("image/x-icon", hdrs.get("Content-Type", ""))
        self.assertTrue(ico.startswith(b"\x00\x00\x01\x00"),
                        "favicon.ico 应该是真正的 ICO 内容")
        self.assertEqual(hdrs.get("Cache-Control"), M.FAVICON_CACHE_CONTROL)

        status, hdrs, png = self._fetch("/favicon.png?v=2")
        self.assertEqual(status, 200)
        self.assertIn("image/png", hdrs.get("Content-Type", ""))
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"),
                        "favicon.png 必须是 PNG 魔数（不是 ico 字节）")
        self.assertEqual(hdrs.get("Cache-Control"), M.FAVICON_CACHE_CONTROL)
        with Image.open(io.BytesIO(png)) as im:
            self.assertEqual(im.format, "PNG")
            self.assertGreaterEqual(im.size[0], 16)

    def test_flash_phase_endpoint_is_get(self):
        # 前端用 GET 轮询 /api/flash/phase：升级请求还挂着的时候靠它把提示
        # 从「正在写入」切到「设备重启中…」。以前只有 POST 分支，GET 会 404。
        status, ctype, body = self._get("/api/flash/phase")
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype)
        data = json.loads(body.decode("utf-8"))
        self.assertTrue(data.get("ok"))
        self.assertIn("phase", data)
        self.assertIn("elapsed", data)

    def test_status_touch_asset_endpoints(self):
        # /api/status 是给轮询用的状态快照，本来就没有 ok 字段
        status, ctype, body = self._get("/api/status")
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype)
        data = json.loads(body.decode("utf-8"))
        for key in ("connected", "touch", "metrics", "background_enabled"):
            self.assertIn(key, data, "/api/status")
        for path in ("/api/touch_config", "/api/asset"):
            status, ctype, body = self._get(path)
            self.assertEqual(status, 200, path)
            self.assertIn("application/json", ctype, path)
            data = json.loads(body.decode("utf-8"))
            self.assertIn("ok", data, path)

    def test_upload_background_and_play_it(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "big_user_pic.png"
            img = Image.new("RGB", (400, 300), (12, 34, 56))
            img.save(src)
            data = src.read_bytes()
            qname = urllib.parse.quote(src.name)
            status, got = self._post("/api/upload", None,
                                     headers={"X-Filename": qname,
                                              "X-Purpose": "background"},
                                     raw=data)
            self.assertEqual(status, 200)
            self.assertTrue(got.get("ok"), got)
            self.assertTrue(got.get("normalized"), got)
            path = Path(got["path"])
            self.assertEqual(path.parent,
                             (self.root / "backgrounds").resolve())
            with Image.open(path) as im:
                self.assertEqual(im.size, (160, 80))
            # 播放这张图
            status, j = self._post("/api/background", {
                "enabled": True, "source": "single", "path": str(path),
                "mode": "fill", "paths": []})
            self.assertEqual(status, 200)
            self.assertTrue(j.get("ok"), j)
            self.assertEqual(j.get("path"), str(path))
            # 列表缩略图
            q = urllib.parse.urlencode({"path": str(path)})
            status, ctype, thumb = self._get("/api/bgthumb?" + q)
            self.assertEqual(status, 200)
            self.assertIn("image/png", ctype)
            self.assertTrue(thumb.startswith(b"\x89PNG"))

    def test_bgthumb_rejects_outside_paths(self):
        outside = self.root / "outside.png"
        Image.new("RGB", (160, 80), (1, 2, 3)).save(outside)
        q = urllib.parse.urlencode({"path": str(outside)})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._get("/api/bgthumb?" + q)
        self.assertEqual(cm.exception.code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)