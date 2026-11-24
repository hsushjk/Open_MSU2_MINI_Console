"""每个插件一份配置文件。

主 `config.json` 只放全局设置（设备、端口、亮度、背景、Web、日志…）；
插件私有的配置不再塞进主文件，而是写到：

    <程序目录>/widget_config/<插件名>.json

一个插件一个文件，好处是：
  · 主配置保持短小好读，不会被 17 个插件的字段撑爆；
  · 想单独备份/对比/手改某个插件的配置，直接开那个文件；
  · 插件升级换了配置结构，坏掉也只坏它自己那一份。

兼容：老版本把插件配置放在 `config.json` 的 `widget_config` 里，
启动时会自动搬进这些文件（`migrate_legacy`），之后主文件里就不再出现。
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

DIRNAME = "widget_config"

# path -> 上次真正写盘时用的文本，用来跳过没变化的文件
_WRITTEN: dict[str, str] = {}
_LOCK = threading.RLock()


def store_dir() -> Path:
    """插件配置目录：程序同目录下的 widget_config/。"""
    from .branding import data_dir

    return Path(data_dir()) / DIRNAME


def path_for(name: str) -> Path:
    """插件名 -> 配置文件路径（名字里的非法字符换成下划线）。"""
    safe = re.sub(r"[^0-9A-Za-z_.\-]+", "_", str(name or "")).strip("._") or "plugin"
    return store_dir() / (safe + ".json")


def _read_file(p: Path) -> dict:
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        # 坏文件不要让整机起不来，退回空配置，并由调用方决定是否覆盖
        return {}


def load_one(name: str) -> dict:
    return _read_file(path_for(name))


def load_all(names) -> dict:
    """按插件名列表读回所有配置（没有文件的给空 dict）。"""
    out = {}
    for name in names:
        out[str(name)] = load_one(name)
    return out


def _atomic_write(p: Path, text: str) -> bool:
    tmp = p.with_name(p.name + ".tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
        return True
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return False


def save_one(name: str, data: dict) -> bool:
    """写一个插件的配置；内容没变就不动文件（省磁盘、也不改 mtime）。"""
    if not isinstance(data, dict):
        data = {}
    p = path_for(name)
    text = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
    key = str(p)
    with _LOCK:
        if _WRITTEN.get(key) == text:
            return False
        if _atomic_write(p, text):
            _WRITTEN[key] = text
            return True
        return False


def save_all(mapping: dict) -> int:
    """把所有插件的配置各写一份，返回实际写了几份。

    空的配置**不建空文件**（否则一启动就多出十几个 `{}`）——
    只有当文件已经存在、内容又被清空时，才写一份空的把它清掉。
    """
    written = 0
    if not isinstance(mapping, dict):
        return 0
    for name, data in mapping.items():
        if not isinstance(data, dict):
            continue
        if not data and not path_for(str(name)).exists():
            continue
        if save_one(str(name), data):
            written += 1
    return written


def migrate_legacy(legacy: dict) -> int:
    """把老 config.json 里的 widget_config 搬进独立文件。

    规则：文件已存在就以文件为准（用户可能手改过），只搬文件里没有的。
    返回搬了几份。
    """
    moved = 0
    if not isinstance(legacy, dict):
        return 0
    for name, data in legacy.items():
        if not isinstance(data, dict) or not data:
            continue
        p = path_for(str(name))
        if p.exists():
            continue
        if save_one(str(name), data):
            moved += 1
    return moved


def list_files() -> list:
    """列出目录里已有的配置文件（调试/统计用）。"""
    d = store_dir()
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.glob("*.json"))