"""Linux backend pure-logic unit tests (all green on Windows too).

Principle: **never really open X11 or /dev/uinput**. Only the half that needs no
device is tested here: scancode/key mapping, input_event byte encoding, pixel to
absolute scale conversion, clipboard tool selection, and "on Windows prepare()
must raise BackendError and nothing else".

That way this logic is protected on a Windows development machine, and moving to
Debian leaves only "can the device actually be opened" for manual verification.
"""
from __future__ import annotations

import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crosspc import keys                                            # noqa: E402
from crosspc.backend import linux as linux_backend                  # noqa: E402
from crosspc.backend import linux_uinput                            # noqa: E402
from crosspc.backend.base import BackendError                       # noqa: E402
from crosspc.backend.linux import LinuxBackend, choose_clipboard_tool  # noqa: E402
from crosspc.layout import Rect                                      # noqa: E402


# ===========================================================================
class TestKeyTables(unittest.TestCase):
    """keys.py is a frozen contract; this also verifies that we reuse it correctly."""

    def test_letter_a(self):
        # in the normal range the evdev code == the scancode (KEY_A = 30 = 0x1E)
        self.assertEqual(keys.evdev_for(0x1E), 30)
        self.assertEqual(keys.keysym_for(0x1E), ord("a"))

    def test_extended_up_arrow(self):
        # up arrow: scancode 0x48 + E0 -> KEY_UP=103, keysym=0xFF52
        self.assertEqual(keys.evdev_for(0x48, 0, True), 103)
        self.assertEqual(keys.keysym_for(0x48, True), keys.XK_UP)
        # the same scancode without the extended prefix is keypad 8 (normal table),
        # so the two must not be mixed up
        self.assertNotEqual(keys.evdev_for(0x48, 0, False), 103)

    def test_extended_kp_enter(self):
        # E0 1C = keypad enter -> KEY_KPENTER=96, keysym=XK_KP_ENTER
        self.assertEqual(keys.evdev_for(0x1C, 0, True), 96)
        self.assertEqual(keys.keysym_for(0x1C, True), keys.XK_KP_ENTER)

    def test_vk_pause(self):
        # VK_PAUSE does not line up in the scancode table (different keyboards send
        # it in different ways), so it goes through the vk special case
        self.assertEqual(keys.evdev_for(0x45, keys.VK_PAUSE), 119)
        self.assertEqual(keys.evdev_for(0xE1, keys.VK_PAUSE), 119)

    def test_unknown_returns_none(self):
        self.assertIsNone(keys.evdev_for(0xFE))
        self.assertIsNone(keys.evdev_for(0xFE, 0, True))
        self.assertIsNone(keys.keysym_for(0xFE))

    def test_printscreen_extended_path(self):
        # E0 37 -> KEY_SYSRQ=99
        self.assertEqual(keys.evdev_for(0x37, 0, True), 99)

    def test_printscreen_nonextended_uses_vk_fallback(self):
        """The non-extended 0x37 must fall back to KEY_SYSRQ (99) through VK_SNAPSHOT.

        There used to be a real bug here: the direct `0x01 <= scancode <= 0x58`
        range mapping sat in front of the VK fallback, so PrintScreen was treated as
        scancode 0x37 and returned 55 (KEY_KPASTERISK). The lead developer has fixed
        keys.py; this test is the lock on that fix.
        """
        self.assertEqual(keys.evdev_for(0x37, keys.VK_SNAPSHOT, False),
                         keys.EVDEV_SYSRQ)
        # the normal range is still "evdev code == scancode"
        self.assertEqual(keys.evdev_for(0x1E, 0x41, False), 0x1E)
        self.assertEqual(keys.evdev_for(0x37, 0, False), 0x37)


