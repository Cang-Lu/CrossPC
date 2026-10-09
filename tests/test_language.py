"""Language policy: the repository is English-only.

The single exception is ``README.zh-CN.md``, which is kept as the original
Chinese README for the author's own reference.

This is enforced mechanically rather than by review discipline: one Chinese
comment pasted into a new module is almost invisible in a diff, so the rule is
checked by ``tools/check_english.py`` and asserted here, in every CI matrix job.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_english as CE                    # noqa: E402  (tools/ is not a package)


class TestEnglishOnly(unittest.TestCase):
    def test_no_cjk_outside_the_chinese_readme(self):
        files = list(CE.iter_files(str(ROOT)))
        self.assertGreater(len(files), 20, "file discovery looks broken")
        violations, _unreadable = CE.scan(files, str(ROOT))
        if violations:
            preview = "\n".join("  %s:%d: %s" % item for item in violations[:15])
            self.fail("%d line(s) still contain CJK characters outside %s:\n%s"
                      % (len(violations), ", ".join(sorted(CE.ALLOWED)), preview))

    def test_chinese_readme_exists_and_stays_chinese(self):
        """Keeping a Chinese README is only useful if the file really is one."""
        path = ROOT / "README.zh-CN.md"
        self.assertTrue(path.is_file(), "README.zh-CN.md is missing")
        self.assertTrue(CE.CJK.search(path.read_text(encoding="utf-8")),
                        "README.zh-CN.md contains no Chinese text")

    def test_english_readme_links_to_the_chinese_one(self):
        """Otherwise nobody can find the Chinese version."""
        english = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("README.zh-CN.md", english)

    def test_checker_ignores_caches_and_binaries(self):
        for rel in CE.iter_files(str(ROOT)):
            self.assertFalse(rel.endswith(".pyc"), rel)
            self.assertNotIn("__pycache__", rel, rel)
            self.assertFalse(rel.startswith(".git/"), rel)


class TestCheckerItself(unittest.TestCase):
    """The guard has to actually be able to fail, or it guards nothing."""

    def test_detects_chinese_in_a_temp_file(self):
        import tempfile
        from crosspc.util import pick_writable_dir, scratch_prefix
        base = pick_writable_dir()
        path = Path(base) / (scratch_prefix("lang") + "sample.txt")
        try:
            path.write_text("hello \u4f60\u597d world\n", encoding="utf-8")
            violations, _ = CE.scan([path.name], base)
            self.assertEqual(len(violations), 1, violations)
            self.assertEqual(violations[0][1], 1)
        finally:
            path.unlink(missing_ok=True)

    def test_english_text_passes(self):
        import tempfile
        from crosspc.util import pick_writable_dir, scratch_prefix
        base = pick_writable_dir()
        path = Path(base) / (scratch_prefix("lang") + "clean.txt")
        try:
            path.write_text("only ascii here\n", encoding="utf-8")
            violations, _ = CE.scan([path.name], base)
            self.assertEqual(violations, [])
        finally:
            path.unlink(missing_ok=True)

    def test_git_view_is_used_when_available(self):
        """Inside a git checkout the scan should be driven by git's file list."""
        listed = CE._git_file_list(str(ROOT))
        if listed is None:
            self.skipTest("not a git checkout here")
        self.assertIn("crosspc/__init__.py", listed)
        self.assertNotIn("dist/crosspc-0.1.0.zip", listed)   # ignored by .gitignore


if __name__ == "__main__":
    unittest.main()
