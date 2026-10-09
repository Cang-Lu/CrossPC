"""Config file (JSON) reading/writing and layout assembly.

The config file looks like this (every field may be omitted, and omitting one
means using its default):

    {
      "version": 1,
      "name": "win11",                  // this machine's name in the UI/logs
      "port": 39987,                    // the port the server listens on
      "discovery_port": 39988,          // the UDP discovery port
      "token": "",                      // once set, both ends must match, to keep unknown machines out
      "clipboard": {"enabled": true, "poll_ms": 300, "max_bytes": 262144},
      "hotkeys": {"panic": "ctrl+alt+f12", "lock": "ctrl+alt+l"},
      "clients": [
        {"name": "debian", "host": "192.168.1.50",
         "rect": {"x": 1920, "y": 0, "w": 2560, "h": 1440}}
      ]
    }

"rect" may be omitted entirely (when a client connects for the first time the
server learns its real resolution and lays it out automatically, "one after
another to the right"), or only x/y may be written and the width/height left to
automatic detection.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .layout import Layout, Machine, Rect, make_client, make_server, relative_direction

CONFIG_VERSION = 1
DEFAULT_PORT = 39987
DEFAULT_DISCOVERY_PORT = 39988
DEFAULT_CLIPBOARD_POLL_MS = 300
DEFAULT_CLIPBOARD_MAX = 256 * 1024
DEFAULT_CLIPBOARD_MAX_IMAGE = 4 * 1024 * 1024
DEFAULT_PANIC = "ctrl+alt+f12"
DEFAULT_LOCK = "ctrl+alt+l"
DEFAULT_CONFIG_NAME = "crosspc.json"
CACHE_NAME = "crosspc.cache.json"
#: Fallback screen size when the real resolution cannot be obtained
FALLBACK_SCREEN = (1920, 1080)


class ConfigError(RuntimeError):
    pass


def user_config_dir() -> str:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "CrossPC")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "crosspc")


def default_config_path() -> str:
    """Lookup order when --config is not given explicitly: current directory > user config directory."""
    local = os.path.join(os.getcwd(), DEFAULT_CONFIG_NAME)
    if os.path.exists(local):
        return local
    return os.path.join(user_config_dir(), DEFAULT_CONFIG_NAME)


@dataclass
class ClientEntry:
    name: str
    host: str = ""
    port: int = 0
    rect: Optional[Rect] = None          # None = automatic placement
    enabled: bool = True
    note: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"name": self.name}
        if self.host:
            d["host"] = self.host
        if self.port:
            d["port"] = self.port
        if self.rect is not None:
            d["rect"] = self.rect.as_dict()
        if not self.enabled:
            d["enabled"] = False
        if self.note:
            d["note"] = self.note
        for k, v in self.extra.items():
            d.setdefault(k, v)
        return d


def _rect_from(value: Any) -> Optional[Rect]:
    if value is None:
        return None
    if isinstance(value, dict):
        w = int(value.get("w", 0) or 0)
        h = int(value.get("h", 0) or 0)
        if w <= 0 or h <= 0:
            # position only, no size: keep the position, leave the size to
            # automatic detection
            return Rect(int(value.get("x", 0) or 0), int(value.get("y", 0) or 0), 0, 0)
        return Rect(int(value.get("x", 0) or 0), int(value.get("y", 0) or 0), w, h)
    if isinstance(value, (list, tuple)) and len(value) == 4:
        return Rect(*(int(v) for v in value))
    if isinstance(value, str) and value.strip().lower() == "auto":
        return None
    raise ConfigError("bad rect format: %r (expected {x,y,w,h} or [x,y,w,h])"
                      % (value,))


def _size_from(value: Any) -> Optional[Tuple[int, int]]:
    """Parse a screen size: {w,h} / [w,h] / "2560x1440" are all accepted."""
    if not value:
        return None
    if isinstance(value, dict):
        w, h = int(value.get("w", 0) or 0), int(value.get("h", 0) or 0)
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        w, h = int(value[0]), int(value[1])
    elif isinstance(value, str):
        try:
            w, h = (int(v) for v in value.lower().replace("*", "x").split("x"))
        except Exception as exc:
            raise ConfigError("screen was written as %r, expected 2560x1440 or "
                              "{w,h}" % value) from exc
    else:
        raise ConfigError("bad screen format: %r" % (value,))
    return (w, h) if w > 0 and h > 0 else None


@dataclass
class Config:
    path: str = ""
    name: str = ""
    port: int = DEFAULT_PORT
    discovery_port: int = DEFAULT_DISCOVERY_PORT
    token: str = ""
    bind: str = "0.0.0.0"
    #: for the client role: the server address (leave empty to use UDP discovery at startup)
    server_host: str = ""
    clients: List[ClientEntry] = field(default_factory=list)
    clipboard_enabled: bool = True
    clipboard_poll_ms: int = DEFAULT_CLIPBOARD_POLL_MS
    clipboard_max_bytes: int = DEFAULT_CLIPBOARD_MAX
    #: whether to sync images (screenshots)
    clipboard_images: bool = True
    #: Image limit (PNG bytes). 4MB holds one 4K screenshot; anything bigger
    #: should be transferred some other way
    clipboard_max_image_bytes: int = DEFAULT_CLIPBOARD_MAX_IMAGE
    #: which one to sync first when text and an image are both present: "text" / "image"
    clipboard_prefer: str = "text"
    hotkey_panic: str = DEFAULT_PANIC
    hotkey_lock: str = DEFAULT_LOCK
    log_level: str = "info"
    debug_events: bool = False
    #: for UI preview only: remembers the previous server resolution
    server_screen: Optional[Tuple[int, int]] = None
    #: for the client role: the local screen size (auto-detected when absent; must be accurate for Linux+uinput)
    screen: Optional[Tuple[int, int]] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------ reading / writing
    @staticmethod
    def defaults(path: str = "") -> "Config":
        return Config(path=path or default_config_path(),
                      name=_hostname())

    @staticmethod
    def load(path: Optional[str] = None, create: bool = False) -> "Config":
        real = path or default_config_path()
        cfg = Config.defaults(real)
        if os.path.exists(real):
            try:
                with open(real, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
            except json.JSONDecodeError as exc:
                raise ConfigError("config file %s is not valid JSON: %s"
                                  % (real, exc)) from exc
            except OSError as exc:
                raise ConfigError("cannot read config file %s: %s"
                                  % (real, exc)) from exc
            cfg = Config.from_dict(raw, real)
        elif not create:
            pass                      # if it does not exist, use the defaults; the caller decides whether to save
        return cfg

    @staticmethod
    def from_dict(raw: Dict[str, Any], path: str = "") -> "Config":
        if not isinstance(raw, dict):
            raise ConfigError("the config root must be an object")
        cfg = Config.defaults(path)
        known = set()
        for key in ("name", "token", "bind", "log_level", "server_host"):
            if key in raw:
                setattr(cfg, key, str(raw[key]))
                known.add(key)
        for key in ("port", "discovery_port", "clipboard_poll_ms",
                    "clipboard_max_bytes"):
            if key in raw:
                setattr(cfg, key, int(raw[key]))
                known.add(key)
        if "version" in raw:
            known.add("version")
            if int(raw["version"]) != CONFIG_VERSION:
                raise ConfigError("config version %s is not supported (current "
                                  "%d)" % (raw["version"], CONFIG_VERSION))
        if "clipboard" in raw:
            known.add("clipboard")
            cb = raw["clipboard"] or {}
            if isinstance(cb, dict):
                cfg.clipboard_enabled = bool(cb.get("enabled", cfg.clipboard_enabled))
                cfg.clipboard_poll_ms = int(cb.get("poll_ms", cfg.clipboard_poll_ms))
                cfg.clipboard_max_bytes = int(cb.get("max_bytes",
                                                     cfg.clipboard_max_bytes))
                cfg.clipboard_images = bool(cb.get("images", cfg.clipboard_images))
                cfg.clipboard_max_image_bytes = int(
                    cb.get("max_image_bytes", cfg.clipboard_max_image_bytes))
                prefer = str(cb.get("prefer", cfg.clipboard_prefer)).lower()
                if prefer not in ("text", "image"):
                    raise ConfigError("clipboard.prefer must be text or image, "
                                      "got %r" % prefer)
                cfg.clipboard_prefer = prefer
        if "hotkeys" in raw:
            known.add("hotkeys")
            hk = raw["hotkeys"] or {}
            if isinstance(hk, dict):
                if "panic" in hk:
                    cfg.hotkey_panic = str(hk["panic"] or "")
                if "lock" in hk:
                    cfg.hotkey_lock = str(hk["lock"] or "")
        if "debug_events" in raw:
            cfg.debug_events = bool(raw["debug_events"])
            known.add("debug_events")
        if "server_screen" in raw:
            known.add("server_screen")
            ss = raw["server_screen"] or {}
            if isinstance(ss, dict) and ss.get("w") and ss.get("h"):
                cfg.server_screen = (int(ss["w"]), int(ss["h"]))
        if "screen" in raw:
            known.add("screen")
            cfg.screen = _size_from(raw["screen"])
        for entry in raw.get("clients", []) or []:
            known.add("clients")
            if not isinstance(entry, dict):
                raise ConfigError("entries in clients must be objects: %r"
                                  % (entry,))
            if not entry.get("name"):
                raise ConfigError("every entry in clients must have a name")
            extra = {k: v for k, v in entry.items()
                     if k not in ("name", "host", "port", "rect", "enabled", "note")}
            cfg.clients.append(ClientEntry(
                name=str(entry["name"]),
                host=str(entry.get("host", "") or ""),
                port=int(entry.get("port", 0) or 0),
                rect=_rect_from(entry.get("rect")),
                enabled=bool(entry.get("enabled", True)),
                note=str(entry.get("note", "") or ""),
                extra=extra,
            ))
        cfg.extra = {k: v for k, v in raw.items() if k not in known}
        return cfg

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"version": CONFIG_VERSION}
        out.update(self.extra)
        out["name"] = self.name
        out["port"] = self.port
        out["discovery_port"] = self.discovery_port
        out["token"] = self.token
        if self.bind != "0.0.0.0":
            out["bind"] = self.bind
        if self.server_host:
            out["server_host"] = self.server_host
        out["clipboard"] = {
            "enabled": self.clipboard_enabled,
            "poll_ms": self.clipboard_poll_ms,
            "max_bytes": self.clipboard_max_bytes,
            "images": self.clipboard_images,
            "max_image_bytes": self.clipboard_max_image_bytes,
            "prefer": self.clipboard_prefer,
        }
        out["hotkeys"] = {"panic": self.hotkey_panic, "lock": self.hotkey_lock}
        if self.debug_events:
            out["debug_events"] = True
        if self.log_level != "info":
            out["log_level"] = self.log_level
        if self.server_screen:
            out["server_screen"] = {"w": self.server_screen[0],
                                    "h": self.server_screen[1]}
        if self.screen:
            out["screen"] = {"w": self.screen[0], "h": self.screen[1]}
        out["clients"] = [c.as_dict() for c in self.clients if c.enabled or c.rect]
        return out

    def save(self, path: Optional[str] = None) -> str:
        target = path or self.path or default_config_path()
        folder = os.path.dirname(os.path.abspath(target))
        if folder and not os.path.isdir(folder):
            os.makedirs(folder, exist_ok=True)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, target)
        self.path = target
        return target

    # ------------------------------------------------------------ layout assembly
    def client_by_name(self, name: str) -> Optional[ClientEntry]:
        for c in self.clients:
            if c.name == name:
                return c
        return None

    def enabled_clients(self) -> List[ClientEntry]:
        return [c for c in self.clients if c.enabled]

    def build_layout(self, server_rect: Rect,
                     sizes: Optional[Dict[str, Tuple[int, int]]] = None
                     ) -> Layout:
        """Lay out the virtual desktop according to the config.

        server_rect: the server virtual desktop detected at runtime (its
        top-left corner is normally 0,0).
        sizes: {client name: (w,h)} real resolutions (from the last-connection
        cache or from discovery); a client whose rect is not written in the
        config is placed automatically to the right using its size.
        """
        sizes = sizes or {}
        server = make_server(self.name or _hostname(), server_rect.w, server_rect.h)
        placed: List[Machine] = [server]
        clients: List[Machine] = []
        for entry in self.enabled_clients():
            w, h = FALLBACK_SCREEN
            if entry.name in sizes:
                w, h = sizes[entry.name]
            rect = entry.rect
            if rect is not None and rect.w > 0 and rect.h > 0:
                final = Rect(rect.x, rect.y, rect.w, rect.h)
            elif rect is not None and (rect.x or rect.y):
                final = Rect(rect.x, rect.y, w, h)
            else:
                final = self._auto_place(placed, w, h)
            m = make_client(entry.name, final.x, final.y, final.w, final.h,
                            host=entry.host, port=entry.port or self.port)
            clients.append(m)
            placed.append(m)
        return Layout(server, clients)

    @staticmethod
    def _auto_place(placed: List[Machine], w: int, h: int) -> Rect:
        """When no position is configured: place one after another to the right of the rightmost machine so far, with top edges aligned."""
        top = placed[0].rect.y
        right = max(m.rect.right for m in placed)
        return Rect(right, top, w, h)

    def rect_for(self, name: str) -> Optional[Rect]:
        e = self.client_by_name(name)
        return e.rect if e else None

    def remember_size(self, name: str, w: int, h: int) -> None:
        """Write a client's reported resolution into the config (only when it has no explicit size of its own)."""
        e = self.client_by_name(name)
        if e is None:
            return
        if e.rect is not None and e.rect.w > 0 and e.rect.h > 0:
            return
        x = e.rect.x if e.rect else 0
        y = e.rect.y if e.rect else 0
        e.rect = Rect(x, y, int(w), int(h))


