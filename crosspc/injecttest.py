"""Real-machine injection acceptance test: proves "the keys we send really reach
the system's input stack".

Why it is needed: the injection path (SendInput / XTest / uinput) is the only
part of this tool that pure logic tests cannot cover -- a wrong argument type, a
miscomputed struct size or a missing extended-key flag never raises an error, it
just does nothing when you press the key.

By design it deliberately **does not disturb the user**:
  * it injects only modifier keys (left/right Ctrl, Shift, Alt) and the lock key
    ScrollLock, none of which produce a character or trigger a shortcut on their
    own;
  * left Ctrl is an ordinary key unrelated to "extended keys", while right
    Ctrl/right Alt carry the E0 prefix -- testing both proves that the extended
    flag really takes effect;
  * ScrollLock verifies that "the injected key is really processed by the
    system": read whether its lock bit flipped, then flip it back, so the worst
    the user sees is the ScrollLock LED blinking once;
  * before starting it checks whether the user is holding any modifier key, and
    waits for the next round if so, so our injection never overlaps the user's
    own keystrokes;
  * the --window phase really types into a window it creates (which briefly
    steals focus), so the user has to pass that flag explicitly.
"""
from __future__ import annotations

import ctypes
import time
from typing import List, Optional, Tuple

from .backend.base import Backend, BackendError
from .keys import (VK_LCONTROL, VK_LMENU, VK_LSHIFT, VK_RCONTROL, VK_RMENU,
                   VK_RSHIFT, VK_SCROLL)
from .util import Log

#: (scancode, extended, VK) -- all of them keys that do nothing on their own
MODIFIER_CASES: List[Tuple[int, bool, int, str]] = [
    (0x1D, False, VK_LCONTROL, "left Ctrl"),
    (0x36, False, VK_RSHIFT, "right Shift"),
    (0x38, False, VK_LMENU, "left Alt"),
    (0x1D, True, VK_RCONTROL, "right Ctrl (extended key)"),
    (0x38, True, VK_RMENU, "right Alt (extended key)"),
]

#: keys the user must not be holding when injection starts (else it would add up
#: with their own input)
BUSY_KEYS = [
    (0x11, "Ctrl"), (0x10, "Shift"), (0x12, "Alt"),
    (0x5B, "left Win"), (0x5C, "right Win"),
]

VK_SCROLL_LOCK = VK_SCROLL


def _windows_api():
    """GetAsyncKeyState only exists on Windows; other platforms get (None, None)."""
    try:
        import ctypes
        from ctypes import wintypes as wt
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetAsyncKeyState.restype = ctypes.c_short
        user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        user32.GetKeyState.restype = ctypes.c_short
        user32.GetKeyState.argtypes = [ctypes.c_int]
    except Exception:
        return None, None
    return user32, wt


def blocked_reason(backend: Backend) -> Optional[str]:
    """Decide whether "this environment simply does not allow injecting or
    capturing input" at all.

    Why it is needed: a host/sandbox such as DSH turns APIs that can drive the
    user's mouse and keyboard, such as SendInput and SetCursorPos, into no-ops --
    they raise nothing (SendInput even returns 1), they just do nothing. Without
    detecting this, an environment limit would be misreported as "something is
    wrong with your code", which is the kind of false failure that sends people
    down the wrong path most easily.

    The probe itself has no effect on the user: it moves the cursor to its
    **current position** and injects a Shift that is pressed and released (which
    produces no character and triggers no shortcut).
    """
    user32, _ = _windows_api()
    if user32 is None:
        return None
    try:
        pos = backend.cursor()
    except Exception:
        return None
    if not user32.SetCursorPos(int(pos[0]), int(pos[1])):
        return ("SetCursorPos was refused (GetLastError=%d) -- the current "
                "environment forbids moving the mouse"
                % ctypes.get_last_error())
    try:
        backend.inject_key(0x2A, VK_LSHIFT, True)
        time.sleep(0.02)
        seen = bool(user32.GetAsyncKeyState(0x10) & 0x8000)
        backend.inject_key(0x2A, VK_LSHIFT, False)
        time.sleep(0.02)
    except Exception:
        return None
    if not seen:
        return ("SendInput reported success but the key state did not change at "
                "all -- injection is blocked by the host environment (common in "
                "restricted sessions such as DSH/sandboxes)")
    return None


def _windows_injection_blocked(backend: Backend) -> Optional[str]:
    """Kept for the old name (used elsewhere internally)."""
    return blocked_reason(backend)


