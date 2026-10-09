# CrossPC v0.1.0-test3 - LAN keyboard/mouse sharing (Windows + Debian)

A test build for two real machines: a Windows PC with the keyboard and mouse
attached (the **server**) and a Debian PC without them (the **client**). Move the
mouse past the edge of one screen and it continues on the other, the keyboard
follows, and the clipboard (text + images) syncs both ways.

**This build is superseded in one respect only:** the project is now
English-only - documentation, code comments, docstrings, log lines, error
messages, CLI help and script output. The earlier `v0.1.0-test1` /
`v0.1.0-test2` builds contained Chinese text and their releases have been
withdrawn. The original Chinese README is kept in the repository as
`README.zh-CN.md`, and the English-only rule is enforced by a test and by CI
rather than by review discipline.

## Download

| | |
|---|---|
| Release zip | `crosspc-0.1.0.zip` (48 files, 169 KB, includes LICENSE) |
| Direct link | https://github.com/Cang-Lu/CrossPC/releases/download/v0.1.0-test3/crosspc-0.1.0.zip |
| SHA256 | `bdab9d310926b532dacf5b20ca33d547647f2f99a2d2867825e3e78c84baac84` |

Extract it and you have the complete, runnable project: pure Python standard
library, **zero third-party dependencies**.

## What this build adds

| Change | Detail |
|---|---|
| English-only repository | 2511 Chinese lines across 47 files translated: docs, comments, docstrings, logs, errors, CLI help, script output. The `.sh` / `.cmd` / `.ps1` / `.service` files are now pure ASCII (which also removes a latent mojibake risk on PowerShell 5.1, where a `.ps1` without a BOM is read in the local code page) |
| The rule cannot drift back | `tools/check_english.py` fails if any file git would commit contains CJK characters (only `README.zh-CN.md` is allowlisted); `tests/test_language.py` drives it from the suite, including three tests that prove the checker can actually fail |
| CI on every push | GitHub Actions runs 207 tests plus the loopback self-test on Ubuntu 3.8 / 3.11 / 3.13 and Windows 3.12, and validates `pip install .` and the release zip |
| LICENSE inside the zip | The MIT terms require the license notice to ship with every copy; `tests/test_release.py` now asserts it |
| Three CI-found test bugs fixed | The GUI snapping cases had hard-coded a 1920x1080 host screen and failed on the 1024x768 CI runner; the Linux backend cases asserted Windows-only outcomes. The snapping logic itself was fine - the tests were environment-dependent |
| Chinese README kept | `README.zh-CN.md`, verbatim, for the author's reference |

## Windows (the server: the machine with the keyboard and mouse)

```powershell
# 1. Python 3.8+ is required (3.12 recommended). If it is missing:
winget install -e --id Python.Python.3.12

# 2. Go to the extracted folder
cd <extracted>\crosspc-0.1.0

# 3. Check the environment first (safe: it never takes over the keyboard/mouse)
python tools\run-tests.cmd            # or double-click it

# 4. Configure and start
python -m crosspc init
python -m crosspc gui                 # drag the debian box to the right of this PC, save
python -m crosspc server --log-file logs\server.log
```

On first start it reminds you to open the firewall (an administrator command
prompt is required):

```
netsh advfirewall firewall add rule name="CrossPC" dir=in action=allow protocol=TCP localport=39987
```

## Debian (the client: the machine without keyboard and mouse)

```bash
# 1. Transfer the zip (or download it directly)
curl -L -O https://github.com/Cang-Lu/CrossPC/releases/download/v0.1.0-test3/crosspc-0.1.0.zip
unzip crosspc-0.1.0.zip -d ~/CrossPC && cd ~/CrossPC

# 2. Install the dependencies in one shot (apt packages, uinput udev rule, input group)
sudo bash tools/install_linux.sh
#    Log out and back in afterwards: group changes do not apply to a live session

# 3. Check the environment, then connect
python3 tools/run-tests.py
python3 -m crosspc client --host <Windows IP> --log-file logs/client.log
```

`python -m crosspc doctor` on the Windows side prints the LAN address to put in
`--host`.

## Usage

| Action | How |
|---|---|
| Switch machines | Push the mouse past the screen edge |
| Panic release | `Ctrl+Alt+F12` (always takes the keyboard and mouse back to this machine) |
| Lock | `Ctrl+Alt+L` (stay on the remote machine even at the screen edge) |
| Clipboard | Automatic, both ways; text and screenshots |

## Please verify these five things

1. `python -m crosspc capturetest --seconds 5` (**move the mouse and press a few
   keys during those 5 seconds**). It must run in a window you opened yourself:
   an AI assistant session or sandbox blocks input injection and capture, and the
   command will say "not permitted by the environment" instead of failing.
2. `python -m crosspc injecttest` (also run it on Debian).
3. Mouse crossing in both directions: push from Windows to Debian and back, and
   check whether the cursor position feels right.
4. Clipboard: copy text or a screenshot on Windows and paste it on Debian, then
   the other way round.
5. Disconnect safety: kill the client on Debian and check that the Windows side
   gets its keyboard and mouse back **immediately**.

Add `--log-file` on both sides; if something goes wrong, send the `logs/`
directory and the problem can be located from it.

## Verified / not verified

**Verified**: 207 unit and integration tests (Windows 3.12 plus Ubuntu
3.8/3.11/3.13), the 22-check end-to-end loopback self-test (including a run on a
real Linux kernel), a real image round trip through the Windows clipboard
(pixel-identical), `pip install .` plus release-zip content checks, and a real
server + client handshake over TCP to read the operational logs end to end.

**Not verified** (this needs two real machines, which is what this build is for):
the real hooks and takeover on Windows, real X11/uinput injection on Debian,
`install_linux.sh` on a real Debian install, and the clipboard across machines.
The development machine is Windows, so those cannot be exercised there.

## Known limitations

- **The Linux side can only be a client.** Capturing and swallowing local input
  needs exclusive evdev devices and cooperation from each Wayland compositor, and
  getting it wrong can leave somebody's keyboard and mouse unusable, so this
  version does not do it.
- The clipboard syncs text and images; **file lists and rich text (HTML/RTF) do
  not sync**.
- **Elevated windows (UAC) receive no injection** because of the Windows UIPI
  rules; CrossPC itself has to run elevated to drive them.
- **`Ctrl+Alt+Del` cannot be forwarded** (it is a secure attention sequence,
  handled only on the winlogon desktop).
- **No encryption**: the token is compared in plaintext, designed for a trusted
  LAN. Do not expose it to the internet.
- Multiple monitors: the whole virtual desktop of a machine participates as one
  screen.

## Feedback

Send the `logs/` directory together with a description of what you saw.
`--log-file` records every step, which is usually enough to locate the problem.
