"""命令行入口: crosspc server / client / gui / doctor / discover / selftest / init。"""
from __future__ import annotations

import argparse
import os
import socket
import sys
from typing import List, Optional

from . import __version__
from .backend import BackendError, get_backend, platform_summary
from .config import (DEFAULT_DISCOVERY_PORT, DEFAULT_PORT, CONFIG_VERSION,
                     Config, ConfigError, default_config_path, user_config_dir)
from .hotkey import HotkeyError, make_hotkeys
from .util import Log


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="crosspc",
        description="CrossPC —— 局域网内共享一套鼠标键盘(Windows/Linux)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""常见用法:
  # Windows(接键鼠的那台, server)
  python -m crosspc init                 # 生成 crosspc.json
  python -m crosspc gui                  # 图形界面拖出两台机器的相对位置
  python -m crosspc server               # 启动 server

  # Debian(没键鼠的那台, client)
  python3 -m crosspc client --host 192.168.1.10
  python3 -m crosspc client              # 不带 --host 时自动发现

  # 排查
  python -m crosspc doctor               # 环境自检
  python -m crosspc selftest             # 单机回环自测(不碰真实键鼠)
""")
    p.add_argument("--version", action="version", version="CrossPC %s" % __version__)
    sub = p.add_subparsers(dest="command", metavar="命令")

    def common(sp: argparse.ArgumentParser, with_port: bool = True) -> None:
        sp.add_argument("-c", "--config", help="配置文件路径(默认 ./crosspc.json)")
        sp.add_argument("--token", help="共享口令, 两端必须一致")
        if with_port:
            sp.add_argument("--port", type=int, help="端口(默认 %d)" % DEFAULT_PORT)
        sp.add_argument("--backend", help="强制后端: windows / x11 / uinput / fake")
        sp.add_argument("--log-level", default=None,
                        choices=["debug", "info", "warn", "error"])
        sp.add_argument("--log-file", help="把日志同时写到这个文件(UTF-8)")
        sp.add_argument("--debug-events", action="store_true",
                        help="打印每个输入事件(排查用, 会明显增加延迟)")

    sp = sub.add_parser("server", help="以 server 身份运行(接键鼠的那台)")
    common(sp)
    sp.add_argument("--bind", help="监听地址(默认 0.0.0.0)")
    sp.add_argument("--stats", action="store_true", help="每 10 秒打印一次统计")
    sp.add_argument("--dry-run", action="store_true",
                    help="不安装键鼠钩子, 只验证网络/握手")

    sp = sub.add_parser("client", help="以 client 身份运行(没键鼠的那台)")
    common(sp)
    sp.add_argument("--host", help="server 的 IP(留空则 UDP 自动发现)")
    sp.add_argument("--once", action="store_true", help="连不上/断开后就退出, 不重连")
    sp.add_argument("--no-clipboard", action="store_true", help="不同步剪辑板")

    sp = sub.add_parser("gui", help="图形界面设置两台机器的相对位置")
    common(sp, with_port=False)

    sp = sub.add_parser("doctor", help="环境自检(安全, 不会接管键鼠)")
    common(sp)

    sp = sub.add_parser("discover", help="在局域网里找 CrossPC server")
    sp.add_argument("--timeout", type=float, default=3.0)
    sp.add_argument("--discovery-port", type=int, default=DEFAULT_DISCOVERY_PORT)

    sp = sub.add_parser("selftest", help="单机回环自测(用假后端, 不碰真实键鼠)")
    sp.add_argument("--keep-alive", action="store_true",
                    help="自测环境保持运行, 便于用另一台机器手工连")
    sp.add_argument("--log-level", default="info",
                    choices=["debug", "info", "warn", "error"])
    sp.add_argument("--log-file", help="把日志同时写到这个文件(UTF-8)")

    sp = sub.add_parser("capturetest",
                        help="真机输入验收: 抓 N 秒键鼠并统计(可选真接管)")
    sp.add_argument("--seconds", type=float, default=5.0, help="抓多久(秒)")
    sp.add_argument("--takeover", action="store_true",
                    help="真的接管(这段时间本机键鼠不生效), 用来验证吞输入")
    sp.add_argument("--backend", help="强制后端: windows / x11 / uinput")
    sp.add_argument("--log-level", default="info",
                    choices=["debug", "info", "warn", "error"])
    sp.add_argument("--log-file", help="把日志同时写到这个文件(UTF-8)")

    sp = sub.add_parser("injecttest",
                        help="真机注入验收: 注入无副作用的键并回读系统状态")
    sp.add_argument("--window", action="store_true",
                    help="连「往自建窗口真打字」一起验证(会短暂抢焦点)")
    sp.add_argument("--backend", help="强制后端: windows / x11 / uinput")
    sp.add_argument("--log-level", default="info",
                    choices=["debug", "info", "warn", "error"])
    sp.add_argument("--log-file", help="把日志同时写到这个文件(UTF-8)")

    sp = sub.add_parser("clipboardtest",
                        help="真机剪辑板验收: 文本与图片写入后读回比对(会临时改剪辑板)")
    sp.add_argument("--backend", help="强制后端: windows / x11 / uinput")
    sp.add_argument("--log-level", default="info",
                    choices=["debug", "info", "warn", "error"])
    sp.add_argument("--log-file", help="把日志同时写到这个文件(UTF-8)")

    sp = sub.add_parser("init", help="生成一份配置文件")
    sp.add_argument("-c", "--config", help="写到哪里(默认 ./crosspc.json)")
    sp.add_argument("--force", action="store_true", help="已存在也覆盖")
    sp.add_argument("--client-name", default="debian", help="预置的 client 名字")
    sp.add_argument("--client-host", default="", help="预置的 client IP(可留空)")
    sp.add_argument("--token", default="", help="共享口令(留空表示不校验)")

    return p


def main(argv: Optional[List[str]] = None) -> int:
    _fix_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    try:
        if args.command == "init":
            return cmd_init(args)
        if args.command == "discover":
            return cmd_discover(args)
        if args.command == "selftest":
            from .selftest import run_selftest
            code = run_selftest(Log(args.log_level,
                                    file_path=getattr(args, "log_file", None)),
                                keep_alive=args.keep_alive)
            return code
        if args.command == "capturetest":
            from .selftest import run_capture_test
            return run_capture_test(Log(args.log_level,
                                        file_path=getattr(args, "log_file", None)),
                                    seconds=args.seconds,
                                    takeover=args.takeover,
                                    backend_name=args.backend)
        if args.command == "injecttest":
            from .injecttest import run_inject_test
            return run_inject_test(Log(args.log_level,
                                       file_path=getattr(args, "log_file", None)),
                                   backend_name=args.backend,
                                   with_window=args.window)
        if args.command == "clipboardtest":
            from .selftest import run_clipboard_test
            return run_clipboard_test(Log(args.log_level,
                                          file_path=getattr(args, "log_file", None)),
                                      backend_name=args.backend)
        if args.command == "doctor":
            return cmd_doctor(args)
        if args.command == "gui":
            from .gui import run_gui
            return run_gui(args)
        if args.command == "server":
            return cmd_server(args)
        if args.command == "client":
            return cmd_client(args)
    except KeyboardInterrupt:
        print("\n已中断")
        return 130
    except (ConfigError, BackendError, HotkeyError) as exc:
        print("错误: %s" % exc, file=sys.stderr)
        return 1
    except Exception as exc:                       # pragma: no cover
        import traceback
        print("未预料的错误: %s" % exc, file=sys.stderr)
        traceback.print_exc()
        return 1
    parser.print_help()
    return 2


# ------------------------------------------------------------------ 子命令
def load_config(args) -> Config:
    path = args.config or default_config_path()
    cfg = Config.load(path)
    if getattr(args, "port", None):
        cfg.port = int(args.port)
    if getattr(args, "token", None) is not None and getattr(args, "token", ""):
        cfg.token = args.token
    if getattr(args, "bind", None):
        cfg.bind = args.bind
    if getattr(args, "host", None):
        cfg.server_host = args.host
    if getattr(args, "log_level", None):
        cfg.log_level = args.log_level
    if getattr(args, "debug_events", False):
        cfg.debug_events = True
    return cfg


def make_log(cfg: Config, args) -> Log:
    level = getattr(args, "log_level", None) or cfg.log_level or "info"
    debug = bool(getattr(args, "debug_events", False) or cfg.debug_events)
    return Log(level, debug_events=debug,
               file_path=getattr(args, "log_file", None))


def cmd_server(args) -> int:
    from .server import ServerApp
    cfg = load_config(args)
    log = make_log(cfg, args)
    log.info("CrossPC %s | %s" % (__version__, platform_summary()))
    if not cfg.path:
        cfg.path = default_config_path()
    backend = get_backend(log, prefer=args.backend)
    if not backend.can_serve:
        raise BackendError(
            "%s 后端不能作为 server(需要捕获并抑制本机输入)。\n"
            "  Linux 端目前只能当 client; 接键鼠的那台请用 Windows,"
            " 或者把键鼠插到 Windows 上。" % backend.name)
    app = ServerApp(cfg, backend, log, port=args.port, bind=args.bind,
                    stats=args.stats, dry_run=args.dry_run)
    return app.run()


def cmd_client(args) -> int:
    from .client import ClientApp
    cfg = load_config(args)
    log = make_log(cfg, args)
    log.info("CrossPC %s | %s" % (__version__, platform_summary()))
    backend = get_backend(log, prefer=args.backend)
    if not backend.can_be_client:
        raise BackendError("%s 后端不支持注入输入" % backend.name)
    app = ClientApp(cfg, backend, log, host=args.host, port=args.port,
                    once=args.once, no_clipboard=args.no_clipboard)
    return app.run()


def cmd_discover(args) -> int:
    from .net import discover
    print("正在广播查找 CrossPC server(UDP %d, %.1f 秒)..."
          % (args.discovery_port, args.timeout))
    found = discover(args.timeout, args.discovery_port)
    if not found:
        print("没有找到。请确认: 1) server 已启动 2) 两台机器在同一网段 "
              "3) 防火墙放行了 UDP %d" % args.discovery_port)
        return 1
    for item in found:
        print("发现 server 「%s」 地址 %s:%s (协议 %s)"
              % (item.get("name"), item.get("address"), item.get("port"),
                 item.get("protocol")))
    return 0


def cmd_init(args) -> int:
    path = args.config or os.path.join(os.getcwd(), "crosspc.json")
    if os.path.exists(path) and not args.force:
        print("配置文件已存在: %s (要覆盖请加 --force)" % path)
        return 1
    cfg = Config.defaults(path)
    from .config import ClientEntry
    cfg.token = args.token or ""
    cfg.clients = [ClientEntry(name=args.client_name, host=args.client_host)]
    backend = None
    try:
        backend = get_backend()
        backend.prepare()
        r = backend.desktop_rect()
        cfg.server_screen = (r.w, r.h)
    except Exception as exc:
        print("提示: 未能探测本机分辨率(%s), 稍后可在界面里手动填写" % exc)
    written = cfg.save(path)
    print("已生成配置: %s" % written)
    if cfg.server_screen:
        print("本机(server)分辨率: %dx%d" % cfg.server_screen)
    print("""