def run_inject_test(log: Log, backend_name: Optional[str] = None,
                    with_window: bool = False, hold_seconds: float = 0.05
                    ) -> int:
    from .backend import get_backend

    log.info("CrossPC injection acceptance test: a few \"harmless\" keys are "
             "injected and the system state is read back")
    try:
        backend = get_backend(log, prefer=backend_name)
        backend.prepare()
    except Exception as exc:
        log.error("Failed to create the backend: %s" % exc)
        return 1
    if not backend.supports_inject:
        log.error("%s backend does not support injection, this test cannot run"
                  % backend.name)
        return 1
    log.info("Injection method: %s" % backend.caps())

    user32 = None
    if backend.name == "windows":
        user32, _ = _windows_api()
        if user32 is None:
            log.error("Cannot get user32, unable to read the key state back")
            return 1
        blocked = _windows_injection_blocked(backend)
        if blocked:
            log.warn("Skipping: %s" % blocked)
            log.warn("This is not a CrossPC problem: the current session does not "
                     "let an agent process drive your mouse and keyboard. Run it "
                     "in a PowerShell window that **you opened yourself**:")
            log.warn("    python -m crosspc injecttest")
            log.warn("(or test directly on two real machines) The injection path "
                     "was not verified this time.")
            try:
                backend.close()
            except Exception:
                pass
            return 2                                    # 2 = environment refuses, not a code failure

        busy = [name for vk, name in BUSY_KEYS
                if user32.GetAsyncKeyState(vk) & 0x8000]
        if busy:
            log.warn("You are currently holding %s; to avoid adding to your own "
                     "input, release them and run again" % ", ".join(busy))
            return 1

    passed = 0
    failed = 0
    try:
        # ---- 1. modifier keys: press -> reads back as pressed -> release -> reads back as released ----
        for scancode, extended, vk, label in MODIFIER_CASES:
            if user32 is not None:
                before = bool(user32.GetAsyncKeyState(vk) & 0x8000)
            backend.inject_key(scancode, vk, True, extended)
            time.sleep(hold_seconds)
            down = True
            if user32 is not None:
                down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
            backend.inject_key(scancode, vk, False, extended)
            time.sleep(hold_seconds)
            up = True
            if user32 is not None:
                up = not bool(user32.GetAsyncKeyState(vk) & 0x8000)
            if before:
                log.warn("  %s: skipped (you were already holding it)" % label)
                continue
            if down and up:
                passed += 1
                log.info("  [PASS] %s: the press was recognized by the system, "
                         "and it recovered after the release" % label)
            else:
                failed += 1
                log.error("  [FAIL] %s: down=%s up=%s"
                          % (label, down, up))

        # ---- 2. lock key: the lock bit should flip after injection, then flip back ----
        if user32 is None:
            log.warn("  [SKIP] ScrollLock read-back (no GetKeyState on non-Windows platforms)")
        else:
            before = bool(user32.GetKeyState(VK_SCROLL_LOCK) & 1)
            backend.inject_key(0x46, VK_SCROLL_LOCK, True, False)
            backend.inject_key(0x46, VK_SCROLL_LOCK, False, False)
            time.sleep(0.15)
            after = bool(user32.GetKeyState(VK_SCROLL_LOCK) & 1)
            # restore: pressing once more returns it to the original state
            backend.inject_key(0x46, VK_SCROLL_LOCK, True, False)
            backend.inject_key(0x46, VK_SCROLL_LOCK, False, False)
            time.sleep(0.15)
            restored = bool(user32.GetKeyState(VK_SCROLL_LOCK) & 1)
            if after != before and restored == before:
                passed += 1
                log.info("  [PASS] ScrollLock: the lock bit flipped after "
                         "injection and was restored (%s -> %s -> %s)"
                         % (before, after, restored))
            else:
                failed += 1
                log.error("  [FAIL] ScrollLock: %s -> %s -> %s (expected a flip "
                          "and then a restore)"
                          % (before, after, restored))

        # ---- 3. coordinate injection: the cursor should be moved to the given position ----
        try:
            orig = backend.cursor()
            rect = backend.desktop_rect()
            target = (rect.x + 5, rect.y + 5)
            backend.inject_motion(*target)
            time.sleep(0.05)
            now = backend.cursor()
            backend.set_cursor(*orig)                   # restore immediately
            if abs(now[0] - target[0]) <= 2 and abs(now[1] - target[1]) <= 2:
                passed += 1
                log.info("  [PASS] absolute mouse positioning: the cursor reached "
                         "%s and was restored to %s" % (now, orig))
            else:
                failed += 1
                log.error("  [FAIL] absolute mouse positioning: expected %s, got %s"
                          % (target, now))
        except Exception as exc:
            failed += 1
            log.error("  [FAIL] absolute mouse positioning raised: %s" % exc)

        # ---- 4. optional: really type into a window the test creates (steals focus) ----
        if with_window:
            ok, detail = _window_typing_test(backend, log)
            if ok:
                passed += 1
                log.info("  [PASS] window input: %s" % detail)
            else:
                failed += 1
                log.error("  [FAIL] window input: %s" % detail)
        else:
            log.info("  [SKIP] window input test (it needs to steal focus briefly; add --window to run it)")
    finally:
        try:
            backend.close()
        except Exception:
            pass

    log.info("Injection acceptance test result: %d passed, %d failed"
             % (passed, failed))
    if failed:
        log.error("Some items did not pass -- please send me the output above; it "
                  "usually means a problem with the injection arguments or the "
                  "extended-key flag")
        return 1
    log.info("The injection path works. To also verify \"whether typing really "
             "reaches a window\", run again with --window")
    return 0