def _hostname() -> str:
    import socket
    try:
        return socket.gethostname() or "crosspc"
    except Exception:
        return "crosspc"


# ------------------------------------------------------------------ size cache
class SizeCache:
    """Remembers each client's last reported resolution, giving the UI and automatic placement something to go on."""

    def __init__(self, path: str):
        self.path = path
        self.sizes: Dict[str, Tuple[int, int]] = {}
        self.load()

    @staticmethod
    def default_path(config_path: str) -> str:
        folder = os.path.dirname(os.path.abspath(config_path or
                                                 default_config_path()))
        return os.path.join(folder, CACHE_NAME)

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            for name, item in (raw.get("clients") or {}).items():
                self.sizes[name] = (int(item["w"]), int(item["h"]))
        except Exception:
            self.sizes = {}

    def get(self, name: str) -> Optional[Tuple[int, int]]:
        return self.sizes.get(name)

    def set(self, name: str, w: int, h: int) -> None:
        if self.sizes.get(name) == (w, h):
            return
        self.sizes[name] = (int(w), int(h))
        self.save()

    def save(self) -> None:
        try:
            folder = os.path.dirname(self.path)
            if folder and not os.path.isdir(folder):
                os.makedirs(folder, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump({"clients": {k: {"w": v[0], "h": v[1]}
                                       for k, v in self.sizes.items()}},
                          fh, ensure_ascii=False, indent=2)
        except OSError:
            pass
