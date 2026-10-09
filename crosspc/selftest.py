"""端到端回环自测: 不需要第二台电脑, 也不碰真实键鼠。

做法: 用 FakeBackend 假装"server 的键鼠"和"client 的屏幕", 然后跑**真正的**
ServerApp / ClientApp / 线协议 / 网络层, 用 127.0.0.1 连起来, 检查:

  1. 鼠标推过屏幕右边缘 -> 控制权应该切到 client;
  2. 切换后的绝对坐标应该落到 client 屏幕的正确位置;
  3. 按键应该被转发, 松开边缘回来时应该把按键全部抬起(防粘键);
  4. 双向剪辑板同步;
  5. 紧急热键能立刻把控制权收回本机。

跑通它就说明"位置计算 + 协议 + 网络 + 路由"这条主线是好的, 剩下的风险只在
平台输入层(钩子/注入)上 —— 那部分由 `crosspc doctor` 和真机联调覆盖。
"""
from __future__ import annotations

import os
import socket
import threading
import time
from typing import List, Optional, Tuple

from .backend.fake import FakeBackend
from .client import ClientApp
from .config import Config, ClientEntry
from .events import BTN_LEFT, Event
from .layout import Rect
from .protocol import T_CONTROL, control, frame
from .server import ServerApp
from .util import Log, pick_writable_dir, scratch_prefix


class Check:
    def __init__(self, log: Log):
        self.log = log
        self.passed = 0
        self.failed = 0

    def ok(self, name: str, detail: str = "") -> None:
        self.passed += 1
        self.log.info("  [通过] %s%s" % (name, " — " + detail if detail else ""))

    def fail(self, name: str, detail: str = "") -> None:
        self.failed += 1
        self.log.error("  [失败] %s%s" % (name, " — " + detail if detail else ""))

    def eq(self, name: str, got, want) -> None:
        if got == want:
            self.ok(name, "= %r" % (got,))
        else:
            self.fail(name, "期望 %r, 实际 %r" % (want, got))

    def true(self, name: str, cond: bool, detail: str = "") -> None:
        if cond:
            self.ok(name, detail)
        else:
            self.fail(name, detail)

    def near(self, name: str, got: Tuple[int, int], want: Tuple[int, int],
             tol: int = 2) -> None:
        if abs(got[0] - want[0]) <= tol and abs(got[1] - want[1]) <= tol:
            self.ok(name, "%r ≈ %r" % (got, want))
        else:
            self.fail(name, "期望约 %r, 实际 %r" % (want, got))


class _Harness:
    """把 server/client 两个 App 跑起来的最小脚手架。

    临时文件直接落在"确认可写"的目录里(不建子目录: 受限环境里新建子目录
    可能不可写), 文件名带 pid 前缀, 结束后自己删干净。
    """

    def __init__(self, server_backend: FakeBackend, client_backend: FakeBackend,
                 log: Log, base: str, prefix: str):
        self.log = log
        self.srv_backend = server_backend
        self.cli_backend = client_backend
        self.base = base
        self.prefix = prefix
        self.files: List[str] = []
        self.server: Optional[ServerApp] = None
        self.client: Optional[ClientApp] = None
        self.threads: List[threading.Thread] = []

    def path(self, name: str) -> str:
        p = os.path.join(self.base, self.prefix + name)
        self.files.append(p)
        return p

    def start(self) -> int:
        srv_cfg = Config.defaults(self.path("server.json"))
        srv_cfg.name = "win11"
        srv_cfg.clipboard_poll_ms = 120
        srv_cfg.clients = [ClientEntry(name="debian", host="127.0.0.1",
                                       rect=Rect(1920, 0, 2560, 1440))]
        srv_cfg.log_level = "info"
        srv_cfg.path = ""        # 自测不落配置文件: 免得碰到用户真实的 crosspc.json
        self.server = ServerApp(srv_cfg, self.srv_backend, self.log,
                                port=0, bind="127.0.0.1",
                                cache_path=self.path("server.cache.json"))
        t = threading.Thread(target=self.server.run, name="selftest-server",
                             daemon=True)
        t.start()
        self.threads.append(t)
        port = 0
        for _ in range(50):
            if self.server._listener is not None:
                port = self.server._listener.getsockname()[1]
                break
            time.sleep(0.1)
        if not port:
            raise RuntimeError("server 没有在 5 秒内监听成功")

        cli_cfg = Config.defaults(self.path("client.json"))
        cli_cfg.name = "debian"
        cli_cfg.clipboard_poll_ms = 120
        cli_cfg.path = ""
        self.client = ClientApp(cli_cfg, self.cli_backend, self.log,
                                host="127.0.0.1", port=port)
        t = threading.Thread(target=self.client.run, name="selftest-client",
                             daemon=True)
        t.start()
        self.threads.append(t)
        for _ in range(60):
            if self.server.sessions:
                break
            time.sleep(0.1)
        if not self.server.sessions:
            raise RuntimeError("client 没有在 6 秒内连上 server")
        time.sleep(0.3)
        return port

    def stop(self) -> None:
        for app in (self.client, self.server):
            if app is not None:
                try:
                    app.stop()
                except Exception:
                    pass
        for app in (self.client, self.server):
            if app is not None:
                try:
                    app.shutdown()
                except Exception:
                    pass
        for t in self.threads:
            t.join(2.0)
        for p in self.files:
            for candidate in (p, p + ".tmp"):
                try:
                    os.remove(candidate)
                except OSError:
                    pass


