"""Command-line entry point: crosspc server / client / gui / doctor / discover / selftest / init."""
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
        description="CrossPC - share one keyboard and mouse over the LAN "
                    "(Windows/Linux)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Common usage:
  # Windows (the machine with the keyboard and mouse, server)
  python -m crosspc init                 # generate crosspc.json
  python -m crosspc gui                  # drag the two machines into their relative positions
  python -m crosspc server               # start the server

  # Debian (the machine without the keyboard and mouse, client)
  python3 -m crosspc client --host 192.168.1.10
  python3 -m crosspc client              # auto-discovery when --host is omitted

  # Troubleshooting
  python -m crosspc doctor               # environment self-check
  python -m crosspc selftest             # loopback self-test on one machine (never touches the real keyboard/mouse)
""")
    p.add_argument("--version", action="version", version="CrossPC %s" % __version__)
    sub = p.add_subparsers(dest="command", metavar="command")

    def common(sp: argparse.ArgumentParser, with_port: bool = True) -> None:
        sp.add_argument("-c", "--config", help="path to the config file (default ./crosspc.json)")
        sp.add_argument("--token", help="shared secret, the two ends must match")
        if with_port:
            sp.add_argument("--port", type=int, help="port (default %d)" % DEFAULT_PORT)
        sp.add_argument("--backend", help="force a backend: windows / x11 / uinput / fake")
        sp.add_argument("--log-level", default=None,
                        choices=["debug", "info", "warn", "error"])
        sp.add_argument("--log-file", help="also write the log to this file (UTF-8)")
        sp.add_argument("--debug-events", action="store_true",
                        help="print every input event (for troubleshooting, adds noticeable latency)")

    sp = sub.add_parser("server", help="run as the server (the machine with the keyboard and mouse)")
    common(sp)
    sp.add_argument("--bind", help="listen address (default 0.0.0.0)")
    sp.add_argument("--stats", action="store_true", help="print statistics every 10 seconds")
    sp.add_argument("--dry-run", action="store_true",
                    help="do not install the keyboard/mouse hooks, only verify the network/handshake")

    sp = sub.add_parser("client", help="run as the client (the machine without the keyboard and mouse)")
    common(sp)
    sp.add_argument("--host", help="the server's IP (empty means UDP auto-discovery)")
    sp.add_argument("--once", action="store_true",
                    help="exit after a connect failure or a disconnect instead of reconnecting")
    sp.add_argument("--no-clipboard", action="store_true", help="do not synchronize the clipboard")

    sp = sub.add_parser("gui", help="graphical setup of the two machines' relative positions")
    common(sp, with_port=False)

    sp = sub.add_parser("doctor", help="environment self-check (safe, never takes over the keyboard/mouse)")
    common(sp)

    sp = sub.add_parser("discover", help="find CrossPC servers on the LAN")
    sp.add_argument("--timeout", type=float, default=3.0)
    sp.add_argument("--discovery-port", type=int, default=DEFAULT_DISCOVERY_PORT)

    sp = sub.add_parser("selftest",
                        help="loopback self-test on one machine (uses the fake backend, never touches the real keyboard/mouse)")
    sp.add_argument("--keep-alive", action="store_true",
                    help="keep the self-test environment running so another machine can connect by hand")
    sp.add_argument("--log-level", default="info",
                    choices=["debug", "info", "warn", "error"])
    sp.add_argument("--log-file", help="also write the log to this file (UTF-8)")

    sp = sub.add_parser("capturetest",
                        help="real-machine input acceptance test: capture keyboard/mouse input for N seconds and report statistics (optional real takeover)")
    sp.add_argument("--seconds", type=float, default=5.0, help="how long to capture (seconds)")
    sp.add_argument("--takeover", action="store_true",
                    help="really take over (the local keyboard/mouse stop working during this time), used to verify input suppression")
    sp.add_argument("--backend", help="force a backend: windows / x11 / uinput")
    sp.add_argument("--log-level", default="info",
                    choices=["debug", "info", "warn", "error"])
    sp.add_argument("--log-file", help="also write the log to this file (UTF-8)")

    sp = sub.add_parser("injecttest",
                        help="real-machine injection acceptance test: inject harmless keys and read the system state back")
    sp.add_argument("--window", action="store_true",
                    help="also verify really typing into a window the test creates (briefly steals focus)")
    sp.add_argument("--backend", help="force a backend: windows / x11 / uinput")
    sp.add_argument("--log-level", default="info",
                    choices=["debug", "info", "warn", "error"])
    sp.add_argument("--log-file", help="also write the log to this file (UTF-8)")

    sp = sub.add_parser("clipboardtest",
                        help="real-machine clipboard acceptance test: write text and an image, then read them back and compare (temporarily changes the clipboard)")
    sp.add_argument("--backend", help="force a backend: windows / x11 / uinput")
    sp.add_argument("--log-level", default="info",
                    choices=["debug", "info", "warn", "error"])
    sp.add_argument("--log-file", help="also write the log to this file (UTF-8)")

    sp = sub.add_parser("init", help="generate a config file")
    sp.add_argument("-c", "--config", help="where to write it (default ./crosspc.json)")
    sp.add_argument("--force", action="store_true", help="overwrite it even if it already exists")
    sp.add_argument("--client-name", default="debian", help="preset client name")
    sp.add_argument("--client-host", default="", help="preset client IP (may be left empty)")
    sp.add_argument("--token", default="", help="shared secret (empty means no verification)")

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
        print("\nInterrupted")
        return 130
    except (ConfigError, BackendError, HotkeyError) as exc:
        print("Error: %s" % exc, file=sys.stderr)
        return 1
    except Exception as exc:                       # pragma: no cover
        import traceback
        print("Unexpected error: %s" % exc, file=sys.stderr)
        traceback.print_exc()
        return 1
    parser.print_help()
    return 2


# ------------------------------------------------------------------ subcommands
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
            "%s backend cannot act as the server (it must capture and suppress "
            "local input).\n"
            "  The Linux side can currently only be a client; use Windows for "
            "the machine with the keyboard and mouse, or plug the keyboard and "
            "mouse into the Windows machine." % backend.name)
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
        raise BackendError("%s backend does not support injecting input"
                           % backend.name)
    app = ClientApp(cfg, backend, log, host=args.host, port=args.port,
                    once=args.once, no_clipboard=args.no_clipboard)
    return app.run()


def cmd_discover(args) -> int:
    from .net import discover
    print("Broadcasting to find CrossPC servers (UDP %d, %.1f s)..."
          % (args.discovery_port, args.timeout))
    found = discover(args.timeout, args.discovery_port)
    if not found:
        print("Nothing found. Please check: 1) the server is running 2) both "
              "machines are on the same subnet 3) the firewall allows UDP %d"
              % args.discovery_port)
        return 1
    for item in found:
        print("Found server \"%s\" at %s:%s (protocol %s)"
              % (item.get("name"), item.get("address"), item.get("port"),
                 item.get("protocol")))
    return 0


def cmd_init(args) -> int:
    path = args.config or os.path.join(os.getcwd(), "crosspc.json")
    if os.path.exists(path) and not args.force:
        print("The config file already exists: %s (pass --force to overwrite)"
              % path)
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
        print("Note: could not detect the local screen resolution (%s), you can "
              "fill it in later in the GUI" % exc)
    written = cfg.save(path)
    print("Config generated: %s" % written)
    if cfg.server_screen:
        print("This machine (server) resolution: %dx%d" % cfg.server_screen)
    print("""
