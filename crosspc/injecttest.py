"""真机注入验收: 证明"我们发出去的按键真的进了系统输入栈"。

为什么需要它: 注入路径(SendInput / XTest / uinput)是整个工具里唯一无法靠纯逻辑
测试覆盖的部分 —— 参数类型写错、结构体大小算错、扩展键标志没带, 都不会报错,
只会"按下去没反应"。

设计上刻意做到**不打扰用户**:
  * 只注入修饰键(左/右 Ctrl、Shift、Alt)与锁定键 ScrollLock, 这些键单按不会
    产生任何字符、不会触发任何快捷键;
  * 左 Ctrl 是与"扩展键"无关的普通键, 右 Ctrl/右 Alt 带 E0 前缀 —— 两者都测,
    才能证明扩展标志真的生效了;
  * ScrollLock 用来验证"注入的键被系统真的处理了": 读它的锁定位是否翻转,
    测完再翻回来, 用户最多看到键盘上 ScrollLock 指示灯闪一下;
  * 开测前先检查用户此刻有没有按着任何修饰键, 按着就等下一轮, 免得我们的
    注入和用户的按键叠在一起;
  * --window 那一段会真的往一个自建窗口里打字(会短暂抢焦点), 所以必须由用户
    显式加这个参数才跑。
"""
from __future__ import annotations

import ctypes
import time
from typing import List, Optional, Tuple

from .backend.base import Backend, BackendError
from .keys import (VK_LCONTROL, VK_LMENU, VK_LSHIFT, VK_RCONTROL, VK_RMENU,
                   VK_RSHIFT, VK_SCROLL)
from .util import Log

#: (扫描码, 是否扩展, VK) —— 全是"单按无副作用"的键
MODIFIER_CASES: List[Tuple[int, bool, int, str]] = [
    (0x1D, False, VK_LCONTROL, "左 Ctrl"),
    (0x36, False, VK_RSHIFT, "右 Shift"),
    (0x38, False, VK_LMENU, "左 Alt"),
    (0x1D, True, VK_RCONTROL, "右 Ctrl(扩展键)"),
    (0x38, True, VK_RMENU, "右 Alt(扩展键)"),
]

#: 注入前要确认用户没按着的键(否则会和他的操作叠加)
BUSY_KEYS = [
    (0x11, "Ctrl"), (0x10, "Shift"), (0x12, "Alt"),
    (0x5B, "左 Win"), (0x5C, "右 Win"),
]

VK_SCROLL_LOCK = VK_SCROLL


def _windows_api():
    """只有 Windows 才有 GetAsyncKeyState; 其它平台返回 (None, None)。"""
    try:
        import ctypes
        from ctypes import wintypes as wt
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetAsyncKeyState.restype = ctypes.c_short
        user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        user32.GetKeyState.restype = ctypes.c_short
        user32.GetKeyState.argtypes = [ctypes.c_int]
    except Exception:
        return None, None
    return user32, wt


def blocked_reason(backend: Backend) -> Optional[str]:
    """判断"这个环境是不是根本不允许注入/捕获输入"。

    为什么需要: DSH 之类的宿主/沙箱会把 SendInput、SetCursorPos 这类"能操作
    用户鼠标键盘"的 API 变成空操作 —— 它们不报错(SendInput 甚至返回 1),
    只是什么也不做。如果不识别这种情况, 就会把环境限制误报成"你的代码有问题",
    那是最容易把人带偏的假故障。

    探针本身对用户没有任何影响: 把光标移到**原地**, 注入一个按下即抬起的
    Shift(不产生字符、不触发任何快捷键)。
    """
    user32, _ = _windows_api()
    if user32 is None:
        return None
    try:
        pos = backend.cursor()
    except Exception:
        return None
    if not user32.SetCursorPos(int(pos[0]), int(pos[1])):
        return ("SetCursorPos 被拒绝(GetLastError=%d) —— 当前环境禁止移动鼠标"
                % ctypes.get_last_error())
    try:
        backend.inject_key(0x2A, VK_LSHIFT, True)
        time.sleep(0.02)
        seen = bool(user32.GetAsyncKeyState(0x10) & 0x8000)
        backend.inject_key(0x2A, VK_LSHIFT, False)
        time.sleep(0.02)
    except Exception:
        return None
    if not seen:
        return ("SendInput 返回成功但按键状态没有任何变化 —— 注入被宿主环境"
                "屏蔽了(常见于 DSH/沙箱等受限会话)")
    return None


def _windows_injection_blocked(backend: Backend) -> Optional[str]:
    """兼容旧名字(内部别处仍在用)。"""
    return blocked_reason(backend)


