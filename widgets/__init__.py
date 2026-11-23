import importlib
import importlib.util
import json
import logging
import os
import pkgutil
import shutil
import subprocess
import sys
import sysconfig
import types
import urllib.request
import zipfile
from pathlib import Path

log = logging.getLogger("msu2.widgets")

_pkg_path = Path(__file__).parent

_EXT_PKG = "msu2_ext_widgets"
_EXT_DIR_ENV = "MSU2_WIDGETS_DIR"

_loaded = []
_failed = []
_ext_loaded = []
_ext_failed = []
_ext_dirs = []

for finder, name, ispkg in pkgutil.iter_modules([str(_pkg_path)]):
    if name.startswith("_") or ispkg:
        continue
    try:
        importlib.import_module(f".{name}", __package__)
        _loaded.append(name)
    except Exception as e:
        _failed.append((name, str(e)))
        log.error("加载插件模块 %s 失败: %s", name, e, exc_info=True)


def _app_dir():
    if getattr(sys, "frozen", False):
        return Path(os.path.dirname(os.path.abspath(sys.executable)))
    return _pkg_path.parent


def _candidate_dirs():
    out = []
    raw = os.environ.get(_EXT_DIR_ENV, "")
    for item in raw.replace(";", os.pathsep).split(os.pathsep):
        item = item.strip().strip('"').strip("'")
        if item:
            out.append(Path(item))
    app = _app_dir()
    if getattr(sys, "frozen", False):
        out.append(app / "widgets")
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            base = os.environ.get("XDG_DATA_HOME")
        if not base:
            try:
                base = str(Path.home() / ".local" / "share")
            except Exception:
                base = ""
        if base:
            out.append(Path(base) / "MSU2_MINI" / "widgets")
    else:
        out.append(app / "widgets_ext")
    return out


def _unique_dirs():
    seen = []
    result = []
    own = None
    try:
        own = _pkg_path.resolve()
    except Exception:
        own = _pkg_path
    for cand in _candidate_dirs():
        try:
            path = cand.expanduser().resolve()
        except Exception:
            continue
        if path == own or path in seen:
            continue
        if not path.is_dir():
            continue
        seen.append(path)
        result.append(path)
    return result


def _load_one(entry):
    if entry.is_dir():
        target = entry / "__init__.py"
        if not target.is_file():
            return
        mod_name = f"{_EXT_PKG}.{entry.name}"
        spec = importlib.util.spec_from_file_location(
            mod_name, target, submodule_search_locations=[str(entry)])
    elif entry.suffix.lower() == ".py":
        mod_name = f"{_EXT_PKG}.{entry.stem}"
        spec = importlib.util.spec_from_file_location(mod_name, entry)
    else:
        return
    if spec is None or spec.loader is None:
        raise ImportError("无法为 %s 创建模块规格" % entry)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(mod_name, None)
        raise


# ---------------------------------------------------------------------------
# 外部插件依赖：插件在自己的目录里放 requirements.txt，
# 缺包时先试 pip（有 Python 时），没 Python 就直接从 PyPI 拉纯 Python / 同 ABI 的
# wheel 解压到 _deps，再把该目录塞进 sys.path。
# ---------------------------------------------------------------------------

_DEPS_DIRNAME = "_deps"
_dep_dirs = []


def _deps_root(folder):
    return Path(folder) / _DEPS_DIRNAME


def _add_dep_path(path):
    p = str(path)
    if Path(p).is_dir() and p not in sys.path:
        sys.path.insert(0, p)
        _dep_dirs.append(p)


def _requirements_for(entry):
    cands = []
    if entry.is_dir():
        cands.append(entry / "requirements.txt")
    else:
        cands.append(entry.with_suffix(".requirements.txt"))
        cands.append(entry.parent / "requirements.txt")
    for c in cands:
        if c.is_file():
            return c
    return None


