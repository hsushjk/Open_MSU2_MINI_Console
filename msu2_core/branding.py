"""资源与路径工具：程序目录、浏览器图标、assets 子目录、日志/数据目录。

从 msu2_mini.py 拆出（行为不变）。这里的目录推导必须和 msu2_mini.py 顶部保持
一致：打包成 exe 时“程序目录”= exe 所在目录，源码运行时 = 项目根目录。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

if getattr(sys, "frozen", False):
    _APP_DIR = Path(os.path.dirname(os.path.abspath(sys.executable)))
else:
    # 源码运行：本文件在 msu2_core/ 里，项目根 = 上一级
    _APP_DIR = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 本文件在 msu2_core/ 里，上一级就是项目根；exe 模式下 = exe 的解包目录（_MEIPASS）。
# 对应原来 msu2_mini.py 里的 Path(__file__).resolve().parent。
_ROOT_DIR = Path(__file__).resolve().parent.parent


def _icon_bytes():
    """浏览器标签图标：和 exe 用同一张 icon.ico。"""
    try:
        cands = []
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            cands.append(Path(meipass) / "icon.ico")
        cands.append(_APP_DIR / "icon.ico")
        for f in cands:
            try:
                if f.is_file():
                    return f.read_bytes()
            except Exception:
                pass
    except Exception:
        pass
    return b""


def _asset_dir(name):
    for base in (_APP_DIR, _APP_DIR / "msu2_core", _ROOT_DIR, _ROOT_DIR / "msu2_core"):
        p = base / "assets" / name
        if p.is_dir():
            return p
    return _APP_DIR / "assets" / name


SCREENSHOT_DIR = _APP_DIR / "screenshots"
DEFAULT_GIF_DIR = _asset_dir("gif")


def _data_dir():
    """日志、崩溃记录、上传文件都放在程序同目录；目录不可写才退回用户目录。"""
    bases = [_APP_DIR]
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