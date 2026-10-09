"""End-to-end loopback self-test: it needs no second computer and never touches
the real keyboard and mouse.

How it works: a FakeBackend pretends to be "the server's keyboard and mouse" and
"the client's screen", then the **real** ServerApp / ClientApp / wire protocol /
network layer run over 127.0.0.1 to verify:

  1. pushing the mouse past the right edge of the screen -> control should switch
     to the client;
  2. after the switch the absolute coordinates should land at the right place on
     the client's screen;
  3. key presses should be forwarded, and coming back across the edge should
     release every held key (stuck-key protection);
  4. clipboard synchronization in both directions;
  5. the panic hotkey takes control back to this machine immediately.

Passing it means the main line of "position computation + protocol + network +
routing" is sound; the remaining risk lives purely in the platform input layer
(hook/inject) -- that part is covered by `crosspc doctor` and real-machine
integration testing.
"""
from __future__ import annotations

import os
import socket
import threading
import time
from typing import List, Optional, Tuple

from .backend.fake import FakeBackend
from .client import ClientApp
from .config import Config, ClientEntry
from .events import BTN_LEFT, Event
from .layout import Rect
from .protocol import T_CONTROL, control, frame
from .server import ServerApp
from .util import Log, pick_writable_dir, scratch_prefix


class Check:
    def __init__(self, log: Log):
        self.log = log
        self.passed = 0
        self.failed = 0

    def ok(self, name: str, detail: str = "") -> None:
        self.passed += 1
        self.log.info("  [PASS] %s%s" % (name, " — " + detail if detail else ""))

    def fail(self, name: str, detail: str = "") -> None:
        self.failed += 1
        self.log.error("  [FAIL] %s%s" % (name, " — " + detail if detail else ""))

    def eq(self, name: str, got, want) -> None:
        if got == want:
            self.ok(name, "= %r" % (got,))
        else:
            self.fail(name, "expected %r, got %r" % (want, got))

    def true(self, name: str, cond: bool, detail: str = "") -> None:
        if cond:
            self.ok(name, detail)
        else:
            self.fail(name, detail)

    def near(self, name: str, got: Tuple[int, int], want: Tuple[int, int],
             tol: int = 2) -> None:
        if abs(got[0] - want[0]) <= tol and abs(got[1] - want[1]) <= tol:
            self.ok(name, "%r ≈ %r" % (got, want))
        else:
            self.fail(name, "expected about %r, got %r" % (want, got))


class _Harness:
    """Minimal scaffolding that brings the server and client apps up.

    Temporary files land directly in a directory that is known to be writable (no
    subdirectory is created: creating one may not be allowed in a restricted
    environment), their names carry a pid prefix, and everything is deleted again
    on the way out.
    """

    def __init__(self, server_backend: FakeBackend, client_backend: FakeBackend,
                 log: Log, base: str, prefix: str):
        self.log = log
        self.srv_backend = server_backend
        self.cli_backend = client_backend
        self.base = base
        self.prefix = prefix
        self.files: List[str] = []
        self.server: Optional[ServerApp] = None
        self.client: Optional[ClientApp] = None
        self.threads: List[threading.Thread] = []

    def path(self, name: str) -> str:
        p = os.path.join(self.base, self.prefix + name)
        self.files.append(p)
        return p

    def start(self) -> int:
        srv_cfg = Config.defaults(self.path("server.json"))
        srv_cfg.name = "win11"
        srv_cfg.clipboard_poll_ms = 120
        srv_cfg.clients = [ClientEntry(name="debian", host="127.0.0.1",
                                       rect=Rect(1920, 0, 2560, 1440))]
        srv_cfg.log_level = "info"
        srv_cfg.path = ""        # no config file for the self-test: never touch the real one
        self.server = ServerApp(srv_cfg, self.srv_backend, self.log,
                                port=0, bind="127.0.0.1",
                                cache_path=self.path("server.cache.json"))
        t = threading.Thread(target=self.server.run, name="selftest-server",
                             daemon=True)
        t.start()
        self.threads.append(t)
        port = 0
        for _ in range(50):
            if self.server._listener is not None:
                port = self.server._listener.getsockname()[1]
                break
            time.sleep(0.1)
        if not port:
            raise RuntimeError("the server did not start listening within 5 seconds")

        cli_cfg = Config.defaults(self.path("client.json"))
        cli_cfg.name = "debian"
        cli_cfg.clipboard_poll_ms = 120
        cli_cfg.path = ""
        self.client = ClientApp(cli_cfg, self.cli_backend, self.log,
                                host="127.0.0.1", port=port)
        t = threading.Thread(target=self.client.run, name="selftest-client",
                             daemon=True)
        t.start()
        self.threads.append(t)
        for _ in range(60):
            if self.server.sessions:
                break
            time.sleep(0.1)
        if not self.server.sessions:
            raise RuntimeError("the client did not connect to the server within 6 seconds")
        time.sleep(0.3)
        return port

    def stop(self) -> None:
        for app in (self.client, self.server):
            if app is not None:
                try:
                    app.stop()
                except Exception:
                    pass
        for app in (self.client, self.server):
            if app is not None:
                try:
                    app.shutdown()
                except Exception:
                    pass
        for t in self.threads:
            t.join(2.0)
        for p in self.files:
            for candidate in (p, p + ".tmp"):
                try:
                    os.remove(candidate)
                except OSError:
                    pass