下一步:
  1) 在这台机器上运行:  python -m crosspc gui      (拖出两台机器的相对位置)
  2) 启动 server:        python -m crosspc server
  3) 在 client 上运行:   python3 -m crosspc client --host <本机IP>
""")
    from .net import local_ipv4_addresses
    ips = local_ipv4_addresses()
    if ips:
        print("本机可能的局域网地址: %s" % ", ".join(ips))
    return 0


def cmd_doctor(args) -> int:
    from .net import local_ipv4_addresses
    # doctor 的输出是"报告"而不是"日志": 屏幕上不要时间戳, 但 --log-file 也要留一份,
    # 这样用户把 logs 目录发出来时, 最关键的环境结论一定在里面。
    report = Log("info", file_path=getattr(args, "log_file", None))
    say = report.plain
    say("CrossPC %s 环境自检" % __version__)
    say("=" * 62)
    say("平台      %s" % platform_summary())
    cfg = None
    try:
        cfg = load_config(args)
        exists = "存在" if cfg.path and os.path.exists(cfg.path) else "不存在(用默认值)"
        say("配置      %s [%s]" % (cfg.path, exists))
    except ConfigError as exc:
        say("配置      !! 读取失败: %s" % exc)
        cfg = Config.defaults()
    ips = local_ipv4_addresses()
    say("本机地址  %s" % (", ".join(ips) if ips else "没检测到局域网地址"))
    say("用户配置目录 %s" % user_config_dir())

    backend = None
    try:
        backend = get_backend(Log("info"), prefer=args.backend)
        say("后端      %s" % backend.caps())
        say("-" * 62)
        results = backend.probe()
        if not results:
            say("(该后端没有提供自检项)")
        width = max((len(r[0]) for r in results), default=0)
        for name, ok, detail in results:
            say("[%s] %-*s %s" % ("通过" if ok else "失败", width, name, detail))
    except BackendError as exc:
        say("后端      !! %s" % exc)
    except Exception as exc:
        say("后端      !! 初始化异常: %s" % exc)

    say("-" * 62)
    # 端口可用性
    if backend is not None and backend.can_serve:
        port = int(getattr(args, "port", None) or (cfg.port if cfg else DEFAULT_PORT))
        s = socket.socket()
        try:
            s.bind(("0.0.0.0", port))
            say("[通过] 端口 %-6d 空闲, server 可以监听" % port)
        except OSError as exc:
            say("[警告] 端口 %-6d 被占用或没权限: %s" % (port, exc))
        finally:
            s.close()
    # 热键
    if cfg is not None:
        try:
            panic, lock = make_hotkeys(cfg.hotkey_panic, cfg.hotkey_lock)
            say("[通过] 热键      紧急收回=%s 锁定=%s" % (panic or "未设",
                                                         lock or "未设"))
        except HotkeyError as exc:
            say("[失败] 热键      %s" % exc)
        if cfg.clients:
            say("       client     %s" % ", ".join(
                "%s@%s%s" % (c.name, c.host or "?", c.rect or " 自动位置")
                for c in cfg.clients))
        else:
            say("       client     配置里还没有, 首次连接会自动登记")
    # 自动发现
    try:
        from .net import discover
        found = discover(1.5, cfg.discovery_port if cfg else DEFAULT_DISCOVERY_PORT)
        if found:
            say("[通过] 发现       %s" % ", ".join(
                "%s@%s:%s" % (f.get("name"), f.get("address"), f.get("port"))
                for f in found))
        else:
            say("[提示] 发现       局域网内没有其它 CrossPC server(正常, "
                "如果你就在 server 上)")
    except Exception as exc:
        say("[提示] 发现       跳过(%s)" % exc)
    say("-" * 62)
    say("""下一步建议:
  * 若"键鼠钩子"一项失败: 关掉杀软/输入法里的"按键保护", 或换管理员权限运行。
  * 若端口被占用: 换端口并保证两端一致(--port)。
  * 若 client 连不上: 在 Windows 上放行入站 TCP %d:
      netsh advfirewall firewall add rule name="CrossPC" dir=in action=allow ^
        protocol=TCP localport=%d
    (需要管理员权限的命令提示符)""" % (
        cfg.port if cfg else DEFAULT_PORT, cfg.port if cfg else DEFAULT_PORT))
    if getattr(args, "log_file", None):
        say("(本报告已写入 %s)" % args.log_file)
    report.close()
    return 0


def _fix_stdout() -> None:
    """Windows 控制台默认可能不是 UTF-8, 中文会变乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore
        except Exception:
            pass