# ===========================================================================
class TestUinputEventEncoding(unittest.TestCase):
    """Encoding of the 24-byte input_event -> decode back to (type, code, value)."""

    @staticmethod
    def _decode(blob: bytes):
        # "=qqHHi" matches struct input_event (see the comment in linux_uinput.pack_event)
        sec, usec, type_, code, value = struct.unpack("=qqHHi", blob)
        return sec, usec, type_, code, value

    def test_size_is_24(self):
        self.assertEqual(len(linux_uinput.pack_event(linux_uinput.EV_KEY, 30, 1)),
                         linux_uinput.INPUT_EVENT_SIZE)
        self.assertEqual(linux_uinput.INPUT_EVENT_SIZE, 24)

    def test_roundtrip_negative_and_positive_value(self):
        blob = linux_uinput.pack_event(linux_uinput.EV_REL,
                                       linux_uinput.REL_WHEEL, -2,
                                       sec=1234, usec=5678)
        sec, usec, type_, code, value = self._decode(blob)
        self.assertEqual((sec, usec), (1234, 5678))
        self.assertEqual(type_, linux_uinput.EV_REL)
        self.assertEqual(code, linux_uinput.REL_WHEEL)
        self.assertEqual(value, -2)

        blob = linux_uinput.pack_event(linux_uinput.EV_ABS,
                                       linux_uinput.ABS_X, 65535,
                                       sec=0, usec=0)
        _, _, type_, code, value = self._decode(blob)
        self.assertEqual((type_, code, value), (linux_uinput.EV_ABS,
                                                linux_uinput.ABS_X, 65535))

    def test_field_widths(self):
        # type/code are u16 and value is s32: the 0xFFFF boundary verifies that no
        # sign extension happened
        blob = linux_uinput.pack_event(0xFFFF, 0xFFFF, 2147483647,
                                       sec=1, usec=1)
        _, _, type_, code, value = self._decode(blob)
        self.assertEqual((type_, code, value), (0xFFFF, 0xFFFF, 2147483647))

    def test_usec_overflow_is_normalized(self):
        # a float clock can yield usec >= 1000000, and the kernel rejects such timestamps
        blob = linux_uinput.pack_event(1, 1, 1, sec=10, usec=1500000)
        sec, usec, _, _, _ = self._decode(blob)
        self.assertEqual(sec, 11)
        self.assertEqual(usec, 500000)

    def test_default_timestamp_is_recent(self):
        blob = linux_uinput.pack_event(1, 1, 1)
        sec, usec, _, _, _ = self._decode(blob)
        self.assertGreater(sec, 1600000000)          # after 2020
        self.assertGreaterEqual(usec, 0)
        self.assertLess(usec, 1000000)

    def test_struct_layout_matches_kernel(self):
        # struct uinput_setup = input_id(8) + name(80) + ff_effects_max(4)
        self.assertEqual(linux_uinput.UINPUT_SETUP_SIZE, 92)
        self.assertEqual(linux_uinput.UINPUT_USER_DEV_SIZE, 1372)
        # the type field must be 16 bits, otherwise the kernel reads the u16 as garbage
        self.assertEqual(linux_uinput._InputEvent.type.size, 2)
        self.assertEqual(linux_uinput._InputEvent.code.size, 2)
        self.assertEqual(linux_uinput._InputEvent.value.size, 4)


# ===========================================================================
class TestIoctlConstants(unittest.TestCase):
    """The ioctl numbers are computed by hand and must line up with linux/uinput.h."""

    def test_known_values(self):
        self.assertEqual(linux_uinput.UI_DEV_CREATE, 0x5501)
        self.assertEqual(linux_uinput.UI_DEV_DESTROY, 0x5502)
        self.assertEqual(linux_uinput.UI_DEV_SETUP, 0x405C5503)
        # the UI_SET_* nr values start at 100 (not 4!); these numbers can be
        # compared directly against /usr/include/linux/uinput.h
        self.assertEqual(linux_uinput.UI_SET_EVBIT, 0x40045564)
        self.assertEqual(linux_uinput.UI_SET_KEYBIT, 0x40045565)
        self.assertEqual(linux_uinput.UI_SET_RELBIT, 0x40045566)
        self.assertEqual(linux_uinput.UI_SET_ABSBIT, 0x40045567)
        self.assertEqual(linux_uinput.UI_SET_PROPBIT, 0x4004556E)

    def test_set_bits_are_consecutive(self):
        # in uinput.h UI_SET_KEYBIT..UI_SET_PROPBIT step by +1 each, and this
        # assertion catches anyone "just nudging one nr"
        self.assertEqual(linux_uinput.UI_SET_KEYBIT, linux_uinput.UI_SET_EVBIT + 1)
        self.assertEqual(linux_uinput.UI_SET_RELBIT, linux_uinput.UI_SET_EVBIT + 2)
        self.assertEqual(linux_uinput.UI_SET_ABSBIT, linux_uinput.UI_SET_EVBIT + 3)
        self.assertEqual(linux_uinput.UI_SET_PROPBIT, linux_uinput.UI_SET_EVBIT + 10)

    def test_macro_formula(self):
        # _IOW('U', 100, int) expanded by hand = (1<<30)|(4<<16)|(0x55<<8)|100
        self.assertEqual(linux_uinput.UI_SET_EVBIT,
                         (1 << 30) | (4 << 16) | (0x55 << 8) | 100)