def _missing_module(exc):
    name = getattr(exc, "name", None)
    if not name:
        text = str(exc)
        marker = "No module named "
        if marker in text:
            name = text.split(marker, 1)[1].strip().strip("'\"")
    if not name:
        return None
    return name.split(".")[0]


def _python_cmd():
    """打包成 exe 后 sys.executable 是 exe 本身，得另找系统 Python。"""
    if os.environ.get("MSU2_NO_PYTHON"):
        return None
    if not getattr(sys, "frozen", False):
        return [sys.executable]
    exe = shutil.which("py")
    if exe:
        return [exe, "-3"]
    for name in ("python", "python3"):
        exe = shutil.which(name)
        if exe:
            return [exe]
    return None


def _pip_install(pkgs, target):
    cmd = _python_cmd()
    if not cmd:
        return False, "系统里没有 Python，跳过 pip"
    try:
        target.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return False, "无法创建依赖目录: %s" % e
    args = cmd + ["-m", "pip", "install", "--disable-pip-version-check",
                  "--no-input", "--quiet", "--target", str(target)] + list(pkgs)
    log.info("安装插件依赖: %s", " ".join(pkgs))
    try:
        res = subprocess.run(args, capture_output=True, text=True, timeout=300)
    except Exception as e:
        return False, "pip 调用失败: %r" % e
    if res.returncode != 0:
        tail = [ln for ln in (res.stderr or res.stdout or "").strip().splitlines() if ln]
        return False, "pip 失败: " + " / ".join(tail[-3:])
    return True, "pip"


def _platform_tag():
    try:
        return sysconfig.get_platform().replace("-", "_").replace(".", "_")
    except Exception:
        return ""


def _wheel_score(filename):
    if not filename.endswith(".whl"):
        return 0
    parts = filename[:-4].split("-")
    if len(parts) < 5:
        return 0
    py, abi, plat = parts[-3], parts[-2], parts[-1]
    impl = "cp%d%d" % sys.version_info[:2]
    here = _platform_tag()
    score = 0
    if abi == "none" and plat == "any":
        score = 1
    elif plat == here or plat.startswith("none-"):
        if abi == "abi3" or abi.startswith(impl) or abi == "none":
            score = 3
    elif py == "py3" and abi == "none":
        score = 1
    return score


def _pypi_requires(name, version=None):
    """读 PyPI 元数据里的 Requires-Dist（只取运行期依赖，忽略 extras）。"""
    try:
        url = "https://pypi.org/pypi/%s/json" % name
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.load(r)
    except Exception as e:
        log.warning("查询 PyPI %s 失败: %s", name, e)
        return []
    out = []
    for raw in data.get("info", {}).get("requires_dist") or []:
        line = raw.strip()
        if not line or "extra ==" in line:
            continue
        base = line.split(";")[0].split("[")[0].split("(")[0].split("<")[0]
        base = base.split(">")[0].split("=")[0].split("!")[0].split("~")[0].strip()
        if base and base.lower() not in out:
            out.append(base)
    return out


def _download_wheel(name, target, depth=0, seen=None):
    if seen is None:
        seen = set()
    if name.lower() in seen or depth > 4:
        return True, ""
    seen.add(name.lower())
    try:
        url = "https://pypi.org/pypi/%s/json" % name
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.load(r)
    except Exception as e:
        return False, "查询 PyPI %s 失败: %s" % (name, e)
    files = [(f.get("filename", ""), f.get("url", "")) for f in data.get("urls", [])]
    scored = [(score, fn, u) for fn, u in files for score in [_wheel_score(fn)] if score]
    if not scored:
        return False, "%s 没有可用的 wheel（可能需要编译）" % name
    scored.sort(key=lambda x: -x[0])
    _, filename, file_url = scored[0]
    try:
        target.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(file_url, timeout=60) as r:
            payload = r.read()
        import io
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            for member in zf.namelist():
                if member.endswith("/"):
                    continue
                if ".dist-info/" in member and not member.endswith("METADATA"):
                    continue
                zf.extract(member, target)
    except Exception as e:
        return False, "下载 %s 失败: %s" % (filename, e)
    log.info("已解压依赖 %s -> %s", filename, target)
    for dep in _pypi_requires(name):
        ok, err = _download_wheel(dep, target, depth + 1, seen)
        if not ok:
            log.warning("依赖 %s 未能安装: %s", dep, err)
    return True, filename


