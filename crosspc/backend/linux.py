"""Linux 后端: 把 X11(XTest) 或 /dev/uinput 合成一个可用的 client 后端。

角色范围(v1):
    Linux 端**只做 client** —— 注入远端键鼠 + 读写文本剪辑板。
    不做 server 的原因: 要在 Linux 上"捕获并抑制"本机输入, 得用 evdev 独占
    抓取(EVIOCGRAB)所有输入设备, 再和窗口系统协商"谁来处理这次按键";
    在 X11 上还要处理 XRecord/XTEST 抢占, 在 Wayland 上只能靠合成器的专用
    协议(gnome-shell / wlroots 各不一样), 而且一旦抢错或崩掉, 用户的键鼠就
    真的不能用了。这属于高风险功能, 不是 v1 该塞进来的东西。所以
    supports_capture/supports_suppress 都是 False, start_capture()/
    set_forwarding() 直接继承基类抛 BackendError。

注入策略(二选一, prepare() 时决定):
    1. X11 优先: DISPLAY 可用且 XTest 就绪 -> linux_x11.X11Injector。
       为什么优先: 走 X 服务器合成事件, 不需要 root/udev 规则, 而且
       XTestFakeMotionEvent 是 X 的绝对屏幕坐标, 不会被指针加速影响。
    2. 否则 uinput: 有 WAYLAND_DISPLAY, 或者 /dev/uinput 可写 ->
       linux_uinput.UInputInjector(绝对定位虚拟指针设备, 详见该模块 docstring)。

剪辑板:
    文本与图片(image/png), 都通过子进程调外部工具(wl-clipboard / xclip;
    纯文本还能用 xsel)。为什么不用 ctypes 直接实现 X 的 selection 协议:
    那需要长期占住一个 X 连接、在事件循环里应答 SelectionRequest, 还要处理
    INCR 大文本分段和 owner 退出 —— 等于在进程里再养一个 X 客户端。子进程
    走的是同一套协议, 但是发行版维护的成熟实现, 我们只需处理"工具不存在/
    超时/非 0 退出"。Wayland 下同理(wl-copy 会 fork 一个后台进程持有
    selection)。
    注意: Linux 侧**从不解码也不编码 PNG** —— 图片就是原样透传的字节流,
    转码只在 Windows 侧(DIB<->PNG)发生。
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

#: 读剪辑板的超时。必须设: 某些工具在"没有 selection 持有者"时会一直等,
#: 把 client 的注入线程/心跳线程整个卡死。2 秒是"慢但能忍"和"卡死"的折中。
CLIPBOARD_TIMEOUT = 2.0

#: 工具名 -> (读命令, 写命令)
CLIPBOARD_TOOLS: Dict[str, Tuple[List[str], List[str]]] = {
    "wl-copy": (["wl-paste", "--no-newline"], ["wl-copy"]),
    "xclip": (["xclip", "-selection", "clipboard", "-o"],
              ["xclip", "-selection", "clipboard", "-i"]),
    "xsel": (["xsel", "-b", "-o"], ["xsel", "-b", "-i"]),
}

#: 图片剪辑板: 工具名 -> (读 image/png 的命令, 写 image/png 的命令)。
#: xsel 不在表里 —— 它只能处理文本 target, 给不了 MIME 类型。
IMAGE_CLIPBOARD_TOOLS: Dict[str, Tuple[List[str], List[str]]] = {
    "wl-copy": (["wl-paste", "--type", "image/png"],
                ["wl-copy", "--type", "image/png"]),
    "xclip": (["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
              ["xclip", "-selection", "clipboard", "-t", "image/png", "-i"]),
}

#: image/png 的魔数(前 8 字节)
PNG_SIG = b"\x89PNG\r\n\x1a\n"


def parse_screen_spec(spec: str, default: Tuple[int, int] = (1920, 1080)
                      ) -> Tuple[Tuple[int, int], str]:
    """解析 CROSSPC_SCREEN(形如 "2560x1440")。

    返回 ((宽, 高), 来源说明), 永远不会抛异常 —— 诊断输出需要它足够结实。
    uinput 是绝对定位设备, 必须知道分辨率才能把像素坐标映射到 0..65535,
    而 uinput 本身问不出"屏幕多大", 所以只能靠这里给。
    """
    spec = (spec or "").strip().lower().replace(" ", "")
    if not spec:
        return default, "环境变量 CROSSPC_SCREEN 未设置, 暂用默认值 %dx%d" % default
    for sep in ("x", "*", ",", "×"):
        if sep in spec:
            left, _, right = spec.partition(sep)
            try:
                w, h = int(left), int(right)
            except ValueError:
                break
            if w > 0 and h > 0:
                return (w, h), "来自环境变量 CROSSPC_SCREEN=%s" % spec
            break
    return default, ("环境变量 CROSSPC_SCREEN=%r 解析不了(要形如 2560x1440), "
                     "暂用默认值 %dx%d" % (spec, default[0], default[1]))


def choose_clipboard_tool(env: Dict[str, str],
                          which: Callable[[str], Optional[str]]
                          ) -> Optional[str]:
    """按环境挑剪辑板工具, 返回工具名(CLIPBOARD_TOOLS 的键)或 None。

    抽成纯函数是为了能在 Windows 上单测选择逻辑(见 tests)。

    顺序:
      * WAYLAND_DISPLAY 存在时**先**试 wl-copy: Wayland 会话里 xclip 也能
        跑(通过 XWayland), 但读写的是 XWayland 那份剪辑板, 和原生 Wayland
        应用看到的不是同一个, 用户会遇到"复制了却粘不上"。
      * 然后 xclip(比 xsel 更常见, 支持 -selection clipboard)。
      * 最后 xsel, 且只在有 X11 线索(DISPLAY 或 XDG_SESSION_TYPE=x11)时用,
        免得在纯 Wayland 上误用它。
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
    """文本剪辑板(子进程实现)。工具在第一次用时才探测, 方便运行时改 PATH。"""

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
                self._say("使用剪辑板工具 %s" % self._tool)
            else:
                self._say("没找到可用的剪辑板工具(装 wl-clipboard / xclip / xsel 之一)")
        return self._tool

    def refresh(self) -> None:
        """重新探测(例如用户刚装完工具)。"""
        self._probed = False
        self._tool = None
        _ = self.tool

    def available(self) -> bool:
        return self.tool is not None

    def describe(self) -> str:
        tool = self.tool
        if tool:
            return "使用 %s" % tool
        return ("没有可用的剪辑板工具: 请安装 wl-clipboard(Wayland) 或 "
                "xclip / xsel(X11), Debian 上 sudo apt install xclip wl-clipboard")

    # ------------------------------------------------------------ 读写
    def read(self) -> Optional[str]:
        tool = self.tool
        if tool is None:
            return None
        cmd = CLIPBOARD_TOOLS[tool][0]
        try:
            # Universal newlines 关掉: 剪辑板内容要保持原样(否则 \r\n 会被改写)
            proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE,
                                  timeout=CLIPBOARD_TIMEOUT,
                                  env=self._child_env())
        except FileNotFoundError:
            self._say("%s 没找到(可能刚被卸载), 本次读剪辑板跳过" % cmd[0])
            return None
        except subprocess.TimeoutExpired:
            # 最常见的原因: 没有客户端持有 selection, 工具在那儿傻等
            self._say("读剪辑板超时(%s 超过 %.1fs): 这段时间剪辑板可能没有内容"
                      % (cmd[0], CLIPBOARD_TIMEOUT))
            return None
        except OSError as exc:
            self._say("读剪辑板失败(%s): %s" % (cmd[0], exc))
            return None
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()
            self._say("读剪辑板返回 %d: %s" % (proc.returncode, err or "(无错误输出)"))
            return None
        raw = proc.stdout or b""
        if not raw:
            # 空剪辑板是正常状态, 不是错误 —— 返回空串让上层照常做哈希轮询
            return ""
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            # 剪辑板里可能有非 UTF-8 的字节(例如某些老程序写的 latin-1 文本),
            # 宁可给一个带替换字符的字符串也别整条丢掉。
            self._say("剪辑板内容不是合法 UTF-8, 已按替换字符解码")
            return raw.decode("utf-8", "replace")

    def write(self, text: str) -> None:
        tool = self.tool
        if tool is None:
            raise BackendError(
                "没有可用的剪辑板工具, 无法写剪辑板。请安装 wl-clipboard(Wayland) "
                "或 xclip / xsel(X11): Debian 上 sudo apt install xclip wl-clipboard")
        cmd = CLIPBOARD_TOOLS[tool][1]
        data = (text or "").encode("utf-8")
        try:
            proc = subprocess.run(cmd, input=data, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE,
                                  timeout=CLIPBOARD_TIMEOUT,
                                  env=self._child_env())
        except FileNotFoundError as exc:
            raise BackendError("%s 没找到: 请重新安装剪辑板工具(apt install %s)"
                               % (cmd[0], "wl-clipboard" if tool == "wl-copy" else tool)) from exc
        except subprocess.TimeoutExpired as exc:
            raise BackendError("写剪辑板超时(%s 超过 %.1fs): 工具可能在等一个不存在的"
                               "显示服务, 检查 DISPLAY/WAYLAND_DISPLAY"
                               % (cmd[0], CLIPBOARD_TIMEOUT)) from exc
        except OSError as exc:
            raise BackendError("写剪辑板失败(%s): %s" % (cmd[0], exc)) from exc
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()
            raise BackendError("写剪辑板失败(%s 返回 %d): %s"
                               % (cmd[0], proc.returncode, err or "(无错误输出)"))

    def _child_env(self) -> Optional[Dict[str, str]]:
        """子进程环境。传了自定义 env(测试)时用自定义的, 否则继承。"""
        if self._env is None:
            return None
        return dict(self._env)

    # ------------------------------------------------------------ 图片
    def supports_images(self) -> bool:
        return self.tool in IMAGE_CLIPBOARD_TOOLS

    def read_image(self, max_bytes: int = 0) -> Optional[bytes]:
        """读 image/png。剪辑板里没有图片时返回 None(工具会非 0 退出, 属正常)。"""
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
            self._say("%s 没找到, 跳过图片" % cmd[0])
            return None
        except subprocess.TimeoutExpired:
            self._say("读图片剪辑板超时(%s > %.1fs)"
                      % (cmd[0], CLIPBOARD_TIMEOUT))
            return None
        except OSError as exc:
            self._say("读图片剪辑板失败(%s): %s" % (cmd[0], exc))
            return None
        raw = proc.stdout or b""
        if proc.returncode != 0 or not raw:
            return None                     # 剪辑板里没有图片, 很常见
        if not raw.startswith(PNG_SIG):
            self._say("剪辑板里的图片不是 PNG(前 8 字节 %r), 不同步" % raw[:8])
            return None
        if max_bytes and len(raw) > max_bytes:
            self._say("图片 %d 字节超过上限 %d, 不同步" % (len(raw), max_bytes))
            return None
        return raw

    def write_image(self, png: bytes) -> None:
        tool = self.tool
        if tool is None:
            raise BackendError(
                "没有可用的剪辑板工具, 无法写图片。请安装 wl-clipboard(Wayland) "
                "或 xclip(X11): Debian 上 sudo apt install xclip wl-clipboard")
        if tool not in IMAGE_CLIPBOARD_TOOLS:
            raise BackendError(
                "%s 不支持图片剪辑板(它只能处理文本)。想收图片请装 "
                "wl-clipboard 或 xclip。" % tool)
        cmd = IMAGE_CLIPBOARD_TOOLS[tool][1]
        try:
            proc = subprocess.run(cmd, input=png, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE,
                                  timeout=CLIPBOARD_TIMEOUT,
                                  env=self._child_env())
        except FileNotFoundError as exc:
            raise BackendError("%s 没找到: 请重新安装剪辑板工具" % cmd[0]) from exc
        except subprocess.TimeoutExpired as exc:
            raise BackendError("写图片剪辑板超时(%s): 检查 DISPLAY/WAYLAND_DISPLAY"
                               % cmd[0]) from exc
        except OSError as exc:
            raise BackendError("写图片剪辑板失败(%s): %s" % (cmd[0], exc)) from exc
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()
            raise BackendError("写图片剪辑板失败(%s 返回 %d): %s"
                               % (cmd[0], proc.returncode, err or "(无错误输出)"))


