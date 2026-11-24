"""未定义名门禁的回归用例：直接复用 tools/lint.py 的扫描逻辑。

没装 ruff / pyflakes 的环境自动 skip，避免把开发机环境差异当成失败。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import lint  # noqa: E402


class LintGateTests(unittest.TestCase):

    def test_no_undefined_names(self):
        backend = lint.detect_backend()
        if backend is None:
            raise unittest.SkipTest("没有安装 ruff / pyflakes，跳过未定义名门禁")
        problems = lint.collect(backend)
        self.assertEqual([], problems,
                         "源码里有未定义名/重复定义（提交前跑 "
                         "python -X utf8 tools\\lint.py）：\n  "
                         + "\n  ".join(problems))


if __name__ == "__main__":
    unittest.main()