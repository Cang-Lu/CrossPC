"""Clipboard sync (shared by both ends): text + images (image/png).

The approach is "polling + content-hash deduplication":
* read the local clipboard contents periodically; when the hash changes we take
  it that the user copied something new and send it to the peer;
* on receiving the peer's contents we write them into the local clipboard at
  once and remember the hash, so the next poll finds "the contents did not
  change" or "this is the copy we just wrote" and does not bounce the contents
  back to the peer (loop prevention).

Trade-offs:
* Windows has GetClipboardSequenceNumber, so a poll only reads one number, which
  is very cheap;
* Linux has no equivalent API (X11 requires listening for SelectionNotify,
  Wayland requires wl-paste --watch), so it degrades to "read the contents out
  and hash them every time", and every read forks an xclip/wl-paste, so the poll
  interval is relaxed automatically to 800ms or more.
* When text and an image are present at the same time, which one is taken is
  decided by backend.clipboard_read(prefer_image=...), driven by the
  clipboard.prefer config option (default text: in a scenario such as copying a
  chart out of Excel, sending a piece of text is more intuitive than sending a
  500KB image).
* Images travel in a frame of their own (T_CLIPBOARD_IMAGE, raw PNG bytes) and
  do not go through JSON, avoiding the one-third size inflation that base64
  would add for nothing.
"""
from __future__ import annotations

import hashlib
import threading
import time
from typing import Callable, Optional, Tuple

from .backend.base import Backend
from .util import Log, human_bytes, shorten

OnLocalChange = Callable[[str, object], None]      # (kind, payload)


