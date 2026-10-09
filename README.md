# CrossPC - share one keyboard and mouse across your LAN

**English** | [Chinese](README.zh-CN.md)

[![CI](https://github.com/Cang-Lu/CrossPC/actions/workflows/ci.yml/badge.svg)](https://github.com/Cang-Lu/CrossPC/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Two computers (one Windows machine with the keyboard and mouse attached, one Debian
machine without), **push the mouse past the edge of one screen and it carries on across
the other one**; the keyboard follows the mouse; copy/paste content syncs both ways.

All the setup you need is dragging the two machine boxes next to each other - either
side by side or stacked - in the GUI.

```
        Windows(server, keyboard+mouse)          Debian(client, no keyboard/mouse)
   ┌──────────────────────────────┐        ┌───────────────────────────┐
   │                              │        │                           │
   │        1920 x 1080           │  ───►  │       2560 x 1440         │
   │                              │        │                           │
   └──────────────────────────────┘        └───────────────────────────┘
        keep pushing past the right edge → the cursor appears on Debian's left edge → the keyboard follows
        push back in from Debian's left edge → the cursor returns to where it was on Windows (held keys are released)
```

---

## Table of contents

- [Features](#features)
- [How it works](#how-it-works)
- [Quick start](#quick-start)
- [Installation](#installation)
- [Everyday use](#everyday-use)
- [Configuration file](#configuration-file)
- [Command-line reference](#command-line-reference)
- [Troubleshooting](#troubleshooting)
- [Known limitations (please read this once)](#known-limitations-please-read-this-once)
- [Security notes](#security-notes)
- [Project layout and development](#project-layout-and-development)

---

## Features

| Capability | Description |
|---|---|
| Configurable screen arrangement | Drag the boxes in the GUI and they snap together seamlessly; editing JSON directly works too |
| Crossing the edge | Push the mouse against an edge and keep pushing to switch to the other machine; coming back lands on the original position |
| Keyboard and mouse forwarding | Events are forwarded as **scancodes**, so different keyboard layouts on the two ends never scramble keys |
| Two-way clipboard sync | Plain text **+ images (screenshots)**, both directions, with automatic loop prevention |
| Stuck-key protection | Switching machines / losing the link / quitting releases every key that was held down |
| Panic release | `Ctrl+Alt+F12` unconditionally pulls control back to the local machine |
| Lock mode | `Ctrl+Alt+L` keeps control on the current machine even when the mouse hits an edge |
| Automatic discovery | The client works without an IP by finding the server over a UDP broadcast (typing the IP by hand is always more reliable) |
| Hot config reload | Change the arrangement while the server is running and it takes effect without a restart |
| Self-check tools | `doctor` / `selftest` / `capturetest` / `injecttest` / `clipboardtest` - run them first when something misbehaves |
| Zero third-party dependencies | Python standard library only (Windows calls user32 through ctypes, Linux calls libX11 / uinput through ctypes, and the DIB↔PNG image codec is hand-written too) |

---

## How it works

The roles come from your setup: **the machine with the physical keyboard and mouse = server, the other one = client**.

**server (Windows)**

- Two low-level hooks (`WH_KEYBOARD_LL` / `WH_MOUSE_LL`) watch global input.
  When not in takeover they only take a quick look; input reaches the local machine as
  usual and you will not notice they are there.
- During takeover the hooks return 1 to **suppress** keystrokes, wheel events and mouse
  buttons, and forward them to the client.
  Mouse **movement** cannot be stopped by a hook (the cursor is updated directly by the
  system input stack), so recentering is used instead: on every movement event the
  cursor is pulled back to the park point with `SetCursorPos`, and the displacement is
  computed from the difference between two consecutive `pt` values - that way you get
  the unrestricted movement while the local cursor stays quietly in the corner of the
  screen.
- The cursor's "virtual position" accumulates in a single coordinate system, and
  whichever box it falls into handles it. If a coordinate lands in the gap between
  boxes it snaps to the nearest machine, so the cursor is never lost.

**client (Debian)**

- Incoming events are injected locally. Under X11 it uses `XTestFakeMotionEvent` with
  absolute positioning (no extra privileges needed); without X (Wayland) it synthesizes
  an **absolutely positioned** virtual pointer device on `/dev/uinput` - a relative
  device would be subject to pointer acceleration applied by the compositor, and the
  coordinates would drift further the more you use it.
- Clipboard reads and writes go through `xclip`/`xsel`/`wl-clipboard` subprocesses.

**Safety floor** (this part matters more than the features)

1. Takeover mode is entered only while a client is really connected;
2. Client link dropped / heartbeat timeout (5 seconds) → control is pulled back to the local machine immediately;
3. Hook thread exits unexpectedly → a watchdog restores local input;
4. `Ctrl+Alt+F12` forces control back at any time;
5. If the process crashes Windows removes the hooks automatically, and `atexit` also releases takeover.

---

## Quick start

This assumes both machines are on the same LAN and can ping each other.

### 1. Windows (the machine with the keyboard and mouse, server)

```powershell
cd C:\Users\User\CrossPC

# generate a config (also probes the local resolution)
python -m crosspc init

# open the GUI and drag Debian's box to the right of this machine (click "Save config" when done)
python -m crosspc gui

# start the server
python -m crosspc server
```

For a first run, do the health check first (**safe, it does not take over the keyboard or mouse**):

```powershell
python -m crosspc doctor          # displays/hooks/injection/clipboard/ports/hotkeys in one go
python -m crosspc selftest        # loopback self-test: never touches the real keyboard or mouse, 22 checks (including image clipboard)
python -m crosspc clipboardtest   # real clipboard: write text and an image, read them back and compare
python -m crosspc injecttest      # real injection: inject side-effect-free keys and read the system state back
python -m crosspc capturetest --seconds 5 --takeover   # real capture: real takeover for 5 seconds
```

> ⚠️ **`capturetest` / `injecttest` must be run in a PowerShell window you opened yourself.**
> If you run them from a restricted session such as an AI assistant like DSH, the host
> blocks `SendInput` / `SetCursorPos` / global hooks for safety reasons (`SetCursorPos`
> returns 0 without reporting an error, `SendInput` returns success but has no effect),
> so those two commands are guaranteed to "fail everything".
> `injecttest` recognizes this situation and tells you straight away (exit code 2, your
> code is not at fault).

During the 5 seconds of `capturetest --takeover` the local keyboard and mouse stop
working (that is the feature); press `Ctrl+Alt+F12` to recover immediately, and it also
recovers on its own when the time is up.

### 2. Debian (the machine without keyboard and mouse, client)

```bash
# one-time dependency install (needs sudo)
sudo bash tools/install_linux.sh

# copy/clone the CrossPC directory to Debian, then:
cd CrossPC
python3 -m crosspc client --host 192.168.1.10        # replace with the Windows IP
# or leave the IP out and let it find the server by broadcast:
python3 -m crosspc client
```

On Windows, `python -m crosspc server` prints the LAN address when it starts, and
`python -m crosspc doctor` lists it as well.

### 3. Set the relative position

You can keep the GUI open on Windows while the server runs (the GUI only edits the config file, it does not touch input):

```powershell
python -m crosspc gui
```

- Select `client` in the list on the left, then drag its square to the **right/left/above/below** this machine on the canvas;
- Releasing snaps it into exact alignment (a few pixels off still counts as aligned), and vertical centering is supported too;
- Put Debian's real resolution into "Screen width/height" (the client fills in the real values automatically once it connects);
- Click "Save config". **The server reloads it within 2 seconds**, no restart needed.

### 4. Acceptance

**The easiest way: the one-shot acceptance script** (double-click `tools\run-tests.cmd` on
Windows, or run `python tools/run-tests.py` on any machine). It walks through environment
self-check → loopback → clipboard → injection → capture in order, skips whatever the
current platform does not support, and finally asks whether you want to do the "takeover
test". The output of every step is written to `logs\*.log`, so sending out that `logs`
directory (or letting an AI assistant read it) is enough to pin down a problem.

> ⚠️ **`capturetest` / `injecttest` must be run in a window you opened yourself.**
> A restricted session such as an AI assistant (DSH and friends) blocks `SendInput` /
> `SetCursorPos` / global hooks (`SetCursorPos` returns 0 without reporting an error,
> `SendInput` returns success but has no effect). Those two commands detect this and tell
> you "the environment does not allow it" (exit code 2) instead of pretending to fail.

Running each item by hand works too:

```powershell
python -m crosspc doctor --log-file logs\doctor.log        # environment self-check
python -m crosspc selftest --log-file logs\selftest.log    # loopback (22 checks)
python -m crosspc clipboardtest                            # real clipboard (text + image)
python -m crosspc injecttest                               # real injection (safe)
python -m crosspc capturetest --seconds 5                  # capture 5 seconds, move the mouse during it
python -m crosspc capturetest --seconds 5 --takeover       # real takeover for 5 seconds
```

During the 5 seconds of `capturetest --takeover` the local keyboard and mouse stop
working (that is the feature); press `Ctrl+Alt+F12` to recover immediately, and it also
recovers on its own when the time is up.

Then keep pushing the mouse out past one edge of the Windows screen:

- The cursor should show up on the Debian screen and the keyboard should be driving Debian;
- Push back in from the Debian side and the cursor returns to Windows;
- Copy some text on Windows with `Ctrl+C` (or take a screenshot and `Ctrl+V` the image),
  and `Ctrl+V` on Debian should paste it (and the other way round as well).

When debugging the two machines together, add `--log-file` to both ends so there is evidence to work from:

```powershell
# Windows
python -m crosspc server --log-file logs\server.log
```
```bash
# Debian
python3 -m crosspc client --host 192.168.1.10 --log-file logs/client.log
```

---

## Installation

### Windows

You only need Python 3.8+ (3.12 recommended); **nothing to install with pip**.

```powershell
# if python is not found, install one first:
winget install -e --id Python.Python.3.12

# check
python --version

# allow the inbound port through the firewall (needs an Administrator command prompt)
netsh advfirewall firewall add rule name="CrossPC" dir=in action=allow protocol=TCP localport=39987
```

You can also just run `tools\install_windows.ps1` and let it walk through those steps.

> Note: if `python` points at the Microsoft Store placeholder (`WindowsApps\python.exe`),
> it will not actually execute code. Use `python --version` to confirm it prints a version.

### Debian

The least painful route is to build a release zip first, copy it over and unpack it:

```powershell
# run this on Windows (no network needed, standard library only)
python tools\make_release.py            # produces dist\crosspc-0.1.0.zip and prints the SHA256
```

```bash
# copy the zip to Debian (USB stick / scp / shared folder all work), then:
unzip crosspc-0.1.0.zip -d ~/CrossPC && cd ~/CrossPC
sudo bash tools/install_linux.sh
python3 -m crosspc client --host <Windows IP>
```

`tools/install_linux.sh` does the following:

| What it does | Why |
|---|---|
| `apt install python3 python3-tk xclip wl-clipboard` | Runtime + GUI + clipboard tools (image sync needs xclip or wl-clipboard) |
| Writes `/etc/udev/rules.d/99-crosspc-uinput.rules` | Lets regular users open `/dev/uinput` (required for Wayland injection) |
| `modprobe uinput` + load it at boot | Without this module there is no virtual keyboard/mouse device |
| Adds the current user to the `input` group | Use uinput without sudo |

**Log out and back in once after the group change** (or run `newgrp input`) for it to take effect.

On an X11 desktop (Xorg) uinput is not needed; python3 alone is enough.

You can also skip the packaging and copy the whole directory over (or use `pip install .`, the project has no dependencies).

---

## Everyday use

### Hotkeys (recognized on the server)

| Hotkey | Effect |
|---|---|
| `Ctrl+Alt+F12` | **Panic release**: no matter which machine is active, control comes back to the local machine at once |
| `Ctrl+Alt+L` | Lock/unlock: once locked, the mouse hitting an edge will not switch away (handy for fullscreen games and presentations) |

They can be changed in the config file (`hotkeys.panic` / `hotkeys.lock`), written for
example as `"ctrl+alt+q"` or `"ctrl+shift+f9"`. Matching is done on scancodes, so it is
immune to input methods and keyboard layouts.

### Clipboard

- Two-way sync: copy on Windows → paste on Debian; copy on Debian → paste on Windows.
- **Text**: 256KB limit (configurable); anything larger is skipped with a message.
- **Images**: 4MB limit (configurable). The Windows side converts automatically between
  `CF_DIB` (what old programs understand) and the registered `PNG` format (what new
  programs understand, lossless and with transparency), while the Debian side goes
  through `xclip`/`wl-copy` with `image/png`. Both ends are **pixel-identical**
  (`crosspc clipboardtest` actually verifies this).
- When both text and an image are present (copying a chart out of Excel, say), **text**
  is sent by default because it is usually lighter and matches what people expect; set
  `"clipboard": {"prefer": "image"}` to favor the image.
- On Windows the polling interval is 300ms (reading the clipboard sequence number is very
  cheap); Linux has no equivalent API, so it relaxes automatically to 800ms or more
  (every poll has to spawn an `xclip`/`wl-paste` process).
- Turn it off in the config with `"clipboard": {"enabled": false}`, or ask for text only
  with `"clipboard": {"images": false}`.

### Checking the state

```bash
python -m crosspc server --stats        # print forwarding statistics every 10 seconds
python -m crosspc server --log-level debug --debug-events   # print every event (it will lag badly)
python -m crosspc discover              # find servers on the LAN
```

---

## Configuration file

Default locations (searched in order): `./crosspc.json` → `%APPDATA%\CrossPC\crosspc.json`
(Windows) / `~/.config/crosspc/crosspc.json` (Linux). You can also pass `--config`.

```jsonc
{
  "version": 1,
  "name": "win11",              // name of this machine (shown in logs and the GUI)
  "port": 39987,                // server listen port, must match on both ends
  "discovery_port": 39988,      // UDP automatic discovery port
  "token": "",                  // if set it must match on both ends, keeps strangers out
  "bind": "0.0.0.0",            // listen address
  "server_host": "",            // client role: IP of the server (empty means auto-discover)
  "screen": {"w": 2560, "h": 1440},   // client role: local resolution (uinput needs it to be exact)
  "server_screen": {"w": 1920, "h": 1080},  // GUI preview only, written automatically
  "clipboard": {
    "enabled": true,
    "poll_ms": 300,
    "max_bytes": 262144,          // text limit
    "images": true,               // whether to sync images (screenshots)
    "max_image_bytes": 4194304,   // image limit (PNG bytes)
    "prefer": "text"              // when text and an image are both present, sync this one first: text / image
  },
  "hotkeys": {
    "panic": "ctrl+alt+f12",     // panic release
    "lock": "ctrl+alt+l"         // lock/unlock
  },
  "log_level": "info",           // debug / info / warn / error
  "debug_events": false,         // true prints every event (for debugging, adds noticeable latency)
  "clients": [
    {
      "name": "debian",          // must match the name in the client's own config
      "host": "192.168.1.10",     // for the record only, the server never connects out to the client
      "rect": {"x": 1920, "y": 0, "w": 2560, "h": 1440},
      "enabled": true
    }
  ]
}
```

About `rect`:

- The coordinate system is the "virtual desktop" and **the server's top-left corner is
  fixed at (0,0)**, in physical pixels;
- Leaving the whole `rect` out → the server places this client automatically to the right
  of the rightmost existing machine, sized with the real resolution the client reported
  last time (cached in `crosspc.cache.json`);
- Giving only `x`/`y` and no `w`/`h` → the position is used as written and the size comes
  from automatic detection;
- The two machines' boxes **must share an edge** (the GUI snaps them). If you write the
  coordinates by hand and leave a gap, CrossPC tolerates small gaps of up to 128 pixels;
  any bigger and the cursor cannot cross.

After the GUI changes the config file, the server reloads the layout automatically within
2 seconds (control is pulled back to the local machine first). A broken config does not
disturb a running server - it keeps using the old layout and prints a warning.

---

## Command-line reference

| Command | Description |
|---|---|
| `crosspc init` | Generate a config file, probing the local resolution along the way |
| `crosspc gui` | GUI for setting the relative position (writes config only, never takes over input) |
| `crosspc server` | Run as the server (the machine with the keyboard and mouse) |
| `crosspc client` | Run as the client (the machine without them) |
| `crosspc doctor` | Environment self-check: displays, hooks, injection, clipboard, ports, hotkeys, discovery |
| `crosspc selftest` | Loopback self-test (fake backend, never touches the real keyboard or mouse), 22 checks |
| `crosspc capturetest` | Real capture acceptance: capture keyboard and mouse for N seconds and report; `--takeover` performs a real takeover |
| `crosspc injecttest` | Real injection acceptance: inject modifier/extension/lock keys and read the state back; `--window` actually types into a self-made window |
| `crosspc clipboardtest` | Real clipboard acceptance: write text and an image then read them back for a **pixel-by-pixel** comparison (it changes the clipboard temporarily and restores it at the end) |
| `crosspc discover` | Broadcast to find servers on the LAN |

Common options:

```bash
--config PATH        use a specific config file
--port N             override the port
--token SECRET       override the token
--host IP            client: address of the server
--backend NAME       force a backend: windows / x11 / uinput / fake
--log-level LEVEL    debug / info / warn / error
--log-file PATH      also write logs to a file (UTF-8, append mode, creates directories)
--debug-events       print every input event (useful when chasing stuck keys or dropped events)
```

`--log-file` is the key to debugging a two-machine setup: real-hardware tests cannot run
in a restricted session, but having the user run once with this option in their own
window leaves the log in a file (the `logs/` directory).

`server` also has `--bind`, `--stats` and `--dry-run` (no hooks installed, it only
exercises the network/handshake); `client` also has `--once` and `--no-clipboard`.

---

## Troubleshooting

**Run these two first; they tell you about the vast majority of problems directly:**

```bash
python -m crosspc doctor            # run this on the server
python -m crosspc selftest          # runs on any machine
```

| Symptom | Likely cause and what to do |
|---|---|
| Pushing the mouse to the edge does nothing | The position is not set up right. Use `crosspc gui` to check that the two boxes share an edge; the `server` startup log prints the virtual desktop layout, confirm the client's position matches what you expect |
| The client cannot connect | ① Windows Firewall has not allowed TCP 39987 (see the `netsh` command above); ② wrong IP (`crosspc doctor` lists the local addresses); ③ the two machines are on different subnets |
| "Keyboard/mouse hooks" fails in `doctor` | Antivirus/input-method "key protection" is blocking global hooks; run with Administrator rights, or whitelist CrossPC |
| The clipboard does not sync | Check the "Clipboard" item in `doctor`; on Linux make sure `xclip` or `wl-clipboard` is installed; under Wayland make sure `wl-copy` is available |
| No mouse cursor on Debian | Under X11 the client should show one; if it is Wayland and `doctor` says uinput is in use, check that `ls -l /dev/uinput` exists and that you have permission (run `install_linux.sh` and log in again) |
| Coordinates on Debian are offset overall | uinput is an absolutely positioned device and needs an exact resolution: put `"screen": {"w": 2560, "h": 1440}` in the client config, or use `CROSSPC_SCREEN=2560x1440 python3 -m crosspc client ...` |
| Typing occasionally sticks (Ctrl stays held) | Normally switching/going offline releases keys automatically. If you can reproduce it, capture a log with `--debug-events` and send it to me |
| The server is stuck and the keyboard and mouse are dead | Press `Ctrl+Alt+F12`. If that does not help, just kill the server process: the hooks disappear with it |
| Some keys do nothing on Debian | `Ctrl+Alt+Del` is part of the Windows secure attention sequence, hooks cannot see it, and no tool of this kind can forward it |

---

## Known limitations (please read this once)

1. **Linux can only be a client (the injection side), never a server.**
   Capturing and suppressing local input on Linux requires exclusive access to every
   evdev device (`EVIOCGRAB`) and separate negotiation with each Wayland compositor;
   grabbing the wrong device or crashing the process would leave the user's keyboard and
   mouse unusable, so this version does not do it. Debian in your setup has no keyboard
   or mouse anyway, so it makes no difference in practice.
2. **Clipboard**: text and images (PNG) are both supported; **file lists and rich text
   (HTML/RTF) are not synced**. Image sync on the Linux side needs `wl-clipboard` or
   `xclip` (`xsel` can only handle text).
3. **Administrator windows (UAC-elevated programs) receive no injection.** That is a
   Windows UIPI restriction; for CrossPC to operate administrator windows, CrossPC itself
   has to run as Administrator.
4. **`Ctrl+Alt+Del` cannot be forwarded** (the secure attention sequence is only handled
   on the winlogon desktop).
5. **Under Wayland some compositor-specific gestures cannot be intercepted or injected**,
   and a udev rule is required (the script already handles it).
6. **Multiple monitors**: the whole virtual desktop participates in sharing as one screen
   (that is, all of the server's monitors are treated as a single big box), monitors are
   not shared individually.
7. **DPI scaling**: everything is handled in physical pixels (the process declares
   per-monitor-v2), so Windows scaling at 125%/150% does not misplace anything; but if
   some application does its own odd coordinate handling, behavior may be inconsistent.
8. **No encryption, no authentication (beyond a plaintext `token` comparison)**: the
   design assumption is a "trusted LAN". Anyone on the same subnet can connect to your
   server (when no token is set). Do not expose it to the internet.
9. **Latency**: TCP with batched sending, usually imperceptible on a LAN; but it is a
   software solution and will not match a hardware KVM.

---

## Security notes

- It only listens on inbound TCP (on the server side); the client connects out to the server;
- `token` is a simple plaintext check used only to keep accidental connections out, not encryption;
- Clipboard content crosses the network in plaintext;
- The server records the names of machines that connect; an unknown name is **registered
  automatically** to the rightmost position and written into the config (convenient for
  first use, but do check the `crosspc server` log for machines you do not recognize).

If confidentiality matters: set a `token` and only use it on a trusted subnet.

---

## Project layout and development

```
crosspc/
  __main__.py       entry point for python -m crosspc
  cli.py            subcommand parsing
  config.py         config read/write + layout assembly + resolution cache
  layout.py         virtual desktop geometry (pure computation, fully covered by tests)
  router.py         input routing state machine (pure logic, fully covered by tests)
  protocol.py       wire protocol: framing + JSON/binary codecs
  net.py            TCP link (batched sending/heartbeat) + UDP automatic discovery
  clipboard.py      clipboard sync (text + images, loop prevention)
  image.py          standard-library-only image codec: PNG <-> RGBA <-> Windows DIB
  server.py         server application (wiring hooks/network/clipboard/hotkeys)
  client.py         client application (connect/inject/reconnect)
  gui.py            Tkinter GUI for setting the relative position
  selftest.py       loopback self-test + real capture acceptance + real clipboard acceptance
  injecttest.py     real injection acceptance (the kind that does not steal focus)
  hotkey.py         hotkey parsing and matching
  keys.py           scancode ↔ keysym/evdev/VK mapping tables
  util.py           logging/coordinate conversion/temporary directory picking
  events.py         normalized input events
  backend/
    base.py         platform backend interface (frozen contract)
    windows.py      ctypes: low-level hooks / SendInput / clipboard (text + CF_DIB/DIBV5/PNG)
    linux.py        Linux facade (injection strategy + clipboard tools)
    linux_x11.py    ctypes: libX11/libXtst (XTest injection)
    linux_uinput.py ctypes: /dev/uinput (absolutely positioned virtual pointer)
    fake.py         fake backend, used for the loopback self-test
tests/              207 unit/integration tests (none of them need a real keyboard or mouse)
tools/              install scripts, systemd service, launch wrappers, one-shot acceptance,
                    release packaging, and check_english.py (the language guard)
logs/               real-hardware test logs (generated automatically, ignored by .gitignore)
```

Running the tests (no second machine needed, and the real keyboard and mouse are never touched):

```bash
python -m unittest discover -s tests -t .      # 207 tests
python -m crosspc selftest                     # end-to-end loopback (22 checks)
python tools/run-tests.py                      # one-shot real-hardware acceptance (includes the two above)
python tools/check_english.py                  # language policy: no CJK outside README.zh-CN.md
```

### Relationship to Barrier / Deskflow / InputLeap

They are mature open-source tools of the same kind and do more than CrossPC (multiple
platforms, encrypted transport and so on). CrossPC's niche is: **zero dependencies, a
codebase small enough to read and modify yourself, aimed specifically at the
"Windows with the keyboard and mouse + Debian as the second screen" topology**. To see
how it works, reading three files is enough: `layout.py` + `router.py` (position
computation) and `backend/windows.py` (hooks and injection).

### Continuous integration

[.github/workflows/ci.yml](.github/workflows/ci.yml) runs three things on GitHub:

| Job | Coverage |
|---|---|
| Unit/integration tests | Ubuntu 22.04 (Python 3.8) / Ubuntu latest (3.11, 3.13) / Windows (3.12) |
| `crosspc selftest` | Runs the full loopback **on a real Linux kernel** (fake backend, no real keyboard or mouse needed) |
| Packaging and installation | `pip install .` + `make_release.py` + verifying the release zip contains no caches or private config |

The development machine is Windows, and the Linux path (X11/uinput injection, xclip
clipboard) cannot run there, so the Linux jobs in CI are not a formality - they are the
only place where the Linux runtime path can be verified. The two real keyboard/mouse jobs
(`capturetest` / `injecttest`) are deliberately kept out of CI: CI machines have no
interactive desktop, so they would fail by definition, and that is an environment
limitation rather than a code problem.

Every matrix job also runs the language guard: `tests/test_language.py` drives
`tools/check_english.py`, which fails the build if any file that git would commit
contains CJK characters (the one exception is `README.zh-CN.md`). The rule is stated
once in prose, so it is easy to break by accident - a single Chinese comment pasted
into a new module is almost invisible in a diff - and this is what makes it stick.

## Language

The repository is English-only: documentation, code comments, docstrings, log lines,
error messages, CLI help and script output. The original Chinese README is kept as
[README.zh-CN.md](README.zh-CN.md) for the author's own reference. The rule is enforced
mechanically by `tools/check_english.py` and by the test suite, not by review discipline.

## License

[MIT](LICENSE).