# ===========================================================================
class TestButtonMapping(unittest.TestCase):
    def test_x11_to_evdev(self):
        self.assertEqual(linux_uinput.button_to_evdev(1), 0x110)   # BTN_LEFT
        self.assertEqual(linux_uinput.button_to_evdev(2), 0x112)   # BTN_MIDDLE
        self.assertEqual(linux_uinput.button_to_evdev(3), 0x111)   # BTN_RIGHT
        self.assertEqual(linux_uinput.button_to_evdev(8), 0x113)   # BTN_SIDE
        self.assertEqual(linux_uinput.button_to_evdev(9), 0x114)   # BTN_EXTRA

    def test_unknown_button(self):
        for bad in (0, 4, 5, 6, 7, 10, -1, 99):
            self.assertIsNone(linux_uinput.button_to_evdev(bad))

    def test_mapping_table_is_complete(self):
        self.assertEqual(sorted(linux_uinput.BUTTON_TO_EVDEV),
                         [1, 2, 3, 8, 9])


# ===========================================================================
class TestCoordinateMapping(unittest.TestCase):
    """Pixel -> 0..65535 absolute scale. The boundaries must be pinned down, otherwise the screen edge cannot be reached."""

    def test_1920x1080_boundaries(self):
        f = linux_uinput.pixel_to_abs
        self.assertEqual(f(0, 0, 1920, 1080), (0, 0))
        self.assertEqual(f(1919, 1079, 1920, 1080),
                         (linux_uinput.ABS_MAX, linux_uinput.ABS_MAX))

    def test_center_is_roughly_half(self):
        # 1920/2 -> 960/1919*65535 = 32784.4; 1080/2 -> 540/1079*65535 = 32798.4
        ax, ay = linux_uinput.pixel_to_abs(960, 540, 1920, 1080)
        self.assertAlmostEqual(ax, 32784, delta=2)
        self.assertAlmostEqual(ay, 32798, delta=2)

    def test_clamped_out_of_range(self):
        f = linux_uinput.pixel_to_abs
        self.assertEqual(f(-100, -100, 1920, 1080), (0, 0))
        self.assertEqual(f(99999, 99999, 1920, 1080),
                         (linux_uinput.ABS_MAX, linux_uinput.ABS_MAX))

    def test_monotonic(self):
        prev = -1
        for x in range(0, 1920, 97):
            ax, _ = linux_uinput.pixel_to_abs(x, 0, 1920, 1080)
            self.assertGreater(ax, prev)
            prev = ax

    def test_degenerate_screen_size(self):
        # with an invalid size it degrades to pass-through, but must not raise
        # (the diagnostics path does reach this)
        self.assertEqual(linux_uinput.pixel_to_abs(10, 20, 0, 0), (10, 20))
        self.assertEqual(linux_uinput.pixel_to_abs(10, 20, 1, 1), (0, 0))

    def test_ultrawide(self):
        ax, _ = linux_uinput.pixel_to_abs(2559, 0, 2560, 1440)
        self.assertEqual(ax, linux_uinput.ABS_MAX)


