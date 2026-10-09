#!/usr/bin/env python3
"""一键真机验收: 把该跑的检查按顺序跑一遍, 每一步都落一份日志。

用法(两种都可以):
    python tools/run-tests.py            # 在项目根目录
    double-click tools/run-tests.cmd     # Windows, 会自己开一个正常窗口

为什么要单独写脚本而不是让你手敲几条命令:
  1. 这些检查有**顺序**: 先环境自检, 再单机回环, 最后才动真键鼠;
  2. 每一步的输出都会写进 logs/ 下的日志文件, 跑完把这个目录发出来就能定位
     问题, 不用你人肉截图;
  3. 会按平台能力自动跳过不支持的项(例如 Linux 端没有捕获能力, 就不跑
     capturetest)。

重要: **capturetest / injecttest 必须在你自己的窗口里跑**。AI 助手(DSH 之类)的
受限会话会屏蔽 SendInput/SetCursorPos/全局钩子 —— 那种情况下这两项必然失败,
但那是环境限制, 不是 CrossPC 的问题。脚本会检测并提示。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(ROOT, "logs")

#: exit code -> 说明
CODE_MEANING = {
    0: "通过",
    1: "失败(要看日志)",
    2: "环境不允许(不是代码问题)",
    130: "被 Ctrl+C 中断",
}


def restricted_session() -> bool:
    """是不是跑在 AI 助手的受限会话里(那种会话会屏蔽输入注入/捕获)。"""
    return any(k.upper().startswith("DSH_") for k in os.environ)


def backend_caps() -> Tuple[bool, bool, bool]:
    """(能当 server, 能当 client, 有剪辑板)。拿不到就当作全 True。"""
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
        n += 1                                  # 同一秒内跑两次也不要互相覆盖
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
        # stdout 直接继承: 不走管道, 这样即使在受限环境里也能跑起来
        code = subprocess.call(cmd, cwd=ROOT)
    except KeyboardInterrupt:
        code = 130
    except OSError as exc:
        print("启动失败: %s" % exc)
        code = 1
    print("<<< %s: %s (日志 %s)"
          % (title, CODE_MEANING.get(code, "退出码 %d" % code),
             os.path.relpath(log_path, ROOT)))
    return code


def main() -> int:
    try:
        os.makedirs(LOGS, exist_ok=True)
    except OSError as exc:
        print("建不了日志目录 %s: %s" % (LOGS, exc))
        return 1

    print("CrossPC 一键验收")
    print("项目目录: %s" % ROOT)
    print("Python  : %s (%s)" % (sys.executable,
                                 ".".join(str(v) for v in sys.version_info[:3])))
    if restricted_session():
        print()
        print("!! 检测到 DSH_* 环境变量: 你现在是在 AI 助手的受限会话里运行。")
        print("!! 这种会话会屏蔽输入注入与全局钩子, 所以 injecttest/capturetest")
        print("!! 一定会失败(环境限制, 不是代码问题)。请关掉这个窗口, 用你自己")
        print("!! 打开的方式跑: 双击 tools\\run-tests.cmd, 或在普通 PowerShell 里")
        print("!! 执行 python tools\\run-tests.py")
    print()
    print("提示: 全程不需要联网; 只有 capturetest --takeover 那 5 秒内本机键鼠")
    print("      会交给 CrossPC(按 Ctrl+Alt+F12 可立即收回)。")

    results: List[Tuple[str, int]] = []
    can_serve, can_be_client, has_clipboard = backend_caps()

    results.append(("环境自检 doctor",
                    run_step("环境自检", ["doctor"],
                             "看显示器/钩子/注入/剪辑板/端口有没有不通过的项")))
    results.append(("单机回环 selftest",
                    run_step("单机回环自测", ["selftest"],
                             "用假后端跑完整链路, 不碰真实键鼠(22 项)")))

    if has_clipboard:
        results.append(("剪辑板 clipboardtest",
                        run_step("真机剪辑板验收", ["clipboardtest"],
                                 "会临时改一下你的剪辑板, 测完自动还原")))
    if can_be_client:
        results.append(("注入 injecttest",
                        run_step("真机注入验收", ["injecttest"],
                                 "注入几个无副作用的键并回读系统状态")))
    if can_serve:
        results.append(("捕获 capturetest",
                        run_step("真机捕获验收",
                                 ["capturetest", "--seconds", "5"],
                                 "这 5 秒里请随便动动鼠标、敲几下键盘")))
        print()
        print("接下来是可选的「接管」测试: 5 秒内你的键鼠会交给 CrossPC 处理")
        print("(本机键鼠暂时不生效 —— 这正是要被验证的功能)。")
        try:
            answer = input("现在做接管测试吗? [y/N] ").strip().lower()
        except EOFError:
            answer = "n"
        if answer in ("y", "yes", "是", "1"):
            results.append(("接管 capturetest --takeover",
                            run_step("真机接管验收",
                                     ["capturetest", "--seconds", "5",
                                      "--takeover"],
                                     "现在键鼠归 CrossPC; 按 Ctrl+Alt+F12 立即收回")))
        else:
            print("已跳过接管测试(随时可以自己跑: "
                  "python -m crosspc capturetest --takeover)")

    print()
    print("=" * 72)
    print("验收汇总")
    print("=" * 72)
    bad = 0
    for name, code in results:
        flag = "OK  " if code == 0 else ("跳过" if code == 2 else "注意")
        if code not in (0, 2):
            bad += 1
        print("  [%s] %-28s %s" % (flag, name,
                                   CODE_MEANING.get(code, "退出码 %d" % code)))
    print()
    print("日志都在: %s" % LOGS)
    if bad:
        print("有 %d 项没通过。把 logs 目录整个发出来(或让 AI 助手读一下),"
              % bad)
        print("里面有每一步的完整输出, 足够定位问题。")
        return 1
    print("没有失败项。")
    if restricted_session():
        print("注意: 你是在受限会话里跑的, 注入/捕获两项的结果不作数, "
              "请在自己打开的窗口里再跑一遍。")
    else:
        print("下一步就是两台机器联调:")
        print("  Windows: python -m crosspc server --log-file logs\\server.log")
        print("  Debian : python3 -m crosspc client --host <Windows的IP> "
              "--log-file logs/client.log")
    return 0


if __name__ == "__main__":
    sys.exit(main())
