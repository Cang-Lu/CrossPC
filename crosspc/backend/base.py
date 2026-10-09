"""平台后端接口(冻结契约)。

后端 = 某个操作系统上「捕获输入 / 抑制本机输入 / 注入输入 / 读写剪辑板」
的能力集合。上层(Router / ServerApp / ClientApp)只依赖这里的接口, 不碰
任何平台细节, 于是:

  * 没有第二台机器也能用 FakeBackend 跑完整的回环自测;
  * 新增平台 = 新增一个子类 + 在 backend/__init__.py 里注册。

三种模式(由上层决定, 后端只执行):
  本地模式   捕获但放行: 输入照常送到本机, 后端只顺便把它交给 sink 观察。
             用于判断"鼠标有没有顶到屏幕边缘要穿出去"。
  转发模式   set_forwarding(True): 输入被吞掉(不再送到本机), 全部交给 sink,
             同时把本机光标"停靠(park)"在一个固定点, 免得它到处乱跑。
  注入模式   client 用: inject() 把远端事件写进本机。

线程约定: start_capture() 注册的 sink 会在后端的输入线程上被调用, 必须
立即返回(不能阻塞、不能做网络 IO)。后端的 stop/close 必须可以从其它线程
调用, 并且必须保证"即使崩溃也不再吞键"(安全底线)。
"""
from __future__ import annotations

from typing import Callable, Iterable, List, Optional, Tuple

from ..events import (BUTTON, KEY, MOTION, WHEEL, BTN_LEFT, BTN_MIDDLE,
                      BTN_RIGHT, Event)
from ..layout import Rect

#: 日志回调
LogFn = Callable[[str], None]
#: 输入回调: 后端捕获到的每一个事件
SinkFn = Callable[[Event], None]


class BackendError(RuntimeError):
    """后端能力缺失或初始化失败(信息面向最终用户)。"""