def run_selftest(log: Optional[Log] = None, keep_alive: bool = False) -> int:
    log = log or Log("info")
    check = Check(log)
    log.info("CrossPC loopback self-test (needs no second computer, never "
             "touches the real keyboard/mouse)")
    # Temporary files go into a directory that is known to be writable, and no
    # subdirectory is created (creating one may be disallowed in restricted
    # environments such as the Windows sandbox)
    base = pick_writable_dir()
    prefix = scratch_prefix("selftest")
    # server: 1920x1080; client: 2560x1440, placed to the right of the server
    srv = FakeBackend(log=log.debug, desktop=Rect(0, 0, 1920, 1080))
    cli = FakeBackend(log=log.debug, desktop=Rect(0, 0, 2560, 1440))
    harness = _Harness(srv, cli, log, base, prefix)
    try:
        harness.start()
        check.true("both ends connected", True,
                   "the server is listening, the client completed the handshake")
        router = harness.server.router
        assert router is not None

        # ---- 1. push the mouse out through the right edge ----
        srv.feed(Event.motion(1900, 500, 0, 0))
        check.eq("still on the server", router.active.name, "win11")
        cli.injected.clear()
        srv.feed(Event.motion(1919, 500, 20, 0))
        check.eq("control switches after crossing the right edge",
                 router.active.name, "debian")
        if _wait_for(lambda: len(cli.injected) >= 1, 2.0):
            check.near("entry point coordinates (flush with the client's left edge)",
                       (cli.injected[-1].a, cli.injected[-1].b), (0, 500), 3)
        else:
            check.fail("the client received the entering motion event",
                       "no injected event after waiting 2 seconds")

        # ---- 2. keep moving on the client ----
        cli.injected.clear()
        srv.feed(Event.motion(1919, 500, 40, 60))
        if _wait_for(lambda: len(cli.injected) >= 1, 2.0):
            check.near("remote absolute coordinates are accurate",
                       (cli.injected[-1].a, cli.injected[-1].b), (40, 560), 3)
        else:
            check.fail("the client keeps receiving motion",
                       "no event after waiting 2 seconds")

        # ---- 3. key forwarding + release ----
        cli.injected.clear()
        srv.feed(Event.key(0x1E, 0x41, True))
        got = _wait_for(lambda: any(e.kind == 4 for e in cli.injected), 2.0)
        check.true("the key press was forwarded to the client", got,
                   "received %d events" % len(cli.injected))
        check.true("the key press was recorded", router.pressed_count() == 1,
                   "%d keys held down" % router.pressed_count())

        # ---- 4. leave the client through its left edge, back to the server ----
        cli.injected.clear()
        srv.feed(Event.motion(0, 700, -4000, 0))
        check.eq("control returns to the server", router.active.name, "win11")
        got = _wait_for(lambda: any(e.kind == 4 and not e.c
                                    for e in cli.injected), 2.0)
        check.true("key releases were sent when leaving", got,
                   "%d key-release events" % len([e for e in cli.injected
                                                  if e.kind == 4 and not e.c]))
        check.true("the held keys were cleared", router.pressed_count() == 0)
        # When sliding back from the client's left edge onto the server, the
        # cursor should land on the server's **right edge** (that is where the
        # client was attached); y stays at the 560 it left with
        check.near("the local cursor was placed at the entry point",
                   srv.cursor(), (1919, 560), 2)
        check.true("takeover mode was left", not srv.forwarding,
                   "forwarding=%s" % srv.forwarding)

        # ---- 5. clipboard, both directions ----
        srv.clipboard = "text from windows"
        srv._clip_rev += 1
        ok = _wait_for(lambda: cli.clipboard == "text from windows", 3.0)
        check.true("server -> client clipboard", ok,
                   "client clipboard = %r" % cli.clipboard)
        cli.clipboard = "text from debian"
        cli._clip_rev += 1
        ok = _wait_for(lambda: srv.clipboard == "text from debian", 3.0)
        check.true("client -> server clipboard", ok,
                   "server clipboard = %r" % srv.clipboard)

        # ---- 5b. image clipboard (a generated PNG, no real screenshot needed) ----
        from .image import encode_png

        def make_png(seed: int) -> bytes:
            rgba = bytearray()
            for y in range(16):
                for x in range(16):
                    rgba += bytes(((x * 16 + seed) % 256, (y * 16) % 256,
                                   128, 255))
            return encode_png(bytes(rgba), 16, 16)

        png_a = make_png(0)
        srv.clipboard = ""            # clear the text: text wins by default, else no image is sent
        srv.clipboard_image = png_a
        srv._clip_rev += 1
        ok = _wait_for(lambda: cli.clipboard_image == png_a, 3.0)
        check.true("server -> client image clipboard", ok,
                   "client received %d bytes" % len(cli.clipboard_image or b""))

        png_b = make_png(64)          # different image: only a new content hash counts as "new"
        cli.clipboard = ""
        cli.clipboard_image = png_b
        cli._clip_rev += 1
        ok = _wait_for(lambda: srv.clipboard_image == png_b, 3.0)
        check.true("client -> server image clipboard", ok,
                   "server received %d bytes" % len(srv.clipboard_image or b""))

        # a failed image decode must not bring the link down (malformed data defense)
        srv.clipboard_image = b"\x89PNG\r\n\x1a\n" + "bad data".encode("utf-8")
        srv._clip_rev += 1
        time.sleep(0.3)
        check.true("a malformed image does not break synchronization",
                   router is not None and not harness.server._input_errors,
                   "the server is still running")

        # ---- 6. panic hotkey ----
        srv.feed(Event.motion(1919, 300, 20, 0))
        check.eq("switch to the client again", router.active.name, "debian")
        for ev in (Event.key(0x1D, 0xA2, True),        # LCtrl
                   Event.key(0x38, 0xA4, True),        # LAlt
                   Event.key(0x58, 0x7B, True)):       # F12
            srv.feed(ev)
        check.eq("the panic hotkey takes control back", router.active.name, "win11")
        check.true("takeover is left after the panic hotkey", not srv.forwarding)

        # ---- 7. take control back automatically when the client disconnects ----
        srv.feed(Event.motion(1919, 300, 20, 0))
        check.eq("switch to the client a third time", router.active.name, "debian")
        harness.server.sessions["debian"].link.close("simulating a cable pull")
        ok = _wait_for(lambda: router.active.name == "win11", 3.0)
        check.true("control is taken back immediately when the client drops", ok,
                   "currently controlling %s" % router.active.name)
    except Exception as exc:
        import traceback
        check.fail("the self-test raised", "%s" % exc)
        log.error(traceback.format_exc())
    finally:
        if keep_alive:
            log.info("--keep-alive: the self-test environment stays up, press Ctrl+C to exit")
            try:
                while True:
                    time.sleep(1.0)
            except KeyboardInterrupt:
                pass
        harness.stop()

    log.info("Self-test result: %d passed, %d failed" % (check.passed, check.failed))
    if check.failed:
        log.error("The self-test did not fully pass; please send me the failing "
                  "items above together with the log")
        return 1
    log.info("All passed: position computation, protocol, network, routing, "
             "clipboard and hotkeys all work")
    return 0


