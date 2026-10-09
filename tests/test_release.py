"""打包脚本的回归测试。

最要紧的一条: **绝不能把用户自己的 crosspc.json / crosspc.cache.json 打进
发行包** —— 里面有 IP、可能还有 token。这条测试就是那个红线。
"""
from __future__ import annotations

import os
import shutil
import sys
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import make_release as MR           # noqa: E402  (脚本不在包里, 要手动加路径)


class TestMakeRelease(unittest.TestCase):
    def setUp(self):
        self.created = []
        for name in MR.EXCLUDE_NAMES:
            path = ROOT / name
            if not path.exists():
                path.write_text('{"name": "不该被打包", "token": "秘密"}',
                                encoding="utf-8")
                self.created.append(path)

    def tearDown(self):
        for path in self.created:
            try:
                os.remove(path)
            except OSError:
                pass

    def test_private_config_is_never_packaged(self):
        rels = [rel for _, rel in MR.collect(with_tests=True)]
        for name in MR.EXCLUDE_NAMES:
            self.assertNotIn(name, rels,
                             "%s 被打进包里了 —— 里面有 IP/token!" % name)

    def test_no_pyc_or_cache_dirs(self):
        for _, rel in MR.collect(with_tests=True):
            self.assertFalse(rel.endswith((".pyc", ".pyo")), rel)
            self.assertNotIn("__pycache__", rel, rel)

    def test_expected_core_files_present(self):
        rels = {rel for _, rel in MR.collect(with_tests=True)}
        for want in ("crosspc/__init__.py", "crosspc/backend/windows.py",
                     "crosspc/backend/linux.py", "crosspc/image.py",
                     "README.md", "pyproject.toml", "tools/install_linux.sh"):
            self.assertIn(want, rels)

    def test_build_produces_readable_zip(self):
        out = ROOT / "dist"
        target = MR.build(out / "_test", with_tests=False, top="crosspc-zip-test")
        try:
            with zipfile.ZipFile(target) as zf:
                names = zf.namelist()
                self.assertTrue(any(n.endswith("crosspc/__init__.py")
                                    for n in names))
                self.assertFalse(any("tests/" in n for n in names),
                                 "--no-tests 时不该有 tests/")
                # zip 必须能通过完整性检查
                self.assertIsNone(zf.testzip())
        finally:
            shutil.rmtree(out / "_test", ignore_errors=True)

    def test_version_matches_package(self):
        from crosspc import __version__
        self.assertEqual(MR.version(), __version__)


if __name__ == "__main__":
    unittest.main()