def run_selftest(log: Optional[Log] = None, keep_alive: bool = False) -> int:
    log = log or Log("info")
    check = Check(log)
    log.info("CrossPC 回环自测(不需要第二台电脑, 不会碰真实键鼠)")
    # 临时文件放在"确认可写"的目录里, 且不建子目录(受限环境里新建子目录
    # 可能不可写, 例如 Windows 沙箱)
    base = pick_writable_dir()
    prefix = scratch_prefix("selftest")
    # server: 1920x1080; client: 2560x1440, 摆在 server 右边
    srv = FakeBackend(log=log.debug, desktop=Rect(0, 0, 1920, 1080))
    cli = FakeBackend(log=log.debug, desktop=Rect(0, 0, 2560, 1440))
    harness = _Harness(srv, cli, log, base, prefix)
    try:
        harness.start()
        check.true("两端建立连接", True, "server 端口已监听, client 已握手")
        router = harness.server.router
        assert router is not None

        # ---- 1. 鼠标从右边推出去 ----
        srv.feed(Event.motion(1900, 500, 0, 0))
        check.eq("仍在 server 上", router.active.name, "win11")
        cli.injected.clear()
        srv.feed(Event.motion(1919, 500, 20, 0))
        check.eq("推过右边缘后控制权转移", router.active.name, "debian")
        if _wait_for(lambda: len(cli.injected) >= 1, 2.0):
            check.near("进入点坐标(贴 client 左边)",
                       (cli.injected[-1].a, cli.injected[-1].b), (0, 500), 3)
        else:
            check.fail("client 收到进入移动事件", "等了 2 秒没有任何注入事件")

        # ---- 2. 在 client 上继续移动 ----
        cli.injected.clear()
        srv.feed(Event.motion(1919, 500, 40, 60))
        if _wait_for(lambda: len(cli.injected) >= 1, 2.0):
            check.near("远端绝对坐标准确",
                       (cli.injected[-1].a, cli.injected[-1].b), (40, 560), 3)
        else:
            check.fail("client 持续收到移动", "等了 2 秒没有事件")

        # ---- 3. 按键转发 + 抬起 ----
        cli.injected.clear()
        srv.feed(Event.key(0x1E, 0x41, True))
        got = _wait_for(lambda: any(e.kind == 4 for e in cli.injected), 2.0)
        check.true("按键已转发到 client", got,
                   "收到 %d 个事件" % len(cli.injected))
        check.true("按键按下被记录", router.pressed_count() == 1,
                   "按住的键 %d 个" % router.pressed_count())

        # ---- 4. 从左边离开 client, 回 server ----
        cli.injected.clear()
        srv.feed(Event.motion(0, 700, -4000, 0))
        check.eq("控制权回到 server", router.active.name, "win11")
        got = _wait_for(lambda: any(e.kind == 4 and not e.c
                                    for e in cli.injected), 2.0)
        check.true("离开时补发了抬键", got,
                   "抬键事件 %d 个" % len([e for e in cli.injected
                                           if e.kind == 4 and not e.c]))
        check.true("按住的键已清空", router.pressed_count() == 0)
        # 从 client 左边滑回 server 时, 光标应该回到 server 的**右边缘**
        # (client 原本就贴在那边), y 沿用离开时的 560
        check.near("本机光标被放到进入点", srv.cursor(), (1919, 560), 2)
        check.true("已退出接管模式", not srv.forwarding,
                   "forwarding=%s" % srv.forwarding)

        # ---- 5. 剪辑板双向 ----
        srv.clipboard = "来自 windows 的文字"
        srv._clip_rev += 1
        ok = _wait_for(lambda: cli.clipboard == "来自 windows 的文字", 3.0)
        check.true("server -> client 剪辑板", ok,
                   "client 剪辑板 = %r" % cli.clipboard)
        cli.clipboard = "来自 debian 的文字"
        cli._clip_rev += 1
        ok = _wait_for(lambda: srv.clipboard == "来自 debian 的文字", 3.0)
        check.true("client -> server 剪辑板", ok,
                   "server 剪辑板 = %r" % srv.clipboard)

        # ---- 5b. 图片剪辑板(用生成的 PNG, 不需要真的截图) ----
        from .image import encode_png

        def make_png(seed: int) -> bytes:
            rgba = bytearray()
            for y in range(16):
                for x in range(16):
                    rgba += bytes(((x * 16 + seed) % 256, (y * 16) % 256,
                                   128, 255))
            return encode_png(bytes(rgba), 16, 16)

        png_a = make_png(0)
        srv.clipboard = ""            # 清掉文本: 默认优先发文本, 不清就不会发图片
        srv.clipboard_image = png_a
        srv._clip_rev += 1
        ok = _wait_for(lambda: cli.clipboard_image == png_a, 3.0)
        check.true("server -> client 图片剪辑板", ok,
                   "client 收到 %d 字节" % len(cli.clipboard_image or b""))

        png_b = make_png(64)          # 换一张, 内容哈希不同才会被当成"新内容"
        cli.clipboard = ""
        cli.clipboard_image = png_b
        cli._clip_rev += 1
        ok = _wait_for(lambda: srv.clipboard_image == png_b, 3.0)
        check.true("client -> server 图片剪辑板", ok,
                   "server 收到 %d 字节" % len(srv.clipboard_image or b""))

        # 图片解码失败也不能把链路搞崩(畸形数据防御)
        srv.clipboard_image = b"\x89PNG\r\n\x1a\n" + "坏数据".encode("utf-8")
        srv._clip_rev += 1
        time.sleep(0.3)
        check.true("畸形图片不会中断同步", router is not None and
                   not harness.server._input_errors,
                   "服务端仍在运行")

        # ---- 6. 紧急热键 ----
        srv.feed(Event.motion(1919, 300, 20, 0))
        check.eq("再次切到 client", router.active.name, "debian")
        for ev in (Event.key(0x1D, 0xA2, True),        # LCtrl
                   Event.key(0x38, 0xA4, True),        # LAlt
                   Event.key(0x58, 0x7B, True)):       # F12
            srv.feed(ev)
        check.eq("紧急热键收回控制权", router.active.name, "win11")
        check.true("紧急热键后退出接管", not srv.forwarding)

        # ---- 7. 断开 client 后自动收回 ----
        srv.feed(Event.motion(1919, 300, 20, 0))
        check.eq("第三次切到 client", router.active.name, "debian")
        harness.server.sessions["debian"].link.close("模拟拔网线")
        ok = _wait_for(lambda: router.active.name == "win11", 3.0)
        check.true("client 断线立即收回控制权", ok,
                   "当前控制 %s" % router.active.name)
    except Exception as exc:
        import traceback
        check.fail("自测异常", "%s" % exc)
        log.error(traceback.format_exc())
    finally:
        if keep_alive:
            log.info("--keep-alive: 自测环境保持运行, Ctrl+C 退出")
            try:
                while True:
                    time.sleep(1.0)
            except KeyboardInterrupt:
                pass
        harness.stop()

    log.info("自测结果: 通过 %d 项, 失败 %d 项" % (check.passed, check.failed))
    if check.failed:
        log.error("自测未全部通过, 请把上面的失败项和日志发给我")
        return 1
    log.info("全部通过: 位置计算、协议、网络、路由、剪辑板、热键都正常")
    return 0


