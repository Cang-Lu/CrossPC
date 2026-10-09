"""剪辑板同步(两端共用): 文本 + 图片(image/png)。

做法是"轮询 + 内容哈希去重":
* 定时读本机剪辑板内容, 哈希变了就认为用户复制了新东西, 发给对端;
* 收到对端的内容后立即写入本机剪辑板, 并把哈希记下来, 所以下一次轮询发现
  "内容没变"或"是刚写进去的那份", 不会把内容又弹回对端(防回环)。

取舍:
* Windows 有 GetClipboardSequenceNumber, 轮询只读一个序号, 很便宜;
* Linux 没有等价 API(X11 要监听 SelectionNotify, Wayland 要靠 wl-paste
  --watch), 所以退化成"每次都把内容读出来算哈希", 且每读一次要 fork 一个
  xclip/wl-paste, 因此轮询间隔会自动放宽到 800ms 以上。
* 文本与图片同时存在时取哪个由 backend.clipboard_read(prefer_image=...) 决定,
  配置项 clipboard.prefer(默认 text: 从 Excel 复制图表这类场景, 发一段文本
  比发 500KB 图片更符合直觉)。
* 图片走独立帧(T_CLIPBOARD_IMAGE, 原始 PNG 字节), 不进 JSON, 避免 base64
  把体积白涨三分之一。
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

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if not self.enabled:
            reason = ("配置里关闭了" if not self.enabled
                      else "%s 后端不支持剪辑板" % self.backend.name)
            self.log.info("剪辑板同步未启用(%s)" % reason)
            return
        self._last_rev = self.backend.clipboard_revision()
        if self._last_rev is None:
            # 没有"剪辑板序号"可用的平台(Linux/X11/Wayland)只能每次真读内容,
            # 而读一次要 fork 一个 xclip/wl-paste, 300ms 就是每秒 3 个进程。
            # 自动放宽到 800ms 以上, 明显省 CPU 又不影响手感。
            if self.poll_ms < 800:
                self.log.info("该平台没有剪辑板序号, 轮询间隔从 %dms 放宽到 800ms"
                              % self.poll_ms)
                self.poll_ms = 800
        content = self._read()
        self._last_hash = self._hash(content)
        self._thread = threading.Thread(target=self._loop, name="clipboard",
                                        daemon=True)
        self._thread.start()
        self.log.info("剪辑板同步已启动(轮询 %dms, 文本上限 %s, 图片上限 %s%s)"
                      % (self.poll_ms, human_bytes(self.max_bytes),
                         human_bytes(self.max_image_bytes)
                         if self.max_image_bytes else "关闭",
                         ", 优先图片" if self.prefer_image else ""))

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------ 内部
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
            self.log.debug("读剪辑板失败: %s" % exc)
            return None

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_ms / 1000.0):
            try:
                self.tick()
            except Exception as exc:            # pragma: no cover
                self.log.debug("剪辑板轮询异常: %s" % exc)

    def tick(self) -> None:
        """一次轮询(测试可直接调用)。"""
        if time.monotonic() < self._paused_until:
            return
        rev = self.backend.clipboard_revision()
        if rev is not None and rev == self._last_rev:
            return                                  # 序号没变, 内容一定没变
        self._last_rev = rev
        content = self._read()
        digest = self._hash(content)
        if digest is None or digest == self._last_hash:
            return
        self._last_hash = digest
        if digest == self._applied_hash:
            return                                  # 就是刚被对端写进来的
        assert content is not None
        kind, payload = content
        if kind == "text":
            raw = len(str(payload).encode("utf-8", "replace"))
            if raw > self.max_bytes:
                if not self._too_big_warned:
                    self.log.warn("剪辑板文本 %d 字节超过上限 %d, 不同步"
                                  % (raw, self.max_bytes))
                    self._too_big_warned = True
                return
            self._too_big_warned = False
            self.sent_text += 1
            self.log.info("剪辑板变化: 发给对端 (%d 字节) 「%s」"
                          % (raw, shorten(str(payload))))
        else:
            raw = len(payload)                      # type: ignore[arg-type]
            if raw > self.max_image_bytes:
                self.log.warn("剪辑板图片 %s 超过上限 %s, 不同步"
                              % (human_bytes(raw),
                                 human_bytes(self.max_image_bytes)))
                return
            self.sent_images += 1
            self.log.info("剪辑板变化: 图片发给对端 (%s)" % human_bytes(raw))
        try:
            self.on_local_change(kind, payload)
        except Exception as exc:
            self.log.warn("发送剪辑板失败: %s" % exc)

    def apply_remote(self, kind: str, payload: object) -> None:
        """收到对端剪辑板: 写进本机, 并记住哈希防止回传。"""
        digest = self._hash((kind, payload))
        if digest == self._last_hash:
            return
        if kind == "text":
            raw = len(str(payload).encode("utf-8", "replace"))
            if raw > self.max_bytes:
                self.log.warn("远端剪辑板文本过大(%d 字节), 忽略" % raw)
                return
            try:
                self.backend.set_clipboard_text(str(payload))
            except Exception as exc:
                self.log.warn("写本机剪辑板失败: %s" % exc)
                return
            detail = "(%d 字节) 「%s」" % (raw, shorten(str(payload)))
        elif kind == "image":
            data = payload if isinstance(payload, (bytes, bytearray)) else b""
            if not data:
                return
            if not self.max_image_bytes:
                # 图片同步被关掉了: 对端发来的也不要写进本机剪辑板
                self.log.debug("图片同步已关闭, 忽略对端图片")
                return
            if len(data) > self.max_image_bytes:
                self.log.warn("远端图片过大(%s), 忽略" % human_bytes(len(data)))
                return
            try:
                self.backend.set_clipboard_image_png(bytes(data))
            except Exception as exc:
                self.log.warn("写本机图片剪辑板失败: %s" % exc)
                return
            detail = "(%s 的图片)" % human_bytes(len(data))
        else:
            self.log.warn("收到未知剪辑板类型: %r" % (kind,))
            return
        self._applied_hash = digest
        self._last_hash = digest
        self._last_rev = self.backend.clipboard_revision()
        # 某些系统写完剪辑板后还会有一小段时间的抖动, 暂停一下免得来回弹
        self._paused_until = time.monotonic() + 0.3
        self.log.info("剪辑板已更新为对端内容 %s" % detail)
