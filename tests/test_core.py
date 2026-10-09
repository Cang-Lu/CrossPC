"""Core layer unit tests: protocol / layout / router / config / hotkey / clipboard.

Everything here is pure logic: no real keyboard or mouse, no network, no second
machine. It covers the two areas that break most easily: "position calculation"
and "critical safety behaviour".
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
        """TCP is a byte stream: partial frames and coalesced frames must both be handled."""
        a = P.frame(P.T_PING, b"a")
        b = P.frame(P.T_PONG, b"bb")
        reader = P.FrameReader()
        self.assertEqual(reader.feed(a[:3]), [])
        # the first 3 bytes, then the rest of a plus all of b fed in together
        out = reader.feed(a[3:] + b)
        self.assertEqual(out, [(P.T_PING, b"a"), (P.T_PONG, b"bb")])
        # feed it one byte at a time
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
        """One motion record = 1 type byte + 4 coordinate bytes; the order must not slip."""
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
        msg = P.parse_json(P.clipboard_text("café ünïcode Ω Привет 🙂", "win")[5:])
        self.assertEqual(msg["text"], "café ünïcode Ω Привет 🙂")
        self.assertEqual(msg["origin"], "win")

    def test_clipboard_image_frame_is_raw_png(self):
        png = b"\x89PNG\r\n\x1a\n" + b"payload"
        data = P.clipboard_image(png)
        (msg_type, body) = P.FrameReader().feed(data)[0]
        self.assertEqual(msg_type, P.T_CLIPBOARD_IMAGE)
        self.assertEqual(body, png)          # passed through verbatim, no JSON/base64 wrapping

    def test_clipboard_image_size_guard(self):
        with self.assertRaises(P.ProtocolError):
            P.clipboard_image(b"x" * (P.MAX_IMAGE + 1))


# ===========================================================================
class TestLayout(unittest.TestCase):
    def setUp(self):
        # server 1920x1080, client 2560x1440 parked on the right
        self.srv = make_server("win", 1920, 1080)
        self.cli = make_client("deb", 1920, 0, 2560, 1440)
        self.layout = Layout(self.srv, [self.cli])

    def test_contains_is_half_open(self):
        r = Rect(0, 0, 10, 10)
        self.assertTrue(r.contains(0, 0))
        self.assertTrue(r.contains(9, 9))
        self.assertFalse(r.contains(10, 9))       # the right edge does not count
        self.assertFalse(r.contains(0, 10))

    def test_clamp_is_virtual_but_clamp_local_is_not(self):
        """Mixing these two up is a trap we fell into during development, so it is pinned down."""
        r = Rect(1920, 0, 2560, 1440)
        self.assertEqual(r.clamp(0, 0), (1920, 0))          # virtual coordinates
        self.assertEqual(r.clamp_local(0, 0), (0, 0))       # local coordinates
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
        # nothing to the right of the client
        self.assertIsNone(self.layout.neighbour(self.cli, RIGHT, 4479, 500))

    def test_resolve_snaps_gap_to_nearest(self):
        """With a gap between the two machines the cursor must snap to the nearest
        rectangle instead of "disappearing"."""
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
        """Inside a gap, with both sides about equally close, stay on the current
        machine (2-pixel hysteresis) to avoid flapping."""
        layout = Layout(make_server("win", 100, 100),
                        [make_client("deb", 200, 0, 100, 100)])
        # 149 is 50 pixels from the server's right edge, 51 from the client's left edge
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
    """Router state machine: the core of "can the mouse move naturally to the other
    computer"."""

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
        """Local motion needs no action at all: the cursor already moved and
        forwarding mode is not on."""
        self.assertEqual(self.feed(Event.motion(500, 500, 5, 5)), [])
        self.assertFalse(self.router.remote)

    def test_push_through_right_edge_enters_client(self):
        actions = self.feed(Event.motion(1919, 500, 20, 0))
        self.assertEqual(self.kinds(actions), ["enter"])
        self.assertEqual(actions[0].machine.name, "deb")
        self.assertEqual((actions[0].x, actions[0].y), (0, 500))
        self.assertTrue(self.router.remote)

    def test_edge_without_neighbour_stays(self):
        """With no machine on that edge we must not switch away (left edge)."""
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
        """Switching back to the server must be local (end takeover + drop the
        cursor), never enter."""
        self.feed(Event.motion(1919, 500, 20, 0))
        actions = self.feed(Event.motion(0, 0, -3000, 0))
        self.assertEqual(self.kinds(actions), ["leave", "local"])
        self.assertEqual((actions[1].x, actions[1].y), (1919, 500))
        self.assertFalse(self.router.remote)

    def test_leaving_client_releases_pressed_keys(self):
        self.feed(Event.motion(1919, 500, 20, 0))
        self.feed(Event.key(0x1D, 0xA2, True))       # hold Ctrl
        self.feed(Event.key(0x1E, 0x41, True))       # then press A
        self.assertEqual(self.router.pressed_count(), 2)
        actions = self.feed(Event.motion(0, 0, -3000, 0))
        self.assertEqual(self.kinds(actions), ["leave", "local"])
        ups = actions[0].events
        self.assertEqual(len(ups), 2)
        self.assertTrue(all(e.kind == KEY and not e.c for e in ups))
        self.assertEqual(self.router.pressed_count(), 0)

    def test_keys_go_to_active_machine_only(self):
        """In local mode keys are not forwarded (the system already handled them);
        only in remote mode are they."""
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
        actions = router.on_event(Event.motion(0, 500, -4000, 0))  # sweep all the way left
        self.assertEqual([a.kind for a in actions], ["leave", "enter"])
        self.assertEqual(actions[1].machine.name, "left")

    def test_force_local_from_remote(self):
        self.feed(Event.motion(1919, 500, 20, 0))
        self.feed(Event.key(0x1E, 0x41, True))
        actions = self.router.force_local("test", (700, 400))
        self.assertEqual(self.kinds(actions), ["leave", "local"])
        self.assertEqual((actions[1].x, actions[1].y), (700, 400))
        self.assertFalse(self.router.remote)
        self.assertEqual(self.router.pressed_count(), 0)

    def test_force_local_when_already_local_is_noop(self):
        self.assertEqual(self.router.force_local("test"), [])

    def test_force_local_clamps_park_point_into_server(self):
        self.feed(Event.motion(1919, 500, 20, 0))
        actions = self.router.force_local("test", (99999, -50))
        self.assertEqual((actions[1].x, actions[1].y), (1919, 0))

    def test_lock_keeps_control_on_remote(self):
        self.feed(Event.motion(1919, 500, 20, 0))
        self.router.set_locked(True)
        # even flinging the cursor far away must not switch machines
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
        """A small gap between the screens (common in hand-written configs) must
        still be crossable in both directions."""
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
        """Too large a gap must not be crossed: that is usually a config mistake,
        and forcing a screen jump anyway would be even more confusing."""
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
        self.assertEqual(K.evdev_for(0x45, 0x13, False), 119)    # Pause special case
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
        # F11/F12 are not in the contiguous range and were once mis-mapped to
        # NumLock/ScrollLock
        self.assertEqual(K.parse_key_name("f10"), (0x44, False))
        self.assertEqual(K.parse_key_name("f11"), (0x57, False))
        self.assertEqual(K.parse_key_name("f12"), (0x58, False))
        self.assertIsNone(K.parse_key_name("no-such-key"))

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
        self.assertFalse(hk.feed(Event.key(0x58, 0, True)))       # Alt missing
        self.assertFalse(hk.feed(Event.key(0x58, 0, False)))
        self.assertFalse(hk.feed(Event.key(0x38, 0, True)))       # Alt
        self.assertTrue(hk.feed(Event.key(0x58, 0, True)))        # all modifiers held

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
        self.assertFalse(hk.feed(Event.key(0x10, 0, True)))       # auto-repeat

    def test_ignores_non_key_events(self):
        hk = Hotkey("ctrl+alt+q")
        self.assertFalse(hk.feed(Event.motion(1, 1)))

    def test_make_hotkeys_allows_empty(self):
        panic, lock = make_hotkeys("", "ctrl+alt+l")
        self.assertIsNone(panic)
        self.assertEqual(str(lock), "ctrl+alt+l")

    def test_make_hotkeys_rejects_bad_spec(self):
        with self.assertRaises(HotkeyError):
            make_hotkeys("ctrl+alt+nonexistent", "")


