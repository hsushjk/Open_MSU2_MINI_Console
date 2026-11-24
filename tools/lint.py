"""未定义名门禁：防止拆包/移动代码时漏 import（F821）、重复定义（F811）。

优先用 ruff（`python -m ruff check --select F821,F811`），没装 ruff 就退回
pyflakes。两者都没装时只打印提示并以 0 退出 —— 门禁尽量严格，但绝不因为
开发机没装 linter 就把构建/测试硬卡死。

用法（仓库根目录，提交前跑）：

    python -X utf8 tools\\lint.py [路径...]

不传路径时扫描 msu2_mini.py / msu2_core / widgets / tests / tools。
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGETS = ("msu2_mini.py", "msu2_core", "widgets", "tests", "tools")
SELECT = "F821,F811"          # F821 未定义名；F811 重复定义（会盖掉 import）


def source_files(extra_paths=None):
    """收集要扫描的 .py 文件（绝对路径）。"""
    out = []
    targets = list(extra_paths) if extra_paths else list(TARGETS)
    for target in targets:
        path = target if os.path.isabs(target) else os.path.join(ROOT, target)
        if os.path.isdir(path):
            for base, dirs, files in os.walk(path):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for name in files:
                    if name.endswith(".py"):
                        out.append(os.path.join(base, name))
        elif os.path.isfile(path):
            out.append(path)
    return sorted(set(out))


def _run(cmd):
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def detect_backend():
    """返回 'ruff' / 'pyflakes' / None。"""
    try:
        probe = _run([sys.executable, "-m", "ruff", "--version"])
        if probe.returncode == 0:
            return "ruff"
    except OSError:
        pass
    try:
        import pyflakes.api  # noqa: F401
    except Exception:
        return None
    return "pyflakes"


def _ruff_problems(paths):
    cmd = [sys.executable, "-m", "ruff", "check", "--select", SELECT,
           "--output-format=concise", "--no-cache"] + list(paths)
    proc = _run(cmd)
    text = (proc.stdout or "") + (proc.stderr or "")
    if "Failed to lint" in text or proc.returncode not in (0, 1):
        raise RuntimeError("ruff 执行失败（rc=%s）：\n%s"
                           % (proc.returncode, text.strip()))
    problems = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line and not line.startswith("All checks passed"):
            problems.append(line)
    return problems


def _pyflakes_problems(paths):
    from pyflakes import api
    from pyflakes import messages as msgmod

    wanted = (msgmod.UndefinedName, msgmod.RedefinedWhileUnused)
    problems = []

    class _Reporter:
        def unexpectedError(self, filename, msg):
            problems.append("%s: %s" % (filename, msg))

        def syntaxError(self, filename, msg, lineno, offset, text):
            problems.append("%s:%s: %s" % (filename, lineno, msg))

        def flake(self, message):
            if isinstance(message, wanted):
                problems.append(str(message))

    reporter = _Reporter()
    for path in paths:
        api.checkPath(path, reporter)
    return problems


def collect(backend, paths=None):
    """用指定后端扫描，返回问题行列表（空列表 = 通过）。"""
    files = source_files(paths)
    if not files:
        return []
    if backend == "ruff":
        return _ruff_problems(files)
    if backend == "pyflakes":
        return _pyflakes_problems(files)
    raise ValueError("未知 linter 后端：%r" % (backend,))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    backend = detect_backend()
    if backend is None:
        print("未安装 ruff / pyflakes，跳过未定义名检查（不算失败）。")
        print("建议：python -m pip install ruff")
        return 0
    files = source_files(argv)
    if not files:
        print("没有找到可扫描的 .py 文件")
        return 0
    print("[%s] 扫描 %d 个文件（F821 未定义名 / F811 重复定义）…"
          % (backend, len(files)))
    try:
        problems = collect(backend, argv)
    except Exception as exc:
        print("检查失败：%s" % (exc,))
        return 2
    if problems:
        print("发现 %d 处问题：" % len(problems))
        for line in problems:
            print("  " + line)
        return 1
    print("OK：未发现未定义名或重复定义")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())