def _wait_for(pred, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


# ------------------------------------------------------------------ real-machine clipboard
def run_clipboard_test(log: Log, backend_name: Optional[str] = None,
                       keep_image: bool = False) -> int:
    """Real-machine clipboard acceptance test: write text and an image once each,
    then read them back and compare.

    This **temporarily** changes your clipboard (test image -> read back ->
    restore), which is why it is an explicit command and never runs behind your
    back inside doctor. The current content is saved first and restored as best
    we can afterwards: text is always restored; an image is restored whenever it
    can be read; formats we cannot read, such as a file list, cannot be restored
    and we say so explicitly.
    """
    from .backend import get_backend
    from .image import ImageError, decode_png, encode_png, has_alpha

    try:
        backend = get_backend(log, prefer=backend_name)
        backend.prepare()
    except Exception as exc:
        log.error("Failed to create the backend: %s" % exc)
        return 1
    if not backend.supports_clipboard:
        log.error("%s backend does not support the clipboard" % backend.name)
        return 1

    saved_text = backend.clipboard_text()
    saved_image = None
    if backend.supports_clipboard_images:
        try:
            saved_image = backend.clipboard_image_png(8 * 1024 * 1024)
        except Exception as exc:
            log.debug("Saving the original image failed: %s" % exc)
    formats = backend.clipboard_formats()
    log.info("Original clipboard formats: %s" % (", ".join(formats) or "(unreadable)"))
    log.info("Saved the current content (text %s, image %s); it will be restored "
             "after the test"
             % ("present" if saved_text else "empty",
                "%d bytes" % len(saved_image) if saved_image else "none"))
    if formats and not saved_text and not saved_image:
        log.warn("The clipboard holds formats we cannot read (a file list, for "
                 "example); they cannot be restored after the test")
    known = ("CF_UNICODETEXT", "CF_DIB", "CF_DIBV5", "CF_BITMAP", "PNG")
    unknown = [f for f in formats if f not in known]
    if unknown:
        log.warn("The clipboard also holds formats we cannot read (%s...); after "
                 "the test only the text/image can be restored"
                 % ", ".join(unknown[:4]))

    passed = 0
    failed = 0

    # ---- 1. text round trip ----
    marker = "CrossPC clipboard test %d" % int(time.time())
    try:
        backend.set_clipboard_text(marker)
        got = backend.clipboard_text()
        if got == marker:
            passed += 1
            log.info("  [PASS] text round trip: written and read back identically "
                     "(%d characters)" % len(marker))
        else:
            failed += 1
            log.error("  [FAIL] text round trip: wrote %r, read back %r"
                      % (marker, got))
    except Exception as exc:
        failed += 1
        log.error("  [FAIL] text round trip raised: %s" % exc)

    # ---- 2. image round trip ----
    if not backend.supports_clipboard_images:
        log.warn("  [SKIP] image round trip: this backend/clipboard tool does not "
                 "support images")
    else:
        w, h = 64, 48
        rgba = bytearray()
        for y in range(h):
            for x in range(w):
                rgba += bytes(((x * 4) % 256, (y * 5) % 256,
                               ((x + y) * 3) % 256, 255))
        src = bytes(rgba)
        png = encode_png(src, w, h)
        try:
            backend.set_clipboard_image_png(png)
            after_formats = backend.clipboard_formats()
            back = backend.clipboard_image_png(8 * 1024 * 1024)
            if not back:
                failed += 1
                log.error("  [FAIL] image round trip: it was written but cannot "
                          "be read back")
            else:
                same_bytes = (back == png)
                try:
                    decoded, dw, dh = decode_png(back)
                    same_pixels = (decoded == src and (dw, dh) == (w, h))
                except ImageError as exc:
                    same_pixels = False
                    log.warn("  the image read back cannot be decoded: %s" % exc)
                if same_pixels:
                    passed += 1
                    log.info("  [PASS] image round trip: all %dx%d pixels match"
                             "(%s, %d bytes written back)"
                             % (w, h, "bytes identical too" if same_bytes
                                else "bytes differ but pixels match", len(back)))
                else:
                    failed += 1
                    log.error("  [FAIL] image round trip: pixels differ")
                if after_formats:
                    log.info("  clipboard formats after writing: %s"
                             % ", ".join(after_formats))
                if backend.name == "windows" and after_formats:
                    has_dib = any(f in ("CF_DIB", "CF_DIBV5")
                                  for f in after_formats)
                    has_png = any(f.upper() == "PNG" for f in after_formats)
                    if has_dib and has_png:
                        passed += 1
                        log.info("  [PASS] both DIB (legacy programs) and PNG "
                                 "(modern programs) were written")
                    else:
                        failed += 1
                        log.error("  [FAIL] incomplete formats: DIB=%s PNG=%s"
                                  % (has_dib, has_png))
        except Exception as exc:
            failed += 1
            log.error("  [FAIL] image round trip raised: %s" % exc)

    # ---- 3. restore ----
    try:
        if saved_image and not keep_image:
            backend.set_clipboard_image_png(saved_image)
            log.info("The clipboard was restored to the original image")
        elif saved_text is not None:
            backend.set_clipboard_text(saved_text)
            log.info("The clipboard was restored to the original text")
        elif saved_image and keep_image:
            backend.set_clipboard_image_png(saved_image)
            log.info("The clipboard was restored to the original image (content "
                     "other than the test image was kept)")
        else:
            backend.set_clipboard_text("")
            log.info("The clipboard was empty to begin with, cleared again")
    except Exception as exc:
        log.warn("Restoring the clipboard failed: %s" % exc)

    try:
        backend.close()
    except Exception:
        pass
    log.info("Clipboard acceptance test result: %d passed, %d failed"
             % (passed, failed))
    return 1 if failed else 0


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ------------------------------------------------------------------ real-machine input acceptance
def run_capture_test(log: Log, seconds: float = 5.0, takeover: bool = False,
                     backend_name: Optional[str] = None) -> int:
    """Real-machine acceptance test: capture keyboard/mouse input for N seconds
    with the real backend, counting events only, never recording content.

    This is the one step that needs a real keyboard and mouse, so it is an
    explicit command for the user to run whenever convenient:
      first phase (default)  observe without taking over; move the mouse and type
                freely in the window and watch whether the counters grow;
      second phase (--takeover) really take over for N seconds, during which the
                local keyboard and mouse have no effect -- this verifies that
                "swallow local input + restore it" works, and it is always
                restored afterwards.

    Whatever happens, the finally block releases the takeover (with
    Ctrl+Alt+F12 as an extra safety net).
    """
    from collections import Counter

    from .backend import get_backend
    from .events import BUTTON, KEY, MOTION, WHEEL, Event

    try:
        backend = get_backend(log, prefer=backend_name)
    except Exception as exc:
        log.error("Failed to create the backend: %s" % exc)
        return 1
    if not backend.supports_capture:
        log.error("%s backend does not support capturing input, this test cannot "
                  "run" % backend.name)
        return 1

    log.info("Backend %s, about to capture for %.0f seconds"
             % (backend.caps(), seconds))
    # A restricted session (sandbox/AI assistant) blocks input -- probe first, so
    # that an environment limit is not reported as "the hook is broken"
    try:
        from .injecttest import blocked_reason
        blocked = blocked_reason(backend)
    except Exception:
        blocked = None
    if blocked:
        log.warn("Skipping: %s" % blocked)
        log.warn("This is not a CrossPC problem: the current session does not let "
                 "an agent process touch your mouse and keyboard. Run it in a "
                 "PowerShell window that **you opened yourself**:")
        log.warn("    python -m crosspc capturetest%s"
                 % (" --takeover" if takeover else ""))
        try:
            backend.close()
        except Exception:
            pass
        return 2                                    # 2 = environment refuses, not a code failure
    if takeover:
        log.warn("Takeover mode: the local keyboard and mouse have no effect for "
                 "the next few seconds; press Ctrl+Alt+F12 to restore them "
                 "immediately, or wait for the automatic restore")
    else:
        log.info("Observe mode: the local keyboard and mouse work as usual, events "
                 "are only counted")
    counts: Counter = Counter()
    name_counts: Counter = Counter()

    def sink(ev: Event) -> None:
        counts[ev.kind] += 1
        if ev.kind == KEY and not ev.c:
            name_counts["key releases"] += 1

    backend.prepare()
    backend.start_capture(sink)
    if takeover:
        try:
            backend.set_forwarding(True)
        except Exception as exc:
            log.error("Entering takeover mode failed: %s" % exc)
            backend.stop_capture()
            return 1
    started = time.monotonic()
    try:
        while time.monotonic() - started < seconds:
            time.sleep(0.2)
            done = time.monotonic() - started
            if int(done) != int(done - 0.2):
                log.info("  %.0f seconds left, %d events captured"
                         % (max(seconds - done, 0), sum(counts.values())))
    except KeyboardInterrupt:
        log.warn("Interrupted")
    finally:
        try:
            backend.set_forwarding(False)
        except Exception:
            pass
        try:
            backend.stop_capture()
        except Exception:
            pass
        try:
            backend.close()
        except Exception:
            pass

    total = sum(counts.values())
    log.info("Capture result: %d events in total" % total)
    for kind, label in ((MOTION, "motion"), (BUTTON, "buttons"),
                        (WHEEL, "wheel"), (KEY, "keys")):
        log.info("  %-8s %d" % (label, counts.get(kind, 0)))
    if total == 0:
        log.error("Not a single event was received. Possible reasons:")
        log.error("  1) you really did not touch the keyboard or mouse (run it "
                  "again and move them during the run)")
        log.error("  2) antivirus or IME software is blocking the global hook")
        log.error("  3) if you ran this command inside a restricted session such "
                  "as DSH, input capture (and injection) is blocked by the host's "
                  "security policy -- run crosspc capturetest in a PowerShell "
                  "window that **you opened yourself**")
        return 1
    log.info("Input capture works. Takeover mode verified" if takeover
             else "Input capture works. Add --takeover to also verify takeover "
                  "(input swallowing)")
    return 0