def run_inject_test(log: Log, backend_name: Optional[str] = None,
                    with_window: bool = False, hold_seconds: float = 0.05
                    ) -> int:
    from .backend import get_backend

    log.info("CrossPC 注入验收: 会注入几个「无副作用」的键并回读系统状态")
    try:
        backend = get_backend(log, prefer=backend_name)
        backend.prepare()
    except Exception as exc:
        log.error("创建后端失败: %s" % exc)
        return 1
    if not backend.supports_inject:
        log.error("%s 后端不支持注入, 无法做这项测试" % backend.name)
        return 1
    log.info("注入方式: %s" % backend.caps())

    user32 = None
    if backend.name == "windows":
        user32, _ = _windows_api()
        if user32 is None:
            log.error("拿不到 user32, 无法回读按键状态")
            return 1
        blocked = _windows_injection_blocked(backend)
        if blocked:
            log.warn("跳过: %s" % blocked)
            log.warn("这不是 CrossPC 的问题, 而是当前会话不允许代理进程操作你的"
                     "鼠标键盘。请在你**自己打开**的 PowerShell 窗口里运行:")
            log.warn("    python -m crosspc injecttest")
            log.warn("(或者在真实两台机器上直接联调) 本次没有验证注入链路。")
            try:
                backend.close()
            except Exception:
                pass
            return 2                                    # 2 = 环境不允许, 非代码失败

        busy = [name for vk, name in BUSY_KEYS
                if user32.GetAsyncKeyState(vk) & 0x8000]
        if busy:
            log.warn("检测到你正按着 %s, 为避免和你的操作叠加, 请松开后再运行"
                     % "、".join(busy))
            return 1

    passed = 0
    failed = 0
    try:
        # ---- 1. 修饰键: 按下 -> 回读为按下 -> 抬起 -> 回读为抬起 ----
        for scancode, extended, vk, label in MODIFIER_CASES:
            if user32 is not None:
                before = bool(user32.GetAsyncKeyState(vk) & 0x8000)
            backend.inject_key(scancode, vk, True, extended)
            time.sleep(hold_seconds)
            down = True
            if user32 is not None:
                down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
            backend.inject_key(scancode, vk, False, extended)
            time.sleep(hold_seconds)
            up = True
            if user32 is not None:
                up = not bool(user32.GetAsyncKeyState(vk) & 0x8000)
            if before:
                log.warn("  %s: 跳过(你本来就在按着它)" % label)
                continue
            if down and up:
                passed += 1
                log.info("  [通过] %s: 按下被系统识别, 抬起后恢复" % label)
            else:
                failed += 1
                log.error("  [失败] %s: 按下=%s 抬起=%s"
                          % (label, down, up))

        # ---- 2. 锁定键: 注入后锁定位应该翻转, 再翻回来 ----
        if user32 is None:
            log.warn("  [跳过] ScrollLock 回读(非 Windows 平台没有 GetKeyState)")
        else:
            before = bool(user32.GetKeyState(VK_SCROLL_LOCK) & 1)
            backend.inject_key(0x46, VK_SCROLL_LOCK, True, False)
            backend.inject_key(0x46, VK_SCROLL_LOCK, False, False)
            time.sleep(0.15)
            after = bool(user32.GetKeyState(VK_SCROLL_LOCK) & 1)
            # 还原: 再按一次就回到原状态
            backend.inject_key(0x46, VK_SCROLL_LOCK, True, False)
            backend.inject_key(0x46, VK_SCROLL_LOCK, False, False)
            time.sleep(0.15)
            restored = bool(user32.GetKeyState(VK_SCROLL_LOCK) & 1)
            if after != before and restored == before:
                passed += 1
                log.info("  [通过] ScrollLock: 注入后锁定位翻转, 已还原"
                         "(%s -> %s -> %s)" % (before, after, restored))
            else:
                failed += 1
                log.error("  [失败] ScrollLock: %s -> %s -> %s(期望翻转后还原)"
                          % (before, after, restored))

        # ---- 3. 坐标注入: 光标应该被挪到指定位置 ----
        try:
            orig = backend.cursor()
            rect = backend.desktop_rect()
            target = (rect.x + 5, rect.y + 5)
            backend.inject_motion(*target)
            time.sleep(0.05)
            now = backend.cursor()
            backend.set_cursor(*orig)                   # 立刻还原
            if abs(now[0] - target[0]) <= 2 and abs(now[1] - target[1]) <= 2:
                passed += 1
                log.info("  [通过] 鼠标绝对定位: 光标到达 %s, 已还原到 %s"
                         % (now, orig))
            else:
                failed += 1
                log.error("  [失败] 鼠标绝对定位: 期望 %s, 实际 %s"
                          % (target, now))
        except Exception as exc:
            failed += 1
            log.error("  [失败] 鼠标绝对定位异常: %s" % exc)

        # ---- 4. 可选: 往自建窗口里真打字(会抢焦点) ----
        if with_window:
            ok, detail = _window_typing_test(backend, log)
            if ok:
                passed += 1
                log.info("  [通过] 窗口输入: %s" % detail)
            else:
                failed += 1
                log.error("  [失败] 窗口输入: %s" % detail)
        else:
            log.info("  [跳过] 窗口输入测试(它需要短暂抢焦点; 想跑请加 --window)")
    finally:
        try:
            backend.close()
        except Exception:
            pass

    log.info("注入验收结果: 通过 %d 项, 失败 %d 项" % (passed, failed))
    if failed:
        log.error("有项目没通过 —— 请把上面输出发我, 这通常意味着注入参数/扩展键"
                  "标志有问题")
        return 1
    log.info("注入链路正常。想连「打字是否真的进了窗口」一起验证, "
             "加 --window 再跑一次")
    return 0