# ===========================================================================
class TestConfig(unittest.TestCase):
    def setUp(self):
        # Use a unique file name directly in the working directory and do not
        # create a subdirectory: in a restricted environment (Windows sandbox)
        # "can create a directory" and "can write a file into it" are two
        # different things
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
        cfg.token = "secret"
        cfg.clients = [ClientEntry(name="debian", host="192.168.1.50",
                                   rect=Rect(1920, 0, 2560, 1440))]
        cfg.clipboard_poll_ms = 500
        cfg.server_screen = (1920, 1080)
        cfg.save()
        again = Config.load(self.path)
        self.assertEqual(again.name, "win11")
        self.assertEqual(again.token, "secret")
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
            fh.write("{ this is not json")
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
        self.assertEqual(b.rect, Rect(4480, 0, 1920, 1080))   # placed to the right, one after another
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
        cfg.remember_size("a", 800, 600)          # already known, must not be shrunk
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
    """The classic clipboard sync bug is the echo loop: content written in by the
    peer gets sent straight back."""

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

    # ------------------------------------------------------------ text
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
        sync._paused_until = 0.0        # skip the brief suppression window after a clipboard write
        self.backend.clipboard = "new local content"
        self.backend._clip_rev += 1
        sync.tick()
        self.assertEqual(sync.calls, [("text", "new local content")])

    def test_oversize_text_is_skipped(self):
        sync = self.make(max_bytes=10)
        self.backend.clipboard = "x" * 100
        self.backend._clip_rev += 1
        sync.tick()
        self.assertEqual(sync.calls, [])

    # ------------------------------------------------------------ image
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
        """When text and an image are both present, text is sent by default (a chart
        copied out of Excel is just a short string)."""
        sync = self.make()
        self.backend.clipboard = "table text"
        self.backend.clipboard_image = self.PNG_A
        self.backend._clip_rev += 1
        sync.tick()
        self.assertEqual(sync.calls, [("text", "table text")])

    def test_prefer_image_when_configured(self):
        sync = self.make(prefer_image=True)
        self.backend.clipboard = "table text"
        self.backend.clipboard_image = self.PNG_A
        self.backend._clip_rev += 1
        sync.tick()
        self.assertEqual(sync.calls, [("image", self.PNG_A)])

    def test_text_fallback_when_no_image(self):
        sync = self.make(prefer_image=True)
        self.backend.clipboard = "text only"
        self.backend._clip_rev += 1
        sync.tick()
        self.assertEqual(sync.calls, [("text", "text only")])

    def test_remote_image_when_images_disabled_is_ignored(self):
        sync = self.make(images=False)
        sync.apply_remote("image", self.PNG_B)
        self.assertIsNone(self.backend.clipboard_image)

    # ------------------------------------------------------------ platform behaviour
    def test_no_revision_platform_slows_polling(self):
        """Linux has no clipboard revision number: relax the polling automatically so
        that we do not fork a pile of processes every second."""
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
    """The preference order of backend.clipboard_read() (pure logic, verified with the fake backend)."""

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
        # the subclass pins the attribute to False (the base class uses a read-only
        # property, so it cannot be assigned on an instance)
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
        r = Rect(-1920, -200, 4480, 1440)       # secondary screen left of the primary one
        self.assertEqual(norm_from_desktop(r, -1000, 100), (920, 300))
        self.assertEqual(desktop_from_norm(r, 920, 300), (-1000, 100))

    def test_human_bytes(self):
        from crosspc.util import human_bytes, shorten
        self.assertEqual(human_bytes(512), "512B")
        self.assertEqual(human_bytes(2048), "2.0KB")
        self.assertEqual(shorten("abcdef", 3), "abc…")
        self.assertEqual(shorten("a\nb"), "a\\nb")


