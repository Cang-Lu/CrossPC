# CrossPC v0.1.0-test2 (pre-release, **use this one**)

> This is the patch release for v0.1.0-test1. test1 is obsolete, please use this version:
> it adds the MIT license and the CI configuration, and fixes three tests that only passed
> on my development machine. You may want to jump straight to [Download](#download) below.

Share one keyboard and mouse across your LAN: **Windows (the machine with the keyboard
and mouse attached) = server**, **Debian (the machine without them) = client**. Push the
mouse past the edge of one screen and it carries on across the other one; the keyboard
follows; the clipboard (text + images) syncs both ways.

## Download

| | |
|---|---|
| Release zip | `crosspc-0.1.0.zip` (46 files, 171 KB, includes LICENSE) |
| Direct link | https://github.com/Cang-Lu/CrossPC/releases/download/v0.1.0-test2/crosspc-0.1.0.zip |
| SHA256 | `25a19caf4feb5bcd740a7b19c3e5d67f33f7a1e815104e5d0e3907f49cc13af7` |

Unpacking it gives you the complete, runnable project (pure Python standard library,
**zero third-party dependencies**).

## Changes from v0.1.0-test1

| Change | Description |
|---|---|
| Added `LICENSE` (MIT) | A public repository without a license defaults to "all rights reserved". The release zip now carries it too (MIT requires the license notice to be distributed with copies) |
| Added GitHub Actions | Runs the whole test suite on **real Linux** (Ubuntu 3.8 / 3.11 / 3.13) and Windows 3.12, plus one end-to-end loopback self-test |
| Fixed 3 test cases | They hard-coded "the local screen is 1920x1080", so they always went red on the CI 1024x768 runner. **The snapping logic itself was fine, the tests just depended on the host environment** - this kind of problem is impossible to find on this machine, which is the direct payoff of adding CI |
| Fixed 2 test cases | Three assertions in the Linux backend tests claimed "this necessarily raises an exception on Windows", but they succeed on real Linux when `/dev/uinput` is available. Rewritten as contract-style assertions plus platform guards |

CI status (all green for this release): all 5 jobs succeeded - packaging and installation
verification, Ubuntu 3.8 / 3.11 / 3.13, Windows 3.12.

## Windows (server, the machine with the keyboard and mouse)

```powershell
# 1. Python 3.8+ is required (3.12 recommended). Install one first if you do not have it:
winget install -e --id Python.Python.3.12

# 2. unpack and enter the directory
cd <extracted>\crosspc-0.1.0

# 3. health check first (safe, it will not take over the keyboard or mouse)
python tools\run-tests.cmd            # or double-click it

# 4. configure + start
python -m crosspc init
python -m crosspc gui                 # drag debian's box to the right of this machine, save
python -m crosspc server --log-file logs\server.log
```

The first start prompts you to allow the port through the firewall (needs an Administrator command prompt):

```
netsh advfirewall firewall add rule name="CrossPC" dir=in action=allow protocol=TCP localport=39987
```

## Debian (client, the machine without keyboard and mouse)

```bash
# 1. transfer the zip over (or download it directly)
curl -L -O https://github.com/Cang-Lu/CrossPC/releases/download/v0.1.0-test2/crosspc-0.1.0.zip
unzip crosspc-0.1.0.zip -d ~/CrossPC && cd ~/CrossPC

# 2. one-shot dependency install (apt packages + uinput udev rule + input group)
sudo bash tools/install_linux.sh
#    log out and back in once when it finishes (group changes do not apply to an existing session)

# 3. health check + connect
python3 tools/run-tests.py
python3 -m crosspc client --host <Windows IP> --log-file logs/client.log
```

On Windows, `python -m crosspc doctor` prints the LAN address; put it into `--host`.

## Usage

| Action | Description |
|---|---|
| Switch machines | Push the mouse against the screen edge and keep pushing |
| Panic release | `Ctrl+Alt+F12` (takes the keyboard and mouse back to the local machine at any time) |
| Lock | `Ctrl+Alt+L` (locks to the remote machine, the mouse hitting an edge will not switch away) |
| Clipboard | Automatic two-way sync; text + screenshots |

## Please help me verify these in particular

1. `python -m crosspc capturetest --seconds 5` (**move the mouse and hit a few keys during
   those 5 seconds**) - it must be run in a window you opened yourself, never in a
   restricted session taken over by an AI assistant.
2. `python -m crosspc injecttest` (run it once on Debian too)
3. Mouse crossing in both directions: push from Windows to Debian and back again, and
   check whether the cursor position matches your expectations.
4. Clipboard: copy text/a screenshot on Windows → paste on Debian; copy on Debian → paste on Windows.
5. Disconnect fallback: kill the client on Debian; the Windows side should restore the
   local keyboard and mouse **immediately**.

Add `--log-file` on both ends; if something goes wrong, sending out the `logs/` directory
is enough to pin it down.

## Verified / not yet verified

**Verified**: 200 unit/integration tests (Windows 3.12 + Ubuntu 3.8/3.11/3.13), a 22-check
end-to-end loopback self-test (including on a real Linux kernel), a real Windows clipboard
image round trip that is pixel-identical, and `pip install .` plus release zip content
verification.

**Not yet verified** (this needs two real machines, which is exactly what you are about to
do): real hook capture/takeover on Windows, real X11/uinput injection on Debian,
`install_linux.sh` actually running on a real Debian box, and a real cross-machine
clipboard. The development machine is Windows, so none of these can run there.

## Known limitations

- **The Linux side can only be a client**: capturing and suppressing local input requires
  exclusive access to evdev devices and negotiation with each Wayland compositor, and
  getting it wrong would leave the user's keyboard and mouse unusable, so this version
  does not do it.
- The clipboard only syncs text and images; **file lists and rich text (HTML/RTF) are not synced**.
- **Administrator windows (UAC-elevated programs) receive no injection** (a Windows UIPI
  restriction); for CrossPC to operate an administrator window, CrossPC itself has to run
  as Administrator.
- **`Ctrl+Alt+Del` cannot be forwarded** (the secure attention sequence is only handled on the winlogon desktop).
- **No encryption**: `token` is a plaintext comparison, and the design assumption is a
  trusted LAN. Do not expose it to the internet.
- Multiple monitors: the whole virtual desktop participates in sharing as one screen.

## Feedback

Just send out the `logs/` directory together with a clear description of what you saw
(`--log-file` records every step).
