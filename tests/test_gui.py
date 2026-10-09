"""GUI smoke tests: no window pops up, but the GUI build/drag/snap/save path runs end to end.

The trick is to create the Tk root window and withdraw (hide) it immediately, so
nothing flashes onto the user's screen while the widgets, canvas, coordinate
conversion and snap algorithm all really run. Skipped automatically with no display.
"""
from __future__ import annotations

import os
import unittest

from crosspc.config import Config, ClientEntry, SizeCache
from crosspc.layout import Rect
from crosspc.util import Log, pick_writable_dir, scratch_prefix

try:
    import tkinter as tk
    HAS_TK = True
except Exception:                                  # pragma: no cover
    HAS_TK = False


def _display_available() -> bool:
    if not HAS_TK:
        return False
    try:
        root = tk.Tk()
    except Exception:
        return False
    root.withdraw()
    root.destroy()
    return True


@unittest.skipUnless(_display_available(), "no usable display environment, skipping GUI tests")
class TestGui(unittest.TestCase):
    def setUp(self):
        from crosspc.gui import GuiApp
        self.dir = pick_writable_dir()
        self.prefix = scratch_prefix("guitest")
        self.path = os.path.join(self.dir, self.prefix + "crosspc.json")
        self.cfg = Config.defaults(self.path)
        self.cfg.name = "win11"
        self.cfg.clients = [ClientEntry(name="debian", host="10.0.0.5",
                                        rect=Rect(1920, 0, 2560, 1440))]
        self.root = tk.Tk()
        self.root.withdraw()                 # never show the window
        self.app = GuiApp(self.root, self.cfg, Log("error"))
        # The GUI detects the "local resolution" at runtime: the dev machine is
        # 1920x1080 while the CI Windows runner only has 1024x768 -- so an assertion
        # like "snap to x=1920" is bound to fail on CI (the snap distance is outside
        # the tolerance, so no snap happens at all). Tests must pin the geometry
        # themselves instead of trusting the host screen. That is exactly how CI
        # first went red.
        self.app.server_rect = Rect(0, 0, 1920, 1080)
        self.app.monitors = [Rect(0, 0, 1920, 1080)]
        self.app.refresh_list()
        self.app.canvas.redraw()
        self.root.update_idletasks()

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:
            pass
        for p in (self.path, self.path + ".tmp"):
            try:
                os.remove(p)
            except OSError:
                pass

    def test_canvas_transform_roundtrip(self):
        self.app.canvas.redraw()
        for point in ((0, 0), (1919, 1079), (-1280, 300)):
            cx, cy = self.app.canvas.to_canvas(*point)
            back = self.app.canvas.to_virtual(cx, cy)
            self.assertLessEqual(abs(back[0] - point[0]), 3)
            self.assertLessEqual(abs(back[1] - point[1]), 3)

    def test_hit_test_and_selection(self):
        self.app.canvas.redraw()
        cx, cy = self.app.canvas.to_canvas(2000, 100)      # inside the client
        self.assertEqual(self.app.canvas._hit(cx, cy), "debian")
        # the server box cannot be dragged
        cx, cy = self.app.canvas.to_canvas(100, 100)
        self.assertIsNone(self.app.canvas._hit(cx, cy))

    def test_move_and_snap_flush_right(self):
        # drag to "12 pixels short of the server's right edge"; releasing must snap
        # flush against it
        self.app.move_client("debian", 1908, 5, snap=True)
        self.assertEqual(self.cfg.clients[0].rect, Rect(1920, 0, 2560, 1440))

    def test_snap_flush_left_and_below(self):
        self.app.move_client("debian", -2570, 300, snap=True)
        self.assertEqual(self.cfg.clients[0].rect.x, -2560)
        self.app.move_client("debian", 300, 1090, snap=True)
        self.assertEqual(self.cfg.clients[0].rect.y, 1080)

    def test_snap_aligns_tops(self):
        self.app.move_client("debian", 1920, 6, snap=False)
        self.app.snap_client("debian")
        self.assertEqual(self.cfg.clients[0].rect, Rect(1920, 0, 2560, 1440))

    def test_add_and_delete_client(self):
        before = len(self.cfg.clients)
        self.app.add_client()
        self.assertEqual(len(self.cfg.clients), before + 1)
        self.assertIsNotNone(self.app.selected)
        self.app.selected = self.cfg.clients[-1].name
        self.cfg.clients = [c for c in self.cfg.clients
                            if c.name != self.app.selected]
        self.assertEqual(len(self.cfg.clients), before)

    def test_snap_works_on_other_resolutions(self):
        """Snapping is pure geometry and must not be bound to the host resolution.

        CI taught us this one: the three cases above had "local 1920x1080" baked in
        and inevitably went red on the CI 1024x768 runner. Here we explicitly switch
        to another resolution and check again, so if anyone ever writes the
        resolution assumption back into the code, this case reports it at once.
        """
        self.app.server_rect = Rect(0, 0, 1024, 768)
        self.app.move_client("debian", 1014, 3, snap=True)
        self.assertEqual(self.cfg.clients[0].rect, Rect(1024, 0, 2560, 1440))
        self.app.move_client("debian", 40, 778, snap=True)
        self.assertEqual(self.cfg.clients[0].rect, Rect(40, 768, 2560, 1440))

    def test_form_apply_updates_model(self):
        self.app.select("debian")
        self.app.vars["w"].set("1280")
        self.app.vars["h"].set("800")
        self.app.vars["x"].set("-1280")
        self.app.vars["y"].set("0")
        self.app.vars["host"].set("192.168.1.77")
        self.app.apply_form()
        entry = self.cfg.client_by_name("debian")
        self.assertEqual(entry.rect, Rect(-1280, 0, 1280, 800))
        self.assertEqual(entry.host, "192.168.1.77")

    def test_form_rejects_bad_numbers(self):
        """Invalid input must neither crash nor corrupt the config (this goes through
        messagebox, which is patched out here)."""
        import tkinter.messagebox as mb
        calls = []
        old = mb.showerror
        mb.showerror = lambda *a, **k: calls.append(a)
        try:
            self.app.select("debian")
            self.app.vars["w"].set("not a number")
            self.app.apply_form()
        finally:
            mb.showerror = old
        self.assertTrue(calls)
        self.assertEqual(self.cfg.clients[0].rect, Rect(1920, 0, 2560, 1440))

    def test_save_writes_config(self):
        import tkinter.messagebox as mb
        old = mb.showinfo
        mb.showinfo = lambda *a, **k: None
        try:
            self.app.save()
        finally:
            mb.showinfo = old
        self.assertTrue(os.path.exists(self.path))
        again = Config.load(self.path)
        self.assertEqual(again.clients[0].name, "debian")

    def test_save_rejects_bad_hotkey(self):
        import tkinter.messagebox as mb
        calls = []
        old_e, old_i = mb.showerror, mb.showinfo
        mb.showerror = lambda *a, **k: calls.append(a)
        mb.showinfo = lambda *a, **k: None
        try:
            self.app.vars["g_panic"].set("ctrl+not-a-key")
            self.app.save()
        finally:
            mb.showerror, mb.showinfo = old_e, old_i
        self.assertTrue(calls)

    def test_uses_cached_client_size(self):
        """A client that has never connected is drawn with the real resolution from the cache."""
        from crosspc.gui import GuiApp
        cfg = Config.defaults(self.path)
        cfg.clients = [ClientEntry(name="debian", rect=None)]
        cache = SizeCache(SizeCache.default_path(cfg.path))
        cache.set("debian", 2560, 1440)
        app = GuiApp(self.root, cfg, Log("error"))
        app.server_rect = Rect(0, 0, 1920, 1080)      # does not depend on the host resolution
        rects = [r for n, r, s, _ in app.machines_for_canvas() if not s]
        self.assertEqual(rects[0], Rect(0, 0, 2560, 1440))
        del app


if __name__ == "__main__":
    unittest.main()