def _wait_for(pred, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


# ------------------------------------------------------------------ 真机剪辑板
def run_clipboard_test(log: Log, backend_name: Optional[str] = None,
                       keep_image: bool = False) -> int:
    """真机剪辑板验收: 文本与图片各写一遍再读回来比对。

    会**临时**改掉你的剪辑板(测试图 -> 读到的东西 -> 还原), 所以是显式命令,
    不会在 doctor 里偷偷跑。测试前会把当前内容存下来, 结束后尽力还原:
    文本一定还原; 图片能读出来就还原; 文件列表之类我们读不了的格式无法还原,
    这一点会明确告诉你。
    """
    from .backend import get_backend
    from .image import ImageError, decode_png, encode_png, has_alpha

    try:
        backend = get_backend(log, prefer=backend_name)
        backend.prepare()
    except Exception as exc:
        log.error("创建后端失败: %s" % exc)
        return 1
    if not backend.supports_clipboard:
        log.error("%s 后端不支持剪辑板" % backend.name)
        return 1

    saved_text = backend.clipboard_text()
    saved_image = None
    if backend.supports_clipboard_images:
        try:
            saved_image = backend.clipboard_image_png(8 * 1024 * 1024)
        except Exception as exc:
            log.debug("保存原图片失败: %s" % exc)
    formats = backend.clipboard_formats()
    log.info("剪辑板原有格式: %s" % (", ".join(formats) or "(读不到)"))
    log.info("已保存当前内容(文本 %s, 图片 %s), 测试结束会还原"
             % ("有" if saved_text else "空",
                "%d 字节" % len(saved_image) if saved_image else "无"))
    if formats and not saved_text and not saved_image:
        log.warn("剪辑板里有我们读不了的格式(例如文件列表), 测试后无法还原它")
    known = ("CF_UNICODETEXT", "CF_DIB", "CF_DIBV5", "CF_BITMAP", "PNG")
    unknown = [f for f in formats if f not in known]
    if unknown:
        log.warn("剪辑板里还有我们读不了的格式(%s...), 测试后只能还原文本/图片"
                 % ", ".join(unknown[:4]))

    passed = 0
    failed = 0

    # ---- 1. 文本往返 ----
    marker = "CrossPC 剪辑板测试 %d" % int(time.time())
    try:
        backend.set_clipboard_text(marker)
        got = backend.clipboard_text()
        if got == marker:
            passed += 1
            log.info("  [通过] 文本往返: 写入并读回一致(%d 字符)" % len(marker))
        else:
            failed += 1
            log.error("  [失败] 文本往返: 写入 %r, 读回 %r" % (marker, got))
    except Exception as exc:
        failed += 1
        log.error("  [失败] 文本往返异常: %s" % exc)

    # ---- 2. 图片往返 ----
    if not backend.supports_clipboard_images:
        log.warn("  [跳过] 图片往返: 该后端/该剪辑板工具不支持图片")
    else:
        w, h = 64, 48
        rgba = bytearray()
        for y in range(h):
            for x in range(w):
                rgba += bytes(((x * 4) % 256, (y * 5) % 256,
                               ((x + y) * 3) % 256, 255))
        src = bytes(rgba)
        png = encode_png(src, w, h)
        try:
            backend.set_clipboard_image_png(png)
            after_formats = backend.clipboard_formats()
            back = backend.clipboard_image_png(8 * 1024 * 1024)
            if not back:
                failed += 1
                log.error("  [失败] 图片往返: 写进去了但读不回来")
            else:
                same_bytes = (back == png)
                try:
                    decoded, dw, dh = decode_png(back)
                    same_pixels = (decoded == src and (dw, dh) == (w, h))
                except ImageError as exc:
                    same_pixels = False
                    log.warn("  读回的图片解不开: %s" % exc)
                if same_pixels:
                    passed += 1
                    log.info("  [通过] 图片往返: %dx%d 像素完全一致"
                             "(%s, 写回 %d 字节)"
                             % (w, h, "字节也相同" if same_bytes
                                else "字节不同但像素相同", len(back)))
                else:
                    failed += 1
                    log.error("  [失败] 图片往返: 像素不一致")
                if after_formats:
                    log.info("  写入后剪辑板格式: %s" % ", ".join(after_formats))
                if backend.name == "windows" and after_formats:
                    has_dib = any(f in ("CF_DIB", "CF_DIBV5")
                                  for f in after_formats)
                    has_png = any(f.upper() == "PNG" for f in after_formats)
                    if has_dib and has_png:
                        passed += 1
                        log.info("  [通过] 同时写入了 DIB(老程序) 与 PNG(新程序)")
                    else:
                        failed += 1
                        log.error("  [失败] 格式不全: DIB=%s PNG=%s"
                                  % (has_dib, has_png))
        except Exception as exc:
            failed += 1
            log.error("  [失败] 图片往返异常: %s" % exc)

    # ---- 3. 还原 ----
    try:
        if saved_image and not keep_image:
            backend.set_clipboard_image_png(saved_image)
            log.info("剪辑板已还原为原来的图片")
        elif saved_text is not None:
            backend.set_clipboard_text(saved_text)
            log.info("剪辑板已还原为原来的文本")
        elif saved_image and keep_image:
            backend.set_clipboard_image_png(saved_image)
            log.info("剪辑板已还原为原来的图片(保留了测试图以外的内容)")
        else:
            backend.set_clipboard_text("")
            log.info("剪辑板原来就是空的, 已清空")
    except Exception as exc:
        log.warn("还原剪辑板失败: %s" % exc)

    try:
        backend.close()
    except Exception:
        pass
    log.info("剪辑板验收结果: 通过 %d 项, 失败 %d 项" % (passed, failed))
    return 1 if failed else 0


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ------------------------------------------------------------------ 真机输入验收
def run_capture_test(log: Log, seconds: float = 5.0, takeover: bool = False,
                     backend_name: Optional[str] = None) -> int:
    """真机验收: 用真实后端抓 N 秒键鼠, 只统计数量, 不记录内容。

    这是唯一需要"真键鼠"的一步, 所以做成一条显式命令让用户自己在方便的时候跑:
      第一段(默认)  只观察不接管, 窗口里随便动动鼠标敲敲键盘, 看计数是否增长;
      第二段(--takeover) 真正接管 N 秒, 期间本机键鼠不生效 —— 用来验证
                "吞掉本机输入 + 恢复"这条路是通的, 结束后一定会恢复。

    无论发生什么, finally 里都会解除接管(还有 Ctrl+Alt+F12 兜底)。
    """
    from collections import Counter

    from .backend import get_backend
    from .events import BUTTON, KEY, MOTION, WHEEL, Event

    try:
        backend = get_backend(log, prefer=backend_name)
    except Exception as exc:
        log.error("创建后端失败: %s" % exc)
        return 1
    if not backend.supports_capture:
        log.error("%s 后端不支持捕获输入, 无法做这项测试" % backend.name)
        return 1

    log.info("后端 %s, 即将捕获 %.0f 秒" % (backend.caps(), seconds))
    # 受限会话(沙箱/AI 助手)会屏蔽输入 —— 先探一下, 免得把环境限制报成"钩子坏了"
    try:
        from .injecttest import blocked_reason
        blocked = blocked_reason(backend)
    except Exception:
        blocked = None
    if blocked:
        log.warn("跳过: %s" % blocked)
        log.warn("这不是 CrossPC 的问题, 而是当前会话不允许代理进程接触你的"
                 "鼠标键盘。请在你**自己打开**的 PowerShell 窗口里运行:")
        log.warn("    python -m crosspc capturetest%s"
                 % (" --takeover" if takeover else ""))
        try:
            backend.close()
        except Exception:
            pass
        return 2                                    # 2 = 环境不允许, 非代码失败
    if takeover:
        log.warn("接管模式: 这几秒内本机键鼠不会生效, "
                 "按 Ctrl+Alt+F12 可立即恢复; 到时间会自动恢复")
    else:
        log.info("观察模式: 本机键鼠照常使用, 只是顺便统计事件数量")
    counts: Counter = Counter()
    name_counts: Counter = Counter()

    def sink(ev: Event) -> None:
        counts[ev.kind] += 1
        if ev.kind == KEY and not ev.c:
            name_counts["按键抬起"] += 1

    backend.prepare()
    backend.start_capture(sink)
    if takeover:
        try:
            backend.set_forwarding(True)
        except Exception as exc:
            log.error("进入接管模式失败: %s" % exc)
            backend.stop_capture()
            return 1
    started = time.monotonic()
    try:
        while time.monotonic() - started < seconds:
            time.sleep(0.2)
            done = time.monotonic() - started
            if int(done) != int(done - 0.2):
                log.info("  还剩 %.0f 秒, 已捕获 %d 个事件"
                         % (max(seconds - done, 0), sum(counts.values())))
    except KeyboardInterrupt:
        log.warn("被中断")
    finally:
        try:
            backend.set_forwarding(False)
        except Exception:
            pass
        try:
            backend.stop_capture()
        except Exception:
            pass
        try:
            backend.close()
        except Exception:
            pass

    total = sum(counts.values())
    log.info("捕获结果: 共 %d 个事件" % total)
    for kind, label in ((MOTION, "鼠标移动"), (BUTTON, "鼠标按键"),
                        (WHEEL, "滚轮"), (KEY, "键盘")):
        log.info("  %-8s %d" % (label, counts.get(kind, 0)))
    if total == 0:
        log.error("一个事件都没收到。可能原因:")
        log.error("  1) 刚才确实没动键盘鼠标(再跑一次, 期间随便动动)")
        log.error("  2) 杀软/输入法拦截了全局钩子")
        log.error("  3) 如果你是在 DSH 这类受限会话里运行的本命令, 输入捕获"
                  "(以及注入)会被宿主安全策略屏蔽 —— 请在你**自己打开**的 "
                  "PowerShell 窗口里运行 crosspc capturetest")
        return 1
    log.info("输入捕获正常。接管模式验证通过" if takeover
             else "输入捕获正常。想进一步验证接管(吞输入)请加 --takeover")
    return 0