Next steps:
  1) On this machine run:  python -m crosspc gui      (drag the two machines into their relative positions)
  2) Start the server:     python -m crosspc server
  3) On the client run:    python3 -m crosspc client --host <this machine's IP>
""")
    from .net import local_ipv4_addresses
    ips = local_ipv4_addresses()
    if ips:
        print("Possible LAN addresses of this machine: %s" % ", ".join(ips))
    return 0


def cmd_doctor(args) -> int:
    from .net import local_ipv4_addresses
    # The doctor output is a "report" rather than a "log": no timestamps on
    # screen, but --log-file still gets a copy so that when the user sends in
    # the logs directory, the crucial environment conclusions are always in it.
    report = Log("info", file_path=getattr(args, "log_file", None))
    say = report.plain
    say("CrossPC %s environment self-check" % __version__)
    say("=" * 62)
    say("Platform   %s" % platform_summary())
    cfg = None
    try:
        cfg = load_config(args)
        exists = ("present" if cfg.path and os.path.exists(cfg.path)
                  else "missing (using defaults)")
        say("Config     %s [%s]" % (cfg.path, exists))
    except ConfigError as exc:
        say("Config     !! could not be read: %s" % exc)
        cfg = Config.defaults()
    ips = local_ipv4_addresses()
    say("Addresses  %s" % (", ".join(ips) if ips else "no LAN address detected"))
    say("User config %s" % user_config_dir())

    backend = None
    try:
        backend = get_backend(Log("info"), prefer=args.backend)
        say("Backend    %s" % backend.caps())
        say("-" * 62)
        results = backend.probe()
        if not results:
            say("(this backend provides no self-check items)")
        width = max((len(r[0]) for r in results), default=0)
        for name, ok, detail in results:
            say("[%s] %-*s %s" % ("PASS" if ok else "FAIL", width, name, detail))
    except BackendError as exc:
        say("Backend    !! %s" % exc)
    except Exception as exc:
        say("Backend    !! initialization threw: %s" % exc)

    say("-" * 62)
    # port availability
    if backend is not None and backend.can_serve:
        port = int(getattr(args, "port", None) or (cfg.port if cfg else DEFAULT_PORT))
        s = socket.socket()
        try:
            s.bind(("0.0.0.0", port))
            say("[PASS] port %-6d free, the server can listen on it" % port)
        except OSError as exc:
            say("[WARN] port %-6d is in use or not permitted: %s" % (port, exc))
        finally:
            s.close()
    # hotkeys
    if cfg is not None:
        try:
            panic, lock = make_hotkeys(cfg.hotkey_panic, cfg.hotkey_lock)
            say("[PASS] hotkey     panic=%s lock=%s"
                % (panic or "unset", lock or "unset"))
        except HotkeyError as exc:
            say("[FAIL] hotkey     %s" % exc)
        if cfg.clients:
            say("       client     %s" % ", ".join(
                "%s@%s%s" % (c.name, c.host or "?", c.rect or " auto position")
                for c in cfg.clients))
        else:
            say("       client     none in the config yet, the first connection "
                "registers automatically")
    # auto-discovery
    try:
        from .net import discover
        found = discover(1.5, cfg.discovery_port if cfg else DEFAULT_DISCOVERY_PORT)
        if found:
            say("[PASS] discovery  %s" % ", ".join(
                "%s@%s:%s" % (f.get("name"), f.get("address"), f.get("port"))
                for f in found))
        else:
            say("[INFO] discovery  no other CrossPC server on the LAN (normal "
                "if you are on the server itself)")
    except Exception as exc:
        say("[INFO] discovery  skipped (%s)" % exc)
    say("-" * 62)
    say("""Suggested next steps:
  * If the "keyboard/mouse hook" item fails: turn off "key protection" in your
    antivirus or IME, or run with administrator rights.
  * If the port is in use: switch ports and keep both ends in sync (--port).
  * If the client cannot connect: allow inbound TCP %d on Windows:
      netsh advfirewall firewall add rule name="CrossPC" dir=in action=allow ^
        protocol=TCP localport=%d
    (needs an administrator command prompt)""" % (
        cfg.port if cfg else DEFAULT_PORT, cfg.port if cfg else DEFAULT_PORT))
    if getattr(args, "log_file", None):
        say("(this report was also written to %s)" % args.log_file)
    report.close()
    return 0


def _fix_stdout() -> None:
    """The Windows console is not necessarily UTF-8 by default, which would
    turn non-ASCII text into mojibake."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore
        except Exception:
            pass