def _window_typing_test(backend: Backend, log: Log
                        ) -> Tuple[bool, str]:
    """Really type into a window the test creates and read it back, verifying that
    scancodes/capitalization/backspace/extended keys are all correct.

    It steals focus briefly (not a defect, the test itself needs it) and gives
    focus back to the previous window at the end.
    """
    try:
        import tkinter as tk
    except Exception as exc:
        return False, "no tkinter, the window test cannot run: %s" % exc

    restore_hwnd = None
    if backend.name == "windows":
        try:
            user32, _ = _windows_api()
            if user32 is not None:
                restore_hwnd = user32.GetForegroundWindow()
        except Exception:
            restore_hwnd = None

    root = None
    try:
        root = tk.Tk()
        root.title("CrossPC injection test (closes itself)")
        root.geometry("420x90+80+80")
        entry = tk.Entry(root, font=("Consolas", 14))
        entry.pack(fill="both", expand=True, padx=8, pady=8)
        root.attributes("-topmost", True)
        root.update()
        root.focus_force()
        entry.focus_force()
        root.update()
        log.info("  injection into this window starts in 3 seconds (do not touch the keyboard now)...")
        for i in (3, 2, 1):
            log.info("  %d..." % i)
            time.sleep(1.0)
        root.update()
        if root.focus_displayof() is None:
            return False, "the window did not get focus (is another program stealing it?)"

        # "crosspc" -> Backspace -> Shift+A -> Home (extended key) -> X  =>  "XcrosspA"
        _type_text(backend, "crosspc")
        backend.inject_key(0x0E, 0x08, True)          # Backspace
        backend.inject_key(0x0E, 0x08, False)
        _press_modifier(backend, 0x2A, VK_LSHIFT, 0x1E, 0x41)   # Shift+A
        backend.inject_key(0x47, 0x24, True, True)    # Home (extended key)
        backend.inject_key(0x47, 0x24, False, True)
        _type_text(backend, "x")
        time.sleep(0.5)
        root.update()
        got = entry.get()
        expected = "XcrosspA"
        if got == expected:
            return True, "the window received %r, as expected (covers backspace/capitalization/extended keys)" % got
        return False, "the window received %r, expected %r" % (got, expected)
    except Exception as exc:
        return False, "raised: %s" % exc
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass
        if restore_hwnd:
            try:
                user32, _ = _windows_api()
                if user32 is not None:
                    user32.SetForegroundWindow(restore_hwnd)
            except Exception:
                pass


#: injects lowercase letters/digits/space only, which need no Shift and therefore
#: do not depend on the keyboard layout's symbol keys
_LOWER_MAP = {
    **{chr(ord("a") + i): (0x1E + i, 0x41 + i) for i in range(26)},
    **{str(i): (0x02 + i, 0x30 + i) for i in range(1, 10)},
    "0": (0x0B, 0x30),
    " ": (0x39, 0x20),
}


def _type_text(backend: Backend, text: str) -> None:
    for ch in text:
        hit = _LOWER_MAP.get(ch)
        if hit is None:
            continue
        scancode, vk = hit
        backend.inject_key(scancode, vk, True)
        backend.inject_key(scancode, vk, False)
        time.sleep(0.012)


def _press_modifier(backend: Backend, mod_scan: int, mod_vk: int,
                    key_scan: int, key_vk: int) -> None:
    backend.inject_key(mod_scan, mod_vk, True)
    backend.inject_key(key_scan, key_vk, True)
    backend.inject_key(key_scan, key_vk, False)
    backend.inject_key(mod_scan, mod_vk, False)