class LinuxBackend(Backend):
    name = "linux"
    #: 捕获/抑制: v1 不做(见模块 docstring), 所以 Linux 只能当 client
    supports_capture = False
    supports_suppress = False
    supports_inject = True
    supports_clipboard = True

    def __init__(self, log=None, prefer: Optional[str] = None):
        super().__init__(log=log)
        #: None / "x11" / "uinput"。doctor 会用 prefer 强制指定来逐项自查。
        self.prefer = prefer or None
        self._impl = None                 # X11Injector 或 UInputInjector
        self._impl_kind = ""              # "x11" / "uinput" / ""
        self._screen: Optional[Tuple[int, int]] = None
        self._screen_source = "尚未探测"
        self._clip = _Clipboard(log=self._log)

    # ------------------------------------------------------------ 生命周期
    def prepare(self) -> None:
        """探测并打开注入通道。幂等: 已经准备好就直接返回。"""
        if self._impl is not None:
            return
        self._impl_kind = self._decide_injection()
        try:
            if self._impl_kind == "x11":
                self._impl = self._open_x11()
                # 屏幕尺寸: X11 问得到就信 X11
                self._screen, self._screen_source = self._impl.screen_size(), \
                    "X11/XTest(当前 X 屏幕)"
            else:
                # uinput 是绝对定位设备, 必须先有分辨率才能开设备
                size, src = self._screen_from_env()
                self._screen, self._screen_source = size, src
                self._impl = self._open_uinput(size)
        except BackendError:
            self._impl = None
            self._impl_kind = ""
            raise
        except Exception as exc:
            # 失败必须把半成品清掉, 否则 _impl 非空而 prepare() 又会"幂等"跳过
            self._impl = None
            self._impl_kind = ""
            raise BackendError(str(exc)) from exc

        self.log("注入方式=%s 屏幕=%s(%s)"
                 % (self._impl_kind, self._screen, self._screen_source))
        if self._impl_kind == "uinput":
            self.log("uinput 是绝对定位设备, 屏幕尺寸必须准确; 若不对请设置 "
                     "CROSSPC_SCREEN=宽x高(当前按 %dx%d 计算)后重启 client"
                     % (self._screen[0], self._screen[1]))
        # 剪辑板工具只是探测一下, 失败不影响 client 工作(注入才是主线)
        self._clip.tool

    def _decide_injection(self) -> str:
        prefer = (self.prefer or "").lower()
        if prefer in ("x11", "x", "xtest"):
            ok, why = self._x11_available()
            if not ok:
                raise BackendError("要求使用 X11 注入, 但不可用: %s" % why)
            return "x11"
        if prefer in ("uinput", "evdev"):
            ok, why = self._uinput_available()
            if not ok:
                raise BackendError("要求使用 uinput 注入, 但不可用: %s" % why)
            return "uinput"
        if prefer:
            raise BackendError("不认识的注入方式 prefer=%r(可选 x11 / uinput)" % (self.prefer,))

        # auto: X11 优先(不需要额外权限, 绝对坐标精确)
        ok, why = self._x11_available()
        if ok:
            self.log("注入策略: X11/XTest(%s)" % why)
            return "x11"
        self.log("X11 注入不可用: %s" % why)
        ok, why = self._uinput_available()
        if ok:
            self.log("注入策略: uinput(%s)" % why)
            return "uinput"
        raise BackendError(
            "Linux 上找不到可用的注入方式。要么给一个 X 会话(DISPLAY, XTest), "
            "要么让 /dev/uinput 可写(Wayland 场景)。X11: %s; uinput: %s。"
            "可以运行 tools/install_linux.sh 安装依赖并配置 udev 规则。"
            % (self._x11_why(), why))

    def _x11_available(self) -> Tuple[bool, str]:
        if not sys.platform.startswith("linux"):
            return (False, "当前平台不是 Linux(%s), X11 注入不可用" % sys.platform)
        try:
            from .linux_x11 import X11Injector
        except Exception as exc:                # pragma: no cover
            return (False, "加载 X11 注入模块失败: %s" % exc)
        try:
            return X11Injector.available()
        except Exception as exc:                # pragma: no cover - available() 不该抛
            return (False, "X11 自检异常: %s" % exc)

    def _uinput_available(self) -> Tuple[bool, str]:
        if not sys.platform.startswith("linux"):
            return (False, "当前平台不是 Linux(%s), /dev/uinput 不可用" % sys.platform)
        try:
            from .linux_uinput import UInputInjector
        except Exception as exc:                # pragma: no cover
            return (False, "加载 uinput 注入模块失败: %s" % exc)
        try:
            return UInputInjector.available()
        except Exception as exc:                # pragma: no cover - available() 不该抛
            return (False, "uinput 自检异常: %s" % exc)

    def _x11_why(self) -> str:
        """只要说明文字, 不要把可用性判断重复一遍(诊断里要能解释原因)。"""
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
        # 设备一创建就可能收到事件, 所以尺寸要在 open() 之后立刻告诉它
        inj.set_screen_size(*size)
        return inj

    def _screen_from_env(self) -> Tuple[Tuple[int, int], str]:
        return parse_screen_spec(os.environ.get("CROSSPC_SCREEN", ""))

    def close(self) -> None:
        """释放注入设备。顺序: 先按基类收尾(抬键/停捕获), 再销毁设备。

        基类的 close() 会调 set_forwarding(False), 而 Linux 后端不支持抑制,
        那里会抛 BackendError。这不算错误(Linux 端本来就没有"在转发"的状态),
        所以这里吞掉它; 但**不能**因此跳过设备销毁。
        """
        try:
            super().close()
        except BackendError as exc:
            self.log("收尾时基类报了不支持的能力(可忽略): %s" % exc)
        finally:
            impl, self._impl = self._impl, None
            self._impl_kind = ""
            if impl is not None:
                try:
                    impl.close()
                except Exception as exc:        # pragma: no cover - 关设备失败不致命
                    self.log("关闭注入设备时出错(忽略): %s" % exc)

    # ------------------------------------------------------------ 桌面几何
    def desktop_rect(self) -> Rect:
        """本机桌面(单屏矩形)。

        简化取舍: 不做 Xinerama/RandR 拼接, 直接取"整个 X 屏幕"(X11)或
        CROSSPC_SCREEN 指定的尺寸(uinput)。多显示器拼接需要 Xinerama 或
        RandR 的 ctypes 绑定, 而且 client 端本来就是"被 server 摆位置"的
        一方, 由用户在布局里填对尺寸更简单可靠。
        """
        w, h = self._screen_size()
        return Rect(0, 0, w, h)

    def _screen_size(self) -> Tuple[int, int]:
        if self._screen:
            return self._screen
        if self._impl_kind == "x11" and self._impl is not None:
            try:
                self._screen = self._impl.screen_size()
                self._screen_source = "X11/XTest(当前 X 屏幕)"
                return self._screen
            except Exception as exc:
                self.log("读 X11 屏幕尺寸失败: %s" % exc)
        if self._impl_kind == "uinput":
            size, src = self._screen_from_env()
            self._screen, self._screen_source = size, src
            return size
        # 没 prepare() 过: 用环境变量/默认值给个合理答案, 不抛异常
        size, src = self._screen_from_env()
        self._screen, self._screen_source = size, src
        return size

    def cursor(self) -> Tuple[int, int]:
        if self._impl_kind == "x11" and self._impl is not None:
            return self._impl.pointer()
        raise BackendError("uinput 注入是「只写」的: 内核不会把光标位置回读给一个"
                           "合成设备, 所以 Linux/uinput 下读不到本机光标位置")

    def set_cursor(self, x: int, y: int) -> None:
        # client 端把光标"放"到某处其实就是一次注入移动
        self.inject_motion(int(x), int(y))

    def monitors(self) -> List[Rect]:
        return [self.desktop_rect()]

    # ------------------------------------------------------------ 注入
    def _need_impl(self):
        if self._impl is None:
            raise BackendError("Linux 后端尚未 prepare()(没有可用的注入通道)")
        return self._impl

    def inject_motion(self, x: int, y: int) -> None:
        impl = self._need_impl()
        if self._impl_kind == "uinput":
            # 每次带上屏幕尺寸: uinput 模块本身不知道屏幕多大(CROSSPC_SCREEN)
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

    # ------------------------------------------------------------ 剪辑板
    def clipboard_text(self) -> Optional[str]:
        return self._clip.read()

    def set_clipboard_text(self, text: str) -> None:
        self._clip.write(text)

    @property
    def supports_clipboard_images(self) -> bool:
        """xsel 只能处理文本 target, 所以它不算支持图片。"""
        return self._clip.supports_images()

    def clipboard_image_png(self, max_bytes: int = 0) -> Optional[bytes]:
        return self._clip.read_image(max_bytes)

    def set_clipboard_image_png(self, png: bytes) -> None:
        self._clip.write_image(png)

    def clipboard_revision(self) -> Optional[object]:
        """Linux 返回 None —— 故意的。

        我们没有常驻的 X selection owner, 也就拿不到"剪辑板换人了"这类事件;
        xclip/xsel/wl-paste 都是一次性进程, 每次调用只能吐出当前内容。
        想拿"变动序号"就得自己 owned selection 并在 X 事件循环里应答, 那正是
        我们不愿意做的事(见模块 docstring)。
        所以这里退化成由上层对文本做哈希轮询 —— 每次读一次内容、算个哈希当
        版本号。代价是"读一次要 fork 一个进程", 因此上层应把轮询间隔放宽到
        0.5~1 秒, 并且要接受"两段内容哈希相同就被当成没变"的理论碰撞。
        """
        return None

    # ------------------------------------------------------------ 自检
    def probe(self) -> List[Tuple[str, bool, str]]:
        """诊断项, 全部用中文, 不抛异常。"""
        items: List[Tuple[str, bool, str]] = []

        # 1) 角色说明(v1 的边界, 放在最前面免得用户以为 Linux 能当 server)
        items.append((
            "角色",
            True,
            "Linux 端 v1 只能作为 client(注入键鼠); 捕获/抑制本机输入需要 "
            "evdev 抓取与合成器配合, 暂未实现"))

        # 2) 注入策略
        kind = self._impl_kind
        if not kind:
            x_ok, x_why = self._x11_available()
            u_ok, u_why = self._uinput_available()
            if x_ok:
                kind, why = "x11", x_why
            elif u_ok:
                kind, why = "uinput", u_why
            else:
                items.append(("注入方式", False,
                              "没有可用注入方式。X11: %s; uinput: %s" % (x_why, u_why)))
                items.append(("uinput 设备", False, u_why))
                self._append_display_and_clipboard(items)
                self._append_screen(items)
                return items
            items.append(("注入方式", True, "将使用 %s: %s" % (kind, why)))
        else:
            items.append(("注入方式", True, "已就绪: %s" % kind))

        # 3) uinput 设备
        items.append(("uinput 设备",) + self._uinput_available())

        # 4) DISPLAY/WAYLAND_DISPLAY
        self._append_display_and_clipboard(items)

        # 5) 屏幕尺寸来源
        self._append_screen(items)
        return items

    def _append_display_and_clipboard(self, items) -> None:
        display = os.environ.get("DISPLAY", "")
        wayland = os.environ.get("WAYLAND_DISPLAY", "")
        if display:
            items.append(("DISPLAY", True, "DISPLAY=%s" % display))
        else:
            items.append(("DISPLAY", False,
                          "DISPLAY 未设置(纯 Wayland 或没跑在图形会话里)"))
        if wayland:
            items.append(("WAYLAND_DISPLAY", True, "WAYLAND_DISPLAY=%s" % wayland))
        else:
            items.append(("WAYLAND_DISPLAY", True, "未设置(不是 Wayland 会话)"))

        if self._clip.available():
            items.append(("剪辑板工具", True, self._clip.describe()))
        else:
            items.append(("剪辑板工具", False, self._clip.describe()))
        if self._clip.supports_images():
            items.append(("图片剪辑板", True,
                          "%s 支持 image/png" % self._clip.tool))
        else:
            items.append(("图片剪辑板", False,
                          "当前工具不支持图片(装 wl-clipboard 或 xclip 才有)"))

        # 真读一次(windows.py 也是这么做的): "工具存在"不等于"现在读得到",
        # 例如没有客户端持有 selection、或者 DISPLAY 指向了一个连不上的 X。
        # 代价是最坏情况要等 CLIPBOARD_TIMEOUT 秒, doctor 可以接受。
        if self._clip.available():
            try:
                txt = self.clipboard_text()
            except Exception as exc:            # pragma: no cover - read() 自带兜底
                txt, detail = None, "读取异常: %s" % exc
            else:
                if txt is None:
                    detail = "读不到(检查 DISPLAY/WAYLAND_DISPLAY, 或剪辑板无内容)"
                elif not txt:
                    detail = "当前为空"
                else:
                    detail = "读到 %d 个字符" % len(txt)
            items.append(("剪辑板读取", txt is not None, detail))

    def _append_screen(self, items) -> None:
        w, h = self._screen_size()
        note = self._screen_source
        if self._impl_kind == "uinput":
            note += "; uinput 是绝对定位设备, 尺寸不对会导致落点偏移, " \
                    "请用 CROSSPC_SCREEN=宽x高 设置正确值"
        items.append(("屏幕尺寸", True, "%dx%d(%s)" % (w, h, note)))