def _window_typing_test(backend: Backend, log: Log
                        ) -> Tuple[bool, str]:
    """在一个自建窗口里真打字并回读, 验证扫描码/大写/退格/扩展键都对。

    会短暂抢焦点(这不是缺陷, 是测试本身的需要), 结束后把焦点还给原来的窗口。
    """
    try:
        import tkinter as tk
    except Exception as exc:
        return False, "没有 tkinter, 跑不了窗口测试: %s" % exc

    restore_hwnd = None
    if backend.name == "windows":
        try:
            user32, _ = _windows_api()
            if user32 is not None:
                restore_hwnd = user32.GetForegroundWindow()
        except Exception:
            restore_hwnd = None

    root = None
    try:
        root = tk.Tk()
        root.title("CrossPC 注入测试(会自己关掉)")
        root.geometry("420x90+80+80")
        entry = tk.Entry(root, font=("Consolas", 14))
        entry.pack(fill="both", expand=True, padx=8, pady=8)
        root.attributes("-topmost", True)
        root.update()
        root.focus_force()
        entry.focus_force()
        root.update()
        log.info("  3 秒后开始注入到本窗口(此刻请勿操作键盘)...")
        for i in (3, 2, 1):
            log.info("  %d..." % i)
            time.sleep(1.0)
        root.update()
        if root.focus_displayof() is None:
            return False, "窗口没拿到焦点(别的程序一直抢焦点?)"

        # "crosspc" -> 退格 -> Shift+A -> Home(扩展键) -> X  =>  "XcrosspA"
        _type_text(backend, "crosspc")
        backend.inject_key(0x0E, 0x08, True)          # Backspace
        backend.inject_key(0x0E, 0x08, False)
        _press_modifier(backend, 0x2A, VK_LSHIFT, 0x1E, 0x41)   # Shift+A
        backend.inject_key(0x47, 0x24, True, True)    # Home(扩展键)
        backend.inject_key(0x47, 0x24, False, True)
        _type_text(backend, "x")
        time.sleep(0.5)
        root.update()
        got = entry.get()
        expected = "XcrosspA"
        if got == expected:
            return True, "窗口收到 %r, 与期望一致(含退格/大写/扩展键)" % got
        return False, "窗口收到 %r, 期望 %r" % (got, expected)
    except Exception as exc:
        return False, "异常: %s" % exc
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass
        if restore_hwnd:
            try:
                user32, _ = _windows_api()
                if user32 is not None:
                    user32.SetForegroundWindow(restore_hwnd)
            except Exception:
                pass


#: 只注入小写字母/数字/空格, 不需要 Shift, 也就不依赖键盘布局的符号位
_LOWER_MAP = {
    **{chr(ord("a") + i): (0x1E + i, 0x41 + i) for i in range(26)},
    **{str(i): (0x02 + i, 0x30 + i) for i in range(1, 10)},
    "0": (0x0B, 0x30),
    " ": (0x39, 0x20),
}


def _type_text(backend: Backend, text: str) -> None:
    for ch in text:
        hit = _LOWER_MAP.get(ch)
        if hit is None:
            continue
        scancode, vk = hit
        backend.inject_key(scancode, vk, True)
        backend.inject_key(scancode, vk, False)
        time.sleep(0.012)


def _press_modifier(backend: Backend, mod_scan: int, mod_vk: int,
                    key_scan: int, key_vk: int) -> None:
    backend.inject_key(mod_scan, mod_vk, True)
    backend.inject_key(key_scan, key_vk, True)
    backend.inject_key(key_scan, key_vk, False)
    backend.inject_key(mod_scan, mod_vk, False)
