"""Linux backend: combine X11 (XTest) or /dev/uinput into a usable client backend.

Role scope (v1):
    The Linux side **is a client only** -- inject remote keyboard/mouse input and
    read/write the text clipboard.
    Why it is not a server: to "capture and suppress" local input on Linux you
    have to grab every input device exclusively with evdev (EVIOCGRAB) and then
    negotiate with the window system over "who handles this keypress"; on X11 you
    also have to deal with XRecord/XTEST preemption, and on Wayland you can only
    rely on a compositor-specific protocol (gnome-shell / wlroots each differ),
    and once you grab the wrong thing or crash, the user's keyboard and mouse
    really do stop working. That is a high-risk feature, not something v1 should
    take on. So supports_capture/supports_suppress are both False, and
    start_capture()/set_forwarding() simply inherit the base class and raise
    BackendError.

Injection strategy (pick one, decided in prepare()):
    1. X11 first: DISPLAY is available and XTest is ready -> linux_x11.X11Injector.
       Why prefer it: events are synthesized through the X server, so no
       root/udev rules are needed, and XTestFakeMotionEvent uses X absolute
       screen coordinates, which pointer acceleration cannot affect.
    2. Otherwise uinput: WAYLAND_DISPLAY is set, or /dev/uinput is writable ->
       linux_uinput.UInputInjector (absolute-positioning virtual pointer device;
       see that module's docstring for details).

Clipboard:
    Text and images (image/png), both by calling external tools in a subprocess
    (wl-clipboard / xclip; plain text can also use xsel). Why not implement X's
    selection protocol directly with ctypes: that would mean holding an X
    connection open for a long time, answering SelectionRequest inside an event
    loop, and also handling INCR chunking for large text and the owner exiting --
    in effect raising another X client inside the process. A subprocess speaks
    the same protocol but is a mature implementation maintained by the
    distribution, so all we have to handle is "tool missing / timeout / non-zero
    exit". The same reasoning applies under Wayland (wl-copy forks a background
    process that holds the selection).
    Note: the Linux side **never decodes or encodes PNG** -- an image is a byte
    stream passed through as it is; transcoding (DIB<->PNG) only happens on the
    Windows side.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Callable, Dict, List, Optional, Tuple

from ..events import MOTION, BUTTON, WHEEL, KEY, Event
from ..layout import Rect
from .base import Backend, BackendError

#: Clipboard read timeout. It must be set: with "no selection owner" some tools
#: wait forever and freeze the client's injection/heartbeat thread completely.
#: 2 seconds is the compromise between "slow but tolerable" and "frozen".
CLIPBOARD_TIMEOUT = 2.0

#: tool name -> (read command, write command)
CLIPBOARD_TOOLS: Dict[str, Tuple[List[str], List[str]]] = {
    "wl-copy": (["wl-paste", "--no-newline"], ["wl-copy"]),
    "xclip": (["xclip", "-selection", "clipboard", "-o"],
              ["xclip", "-selection", "clipboard", "-i"]),
    "xsel": (["xsel", "-b", "-o"], ["xsel", "-b", "-i"]),
}

#: Image clipboard: tool name -> (command to read image/png, command to write image/png).
#: xsel is not in the table -- it can only handle text targets and cannot do MIME types.
IMAGE_CLIPBOARD_TOOLS: Dict[str, Tuple[List[str], List[str]]] = {
    "wl-copy": (["wl-paste", "--type", "image/png"],
                ["wl-copy", "--type", "image/png"]),
    "xclip": (["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
              ["xclip", "-selection", "clipboard", "-t", "image/png", "-i"]),
}

#: magic number of image/png (the first 8 bytes)
PNG_SIG = b"\x89PNG\r\n\x1a\n"


def parse_screen_spec(spec: str, default: Tuple[int, int] = (1920, 1080)
                      ) -> Tuple[Tuple[int, int], str]:
    """Parse CROSSPC_SCREEN (of the form "2560x1440").

    Returns ((width, height), description of its origin) and never raises -- the
    diagnostic output needs it to be robust enough. uinput is an
    absolute-positioning device, so the resolution has to be known in order to map
    pixel coordinates into 0..65535, and uinput itself cannot be asked "how big is
    the screen", so it has to be supplied here.
    """
    spec = (spec or "").strip().lower().replace(" ", "")
    if not spec:
        return default, "environment variable CROSSPC_SCREEN is not set, using the default %dx%d for now" % default
    for sep in ("x", "*", ",", "×"):
        if sep in spec:
            left, _, right = spec.partition(sep)
            try:
                w, h = int(left), int(right)
            except ValueError:
                break
            if w > 0 and h > 0:
                return (w, h), "from the CROSSPC_SCREEN environment variable =%s" % spec
            break
    return default, ("CROSSPC_SCREEN=%r cannot be parsed (it must look like 2560x1440), "
                     "using the default %dx%d for now" % (spec, default[0], default[1]))


def choose_clipboard_tool(env: Dict[str, str],
                          which: Callable[[str], Optional[str]]
                          ) -> Optional[str]:
    """Pick a clipboard tool from the environment; returns the tool name (a CLIPBOARD_TOOLS key) or None.

    It is factored out as a pure function so that the selection logic can be
    unit-tested on Windows (see tests).

    Order:
      * When WAYLAND_DISPLAY is present, try wl-copy **first**: xclip does run in a
        Wayland session (through XWayland), but it reads and writes the XWayland
        clipboard, which is not the same one native Wayland applications see, and
        users end up with "I copied it but it will not paste".
      * Then xclip (more common than xsel and supports -selection clipboard).
      * Finally xsel, and only when there is an X11 hint (DISPLAY or
        XDG_SESSION_TYPE=x11), so that it is not misused on pure Wayland.
    """
    def have(name: str) -> bool:
        try:
            return bool(which(name))
        except Exception:
            return False

    if env.get("WAYLAND_DISPLAY") and have("wl-copy"):
        return "wl-copy"
    if have("xclip"):
        return "xclip"
    x11_hint = bool(env.get("DISPLAY")) or env.get("XDG_SESSION_TYPE") == "x11"
    if x11_hint and have("xsel"):
        return "xsel"
    return None


class _Clipboard:
    """Text clipboard (subprocess implementation). The tool is probed only on first use, so PATH can change at runtime."""

    def __init__(self, log: Optional[Callable[[str], None]] = None,
                 env: Optional[Dict[str, str]] = None):
        self._log = log or (lambda m: None)
        self._env = env
        self._tool: Optional[str] = None
        self._probed = False

    def _say(self, msg: str) -> None:
        self._log("[clipboard] %s" % msg)

    @property
    def tool(self) -> Optional[str]:
        if not self._probed:
            self._probed = True
            env = self._env if self._env is not None else os.environ
            self._tool = choose_clipboard_tool(dict(env), shutil.which)
            if self._tool:
                self._say("using clipboard tool %s" % self._tool)
            else:
                self._say("no usable clipboard tool found (install one of wl-clipboard / xclip / xsel)")
        return self._tool

    def refresh(self) -> None:
        """Probe again (for instance right after the user installed a tool)."""
        self._probed = False
        self._tool = None
        _ = self.tool

    def available(self) -> bool:
        return self.tool is not None

    def describe(self) -> str:
        tool = self.tool
        if tool:
            return "using %s" % tool
        return ("no usable clipboard tool: please install wl-clipboard (Wayland) or "
                "xclip / xsel (X11); on Debian: sudo apt install xclip wl-clipboard")

    # ------------------------------------------------------------ read/write
    def read(self) -> Optional[str]:
        tool = self.tool
        if tool is None:
            return None
        cmd = CLIPBOARD_TOOLS[tool][0]
        try:
            # Universal newlines is off: the clipboard content must be preserved
            # exactly (otherwise \r\n gets rewritten)
            proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE,
                                  timeout=CLIPBOARD_TIMEOUT,
                                  env=self._child_env())
        except FileNotFoundError:
            self._say("%s not found (it may have just been uninstalled), skipping this clipboard read" % cmd[0])
            return None
        except subprocess.TimeoutExpired:
            # The most common cause: no client holds the selection and the tool
            # just sits there waiting
            self._say("clipboard read timed out (%s exceeded %.1fs): the clipboard "
                      "may have had no content during that time"
                      % (cmd[0], CLIPBOARD_TIMEOUT))
            return None
        except OSError as exc:
            self._say("clipboard read failed (%s): %s" % (cmd[0], exc))
            return None
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()
            self._say("clipboard read returned %d: %s" % (proc.returncode, err or "(no error output)"))
            return None
        raw = proc.stdout or b""
        if not raw:
            # An empty clipboard is a normal state, not an error -- return an empty
            # string so the upper layer keeps doing its hash polling as usual
            return ""
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            # The clipboard may hold non-UTF-8 bytes (latin-1 text written by some
            # old programs, for instance); better to hand back a string with
            # replacement characters than to drop the whole thing.
            self._say("clipboard content is not valid UTF-8, decoded with replacement characters")
            return raw.decode("utf-8", "replace")

    def write(self, text: str) -> None:
        tool = self.tool
        if tool is None:
            raise BackendError(
                "no usable clipboard tool, cannot write the clipboard. Please install "
                "wl-clipboard (Wayland) or xclip / xsel (X11): on Debian, "
                "sudo apt install xclip wl-clipboard")
        cmd = CLIPBOARD_TOOLS[tool][1]
        data = (text or "").encode("utf-8")
        try:
            proc = subprocess.run(cmd, input=data, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE,
                                  timeout=CLIPBOARD_TIMEOUT,
                                  env=self._child_env())
        except FileNotFoundError as exc:
            raise BackendError("%s not found: please reinstall the clipboard tool (apt install %s)"
                               % (cmd[0], "wl-clipboard" if tool == "wl-copy" else tool)) from exc
        except subprocess.TimeoutExpired as exc:
            raise BackendError("clipboard write timed out (%s exceeded %.1fs): the tool "
                               "may be waiting for a display server that is not there, "
                               "check DISPLAY/WAYLAND_DISPLAY"
                               % (cmd[0], CLIPBOARD_TIMEOUT)) from exc
        except OSError as exc:
            raise BackendError("clipboard write failed (%s): %s" % (cmd[0], exc)) from exc
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()
            raise BackendError("clipboard write failed (%s returned %d): %s"
                               % (cmd[0], proc.returncode, err or "(no error output)"))

    def _child_env(self) -> Optional[Dict[str, str]]:
        """Environment for child processes. When a custom env is given (tests) use that one, otherwise inherit."""
        if self._env is None:
            return None
        return dict(self._env)

    # ------------------------------------------------------------ images
    def supports_images(self) -> bool:
        return self.tool in IMAGE_CLIPBOARD_TOOLS

    def read_image(self, max_bytes: int = 0) -> Optional[bytes]:
        """Read image/png. Returns None when the clipboard holds no image (the tool exits non-zero, which is normal)."""
        tool = self.tool
        if tool is None or tool not in IMAGE_CLIPBOARD_TOOLS:
            return None
        cmd = IMAGE_CLIPBOARD_TOOLS[tool][0]
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE,
                                  timeout=CLIPBOARD_TIMEOUT,
                                  env=self._child_env())
        except FileNotFoundError:
            self._say("%s not found, skipping the image" % cmd[0])
            return None
        except subprocess.TimeoutExpired:
            self._say("image clipboard read timed out (%s > %.1fs)"
                      % (cmd[0], CLIPBOARD_TIMEOUT))
            return None
        except OSError as exc:
            self._say("image clipboard read failed (%s): %s" % (cmd[0], exc))
            return None
        raw = proc.stdout or b""
        if proc.returncode != 0 or not raw:
            return None                     # there is no image on the clipboard, which is very common
        if not raw.startswith(PNG_SIG):
            self._say("the image on the clipboard is not PNG (first 8 bytes %r), not syncing" % raw[:8])
            return None
        if max_bytes and len(raw) > max_bytes:
            self._say("image is %d bytes, over the cap of %d, not syncing" % (len(raw), max_bytes))
            return None
        return raw

    def write_image(self, png: bytes) -> None:
        tool = self.tool
        if tool is None:
            raise BackendError(
                "no usable clipboard tool, cannot write an image. Please install "
                "wl-clipboard (Wayland) or xclip (X11): on Debian, "
                "sudo apt install xclip wl-clipboard")
        if tool not in IMAGE_CLIPBOARD_TOOLS:
            raise BackendError(
                "%s does not support the image clipboard (it can only handle text). "
                "To receive images, install wl-clipboard or xclip." % tool)
        cmd = IMAGE_CLIPBOARD_TOOLS[tool][1]
        try:
            proc = subprocess.run(cmd, input=png, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE,
                                  timeout=CLIPBOARD_TIMEOUT,
                                  env=self._child_env())
        except FileNotFoundError as exc:
            raise BackendError("%s not found: please reinstall the clipboard tool" % cmd[0]) from exc
        except subprocess.TimeoutExpired as exc:
            raise BackendError("image clipboard write timed out (%s): check DISPLAY/WAYLAND_DISPLAY"
                               % cmd[0]) from exc
        except OSError as exc:
            raise BackendError("image clipboard write failed (%s): %s" % (cmd[0], exc)) from exc
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()
            raise BackendError("image clipboard write failed (%s returned %d): %s"
                               % (cmd[0], proc.returncode, err or "(no error output)"))


class LinuxBackend(Backend):
    name = "linux"
    #: capture/suppress: not done in v1 (see the module docstring), so Linux can only be a client
    supports_capture = False
    supports_suppress = False
    supports_inject = True
    supports_clipboard = True

    def __init__(self, log=None, prefer: Optional[str] = None):
        super().__init__(log=log)
        #: None / "x11" / "uinput". doctor forces one through prefer to self-check item by item.
        self.prefer = prefer or None
        self._impl = None                 # X11Injector or UInputInjector
        self._impl_kind = ""              # "x11" / "uinput" / ""
        self._screen: Optional[Tuple[int, int]] = None
        self._screen_source = "not probed yet"
        self._clip = _Clipboard(log=self._log)

    # ------------------------------------------------------------ lifecycle
    def prepare(self) -> None:
        """Probe and open an injection channel. Idempotent: if it is already prepared, return right away."""
        if self._impl is not None:
            return
        self._impl_kind = self._decide_injection()
        try:
            if self._impl_kind == "x11":
                self._impl = self._open_x11()
                # Screen size: if X11 can be asked, trust X11
                self._screen, self._screen_source = self._impl.screen_size(), \
                    "X11/XTest (current X screen)"
            else:
                # uinput is an absolute-positioning device, so the resolution must
                # be known before the device can be opened
                size, src = self._screen_from_env()
                self._screen, self._screen_source = size, src
                self._impl = self._open_uinput(size)
        except BackendError:
            self._impl = None
            self._impl_kind = ""
            raise
        except Exception as exc:
            # On failure the half-built state must be cleared, otherwise _impl
            # stays non-None and prepare() would "idempotently" skip everything
            self._impl = None
            self._impl_kind = ""
            raise BackendError(str(exc)) from exc

        self.log("injection method=%s screen=%s (%s)"
                 % (self._impl_kind, self._screen, self._screen_source))
        if self._impl_kind == "uinput":
            self.log("uinput is an absolute-positioning device, so the screen size must "
                     "be exact; if it is wrong, set CROSSPC_SCREEN=WxH (currently "
                     "computed as %dx%d) and restart the client"
                     % (self._screen[0], self._screen[1]))
        # The clipboard tool is only probed here; a failure does not stop the
        # client from working (injection is the main line)
        self._clip.tool

    def _decide_injection(self) -> str:
        prefer = (self.prefer or "").lower()
        if prefer in ("x11", "x", "xtest"):
            ok, why = self._x11_available()
            if not ok:
                raise BackendError("X11 injection was requested but is unavailable: %s" % why)
            return "x11"
        if prefer in ("uinput", "evdev"):
            ok, why = self._uinput_available()
            if not ok:
                raise BackendError("uinput injection was requested but is unavailable: %s" % why)
            return "uinput"
        if prefer:
            raise BackendError("unknown injection method prefer=%r (choose x11 / uinput)" % (self.prefer,))

        # auto: X11 first (no extra privileges needed, absolute coordinates are precise)
        ok, why = self._x11_available()
        if ok:
            self.log("injection strategy: X11/XTest (%s)" % why)
            return "x11"
        self.log("X11 injection is unavailable: %s" % why)
        ok, why = self._uinput_available()
        if ok:
            self.log("injection strategy: uinput (%s)" % why)
            return "uinput"
        raise BackendError(
            "no usable injection method found on Linux. Either provide an X session "
            "(DISPLAY, XTest), or make /dev/uinput writable (the Wayland case). "
            "X11: %s; uinput: %s. "
            "You can run tools/install_linux.sh to install the dependencies and "
            "configure the udev rules."
            % (self._x11_why(), why))

    def _x11_available(self) -> Tuple[bool, str]:
        if not sys.platform.startswith("linux"):
            return (False, "the current platform is not Linux (%s), X11 injection is unavailable" % sys.platform)
        try:
            from .linux_x11 import X11Injector
        except Exception as exc:                # pragma: no cover
            return (False, "failed to load the X11 injection module: %s" % exc)
        try:
            return X11Injector.available()
        except Exception as exc:                # pragma: no cover - available() should not raise
            return (False, "X11 self-check raised: %s" % exc)

    def _uinput_available(self) -> Tuple[bool, str]:
        if not sys.platform.startswith("linux"):
            return (False, "the current platform is not Linux (%s), /dev/uinput is unavailable" % sys.platform)
        try:
            from .linux_uinput import UInputInjector
        except Exception as exc:                # pragma: no cover
            return (False, "failed to load the uinput injection module: %s" % exc)
        try:
            return UInputInjector.available()
        except Exception as exc:                # pragma: no cover - available() should not raise
            return (False, "uinput self-check raised: %s" % exc)

    def _x11_why(self) -> str:
        """Return the explanation text only, without repeating the availability check (diagnostics must be able to explain the reason)."""
        ok, why = self._x11_available()
        return why

    def _open_x11(self):
        from .linux_x11 import X11Injector
        inj = X11Injector(log=self._log)
        inj.open()
        return inj

    def _open_uinput(self, size: Tuple[int, int]):
        from .linux_uinput import UInputInjector
        inj = UInputInjector(log=self._log)
        inj.open()
        # The device may receive events as soon as it is created, so tell it the
        # size right after open()
        inj.set_screen_size(*size)
        return inj

    def _screen_from_env(self) -> Tuple[Tuple[int, int], str]:
        return parse_screen_spec(os.environ.get("CROSSPC_SCREEN", ""))

    def close(self) -> None:
        """Release the injection devices. Order: wind down through the base class first (release keys / stop capture), then destroy the devices.

        The base class's close() calls set_forwarding(False), and the Linux backend
        does not support suppression, so that raises BackendError. That is not an
        error (the Linux side has no "forwarding" state to begin with), so it is
        swallowed here; but the device teardown must **not** be skipped because of
        it.
        """
        try:
            super().close()
        except BackendError as exc:
            self.log("the base class reported an unsupported capability while winding down (ignorable): %s" % exc)
        finally:
            impl, self._impl = self._impl, None
            self._impl_kind = ""
            if impl is not None:
                try:
                    impl.close()
                except Exception as exc:        # pragma: no cover - failing to close a device is not fatal
                    self.log("error while closing the injection device (ignored): %s" % exc)

    # ------------------------------------------------------------ desktop geometry
    def desktop_rect(self) -> Rect:
        """The local desktop (a single-screen rectangle).

        Simplification trade-off: no Xinerama/RandR stitching; take "the whole X
        screen" (X11) or the size given by CROSSPC_SCREEN (uinput) directly.
        Stitching multiple monitors would need ctypes bindings for Xinerama or
        RandR, and the client is the side that gets positioned by the server
        anyway, so having the user enter the correct size in the layout is simpler
        and more reliable.
        """
        w, h = self._screen_size()
        return Rect(0, 0, w, h)

    def _screen_size(self) -> Tuple[int, int]:
        if self._screen:
            return self._screen
        if self._impl_kind == "x11" and self._impl is not None:
            try:
                self._screen = self._impl.screen_size()
                self._screen_source = "X11/XTest (current X screen)"
                return self._screen
            except Exception as exc:
                self.log("reading the X11 screen size failed: %s" % exc)
        if self._impl_kind == "uinput":
            size, src = self._screen_from_env()
            self._screen, self._screen_source = size, src
            return size
        # prepare() was never called: give a reasonable answer from the environment
        # variables/defaults instead of raising
        size, src = self._screen_from_env()
        self._screen, self._screen_source = size, src
        return size

    def cursor(self) -> Tuple[int, int]:
        if self._impl_kind == "x11" and self._impl is not None:
            return self._impl.pointer()
        raise BackendError("uinput injection is write-only: the kernel does not read "
                           "a cursor position back from a synthetic device, so under "
                           "Linux/uinput the local cursor position cannot be read")

    def set_cursor(self, x: int, y: int) -> None:
        # On the client side, "putting" the cursor somewhere is just an injected motion
        self.inject_motion(int(x), int(y))

    def monitors(self) -> List[Rect]:
        return [self.desktop_rect()]

    # ------------------------------------------------------------ injection
    def _need_impl(self):
        if self._impl is None:
            raise BackendError("the Linux backend has not been prepare()d yet (there is no usable injection channel)")
        return self._impl

    def inject_motion(self, x: int, y: int) -> None:
        impl = self._need_impl()
        if self._impl_kind == "uinput":
            # Pass the screen size every time: the uinput module itself does not
            # know how big the screen is (CROSSPC_SCREEN)
            w, h = self._screen_size()
            impl.inject_motion(int(x), int(y), w, h)
        else:
            impl.inject_motion(int(x), int(y))

    def inject_button(self, button: int, pressed: bool) -> None:
        self._need_impl().inject_button(int(button), bool(pressed))

    def inject_wheel(self, dx: int, dy: int) -> None:
        self._need_impl().inject_wheel(int(dx), int(dy))

    def inject_key(self, scancode: int, vk: int, pressed: bool,
                   extended: bool = False) -> None:
        impl = self._need_impl()
        if self._impl_kind == "uinput":
            impl.inject_key(int(scancode), int(vk), bool(pressed), bool(extended))
        else:
            impl.inject_key(int(scancode), bool(pressed), bool(extended))

    # ------------------------------------------------------------ clipboard
    def clipboard_text(self) -> Optional[str]:
        return self._clip.read()

    def set_clipboard_text(self, text: str) -> None:
        self._clip.write(text)

    @property
    def supports_clipboard_images(self) -> bool:
        """xsel only handles text targets, so it does not count as supporting images."""
        return self._clip.supports_images()

    def clipboard_image_png(self, max_bytes: int = 0) -> Optional[bytes]:
        return self._clip.read_image(max_bytes)

    def set_clipboard_image_png(self, png: bytes) -> None:
        self._clip.write_image(png)

    def clipboard_revision(self) -> Optional[object]:
        """Linux returns None -- deliberately.

        We have no resident X selection owner, so events such as "the clipboard
        changed hands" are unavailable to us; xclip/xsel/wl-paste are one-shot
        processes that can only spit out the current content on each call.
        Getting a "change sequence number" would mean owning the selection
        ourselves and answering inside an X event loop, which is exactly what we do
        not want to do (see the module docstring).
        So this degrades into the upper layer hashing the text in a poll loop --
        read the content once, compute a hash, use it as the version number. The
        price is that "one read forks one process", so the upper layer should widen
        the polling interval to 0.5-1 s, and accept the theoretical collision where
        two contents hashing the same are treated as unchanged.
        """
        return None

    # ------------------------------------------------------------ self-check
    def probe(self) -> List[Tuple[str, bool, str]]:
        """Diagnostic items, all user-facing; never raises."""
        items: List[Tuple[str, bool, str]] = []

        # 1) Role description (the v1 boundary; put it first so users do not assume
        # Linux can be a server)
        items.append((
            "role",
            True,
            "the Linux side can only be a client in v1 (injecting keyboard/mouse); "
            "capturing/suppressing local input needs an evdev grab working with the "
            "compositor and is not implemented yet"))

        # 2) Injection strategy
        kind = self._impl_kind
        if not kind:
            x_ok, x_why = self._x11_available()
            u_ok, u_why = self._uinput_available()
            if x_ok:
                kind, why = "x11", x_why
            elif u_ok:
                kind, why = "uinput", u_why
            else:
                items.append(("injection method", False,
                              "no usable injection method. X11: %s; uinput: %s" % (x_why, u_why)))
                items.append(("uinput device", False, u_why))
                self._append_display_and_clipboard(items)
                self._append_screen(items)
                return items
            items.append(("injection method", True, "will use %s: %s" % (kind, why)))
        else:
            items.append(("injection method", True, "ready: %s" % kind))

        # 3) uinput device
        items.append(("uinput device",) + self._uinput_available())

        # 4) DISPLAY/WAYLAND_DISPLAY
        self._append_display_and_clipboard(items)

        # 5) Where the screen size came from
        self._append_screen(items)
        return items

    def _append_display_and_clipboard(self, items) -> None:
        display = os.environ.get("DISPLAY", "")
        wayland = os.environ.get("WAYLAND_DISPLAY", "")
        if display:
            items.append(("DISPLAY", True, "DISPLAY=%s" % display))
        else:
            items.append(("DISPLAY", False,
                          "DISPLAY is not set (pure Wayland, or not running inside a graphical session)"))
        if wayland:
            items.append(("WAYLAND_DISPLAY", True, "WAYLAND_DISPLAY=%s" % wayland))
        else:
            items.append(("WAYLAND_DISPLAY", True, "not set (not a Wayland session)"))

        if self._clip.available():
            items.append(("clipboard tool", True, self._clip.describe()))
        else:
            items.append(("clipboard tool", False, self._clip.describe()))
        if self._clip.supports_images():
            items.append(("image clipboard", True,
                          "%s supports image/png" % self._clip.tool))
        else:
            items.append(("image clipboard", False,
                          "the current tool does not support images (only available with wl-clipboard or xclip)"))

        # Really read once (windows.py does the same): "the tool exists" does not
        # mean "it can read right now" -- for instance when no client holds the
        # selection, or DISPLAY points at an X server that cannot be reached. The
        # cost is waiting up to CLIPBOARD_TIMEOUT seconds in the worst case, which
        # doctor can live with.
        if self._clip.available():
            try:
                txt = self.clipboard_text()
            except Exception as exc:            # pragma: no cover - read() has its own fallback
                txt, detail = None, "read raised: %s" % exc
            else:
                if txt is None:
                    detail = "unreadable (check DISPLAY/WAYLAND_DISPLAY, or the clipboard has no content)"
                elif not txt:
                    detail = "empty right now"
                else:
                    detail = "read %d characters" % len(txt)
            items.append(("clipboard read", txt is not None, detail))

    def _append_screen(self, items) -> None:
        w, h = self._screen_size()
        note = self._screen_source
        if self._impl_kind == "uinput":
            note += "; uinput is an absolute-positioning device, a wrong size shifts " \
                    "where the pointer lands, so set the correct value with CROSSPC_SCREEN=WxH"
        items.append(("screen size", True, "%dx%d (%s)" % (w, h, note)))
