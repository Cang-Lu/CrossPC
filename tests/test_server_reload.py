"""Config hot-reload tests: positions changed in the GUI need no server restart.

This is the everyday "set the relative position" flow, so it gets a dedicated
integration test: start a real ServerApp (with FakeBackend, no real keyboard or
mouse), change the config file on disk, and see whether the layout follows.
"""
from __future__ import annotations

import os
import threading
import time
import unittest

from crosspc.backend.fake import FakeBackend
from crosspc.config import ClientEntry, Config
from crosspc.layout import Rect
from crosspc.server import ServerApp
from crosspc.util import Log, pick_writable_dir, scratch_prefix


class TestConfigHotReload(unittest.TestCase):
    def setUp(self):
        self.dir = pick_writable_dir()
        self.prefix = scratch_prefix("reload")
        self.path = os.path.join(self.dir, self.prefix + "crosspc.json")
        self.cache = os.path.join(self.dir, self.prefix + "cache.json")
        self.cfg = Config.defaults(self.path)
        self.cfg.name = "win11"
        self.cfg.clients = [ClientEntry(name="debian", host="127.0.0.1",
                                        rect=Rect(1920, 0, 1280, 800))]
        self.cfg.save()
        self.backend = FakeBackend(desktop=Rect(0, 0, 1920, 1080))
        self.log = Log("error")
        self.app = ServerApp(self.cfg, self.backend, self.log, port=0,
                             bind="127.0.0.1", cache_path=self.cache)
        self.thread = threading.Thread(target=self.app.run, daemon=True)
        self.thread.start()
        for _ in range(50):
            if self.app.router is not None:
                break
            time.sleep(0.1)
        self.assertIsNotNone(self.app.router)

    def tearDown(self):
        try:
            self.app.stop()
            self.app.shutdown()
        except Exception:
            pass
        self.thread.join(3.0)
        for p in (self.path, self.path + ".tmp", self.cache, self.cache + ".tmp"):
            try:
                os.remove(p)
            except OSError:
                pass

    def _client_rect(self):
        return self.app.layout.by_name("debian").rect

    def test_layout_follows_config_change(self):
        self.assertEqual(self._client_rect(), Rect(1920, 0, 1280, 800))
        # the GUI moves the client below the local machine
        time.sleep(1.0)                       # make the mtime change noticeably
        cfg = Config.load(self.path)
        cfg.clients[0].rect = Rect(0, 1080, 1280, 800)
        cfg.save()
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            if self._client_rect() == Rect(0, 1080, 1280, 800):
                break
            time.sleep(0.2)
        self.assertEqual(self._client_rect(), Rect(0, 1080, 1280, 800),
                         "the config file changed but the layout did not hot-reload")

    def test_broken_config_keeps_old_layout(self):
        time.sleep(1.0)
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{ broken json")
        time.sleep(3.0)
        # the old layout must still be there, and the service must not die
        self.assertEqual(self._client_rect(), Rect(1920, 0, 1280, 800))
        self.assertTrue(self.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
