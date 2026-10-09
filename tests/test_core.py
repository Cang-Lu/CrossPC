"""核心层单元测试: 协议 / 布局 / 路由 / 配置 / 热键 / 剪辑板。

全部是纯逻辑, 不碰真实键鼠、不碰网络、不需要第二台机器。
覆盖的是"位置计算 + 关键安全行为"这两块最容易出错的地方。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest

from crosspc import keys as K
from crosspc import protocol as P
from crosspc.backend.fake import FakeBackend
from crosspc.clipboard import ClipboardSync
from crosspc.config import (ClientEntry, Config, ConfigError, SizeCache,
                            default_config_path)
from crosspc.events import BUTTON, KEY, MOTION, WHEEL, BTN_LEFT, Event
from crosspc.hotkey import Hotkey, HotkeyError, make_hotkeys, parse_spec
from crosspc.layout import (BOTTOM, LEFT, RIGHT, TOP, Layout, Rect, Machine,
                            make_client, make_server, relative_direction)
from crosspc.router import Router
from crosspc.util import desktop_from_norm, norm_from_desktop


# ===========================================================================
class TestProtocol(unittest.TestCase):
    def test_frame_roundtrip(self):
        data = P.frame(P.T_CLIPBOARD, b"hello")
        reader = P.FrameReader()
        out = reader.feed(data)
        self.assertEqual(out, [(P.T_CLIPBOARD, b"hello")])
        self.assertEqual(reader.pending(), 0)

    def test_frame_reader_partial_and_multiple(self):
        """TCP 是字节流: 半帧、粘帧都必须能正确处理。"""
        a = P.frame(P.T_PING, b"a")
        b = P.frame(P.T_PONG, b"bb")
        reader = P.FrameReader()
        self.assertEqual(reader.feed(a[:3]), [])
        # 前 3 字节 + 剩下的 a + 整个 b 一起喂进去
        out = reader.feed(a[3:] + b)
        self.assertEqual(out, [(P.T_PING, b"a"), (P.T_PONG, b"bb")])
        # 一字节一字节地喂
        reader = P.FrameReader()
        got = []
        for i in range(len(b)):
            got += reader.feed(bytes([b[i]]))
        self.assertEqual(got, [(P.T_PONG, b"bb")])

    def test_frame_too_large_is_rejected(self):
        bogus = P.FRAME_HEADER.pack(P.MAX_FRAME + 1, P.T_PING)
        with self.assertRaises(P.ProtocolError):
            P.FrameReader().feed(bogus)

    def test_encode_input_uses_fixed_offsets(self):
        """一条移动记录 = 1 字节类型 + 4 字节坐标; 顺序不能错。"""
        payload = P.encode_input([Event.motion(0x0102, 0x0304)])
        self.assertEqual(payload, b"\x00\x01" + bytes([P.EV_MOTION]) +
                         bytes([1, 2, 3, 4]))

    def test_input_roundtrip_all_kinds(self):
        events = [
            Event.motion(1919, 500),
            Event.button(BTN_LEFT, True),
            Event.wheel(0, -1),
            Event.key(0x1E, 0x41, True, False),
            Event.key(0x48, 0x26, False, True),
        ]
        self.assertEqual(P.decode_input(P.encode_input(events)), events)

    def test_input_coordinates_are_clamped_to_int16(self):
        payload = P.encode_input([Event.motion(-99999, 99999)])
        (ev,) = P.decode_input(payload)
        self.assertEqual((ev.a, ev.b), (-32768, 32767))

    def test_decode_truncated_raises(self):
        with self.assertRaises(P.ProtocolError):
            P.decode_input(b"")
        with self.assertRaises(P.ProtocolError):
            P.decode_input(b"\x00\x02" + bytes([P.EV_BUTTON, 1]))

    def test_hello_and_ack_are_json(self):
        msg = P.parse_json(P.hello("win", "tok", Rect(0, 0, 10, 10).as_dict(),
                                   version="1.0")[5:])
        self.assertEqual(msg["magic"], P.MAGIC)
        self.assertEqual(msg["protocol"], P.PROTOCOL_VERSION)
        self.assertEqual(msg["name"], "win")

    def test_clipboard_message_keeps_unicode(self):
        msg = P.parse_json(P.clipboard_text("中文🙂", "win")[5:])
        self.assertEqual(msg["text"], "中文🙂")
        self.assertEqual(msg["origin"], "win")

    def test_clipboard_image_frame_is_raw_png(self):
        png = b"\x89PNG\r\n\x1a\n" + b"payload"
        data = P.clipboard_image(png)
        (msg_type, body) = P.FrameReader().feed(data)[0]
        self.assertEqual(msg_type, P.T_CLIPBOARD_IMAGE)
        self.assertEqual(body, png)          # 原样透传, 不套 JSON/base64

    def test_clipboard_image_size_guard(self):
        with self.assertRaises(P.ProtocolError):
            P.clipboard_image(b"x" * (P.MAX_IMAGE + 1))


# ===========================================================================
class TestLayout(unittest.TestCase):
    def setUp(self):
        # server 1920x1080, client 2560x1440 贴在右边
        self.srv = make_server("win", 1920, 1080)
        self.cli = make_client("deb", 1920, 0, 2560, 1440)
        self.layout = Layout(self.srv, [self.cli])

    def test_contains_is_half_open(self):
        r = Rect(0, 0, 10, 10)
        self.assertTrue(r.contains(0, 0))
        self.assertTrue(r.contains(9, 9))
        self.assertFalse(r.contains(10, 9))       # 右边界不算
        self.assertFalse(r.contains(0, 10))

    def test_clamp_is_virtual_but_clamp_local_is_not(self):
        """这两个方法用错就是本次开发踩过的坑, 必须钉死。"""
        r = Rect(1920, 0, 2560, 1440)
        self.assertEqual(r.clamp(0, 0), (1920, 0))          # 虚拟坐标
        self.assertEqual(r.clamp_local(0, 0), (0, 0))       # 本机坐标
        self.assertEqual(r.clamp_local(-5, 99999), (0, 1439))
        self.assertEqual(r.clamp_local(99999, 5), (2559, 5))

    def test_virtual_local_conversion(self):
        self.assertEqual(self.cli.local_to_virtual(40, 60), (1960, 60))
        self.assertEqual(self.cli.virtual_to_local(1960, 60), (40, 60))

    def test_hit_test(self):
        self.assertIs(self.layout.machine_at(100, 100), self.srv)
        self.assertIs(self.layout.machine_at(1919, 100), self.srv)
        self.assertIs(self.layout.machine_at(1920, 100), self.cli)
        self.assertIsNone(self.layout.machine_at(0, -1))

    def test_edge_detection(self):
        self.assertEqual(self.layout.exit_direction(self.srv, 5, 0, 1919, 500),
                         RIGHT)
        self.assertIsNone(self.layout.exit_direction(self.srv, -5, 0, 1919, 500))
        self.assertIsNone(self.layout.exit_direction(self.srv, 5, 0, 1000, 500))
        self.assertEqual(self.layout.exit_direction(self.srv, 0, -5, 100, 0), TOP)
        self.assertEqual(self.layout.exit_direction(self.srv, 0, 5, 100, 1079),
                         BOTTOM)
        self.assertEqual(self.layout.exit_direction(self.cli, -5, 0, 0, 100),
                         LEFT)

    def test_neighbour(self):
        self.assertIs(self.layout.neighbour(self.srv, RIGHT, 1919, 500),
                      self.cli)
        self.assertIsNone(self.layout.neighbour(self.srv, LEFT, 0, 500))
        # client 右边没有东西
        self.assertIsNone(self.layout.neighbour(self.cli, RIGHT, 4479, 500))

    def test_resolve_snaps_gap_to_nearest(self):
        """两台机器之间有缝时, 光标要吸附到最近的矩形, 不能"消失"。"""
        layout = Layout(make_server("win", 1920, 1080),
                        [make_client("deb", 2000, 0, 1920, 1080)])
        m, lx, ly, snapped = layout.resolve(1950, 500)
        self.assertIs(m, layout.server)
        self.assertTrue(snapped)
        self.assertEqual((lx, ly), (1919, 500))
        m, lx, ly, snapped = layout.resolve(1990, 500)
        self.assertIs(m, layout.clients[0])
        self.assertTrue(snapped)
        self.assertEqual((lx, ly), (0, 500))

    def test_resolve_prefers_current_machine_on_ties(self):
        """缝隙里两边距离接近时优先保持当前机器(2 像素迟滞), 避免抖动。"""
        layout = Layout(make_server("win", 100, 100),
                        [make_client("deb", 200, 0, 100, 100)])
        # 149 离 server 右边缘 50 像素、离 client 左边缘 51 像素
        m, _, _, _ = layout.resolve(149, 50, prefer=layout.server)
        self.assertIs(m, layout.server)
        m, _, _, _ = layout.resolve(149, 50, prefer=layout.clients[0])
        self.assertIs(m, layout.clients[0])

    def test_relative_direction(self):
        self.assertEqual(relative_direction(Rect(0, 0, 10, 10), Rect(20, 0, 10, 10)),
                         RIGHT)
        self.assertEqual(relative_direction(Rect(20, 0, 10, 10), Rect(0, 0, 10, 10)),
                         LEFT)
        self.assertEqual(relative_direction(Rect(0, 0, 10, 10), Rect(0, 50, 10, 10)),
                         BOTTOM)
        self.assertEqual(relative_direction(Rect(0, 50, 10, 10), Rect(0, 0, 10, 10)),
                         TOP)

    def test_bounds(self):
        self.assertEqual(self.layout.bounds(), Rect(0, 0, 4480, 1440))

    def test_json_roundtrip(self):
        r = Rect(1, 2, 3, 4)
        self.assertEqual(Rect.from_dict(r.as_dict()), r)
        self.assertEqual(Rect.from_dict([1, 2, 3, 4]), r)


# ===========================================================================
class TestRouter(unittest.TestCase):
    """路由状态机: 这是"鼠标能不能自然跑到另一台电脑"的核心。"""

    def setUp(self):
        self.srv = make_server("win", 1920, 1080)
        self.cli = make_client("deb", 1920, 0, 2560, 1440)
        self.layout = Layout(self.srv, [self.cli])
        self.router = Router(self.layout)

    def feed(self, ev):
        return self.router.on_event(ev)

    def kinds(self, actions):
        return [a.kind for a in actions]

    def test_motion_inside_server_produces_nothing(self):
        """本机移动不需要任何动作: 光标已经动了, 转发模式也没开。"""
        self.assertEqual(self.feed(Event.motion(500, 500, 5, 5)), [])
        self.assertFalse(self.router.remote)

    def test_push_through_right_edge_enters_client(self):
        actions = self.feed(Event.motion(1919, 500, 20, 0))
        self.assertEqual(self.kinds(actions), ["enter"])
        self.assertEqual(actions[0].machine.name, "deb")
        self.assertEqual((actions[0].x, actions[0].y), (0, 500))
        self.assertTrue(self.router.remote)

    def test_edge_without_neighbour_stays(self):
        """边上没有机器时不能切走(左边缘)。"""
        actions = self.feed(Event.motion(0, 500, -20, 0))
        self.assertEqual(actions, [])
        self.assertFalse(self.router.remote)

    def test_remote_motion_is_client_local(self):
        self.feed(Event.motion(1919, 500, 20, 0))
        actions = self.feed(Event.motion(1919, 500, 40, 60))
        self.assertEqual(self.kinds(actions), ["remote"])
        ev = actions[0].events[0]
        self.assertEqual((ev.a, ev.b), (40, 560))

    def test_return_to_server_emits_local_not_enter(self):
        """切回 server 必须是 local(关接管+放光标), 不能是 enter。"""
        self.feed(Event.motion(1919, 500, 20, 0))
        actions = self.feed(Event.motion(0, 0, -3000, 0))
        self.assertEqual(self.kinds(actions), ["leave", "local"])
        self.assertEqual((actions[1].x, actions[1].y), (1919, 500))
        self.assertFalse(self.router.remote)

    def test_leaving_client_releases_pressed_keys(self):
        self.feed(Event.motion(1919, 500, 20, 0))
        self.feed(Event.key(0x1D, 0xA2, True))       # 按住 Ctrl
        self.feed(Event.key(0x1E, 0x41, True))       # 再按 A
        self.assertEqual(self.router.pressed_count(), 2)
        actions = self.feed(Event.motion(0, 0, -3000, 0))
        self.assertEqual(self.kinds(actions), ["leave", "local"])
        ups = actions[0].events
        self.assertEqual(len(ups), 2)
        self.assertTrue(all(e.kind == KEY and not e.c for e in ups))
        self.assertEqual(self.router.pressed_count(), 0)

    def test_keys_go_to_active_machine_only(self):
        """本机模式下按键不转发(系统已经处理过了), 远端模式下才转发。"""
        self.assertEqual(self.feed(Event.key(0x1E, 0x41, True)), [])
        self.feed(Event.motion(1919, 500, 20, 0))
        actions = self.feed(Event.key(0x1E, 0x41, True))
        self.assertEqual(self.kinds(actions), ["remote"])

    def test_buttons_and_wheel_follow_the_same_rule(self):
        self.assertEqual(self.feed(Event.button(BTN_LEFT, True)), [])
        self.assertEqual(self.feed(Event.wheel(0, 1)), [])
        self.feed(Event.motion(1919, 500, 20, 0))
        self.assertEqual(self.kinds(self.feed(Event.button(BTN_LEFT, True))),
                         ["remote"])
        self.assertEqual(self.kinds(self.feed(Event.wheel(0, 1))), ["remote"])

    def test_switch_between_two_clients(self):
        left = make_client("left", -1280, 0, 1280, 1024)
        layout = Layout(self.srv, [self.cli, left])
        router = Router(layout)
        router.on_event(Event.motion(1919, 500, 20, 0))          # -> deb
        self.assertEqual(router.active.name, "deb")
        actions = router.on_event(Event.motion(0, 500, -4000, 0))  # 一路向左
        self.assertEqual([a.kind for a in actions], ["leave", "enter"])
        self.assertEqual(actions[1].machine.name, "left")

    def test_force_local_from_remote(self):
        self.feed(Event.motion(1919, 500, 20, 0))
        self.feed(Event.key(0x1E, 0x41, True))
        actions = self.router.force_local("测试", (700, 400))
        self.assertEqual(self.kinds(actions), ["leave", "local"])
        self.assertEqual((actions[1].x, actions[1].y), (700, 400))
        self.assertFalse(self.router.remote)
        self.assertEqual(self.router.pressed_count(), 0)

    def test_force_local_when_already_local_is_noop(self):
        self.assertEqual(self.router.force_local("测试"), [])

    def test_force_local_clamps_park_point_into_server(self):
        self.feed(Event.motion(1919, 500, 20, 0))
        actions = self.router.force_local("测试", (99999, -50))
        self.assertEqual((actions[1].x, actions[1].y), (1919, 0))

    def test_lock_keeps_control_on_remote(self):
        self.feed(Event.motion(1919, 500, 20, 0))
        self.router.set_locked(True)
        # 就算一路甩到很远也不换机器
        for _ in range(5):
            actions = self.feed(Event.motion(0, 0, -2000, 0))
            self.assertTrue(all(a.kind == "remote" for a in actions))
        self.assertEqual(self.router.active.name, "deb")
        self.router.set_locked(False)
        self.assertEqual(self.kinds(self.feed(Event.motion(0, 0, -2000, 0))),
                         ["leave", "local"])

    def test_lock_ignored_when_local(self):
        self.router.set_locked(True)
        self.assertFalse(self.router.locked)

    def test_gap_layout_crosses_small_gap(self):
        """两屏之间留了小缝(手写配置常见)也必须能过去和回来。"""
        layout = Layout(make_server("win", 1920, 1080),
                        [make_client("deb", 2000, 0, 1920, 1080)])
        router = Router(layout)
        actions = router.on_event(Event.motion(1919, 500, 100, 0))
        self.assertEqual([a.kind for a in actions], ["enter"])
        self.assertEqual(router.active.name, "deb")
        self.assertEqual((actions[0].x, actions[0].y), (0, 500))
        actions = router.on_event(Event.motion(0, 0, -5000, 0))
        self.assertEqual([a.kind for a in actions], ["leave", "local"])
        self.assertEqual(router.active.name, "win")

    def test_huge_gap_is_not_crossed(self):
        """缝太大就不该穿过去: 那多半是配置写错了, 强行跨屏会更让人困惑。"""
        layout = Layout(make_server("win", 1920, 1080),
                        [make_client("deb", 5000, 0, 1920, 1080)])
        router = Router(layout)
        self.assertEqual(router.on_event(Event.motion(1919, 500, 100, 0)), [])
        self.assertEqual(router.active.name, "win")


# ===========================================================================
class TestKeys(unittest.TestCase):
    def test_scancode_tables(self):
        self.assertEqual(K.keysym_for(0x1E), ord("a"))
        self.assertEqual(K.keysym_for(0x39), K.XK_SPACE)
        self.assertEqual(K.keysym_for(0x48, True), K.XK_UP)
        self.assertEqual(K.keysym_for(0x0F), K.XK_TAB)
        self.assertIsNone(K.keysym_for(0xFE))

    def test_evdev_mapping(self):
        self.assertEqual(K.evdev_for(0x1E, 0x41, False), 30)
        self.assertEqual(K.evdev_for(0x48, 0x26, True), 103)     # KEY_UP
        self.assertEqual(K.evdev_for(0x1C, 0x0D, True), 96)      # KEY_KPENTER
        self.assertEqual(K.evdev_for(0x1D, 0xA2, False), 29)     # KEY_LEFTCTRL
        self.assertEqual(K.evdev_for(0x45, 0x13, False), 119)    # Pause 特判
        self.assertIsNone(K.evdev_for(0xFE))

    def test_modifier_and_toggle_detection(self):
        self.assertTrue(K.is_modifier(0x1D, False))
        self.assertTrue(K.is_modifier(0x38, True))
        self.assertFalse(K.is_modifier(0x1E, False))
        self.assertTrue(K.is_toggle(0x3A, False))

    def test_parse_key_name(self):
        self.assertEqual(K.parse_key_name("q"), (0x10, False))
        self.assertEqual(K.parse_key_name("CTRL"), (0x1D, False))
        self.assertEqual(K.parse_key_name("escape"), (0x01, False))
        # F11/F12 不在连续区间里, 曾经被错映射成 NumLock/ScrollLock
        self.assertEqual(K.parse_key_name("f10"), (0x44, False))
        self.assertEqual(K.parse_key_name("f11"), (0x57, False))
        self.assertEqual(K.parse_key_name("f12"), (0x58, False))
        self.assertIsNone(K.parse_key_name("没有这个键"))

    def test_key_name_for_logging(self):
        self.assertEqual(K.key_name(0x1E), "A")
        self.assertEqual(K.key_name(0x48, 0, True), "UP")
        self.assertEqual(K.key_name(0x1D, 0, False), "LCtrl")


# ===========================================================================
class TestHotkey(unittest.TestCase):
    def test_parse_spec(self):
        mods, trig = parse_spec("ctrl+alt+f12")
        self.assertEqual(mods, [(0x1D, False), (0x38, False)])
        self.assertEqual(trig, [(0x58, False)])
        with self.assertRaises(HotkeyError):
            parse_spec("")
        with self.assertRaises(HotkeyError):
            parse_spec("ctrl+nonsense")

    def test_fires_only_with_all_modifiers(self):
        hk = Hotkey("ctrl+alt+f12")
        self.assertFalse(hk.feed(Event.key(0x1D, 0, True)))       # Ctrl
        self.assertFalse(hk.feed(Event.key(0x58, 0, True)))       # 缺 Alt
        self.assertFalse(hk.feed(Event.key(0x58, 0, False)))
        self.assertFalse(hk.feed(Event.key(0x38, 0, True)))       # Alt
        self.assertTrue(hk.feed(Event.key(0x58, 0, True)))        # 齐了

    def test_modifier_release_resets_state(self):
        hk = Hotkey("ctrl+alt+f12")
        hk.feed(Event.key(0x1D, 0, True))
        hk.feed(Event.key(0x1D, 0, False))
        hk.feed(Event.key(0x38, 0, True))
        self.assertFalse(hk.feed(Event.key(0x58, 0, True)))

    def test_does_not_fire_twice_on_key_repeat(self):
        hk = Hotkey("ctrl+alt+q")
        hk.feed(Event.key(0x1D, 0, True))
        hk.feed(Event.key(0x38, 0, True))
        self.assertTrue(hk.feed(Event.key(0x10, 0, True)))
        self.assertFalse(hk.feed(Event.key(0x10, 0, True)))       # 自动重复

    def test_ignores_non_key_events(self):
        hk = Hotkey("ctrl+alt+q")
        self.assertFalse(hk.feed(Event.motion(1, 1)))

    def test_make_hotkeys_allows_empty(self):
        panic, lock = make_hotkeys("", "ctrl+alt+l")
        self.assertIsNone(panic)
        self.assertEqual(str(lock), "ctrl+alt+l")

    def test_make_hotkeys_rejects_bad_spec(self):
        with self.assertRaises(HotkeyError):
            make_hotkeys("ctrl+alt+不存在", "")


# ===========================================================================
class TestConfig(unittest.TestCase):
    def setUp(self):
        # 直接在工作目录里用唯一文件名, 不建子目录: 受限环境(Windows 沙箱)
        # 里"能建目录"和"能往目录里写文件"是两回事
        from crosspc.util import pick_writable_dir, scratch_prefix
        self.tmp = pick_writable_dir()
        self.prefix = scratch_prefix("test")
        self.path = os.path.join(self.tmp, self.prefix + "crosspc.json")
        self._made = [self.path]

    def tearDown(self):
        for p in self._made:
            for candidate in (p, p + ".tmp"):
                try:
                    os.remove(candidate)
                except OSError:
                    pass

    def test_defaults(self):
        cfg = Config.defaults(self.path)
        self.assertEqual(cfg.port, 39987)
        self.assertEqual(cfg.hotkey_panic, "ctrl+alt+f12")
        self.assertTrue(cfg.clipboard_enabled)

    def test_save_load_roundtrip(self):
        cfg = Config.defaults(self.path)
        cfg.name = "win11"
        cfg.token = "秘密"
        cfg.clients = [ClientEntry(name="debian", host="192.168.1.50",
                                   rect=Rect(1920, 0, 2560, 1440))]
        cfg.clipboard_poll_ms = 500
        cfg.server_screen = (1920, 1080)
        cfg.save()
        again = Config.load(self.path)
        self.assertEqual(again.name, "win11")
        self.assertEqual(again.token, "秘密")
        self.assertEqual(again.clipboard_poll_ms, 500)
        self.assertEqual(again.clients[0].rect, Rect(1920, 0, 2560, 1440))
        self.assertEqual(again.server_screen, (1920, 1080))

    def test_unknown_keys_are_preserved(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "future_option": {"a": 1}}, fh)
        cfg = Config.load(self.path)
        cfg.save()
        with open(self.path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        self.assertEqual(raw["future_option"], {"a": 1})

    def test_bad_json_gives_friendly_error(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{ 这不是 json")
        with self.assertRaises(ConfigError):
            Config.load(self.path)

    def test_client_entry_requires_name(self):
        with self.assertRaises(ConfigError):
            Config.from_dict({"clients": [{"host": "1.2.3.4"}]})

    def test_screen_size_forms(self):
        self.assertEqual(Config.from_dict({"screen": "2560x1440"}).screen,
                         (2560, 1440))
        self.assertEqual(Config.from_dict({"screen": {"w": 800, "h": 600}}).screen,
                         (800, 600))
        self.assertEqual(Config.from_dict({"screen": [1024, 768]}).screen,
                         (1024, 768))
        self.assertIsNone(Config.from_dict({}).screen)

    def test_rect_forms_and_auto(self):
        cfg = Config.from_dict({"clients": [
            {"name": "a", "rect": [1920, 0, 100, 100]},
            {"name": "b", "rect": {"x": 100, "y": 5, "w": 50, "h": 50}},
            {"name": "c"},
        ]})
        self.assertEqual(cfg.clients[0].rect, Rect(1920, 0, 100, 100))
        self.assertEqual(cfg.clients[1].rect, Rect(100, 5, 50, 50))
        self.assertIsNone(cfg.clients[2].rect)

    def test_build_layout_uses_cache_and_auto_places(self):
        cfg = Config.from_dict({"name": "win", "clients": [
            {"name": "a"}, {"name": "b", "host": "10.0.0.9"}]})
        layout = cfg.build_layout(Rect(0, 0, 1920, 1080),
                                  {"a": (2560, 1440)})
        self.assertEqual(layout.server.rect, Rect(0, 0, 1920, 1080))
        a, b = layout.clients
        self.assertEqual(a.rect, Rect(1920, 0, 2560, 1440))
        self.assertEqual(b.rect, Rect(4480, 0, 1920, 1080))   # 依次往右摆
        self.assertEqual(b.host, "10.0.0.9")

    def test_build_layout_skips_disabled(self):
        cfg = Config.from_dict({"clients": [
            {"name": "a", "enabled": False}, {"name": "b"}]})
        layout = cfg.build_layout(Rect(0, 0, 100, 100), {})
        self.assertEqual([m.name for m in layout.clients], ["b"])

    def test_remember_size_only_when_missing(self):
        cfg = Config.from_dict({"clients": [{"name": "a"}]})
        cfg.remember_size("a", 2560, 1440)
        self.assertEqual(cfg.clients[0].rect, Rect(0, 0, 2560, 1440))
        cfg.remember_size("a", 800, 600)          # 已经有了, 不该被改小
        self.assertEqual(cfg.clients[0].rect, Rect(0, 0, 2560, 1440))

    def test_size_cache_roundtrip(self):
        cache_path = os.path.join(self.tmp, self.prefix + "cache.json")
        self._made.append(cache_path)
        cache = SizeCache(cache_path)
        self.assertIsNone(cache.get("debian"))
        cache.set("debian", 2560, 1440)
        again = SizeCache(cache_path)
        self.assertEqual(again.get("debian"), (2560, 1440))

    def test_default_config_path_is_absolute(self):
        self.assertTrue(os.path.isabs(default_config_path()))


# ===========================================================================
class TestClipboardSync(unittest.TestCase):
    """剪辑板同步最容易出的问题是"回环": 对端写进来的内容又被发回去。"""

    PNG_A = b"\x89PNG\r\n\x1a\n" + b"A" * 64
    PNG_B = b"\x89PNG\r\n\x1a\n" + b"B" * 64

    def make(self, **kw):
        self.backend = FakeBackend()
        calls = []
        sync = ClipboardSync(self.backend,
                             lambda kind, payload: calls.append((kind, payload)),
                             log=_quiet_log(), poll_ms=100, **kw)
        sync.calls = calls
        sync._last_rev = self.backend.clipboard_revision()
        sync._last_hash = sync._hash(sync._read())
        return sync

    # ------------------------------------------------------------ 文本
    def test_local_text_change_is_sent_once(self):
        sync = self.make()
        self.backend.clipboard = "hello"
        self.backend._clip_rev += 1
        sync.tick()
        sync.tick()
        self.assertEqual(sync.calls, [("text", "hello")])

    def test_remote_text_is_not_echoed_back(self):
        sync = self.make()
        sync.apply_remote("text", "from remote")
        self.assertEqual(self.backend.clipboard, "from remote")
        sync.tick()
        sync.tick()
        self.assertEqual(sync.calls, [])

    def test_remote_then_new_local_change_is_sent(self):
        sync = self.make()
        sync.apply_remote("text", "from remote")
        sync.tick()
        sync._paused_until = 0.0        # 跳过"刚写完剪辑板"的短暂抑制窗口
        self.backend.clipboard = "本地新内容"
        self.backend._clip_rev += 1
        sync.tick()
        self.assertEqual(sync.calls, [("text", "本地新内容")])

    def test_oversize_text_is_skipped(self):
        sync = self.make(max_bytes=10)
        self.backend.clipboard = "x" * 100
        self.backend._clip_rev += 1
        sync.tick()
        self.assertEqual(sync.calls, [])

    # ------------------------------------------------------------ 图片
    def test_local_image_is_sent(self):
        sync = self.make()
        self.backend.clipboard_image = self.PNG_A
        self.backend._clip_rev += 1
        sync.tick()
        sync.tick()
        self.assertEqual(sync.calls, [("image", self.PNG_A)])
        self.assertEqual(sync.sent_images, 1)

    def test_remote_image_is_written_and_not_echoed(self):
        sync = self.make()
        sync.apply_remote("image", self.PNG_B)
        self.assertEqual(self.backend.clipboard_image, self.PNG_B)
        sync.tick()
        sync.tick()
        self.assertEqual(sync.calls, [])

    def test_oversize_image_is_skipped(self):
        sync = self.make(max_image_bytes=16)
        self.backend.clipboard_image = self.PNG_A
        self.backend._clip_rev += 1
        sync.tick()
        self.assertEqual(sync.calls, [])

    def test_images_can_be_disabled(self):
        sync = self.make(images=False)
        self.assertEqual(sync.max_image_bytes, 0)
        self.backend.clipboard_image = self.PNG_A
        self.backend._clip_rev += 1
        sync.tick()
        self.assertEqual(sync.calls, [])

    def test_text_is_preferred_by_default(self):
        """文本和图片同时在: 默认发文本(图表从 Excel 复制过来只是一小段字)。"""
        sync = self.make()
        self.backend.clipboard = "表格文字"
        self.backend.clipboard_image = self.PNG_A
        self.backend._clip_rev += 1
        sync.tick()
        self.assertEqual(sync.calls, [("text", "表格文字")])

    def test_prefer_image_when_configured(self):
        sync = self.make(prefer_image=True)
        self.backend.clipboard = "表格文字"
        self.backend.clipboard_image = self.PNG_A
        self.backend._clip_rev += 1
        sync.tick()
        self.assertEqual(sync.calls, [("image", self.PNG_A)])

    def test_text_fallback_when_no_image(self):
        sync = self.make(prefer_image=True)
        self.backend.clipboard = "只有文字"
        self.backend._clip_rev += 1
        sync.tick()
        self.assertEqual(sync.calls, [("text", "只有文字")])

    def test_remote_image_when_images_disabled_is_ignored(self):
        sync = self.make(images=False)
        sync.apply_remote("image", self.PNG_B)
        self.assertIsNone(self.backend.clipboard_image)

    # ------------------------------------------------------------ 平台行为
    def test_no_revision_platform_slows_polling(self):
        """Linux 没有剪辑板序号: 自动把轮询放宽, 免得每秒 fork 一堆进程。"""
        backend = FakeBackend()
        backend.clipboard_revision = lambda: None     # type: ignore[assignment]
        sync = ClipboardSync(backend, lambda k, p: None, log=_quiet_log(),
                             poll_ms=100)
        sync.start()
        sync.stop()
        self.assertGreaterEqual(sync.poll_ms, 800)

    def test_disabled_when_backend_lacks_clipboard(self):
        backend = FakeBackend()
        backend.supports_clipboard = False
        sync = ClipboardSync(backend, lambda k, p: None, log=_quiet_log())
        sync.start()
        self.assertFalse(sync._thread)
        sync.stop()


class TestClipboardReadPreference(unittest.TestCase):
    """backend.clipboard_read() 的取用顺序(纯逻辑, 用假后端验证)。"""

    def test_text_first_by_default(self):
        be = FakeBackend()
        be.clipboard = "T"
        be.clipboard_image = b"\x89PNG\r\n\x1a\nA"
        self.assertEqual(be.clipboard_read(1024), ("text", "T"))

    def test_image_first_when_asked(self):
        be = FakeBackend()
        be.clipboard = "T"
        be.clipboard_image = b"\x89PNG\r\n\x1a\nA"
        self.assertEqual(be.clipboard_read(1024, prefer_image=True)[0], "image")

    def test_falls_back_to_image_without_text(self):
        be = FakeBackend()
        be.clipboard = ""
        be.clipboard_image = b"\x89PNG\r\n\x1a\nA"
        self.assertEqual(be.clipboard_read(1024), ("image", b"\x89PNG\r\n\x1a\nA"))

    def test_none_when_empty(self):
        be = FakeBackend()
        self.assertIsNone(be.clipboard_read(1024))

    def test_image_skipped_when_cap_is_zero(self):
        be = FakeBackend()
        be.clipboard_image = b"\x89PNG\r\n\x1a\nA"
        self.assertIsNone(be.clipboard_read(0))

    def test_image_skipped_over_cap(self):
        be = FakeBackend()
        be.clipboard_image = b"\x89PNG\r\n\x1a\n" + b"x" * 100
        self.assertIsNone(be.clipboard_read(16))

    def test_backend_without_images(self):
        # 子类把属性压成 False(基类是只读 property, 实例上赋不了值)
        no_image = type("NoImageFake", (FakeBackend,),
                        {"supports_clipboard_images": False})()
        no_image.clipboard_image = b"\x89PNG\r\n\x1a\nA"
        self.assertIsNone(no_image.clipboard_read(1024))


def _quiet_log():
    from crosspc.util import Log
    return Log("error")


# ===========================================================================
class TestUtil(unittest.TestCase):
    def test_norm_and_denorm(self):
        r = Rect(-1920, -200, 4480, 1440)       # 副屏在主屏左侧
        self.assertEqual(norm_from_desktop(r, -1000, 100), (920, 300))
        self.assertEqual(desktop_from_norm(r, 920, 300), (-1000, 100))

    def test_human_bytes(self):
        from crosspc.util import human_bytes, shorten
        self.assertEqual(human_bytes(512), "512B")
        self.assertEqual(human_bytes(2048), "2.0KB")
        self.assertEqual(shorten("abcdef", 3), "abc…")
        self.assertEqual(shorten("a\nb"), "a\\nb")


class TestLogFile(unittest.TestCase):
    """--log-file: 真机联调时用户在自己窗口里跑, 我们只能事后看日志。"""

    def setUp(self):
        from crosspc.util import pick_writable_dir, scratch_prefix
        self.dir = pick_writable_dir()
        self.path = os.path.join(self.dir, scratch_prefix("log") + "test.log")
        self._made = [self.path]

    def tearDown(self):
        for p in self._made:
            try:
                os.remove(p)
            except OSError:
                pass

    def test_writes_to_both_stream_and_file(self):
        import io
        from crosspc.util import Log
        buf = io.StringIO()
        log = Log("debug", stream=buf, file_path=self.path)
        log.info("你好")
        log.warn("注意")
        log.plain("报告一行(不带时间戳)")
        log.debug("debug 也该进去")
        log.close()
        with open(self.path, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("你好", buf.getvalue())
        self.assertIn("你好", content)
        self.assertIn("注意", content)
        self.assertIn("报告一行(不带时间戳)", content)
        self.assertIn("debug 也该进去", content)
        # 文件里每条都该带时间戳+级别(便于对时间); plain 除外
        self.assertIn("INFO", content)
        self.assertIn("WARN", content)

    def test_level_filters_file_too(self):
        import io
        from crosspc.util import Log
        log = Log("warn", stream=io.StringIO(), file_path=self.path)
        log.info("这条不该出现")
        log.error("这条要出现")
        log.close()
        with open(self.path, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertNotIn("这条不该出现", content)
        self.assertIn("这条要出现", content)

    def test_appends_instead_of_overwriting(self):
        import io
        from crosspc.util import Log
        for text in ("第一轮", "第二轮"):
            log = Log("info", stream=io.StringIO(), file_path=self.path)
            log.info(text)
            log.close()
        with open(self.path, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("第一轮", content)
        self.assertIn("第二轮", content)

    def test_unwritable_path_does_not_crash(self):
        """日志写不了不能影响主功能(用户可能在只读目录里跑)。"""
        import io
        from crosspc.util import Log
        bad = os.path.join(self.dir, "不存在的子目录" + str(time.time()),
                           "x.log")
        buf = io.StringIO()
        log = Log("info", stream=buf, file_path=bad)
        log.info("仍然要能打出来")
        log.close()
        self.assertIn("仍然要能打出来", buf.getvalue())

    def test_creates_parent_directory(self):
        import io
        from crosspc.util import Log
        nested = os.path.join(self.dir, "logs-%d" % os.getpid(), "a.log")
        self._made.append(nested)
        log = Log("info", stream=io.StringIO(), file_path=nested)
        log.info("x")
        log.close()
        self.assertTrue(os.path.exists(nested))
        import shutil
        shutil.rmtree(os.path.dirname(nested), ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
