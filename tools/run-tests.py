#!/usr/bin/env python3
"""One-shot real-machine acceptance test: runs the checks that should run, in
order, and writes a log for every step.

Usage (either works):
    python tools/run-tests.py            # from the project root
    double-click tools/run-tests.cmd     # Windows, opens a normal console

Why a separate script instead of a handful of commands you type by hand:
  1. the checks have an **order**: environment self-check first, then the
     single-machine loopback, and only last the real keyboard and mouse;
  2. every step writes its output to a log file under logs/, so sending that
     directory afterwards is enough to locate the problem -- no manual
     screenshots needed;
  3. unsupported items are skipped automatically according to platform
     capabilities (the Linux side has no capture support, for example, so
     capturetest is not run there).

Important: **capturetest / injecttest must run in a window of your own**. A
restricted session such as an AI assistant (DSH and the like) blocks
SendInput/SetCursorPos/global hooks -- in that case these two items are bound to
fail, but that is an environment limitation, not a CrossPC problem. The script
detects this and says so.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(ROOT, "logs")

#: exit code -> meaning
CODE_MEANING = {
    0: "passed",
    1: "failed (read the log)",
    2: "not permitted by the environment (not a code problem)",
    130: "interrupted by Ctrl+C",
}


def restricted_session() -> bool:
    """Are we running in a restricted AI-assistant session (those block input injection/capture)?"""
    return any(k.upper().startswith("DSH_") for k in os.environ)


def backend_caps() -> Tuple[bool, bool, bool]:
    """(can serve, can be a client, has a clipboard). Treat everything as True if unavailable."""
    try:
        sys.path.insert(0, ROOT)
        from crosspc.backend import get_backend
        from crosspc.util import Log
        be = get_backend(Log("error"))
        return be.can_serve, be.can_be_client, be.supports_clipboard
    except Exception:
        return True, True, True


def run_step(title: str, args: List[str], hint: str = "") -> int:
    stamp = time.strftime("%H%M%S")
    base = "%s-%s" % (stamp, args[0] if args else "step")
    log_name = base + ".log"
    n = 1
    while os.path.exists(os.path.join(LOGS, log_name)):
        n += 1                                  # never overwrite each other when run twice in the same second
        log_name = "%s-%d.log" % (base, n)
    log_path = os.path.join(LOGS, log_name)
    print()
    print("=" * 72)
    print(">>> %s" % title)
    if hint:
        print("    %s" % hint)
    print("=" * 72)
    cmd = [sys.executable, "-m", "crosspc"] + args + ["--log-file", log_path]
    try:
        # inherit stdout directly: no pipe, so it still starts in a restricted environment
        code = subprocess.call(cmd, cwd=ROOT)
    except KeyboardInterrupt:
        code = 130
    except OSError as exc:
        print("Failed to start: %s" % exc)
        code = 1
    print("<<< %s: %s (log %s)"
          % (title, CODE_MEANING.get(code, "exit code %d" % code),
             os.path.relpath(log_path, ROOT)))
    return code


def main() -> int:
    try:
        os.makedirs(LOGS, exist_ok=True)
    except OSError as exc:
        print("Cannot create the log directory %s: %s" % (LOGS, exc))
        return 1

    print("CrossPC one-shot acceptance test")
    print("Project directory: %s" % ROOT)
    print("Python  : %s (%s)" % (sys.executable,
                                 ".".join(str(v) for v in sys.version_info[:3])))
    if restricted_session():
        print()
        print("!! DSH_* environment variables detected: you are running inside a")
        print("!! restricted AI-assistant session. Such a session blocks input")
        print("!! injection and global hooks, so injecttest/capturetest are bound to")
        print("!! fail (environment limitation, not a code problem). Close this")
        print("!! window and run it your own way instead: double-click")
        print("!! tools\\run-tests.cmd, or run python tools\\run-tests.py in a")
        print("!! normal PowerShell window.")
    print()
    print("Note: no network access is needed anywhere; only the 5 seconds of")
    print("      capturetest --takeover hand your local keyboard and mouse to")
    print("      CrossPC (press Ctrl+Alt+F12 to take them back immediately).")

    results: List[Tuple[str, int]] = []
    can_serve, can_be_client, has_clipboard = backend_caps()

    results.append(("environment self-check doctor",
                    run_step("environment self-check", ["doctor"],
                             "see whether displays/hooks/injection/clipboard/port have a failing item")))
    results.append(("single-machine loopback selftest",
                    run_step("single-machine loopback self-test", ["selftest"],
                             "runs the whole chain on the fake backend without touching the real keyboard or mouse (22 items)")))

    if has_clipboard:
        results.append(("clipboard clipboardtest",
                        run_step("real-machine clipboard acceptance test", ["clipboardtest"],
                                 "briefly changes your clipboard and restores it when done")))
    if can_be_client:
        results.append(("injection injecttest",
                        run_step("real-machine injection acceptance test", ["injecttest"],
                                 "injects a few harmless keys and reads the system state back")))
    if can_serve:
        results.append(("capture capturetest",
                        run_step("real-machine capture acceptance test",
                                 ["capturetest", "--seconds", "5"],
                                 "for these 5 seconds, move the mouse around and type a few keys")))
        print()
        print("Next comes the optional takeover test: for 5 seconds your keyboard")
        print("and mouse are handed to CrossPC (your local keyboard and mouse stop")
        print("working for that moment -- which is exactly the feature under test).")
        try:
            answer = input("Run the takeover test now? [y/N] ").strip().lower()
        except EOFError:
            answer = "n"
        if answer in ("y", "yes", "1"):
            results.append(("takeover capturetest --takeover",
                            run_step("real-machine takeover acceptance test",
                                     ["capturetest", "--seconds", "5",
                                      "--takeover"],
                                     "the keyboard and mouse now belong to CrossPC; press Ctrl+Alt+F12 to take them back immediately")))
        else:
            print("Takeover test skipped (you can run it yourself at any time: "
                  "python -m crosspc capturetest --takeover)")

    print()
    print("=" * 72)
    print("acceptance summary")
    print("=" * 72)
    bad = 0
    for name, code in results:
        flag = "OK  " if code == 0 else ("skip" if code == 2 else "warn")
        if code not in (0, 2):
            bad += 1
        print("  [%s] %-28s %s" % (flag, name,
                                   CODE_MEANING.get(code, "exit code %d" % code)))
    print()
    print("All logs are in: %s" % LOGS)
    if bad:
        print("%d item(s) did not pass. Send the whole logs directory (or have an"
              % bad)
        print("AI assistant read it); it holds the full output of every step, which")
        print("is enough to locate the problem.")
        return 1
    print("No failures.")
    if restricted_session():
        print("Note: you ran inside a restricted session, so the injection and "
              "capture results do not count; please run it again in a window you "
              "opened yourself.")
    else:
        print("The next step is bringing the two machines together:")
        print("  Windows: python -m crosspc server --log-file logs\\server.log")
        print("  Debian : python3 -m crosspc client --host <Windows IP> "
              "--log-file logs/client.log")
    return 0


if __name__ == "__main__":
    sys.exit(main())
