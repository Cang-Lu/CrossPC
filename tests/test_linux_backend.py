"""Linux 后端纯逻辑单元测试(在 Windows 上也能全绿)。

原则: **绝不真的打开 X11 或 /dev/uinput**。这里只测"不需要设备的那一半":
键码/按键映射、input_event 的字节编码、像素->绝对刻度换算、剪辑板工具挑选、
以及"在 Windows 上 prepare() 必须抛 BackendError 而不是别的异常"。

这样这些逻辑在 Windows 开发机上就能被保护, 上 Debian 只剩下"设备真的能打开
吗"这一层需要人工验证。
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
    """keys.py 是冻结契约, 这里同时也验证我们复用它的方式是对的。"""

    def test_letter_a(self):
        # 普通区 evdev 码 == 扫描码(KEY_A = 30 = 0x1E)
        self.assertEqual(keys.evdev_for(0x1E), 30)
        self.assertEqual(keys.keysym_for(0x1E), ord("a"))

    def test_extended_up_arrow(self):
        # 上箭头: 扫描码 0x48 + E0 -> KEY_UP=103, keysym=0xFF52
        self.assertEqual(keys.evdev_for(0x48, 0, True), 103)
        self.assertEqual(keys.keysym_for(0x48, True), keys.XK_UP)
        # 同一个扫描码不带扩展时是数字键盘 8(普通表), 两者不能混
        self.assertNotEqual(keys.evdev_for(0x48, 0, False), 103)

    def test_extended_kp_enter(self):
        # E0 1C = 小键盘回车 -> KEY_KPENTER=96, keysym=XK_KP_ENTER
        self.assertEqual(keys.evdev_for(0x1C, 0, True), 96)
        self.assertEqual(keys.keysym_for(0x1C, True), keys.XK_KP_ENTER)

    def test_vk_pause(self):
        # VK_PAUSE 在扫描码表里对不上(不同键盘发法不同), 走 vk 特判
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
        """非扩展的 0x37 必须靠 VK_SNAPSHOT 兜底到 KEY_SYSRQ(99)。

        这里曾经有一个真 bug: `0x01 <= scancode <= 0x58` 的区间直映挡在
        VK 兜底之前, 于是 PrintScreen 被当成扫描码 0x37 直接返回 55
        (KEY_KPASTERISK)。主开发者已修 keys.py, 这条测试就是那个修复的锁。
        """
        self.assertEqual(keys.evdev_for(0x37, keys.VK_SNAPSHOT, False),
                         keys.EVDEV_SYSRQ)
        # 普通区仍然是"evdev 码 == 扫描码"
        self.assertEqual(keys.evdev_for(0x1E, 0x41, False), 0x1E)
        self.assertEqual(keys.evdev_for(0x37, 0, False), 0x37)


# ===========================================================================
class TestUinputEventEncoding(unittest.TestCase):
    """24 字节 input_event 的编码 -> 解码回 (type, code, value)。"""

    @staticmethod
    def _decode(blob: bytes):
        # "=qqHHi" 与 struct input_event 一致(见 linux_uinput.pack_event 的注释)
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
        # type/code 是 u16, value 是 s32: 用 0xFFFF 边界验证没有被符号扩展
        blob = linux_uinput.pack_event(0xFFFF, 0xFFFF, 2147483647,
                                       sec=1, usec=1)
        _, _, type_, code, value = self._decode(blob)
        self.assertEqual((type_, code, value), (0xFFFF, 0xFFFF, 2147483647))

    def test_usec_overflow_is_normalized(self):
        # 浮点时钟可能给出 usec >= 1000000, 内核会拒绝这种时间戳
        blob = linux_uinput.pack_event(1, 1, 1, sec=10, usec=1500000)
        sec, usec, _, _, _ = self._decode(blob)
        self.assertEqual(sec, 11)
        self.assertEqual(usec, 500000)

    def test_default_timestamp_is_recent(self):
        blob = linux_uinput.pack_event(1, 1, 1)
        sec, usec, _, _, _ = self._decode(blob)
        self.assertGreater(sec, 1600000000)          # 2020 年以后
        self.assertGreaterEqual(usec, 0)
        self.assertLess(usec, 1000000)

    def test_struct_layout_matches_kernel(self):
        # struct uinput_setup = input_id(8) + name(80) + ff_effects_max(4)
        self.assertEqual(linux_uinput.UINPUT_SETUP_SIZE, 92)
        self.assertEqual(linux_uinput.UINPUT_USER_DEV_SIZE, 1372)
        # type 字段必须是 16 位, 否则 u16 会被 kernel 当成垃圾
        self.assertEqual(linux_uinput._InputEvent.type.size, 2)
        self.assertEqual(linux_uinput._InputEvent.code.size, 2)
        self.assertEqual(linux_uinput._InputEvent.value.size, 4)


# ===========================================================================
class TestIoctlConstants(unittest.TestCase):
    """ioctl 编号是手算的, 必须和 linux/uinput.h 对得上。"""

    def test_known_values(self):
        self.assertEqual(linux_uinput.UI_DEV_CREATE, 0x5501)
        self.assertEqual(linux_uinput.UI_DEV_DESTROY, 0x5502)
        self.assertEqual(linux_uinput.UI_DEV_SETUP, 0x405C5503)
        # UI_SET_* 的 nr 从 100 起(不是 4!), 这几个值可以直接和
        # /usr/include/linux/uinput.h 对照
        self.assertEqual(linux_uinput.UI_SET_EVBIT, 0x40045564)
        self.assertEqual(linux_uinput.UI_SET_KEYBIT, 0x40045565)
        self.assertEqual(linux_uinput.UI_SET_RELBIT, 0x40045566)
        self.assertEqual(linux_uinput.UI_SET_ABSBIT, 0x40045567)
        self.assertEqual(linux_uinput.UI_SET_PROPBIT, 0x4004556E)

    def test_set_bits_are_consecutive(self):
        # uinput.h 里 UI_SET_KEYBIT..UI_SET_PROPBIT 是逐个 +1 的, 这条断言
        # 能在有人"顺手改一个 nr"时立刻抓住
        self.assertEqual(linux_uinput.UI_SET_KEYBIT, linux_uinput.UI_SET_EVBIT + 1)
        self.assertEqual(linux_uinput.UI_SET_RELBIT, linux_uinput.UI_SET_EVBIT + 2)
        self.assertEqual(linux_uinput.UI_SET_ABSBIT, linux_uinput.UI_SET_EVBIT + 3)
        self.assertEqual(linux_uinput.UI_SET_PROPBIT, linux_uinput.UI_SET_EVBIT + 10)

    def test_macro_formula(self):
        # _IOW('U', 100, int) 手工展开 = (1<<30)|(4<<16)|(0x55<<8)|100
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
    """像素 -> 0..65535 绝对刻度。边界必须钉死, 否则屏幕边缘点不到。"""

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
        # 尺寸非法时退化成直通, 但不能抛异常(诊断路径会走到)
        self.assertEqual(linux_uinput.pixel_to_abs(10, 20, 0, 0), (10, 20))
        self.assertEqual(linux_uinput.pixel_to_abs(10, 20, 1, 1), (0, 0))

    def test_ultrawide(self):
        ax, _ = linux_uinput.pixel_to_abs(2559, 0, 2560, 1440)
        self.assertEqual(ax, linux_uinput.ABS_MAX)


# ===========================================================================
class TestKeyboardRegistration(unittest.TestCase):
    def test_all_table_codes_present(self):
        codes = linux_uinput.keyboard_evdev_codes()
        # 表里出现过的每个扫描码都必须被注册, 否则兼容性上会出现"哑键"
        for scan in keys.SCAN_TO_KEYSYM:
            code = keys.evdev_for(scan, 0, False)
            if code:
                self.assertIn(code, codes, "扫描码 0x%02X" % scan)
        for scan in keys.EXT_SCAN_TO_EVDEV:
            code = keys.evdev_for(scan, 0, True)
            if code:
                self.assertIn(code, codes, "扩展扫描码 0x%02X" % scan)

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
    """choose_clipboard_tool(env, which) 是纯函数, 4 种组合。"""

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
        # 纯 Wayland 且没有显示变量时不要瞎猜 xsel(它连不上 Wayland 剪辑板)
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
    """能力声明必须和任务约定一致: Linux 只能当 client。"""

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

    def test_prepare_uinput_raises_backend_error_on_windows(self):
        # 关键契约: Windows 上必须抛 BackendError(上层只捕获它), 不能是
        # FileNotFoundError/OSError 之类的原生异常
        b = LinuxBackend(prefer="uinput")
        try:
            b.prepare()
        except BackendError as exc:
            self.assertTrue(str(exc))
        except Exception as exc:                     # pragma: no cover
            self.fail("prepare() 抛了 %s 而不是 BackendError: %s"
                      % (type(exc).__name__, exc))
        else:                                        # pragma: no cover
            self.fail("Windows 上 prepare(prefer='uinput') 居然成功了")
        finally:
            b.close()

    def test_prepare_x11_raises_backend_error(self):
        b = LinuxBackend(prefer="x11")
        with self.assertRaises(BackendError):
            b.prepare()
        b.close()

    def test_prepare_auto_raises_backend_error_on_windows(self):
        b = LinuxBackend()
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
        b.close()                                    # 不许抛

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
        # probe() 自己不许抛异常, 而且每项都是 (str, bool, str)
        items = LinuxBackend().probe()
        self.assertTrue(items)
        for item in items:
            self.assertEqual(len(item), 3, item)
            self.assertIsInstance(item[0], str)
            self.assertIsInstance(item[1], bool)
            self.assertIsInstance(item[2], str)
            self.assertTrue(item[2], item)
        joined = " ".join(i[2] for i in items)
        self.assertIn("client", joined)              # 角色说明必须在

    def test_uinput_available_never_raises(self):
        ok, why = linux_uinput.UInputInjector.available()
        self.assertIsInstance(ok, bool)
        self.assertTrue(why)
        if not sys.platform.startswith("linux"):
            self.assertFalse(ok)                     # Windows 上必然不可用

    def test_uinput_module_import_has_no_side_effects(self):
        # 顶层 import 过这个模块了(本文件开头), 没炸就说明没有加载设备/库
        self.assertFalse(linux_uinput.UInputInjector().opened)
        self.assertIsNone(linux_uinput.UInputInjector().screen_size)

    def test_uinput_motion_without_screen_size_raises_chinese_error(self):
        inj = linux_uinput.UInputInjector()
        with self.assertRaises(RuntimeError) as ctx:
            # fd 是 None: 既验证了"没有尺寸"的报错, 又不会碰设备
            inj.inject_motion(1, 2)
        self.assertIn("CROSSPC_SCREEN", str(ctx.exception))

    def test_set_screen_size_ignores_garbage(self):
        inj = linux_uinput.UInputInjector()
        inj.set_screen_size(0, -5)
        self.assertIsNone(inj.screen_size)
        inj.set_screen_size(2560, 1440)
        self.assertEqual(inj.screen_size, (2560, 1440))

    def test_inject_records_pressed_keys_through_base(self):
        # 基类 inject() 会记按下状态; 用一个假实现验证委托路径正确
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
        # X11Injector 是独立小类, 不该是 Backend 子类(职责分离)
        from crosspc.backend.linux_x11 import X11Injector
        from crosspc.backend.base import Backend
        self.assertFalse(issubclass(X11Injector, Backend))
        ok, why = X11Injector.available()
        self.assertIsInstance(ok, bool)
        self.assertTrue(why)

    def test_x11_unknown_button_is_skipped(self):
        from crosspc.backend.linux_x11 import X11Injector
        inj = X11Injector(log=lambda m: None)
        # 没有 open(): 未知按键必须在碰 dpy 之前就返回, 不能抛
        inj.inject_button(4, True)

    def test_x11_wheel_sign_selection(self):
        from crosspc.backend.linux_x11 import (X_BUTTON_WHEEL_DOWN,
                                               X_BUTTON_WHEEL_LEFT,
                                               X_BUTTON_WHEEL_RIGHT,
                                               X_BUTTON_WHEEL_UP)
        # 常量本身: 4 上 5 下 6 左 7 右(X11 的滚轮就是按钮)
        self.assertEqual((X_BUTTON_WHEEL_UP, X_BUTTON_WHEEL_DOWN), (4, 5))
        self.assertEqual((X_BUTTON_WHEEL_LEFT, X_BUTTON_WHEEL_RIGHT), (6, 7))


if __name__ == "__main__":
    unittest.main(verbosity=2)