# ===========================================================================
class TestKeyboardRegistration(unittest.TestCase):
    def test_all_table_codes_present(self):
        codes = linux_uinput.keyboard_evdev_codes()
        # every scancode that appears in the table must be registered, otherwise
        # compatibility suffers from "dead keys"
        for scan in keys.SCAN_TO_KEYSYM:
            code = keys.evdev_for(scan, 0, False)
            if code:
                self.assertIn(code, codes, "scancode 0x%02X" % scan)
        for scan in keys.EXT_SCAN_TO_EVDEV:
            code = keys.evdev_for(scan, 0, True)
            if code:
                self.assertIn(code, codes, "extended scancode 0x%02X" % scan)

    def test_buttons_and_special_keys(self):
        codes = linux_uinput.keyboard_evdev_codes()
        for code in (keys.EVDEV_PAUSE, keys.EVDEV_SYSRQ):
            self.assertIn(code, codes)
        for btn in (0x110, 0x111, 0x112, 0x113, 0x114):
            self.assertIn(btn, linux_uinput.BUTTON_TO_EVDEV.values())

    def test_not_empty_and_reasonable(self):
        codes = linux_uinput.keyboard_evdev_codes()
        self.assertGreater(len(codes), 50)
        self.assertTrue(all(0 < c < 0x300 for c in codes))


# ===========================================================================
class TestClipboardToolChoice(unittest.TestCase):
    """choose_clipboard_tool(env, which) is a pure function with 4 combinations."""

    @staticmethod
    def _which(*available):
        return lambda name: ("/usr/bin/" + name) if name in available else None

    def test_wayland_prefers_wl_copy(self):
        env = {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}
        got = choose_clipboard_tool(env, self._which("wl-copy", "xclip", "xsel"))
        self.assertEqual(got, "wl-copy")

    def test_wayland_without_wl_copy_falls_back_to_xclip(self):
        env = {"WAYLAND_DISPLAY": "wayland-0"}
        got = choose_clipboard_tool(env, self._which("xclip"))
        self.assertEqual(got, "xclip")

    def test_x11_uses_xclip_then_xsel(self):
        env = {"DISPLAY": ":0"}
        self.assertEqual(choose_clipboard_tool(env, self._which("xclip", "xsel")),
                         "xclip")
        self.assertEqual(choose_clipboard_tool(env, self._which("xsel")), "xsel")

    def test_nothing_available(self):
        self.assertIsNone(choose_clipboard_tool({"DISPLAY": ":0"}, self._which()))

    def test_xsel_needs_x11_hint(self):
        # pure Wayland with no display variables: do not guess xsel (it cannot reach
        # the Wayland clipboard)
        self.assertIsNone(choose_clipboard_tool({}, self._which("xsel")))
        self.assertEqual(choose_clipboard_tool({"XDG_SESSION_TYPE": "x11"},
                                               self._which("xsel")), "xsel")

    def test_wayland_without_tools_at_all(self):
        self.assertIsNone(choose_clipboard_tool({"WAYLAND_DISPLAY": "wayland-1"},
                                                self._which()))

    def test_commands_exist_for_every_tool(self):
        for tool, (read_cmd, write_cmd) in linux_backend.CLIPBOARD_TOOLS.items():
            self.assertTrue(read_cmd and write_cmd, tool)
            self.assertTrue(all(isinstance(x, str) for x in read_cmd + write_cmd))