def _ensure_deps(entry, missing):
    folder = entry.parent
    root = _deps_root(folder)
    if missing and missing in getattr(sys, "stdlib_module_names", ()):
        return False, ("缺的是标准库 %s：打包版解释器只带主程序用到的模块，"
                       "请改用源码方式运行（python msu2_mini.py）或重新打包时加上该模块" % missing)
    req = _requirements_for(entry)
    pkgs = []
    if req is not None:
        try:
            pkgs = [ln.strip() for ln in req.read_text(encoding="utf-8", errors="replace").splitlines()
                    if ln.strip() and not ln.strip().startswith("#")]
        except Exception as e:
            log.warning("读取 %s 失败: %s", req, e)
    if not pkgs and missing:
        pkgs = [missing]
    if not pkgs:
        return False, "没有 requirements.txt，也判断不出缺什么"
    ok, why = _pip_install(pkgs, root)
    if not ok and missing:
        ok, why = _download_wheel(missing, root)
    if ok:
        _add_dep_path(root)
    log.info("插件 %s 依赖处理: %s（%s）", entry.name, "成功" if ok else "失败", why)
    return ok, why


def _load_external():
    dirs = _unique_dirs()
    if not dirs:
        return
    pkg = types.ModuleType(_EXT_PKG)
    pkg.__path__ = [str(d) for d in dirs]
    pkg.__package__ = _EXT_PKG
    sys.modules[_EXT_PKG] = pkg
    for folder in dirs:
        _ext_dirs.append(str(folder))
        _add_dep_path(_deps_root(folder))       # 目录里已有的 _deps 直接挂上
        try:
            entries = sorted(folder.iterdir(), key=lambda p: p.name)
        except Exception as e:
            log.error("读取外部插件目录 %s 失败: %s", folder, e)
            continue
        for entry in entries:
            if entry.name.startswith("_") or entry.name.startswith("."):
                continue
            if entry.is_dir():
                _add_dep_path(entry / _DEPS_DIRNAME)
            err = None
            for attempt in range(3):
                try:
                    _load_one(entry)
                    _ext_loaded.append("%s (%s)" % (entry.name, folder))
                    err = None
                    break
                except BaseException as e:
                    err = e
                    missing = _missing_module(e) if isinstance(e, ModuleNotFoundError) else None
                    if missing and attempt < 2:
                        ok, why = _ensure_deps(entry, missing)
                        if ok:
                            continue
                    break
            if err is not None:
                _ext_failed.append((str(entry), str(err)))
                log.error("加载外部插件 %s 失败: %s", entry, err, exc_info=(err.__traceback__ is not None))


_load_external()


def external_report():
    return {
        "dirs": list(_ext_dirs),
        "loaded": list(_ext_loaded),
        "failed": list(_ext_failed),
        "builtin": list(_loaded),
        "builtin_failed": list(_failed),
    }


log.info("插件模块加载完成: 内置 %d 个 (%s)",
         len(_loaded), ", ".join(_loaded))
if _ext_dirs:
    log.info("外部插件目录: %s", ", ".join(_ext_dirs))
if _loaded and _ext_loaded:
    log.info("外部插件加载 %d 个: %s", len(_ext_loaded), ", ".join(_ext_loaded))
if _failed:
    log.warning("内置插件模块加载失败 %d 个: %s", len(_failed), _failed)
if _ext_failed:
    log.warning("外部插件加载失败 %d 个: %s", len(_ext_failed), _ext_failed)