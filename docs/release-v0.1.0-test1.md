# CrossPC v0.1.0-test1 (pre-release)

Share one keyboard and mouse across your LAN: **Windows (the machine with the keyboard
and mouse attached) = server**, **Debian (the machine without them) = client**. Push the
mouse past the edge of one screen and it carries on across the other one; the keyboard
follows; the clipboard (text + images) syncs both ways.

This is a **pre-release**, meant to let you get it running on two real machines and report
problems. What has already been verified on this development machine: 199
unit/integration tests, a 22-check end-to-end loopback self-test, a real Windows clipboard
image round trip (pixel-identical), and a real system clipboard text round trip.

## Download

| | |
|---|---|
| Release zip | `crosspc-0.1.0.zip` (45 files, 169 KB) |
| Direct link | https://github.com/Cang-Lu/CrossPC/releases/download/v0.1.0-test1/crosspc-0.1.0.zip |
| SHA256 | `0554cc050a4d3123d546c297748f740ff866280e2f26727f293b676f931e6f85` |

Unpacking it gives you a complete, runnable project (pure Python standard library, **zero
third-party dependencies**).

## Windows (server, the machine with the keyboard and mouse)

```powershell
# 1. Python 3.8+ is required (3.12 recommended). Install one first if you do not have it:
winget install -e --id Python.Python.3.12

# 2. unpack the zip and enter the directory
cd <extracted>\crosspc-0.1.0

# 3. health check first (safe, does not take over the keyboard or mouse)
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
curl -L -O https://github.com/Cang-Lu/CrossPC/releases/download/v0.1.0-test1/crosspc-0.1.0.zip
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