# ===========================================================================
class TestScreenSpecParsing(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(linux_backend.parse_screen_spec("2560x1440")[0],
                         (2560, 1440))
        self.assertEqual(linux_backend.parse_screen_spec("1920X1080")[0],
                         (1920, 1080))
        self.assertEqual(linux_backend.parse_screen_spec(" 1366 * 768 ")[0],
                         (1366, 768))
        self.assertIn("CROSSPC_SCREEN", linux_backend.parse_screen_spec("2560x1440")[1])

    def test_invalid_falls_back(self):
        for bad in ("", "abc", "1920", "0x0", "x1080", "1920x-5"):
            size, why = linux_backend.parse_screen_spec(bad)
            self.assertEqual(size, (1920, 1080), bad)
            self.assertTrue(why, bad)

    def test_custom_default(self):
        size, _ = linux_backend.parse_screen_spec("", default=(800, 600))
        self.assertEqual(size, (800, 600))


# ===========================================================================
class TestBackendContract(unittest.TestCase):
    """Capability declarations must match the task contract: Linux can only be a client."""

    def test_capabilities(self):
        b = LinuxBackend()
        self.assertFalse(b.supports_capture)
        self.assertFalse(b.supports_suppress)
        self.assertTrue(b.supports_inject)
        self.assertTrue(b.supports_clipboard)
        self.assertTrue(b.can_be_client)
        self.assertFalse(b.can_serve)

    def test_unsupported_operations_raise_backend_error(self):
        b = LinuxBackend()
        with self.assertRaises(BackendError):
            b.start_capture(lambda ev: None)
        with self.assertRaises(BackendError):
            b.set_forwarding(True)

    def test_prepare_never_leaks_raw_exceptions(self):
        """The prepare() contract: either it succeeds, or it raises BackendError.

        The layer above only catches BackendError, so native exceptions such as
        FileNotFoundError / OSError / permission errors must never escape. On the
        development machine (Windows) it necessarily raises; on real Linux it
        succeeds if /dev/uinput is writable (and really creates a virtual pointer
        device, which close() destroys). Both are compliant -- this asserts the
        contract, not the outcome on some particular platform.
        """
        for prefer in ("uinput", "x11", None):
            b = LinuxBackend(prefer=prefer)
            try:
                b.prepare()
            except BackendError as exc:
                self.assertTrue(str(exc), "the error message must not be empty")
            except Exception as exc:                 # pragma: no cover
                self.fail("prepare(prefer=%r) raised %s instead of BackendError: %s"
                          % (prefer, type(exc).__name__, exc))
            finally:
                b.close()

    @unittest.skipUnless(sys.platform == "win32",
                         "this asserts the guaranteed outcome on Windows")
    def test_prepare_raises_on_windows(self):
        """Windows has neither /dev/uinput nor X, so all three prefer values must raise BackendError."""
        for prefer in ("uinput", "x11", None):
            b = LinuxBackend(prefer=prefer)
            with self.assertRaises(BackendError):
                b.prepare()
            b.close()

    def test_prepare_x11_without_display_raises(self):
        """With no DISPLAY, explicitly asking for X11 injection must give a BackendError rather than crashing."""
        if os.environ.get("DISPLAY"):
            self.skipTest("DISPLAY is set here; this case tests the behaviour without X")
        b = LinuxBackend(prefer="x11")
        with self.assertRaises(BackendError):
            b.prepare()
        b.close()

    def test_unknown_prefer_rejected(self):
        b = LinuxBackend(prefer="nonsense")
        with self.assertRaises(BackendError):
            b.prepare()
        b.close()

    def test_close_is_idempotent_without_prepare(self):
        b = LinuxBackend()
        b.close()
        b.close()                                    # must not raise

    def test_inject_without_prepare_raises_backend_error(self):
        b = LinuxBackend()
        with self.assertRaises(BackendError):
            b.inject_motion(1, 2)
        with self.assertRaises(BackendError):
            b.inject_key(0x1E, 0, True, False)

    def test_desktop_rect_defaults_without_prepare(self):
        saved = os.environ.pop("CROSSPC_SCREEN", None)
        try:
            b = LinuxBackend()
            self.assertEqual(b.desktop_rect(), Rect(0, 0, 1920, 1080))
            os.environ["CROSSPC_SCREEN"] = "2560x1440"
            b2 = LinuxBackend()
            self.assertEqual(b2.desktop_rect(), Rect(0, 0, 2560, 1440))
        finally:
            os.environ.pop("CROSSPC_SCREEN", None)
            if saved is not None:
                os.environ["CROSSPC_SCREEN"] = saved

    def test_clipboard_revision_is_none(self):
        self.assertIsNone(LinuxBackend().clipboard_revision())

    def test_probe_returns_tuples(self):
        # probe() itself must not raise, and every item is (str, bool, str)
        items = LinuxBackend().probe()
        self.assertTrue(items)
        for item in items:
            self.assertEqual(len(item), 3, item)
            self.assertIsInstance(item[0], str)
            self.assertIsInstance(item[1], bool)
            self.assertIsInstance(item[2], str)
            self.assertTrue(item[2], item)
        joined = " ".join(i[2] for i in items)
        self.assertIn("client", joined)              # the role description must be there

    def test_uinput_available_never_raises(self):
        ok, why = linux_uinput.UInputInjector.available()
        self.assertIsInstance(ok, bool)
        self.assertTrue(why)
        if not sys.platform.startswith("linux"):
            self.assertFalse(ok)                     # necessarily unavailable on Windows

    def test_uinput_module_import_has_no_side_effects(self):
        # this module was already imported at top level (start of this file); not
        # blowing up means no device/library was loaded
        self.assertFalse(linux_uinput.UInputInjector().opened)
        self.assertIsNone(linux_uinput.UInputInjector().screen_size)

    def test_uinput_motion_without_screen_size_raises_error(self):
        inj = linux_uinput.UInputInjector()
        with self.assertRaises(RuntimeError) as ctx:
            # fd is None: this verifies the "no size" error without touching a device
            inj.inject_motion(1, 2)
        self.assertIn("CROSSPC_SCREEN", str(ctx.exception))

    def test_set_screen_size_ignores_garbage(self):
        inj = linux_uinput.UInputInjector()
        inj.set_screen_size(0, -5)
        self.assertIsNone(inj.screen_size)
        inj.set_screen_size(2560, 1440)
        self.assertEqual(inj.screen_size, (2560, 1440))

    def test_inject_records_pressed_keys_through_base(self):
        # the base class inject() records pressed state; a fake implementation
        # verifies the delegation path is correct
        b = LinuxBackend()
        calls = []
        b._impl_kind = "uinput"
        b._impl = type("FakeImpl", (), {
            "inject_motion": lambda s, x, y, w=None, h=None: calls.append(
                ("motion", x, y, w, h)),
            "inject_button": lambda s, btn, pressed: calls.append(
                ("button", btn, pressed)),
            "inject_wheel": lambda s, dx, dy: calls.append(("wheel", dx, dy)),
            "inject_key": lambda s, sc, vk, pressed, ext: calls.append(
                ("key", sc, vk, pressed, ext)),
            "close": lambda s: calls.append(("close",)),
        })()
        b._screen = (2560, 1440)
        from crosspc.events import Event
        b.inject(Event.motion(100, 200))
        b.inject(Event.button(1, True))
        b.inject(Event.wheel(0, -1))
        b.inject(Event.key(0x1E, 0, True, False))
        self.assertEqual(calls[0], ("motion", 100, 200, 2560, 1440))
        self.assertEqual(calls[1], ("button", 1, True))
        self.assertEqual(calls[2], ("wheel", 0, -1))
        self.assertEqual(calls[3], ("key", 0x1E, 0, True, False))
        b.release_all()
        self.assertIn(("key", 0x1E, 0, False, False), calls)
        self.assertIn(("button", 1, False), calls)
        b.close()
        self.assertIn(("close",), calls)

    def test_x11_injector_not_a_backend(self):
        # X11Injector is a standalone small class and must not be a Backend subclass
        # (separation of concerns)
        from crosspc.backend.linux_x11 import X11Injector
        from crosspc.backend.base import Backend
        self.assertFalse(issubclass(X11Injector, Backend))
        ok, why = X11Injector.available()
        self.assertIsInstance(ok, bool)
        self.assertTrue(why)

    def test_x11_unknown_button_is_skipped(self):
        from crosspc.backend.linux_x11 import X11Injector
        inj = X11Injector(log=lambda m: None)
        # no open(): an unknown button must return before touching dpy, and must not raise
        inj.inject_button(4, True)

    def test_x11_wheel_sign_selection(self):
        from crosspc.backend.linux_x11 import (X_BUTTON_WHEEL_DOWN,
                                               X_BUTTON_WHEEL_LEFT,
                                               X_BUTTON_WHEEL_RIGHT,
                                               X_BUTTON_WHEEL_UP)
        # the constants themselves: 4 up, 5 down, 6 left, 7 right (in X11 the wheel is just buttons)
        self.assertEqual((X_BUTTON_WHEEL_UP, X_BUTTON_WHEEL_DOWN), (4, 5))
        self.assertEqual((X_BUTTON_WHEEL_LEFT, X_BUTTON_WHEEL_RIGHT), (6, 7))


if __name__ == "__main__":
    unittest.main(verbosity=2)