class Backend:
    # ------------------------------------------------------------ 能力声明
    name = "base"
    display_server = ""            # windows / x11 / wayland
    supports_capture = False       # 能捕获本机输入
    supports_suppress = False      # 能在转发模式下吞掉本机输入
    supports_inject = False        # 能注入输入
    supports_clipboard = False     # 能读写系统剪辑板

    # ------------------------------------------------------------ 生命周期
    def __init__(self, log: Optional[LogFn] = None):
        self._log = log or (lambda m: None)
        self._sink: Optional[SinkFn] = None
        self._forwarding = False
        self._park: Optional[Tuple[int, int]] = None
        # 已按下的键/键, 用于连接断开时"释放所有键", 避免粘键
        self._pressed_keys: List[Event] = []
        self._pressed_buttons: List[int] = []

    # ------------------------------------------------------------ 工具
    def log(self, msg: str) -> None:
        self._log("[%s] %s" % (self.name, msg))

    @property
    def can_serve(self) -> bool:
        """能否作为 server(接了键鼠的那台)。"""
        return self.supports_capture and self.supports_suppress

    @property
    def can_be_client(self) -> bool:
        return self.supports_inject

    def caps(self) -> str:
        bits = []
        if self.supports_capture:
            bits.append("capture")
        if self.supports_suppress:
            bits.append("suppress")
        if self.supports_inject:
            bits.append("inject")
        if self.supports_clipboard:
            bits.append("clipboard")
        return "%s(%s)" % (self.name, ",".join(bits) or "none")

    def prepare(self) -> None:
        """打开设备/设置 DPI 感知等。必须幂等。"""

    def close(self) -> None:
        """释放一切资源; 必须先 set_forwarding(False) 再拆钩子。"""
        try:
            self.stop_capture()
        finally:
            self.set_forwarding(False)

    # ------------------------------------------------------------ 桌面几何
    def desktop_rect(self) -> Rect:
        """本机虚拟桌面矩形(所有显示器的并集), 左上角可能不是 (0,0)。"""
        raise BackendError("%s 后端未实现 desktop_rect()" % self.name)

    def monitors(self) -> List[Rect]:
        return [self.desktop_rect()]

    # ------------------------------------------------------------ 捕获(server)
    def start_capture(self, sink: SinkFn) -> None:
        raise BackendError("%s 后端不支持捕获输入" % self.name)

    def stop_capture(self) -> None:
        pass

    def set_forwarding(self, on: bool) -> None:
        """True: 吞掉本机输入并交给 sink; False: 恢复本机输入。

        必须保证在进程异常时也不会把用户的键鼠卡死 —— 实现里要在
        finally 中恢复, 并注册 atexit/信号兜底。
        """
        raise BackendError("%s 后端不支持抑制本机输入" % self.name)

    @property
    def forwarding(self) -> bool:
        return self._forwarding

    def set_park_point(self, x: int, y: int) -> None:
        """转发模式下把本机光标停在这个点(本机桌面坐标)。"""
        self._park = (int(x), int(y))

    def park_point(self) -> Optional[Tuple[int, int]]:
        return self._park

    def emergency_restore(self) -> None:
        """紧急恢复: 立刻不再吞输入, 并把光标放回正常位置。

        中断处理、看门狗、异常退出都走这里。
        """
        try:
            self.set_forwarding(False)
        except Exception:
            pass

    # ------------------------------------------------------------ 光标
    def cursor(self) -> Tuple[int, int]:
        """读本机光标位置(本机桌面坐标)。"""
        raise BackendError("%s 后端未实现 cursor()" % self.name)

    def set_cursor(self, x: int, y: int) -> None:
        """把本机光标移到指定位置(本机桌面坐标)。"""
        raise BackendError("%s 后端未实现 set_cursor()" % self.name)

    # ------------------------------------------------------------ 注入(client)
    def inject_motion(self, x: int, y: int) -> None:
        raise BackendError("%s 后端不支持注入" % self.name)

    def inject_button(self, button: int, pressed: bool) -> None:
        raise BackendError("%s 后端不支持注入" % self.name)

    def inject_wheel(self, dx: int, dy: int) -> None:
        raise BackendError("%s 后端不支持注入" % self.name)

    def inject_key(self, scancode: int, vk: int, pressed: bool,
                   extended: bool = False) -> None:
        raise BackendError("%s 后端不支持注入" % self.name)

    # ------------------------------------------------------------ 注入统一入口
    def inject(self, ev: Event) -> None:
        """client 只调用这个方法; 顺带记录按键状态以便 release_all()。"""
        k = ev.kind
        if k == MOTION:
            self.inject_motion(ev.a, ev.b)
        elif k == BUTTON:
            if ev.b:
                if ev.a not in self._pressed_buttons:
                    self._pressed_buttons.append(ev.a)
            elif ev.a in self._pressed_buttons:
                self._pressed_buttons.remove(ev.a)
            self.inject_button(ev.a, bool(ev.b))
        elif k == WHEEL:
            self.inject_wheel(ev.a, ev.b)
        elif k == KEY:
            if ev.c:
                if ev not in self._pressed_keys:
                    self._pressed_keys.append(ev)
            elif ev in self._pressed_keys:
                self._pressed_keys.remove(ev)
            self.inject_key(ev.a, ev.b, bool(ev.c), bool(ev.d))
        else:
            raise BackendError("未知事件类型 %r" % (k,))

    def release_all(self) -> None:
        """把当前按下的键和鼠标键全部抬起(切换/断线时调用, 防粘键)。"""
        keys, self._pressed_keys = self._pressed_keys, []
        buttons, self._pressed_buttons = self._pressed_buttons, []
        for ev in keys:
            try:
                self.inject_key(ev.a, ev.b, False, bool(ev.d))
            except Exception as exc:            # pragma: no cover - 兜底
                self.log("释放按键失败 %s: %s" % (ev.describe(), exc))
        for btn in buttons:
            try:
                self.inject_button(btn, False)
            except Exception as exc:            # pragma: no cover
                self.log("释放鼠标键失败 %s: %s" % (btn, exc))

    # ------------------------------------------------------------ 剪辑板
    def clipboard_text(self) -> Optional[str]:
        return None

    def set_clipboard_text(self, text: str) -> None:
        raise BackendError("%s 后端不支持写剪辑板" % self.name)

    def clipboard_revision(self) -> Optional[object]:
        """剪辑板变动标记(Windows: 序号; X11: 内容哈希; 不支持则 None)。"""
        return None

    def clipboard_formats(self) -> List[str]:
        """剪辑板里当前有哪些格式(仅诊断用, 只读)。不支持的平台返回空表。"""
        return []

    # ------------------------------------------------------------ 剪辑板图片
    @property
    def supports_clipboard_images(self) -> bool:
        """是否支持图片剪辑板。默认不支持, Windows/Linux 后端各自覆写。"""
        return False

    def clipboard_image_png(self, max_bytes: int = 0) -> Optional[bytes]:
        """读剪辑板里的图片, 返回 PNG 字节; 没有图片/不支持/超过上限返回 None。"""
        return None

    def set_clipboard_image_png(self, png: bytes) -> None:
        raise BackendError("%s 后端不支持写图片剪辑板" % self.name)

    def clipboard_read(self, max_image_bytes: int = 0,
                       prefer_image: bool = False) -> Optional[Tuple[str, object]]:
        """统一的"读出当前剪辑板内容"。

        返回 ("text", str) 或 ("image", png_bytes), 没有东西可同步时返回 None。
        prefer_image 决定两类内容同时存在时先取哪个 —— 这是配置项, 因为
        "从 Excel 复制一个图表"这种场景: 文本轻、图片准, 用户偏好不一定相同。
        """
        order = ("image", "text") if prefer_image else ("text", "image")
        for kind in order:
            if kind == "text":
                if not self.supports_clipboard:
                    continue
                text = self.clipboard_text()
                if text:
                    return ("text", text)
            else:
                if not (self.supports_clipboard_images and max_image_bytes > 0):
                    continue
                png = self.clipboard_image_png(max_image_bytes)
                if png:
                    return ("image", png)
        return None

    # ------------------------------------------------------------ 自检
    def probe(self) -> List[Tuple[str, bool, str]]:
        """返回 [(检查项, 是否通过, 说明)] 供 `crosspc doctor` 展示。"""
        return []