class ClipboardSync:
    def __init__(self, backend: Backend, on_local_change: OnLocalChange,
                 log: Log, poll_ms: int = 300, max_bytes: int = 256 * 1024,
                 enabled: bool = True, name: str = "",
                 max_image_bytes: int = 4 * 1024 * 1024,
                 images: bool = True, prefer_image: bool = False):
        self.backend = backend
        self.on_local_change = on_local_change
        self.log = log
        self.poll_ms = max(80, int(poll_ms))
        self.max_bytes = int(max_bytes)
        self.max_image_bytes = int(max_image_bytes) if images else 0
        self.prefer_image = bool(prefer_image)
        self.enabled = bool(enabled) and backend.supports_clipboard
        self.name = name
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_rev: object = None
        self._last_hash: Optional[str] = None
        self._applied_hash: Optional[str] = None
        self._paused_until = 0.0
        self._too_big_warned = False
        self.sent_text = 0
        self.sent_images = 0

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if not self.enabled:
            reason = ("disabled in the config" if not self.enabled
                      else "the %s backend does not support the clipboard"
                      % self.backend.name)
            self.log.info("clipboard sync not enabled (%s)" % reason)
            return
        self._last_rev = self.backend.clipboard_revision()
        if self._last_rev is None:
            # A platform with no clipboard sequence number (Linux/X11/Wayland)
            # can only read the contents for real every time, and every read
            # forks an xclip/wl-paste: at 300ms that is 3 processes per second.
            # Relax it automatically to 800ms or more, which saves noticeable
            # CPU without hurting the feel.
            if self.poll_ms < 800:
                self.log.info("no clipboard sequence number on this platform, "
                              "relaxing the poll interval from %dms to 800ms"
                              % self.poll_ms)
                self.poll_ms = 800
        content = self._read()
        self._last_hash = self._hash(content)
        self._thread = threading.Thread(target=self._loop, name="clipboard",
                                        daemon=True)
        self._thread.start()
        self.log.info("clipboard sync started (poll %dms, text limit %s, "
                      "image limit %s%s)"
                      % (self.poll_ms, human_bytes(self.max_bytes),
                         human_bytes(self.max_image_bytes)
                         if self.max_image_bytes else "off",
                         ", images preferred" if self.prefer_image else ""))

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------ internals
    @staticmethod
    def _hash(content: Optional[Tuple[str, object]]) -> Optional[str]:
        if content is None:
            return None
        kind, payload = content
        digest = hashlib.sha1()
        digest.update(kind.encode("ascii"))
        digest.update(b"\x00")
        if kind == "image":
            digest.update(payload)                     # type: ignore[arg-type]
        else:
            digest.update(str(payload).encode("utf-8", "replace"))
        return digest.hexdigest()

    def _read(self) -> Optional[Tuple[str, object]]:
        try:
            return self.backend.clipboard_read(self.max_image_bytes,
                                               self.prefer_image)
        except Exception as exc:
            self.log.debug("failed to read the clipboard: %s" % exc)
            return None

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_ms / 1000.0):
            try:
                self.tick()
            except Exception as exc:            # pragma: no cover
                self.log.debug("clipboard poll raised: %s" % exc)

    def tick(self) -> None:
        """One poll (tests may call this directly)."""
        if time.monotonic() < self._paused_until:
            return
        rev = self.backend.clipboard_revision()
        if rev is not None and rev == self._last_rev:
            return                                  # sequence number unchanged, so the contents certainly are too
        self._last_rev = rev
        content = self._read()
        digest = self._hash(content)
        if digest is None or digest == self._last_hash:
            return
        self._last_hash = digest
        if digest == self._applied_hash:
            return                                  # this is what the peer just wrote in
        assert content is not None
        kind, payload = content
        if kind == "text":
            raw = len(str(payload).encode("utf-8", "replace"))
            if raw > self.max_bytes:
                if not self._too_big_warned:
                    self.log.warn("clipboard text is %d bytes, over the limit "
                                  "of %d; not syncing" % (raw, self.max_bytes))
                    self._too_big_warned = True
                return
            self._too_big_warned = False
            self.sent_text += 1
            self.log.info("clipboard changed: sending to peer (%d bytes) \"%s\""
                          % (raw, shorten(str(payload))))
        else:
            raw = len(payload)                      # type: ignore[arg-type]
            if raw > self.max_image_bytes:
                self.log.warn("clipboard image %s is over the limit of %s; "
                              "not syncing"
                              % (human_bytes(raw),
                                 human_bytes(self.max_image_bytes)))
                return
            self.sent_images += 1
            self.log.info("clipboard changed: sending image to peer (%s)"
                          % human_bytes(raw))
        try:
            self.on_local_change(kind, payload)
        except Exception as exc:
            self.log.warn("failed to send the clipboard: %s" % exc)

    def apply_remote(self, kind: str, payload: object) -> None:
        """Peer clipboard received: write it into the local one, and remember the hash to prevent echoing it back."""
        digest = self._hash((kind, payload))
        if digest == self._last_hash:
            return
        if kind == "text":
            raw = len(str(payload).encode("utf-8", "replace"))
            if raw > self.max_bytes:
                self.log.warn("remote clipboard text too large (%d bytes), "
                              "ignoring" % raw)
                return
            try:
                self.backend.set_clipboard_text(str(payload))
            except Exception as exc:
                self.log.warn("failed to write the local clipboard: %s" % exc)
                return
            detail = "(%d bytes) \"%s\"" % (raw, shorten(str(payload)))
        elif kind == "image":
            data = payload if isinstance(payload, (bytes, bytearray)) else b""
            if not data:
                return
            if not self.max_image_bytes:
                # image sync is turned off: do not write the peer's image into
                # the local clipboard either
                self.log.debug("image sync is off, ignoring the peer's image")
                return
            if len(data) > self.max_image_bytes:
                self.log.warn("remote image too large (%s), ignoring"
                              % human_bytes(len(data)))
                return
            try:
                self.backend.set_clipboard_image_png(bytes(data))
            except Exception as exc:
                self.log.warn("failed to write the local image clipboard: %s"
                              % exc)
                return
            detail = "(image of %s)" % human_bytes(len(data))
        else:
            self.log.warn("unknown clipboard kind received: %r" % (kind,))
            return
        self._applied_hash = digest
        self._last_hash = digest
        self._last_rev = self.backend.clipboard_revision()
        # On some systems the clipboard keeps churning for a short while after a
        # write, so pause briefly to avoid bouncing back and forth
        self._paused_until = time.monotonic() + 0.3
        self.log.info("clipboard updated with the peer's contents %s" % detail)