class TestLogFile(unittest.TestCase):
    """--log-file: on a real machine the user runs it in their own window, so all we
    can do is read the log afterwards."""

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
        log.info("hello")
        log.warn("warning")
        log.plain("report line (no timestamp)")
        log.debug("debug should go in too")
        log.close()
        with open(self.path, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("hello", buf.getvalue())
        self.assertIn("hello", content)
        self.assertIn("warning", content)
        self.assertIn("report line (no timestamp)", content)
        self.assertIn("debug should go in too", content)
        # every line in the file should carry a timestamp + level (to match up
        # times); plain is the exception
        self.assertIn("INFO", content)
        self.assertIn("WARN", content)

    def test_level_filters_file_too(self):
        import io
        from crosspc.util import Log
        log = Log("warn", stream=io.StringIO(), file_path=self.path)
        log.info("this line must not appear")
        log.error("this line must appear")
        log.close()
        with open(self.path, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertNotIn("this line must not appear", content)
        self.assertIn("this line must appear", content)

    def test_appends_instead_of_overwriting(self):
        import io
        from crosspc.util import Log
        for text in ("first round", "second round"):
            log = Log("info", stream=io.StringIO(), file_path=self.path)
            log.info(text)
            log.close()
        with open(self.path, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("first round", content)
        self.assertIn("second round", content)

    def test_unwritable_path_does_not_crash(self):
        """A log that cannot be written must not affect the main functionality (the
        user may run it from a read-only directory)."""
        import io
        from crosspc.util import Log
        bad = os.path.join(self.dir, "missing-subdir-" + str(time.time()),
                           "x.log")
        buf = io.StringIO()
        log = Log("info", stream=buf, file_path=bad)
        log.info("must still be printed")
        log.close()
        self.assertIn("must still be printed", buf.getvalue())

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
