#!/usr/bin/env bash
# CrossPC -- one-shot installer for the Debian side (client role)
#
# What it does:
#   1. installs python3 / python3-tk / xclip / wl-clipboard;
#   2. loads the uinput kernel module and makes it load automatically at boot;
#   3. writes a udev rule so the input group can read and write /dev/uinput
#      (not 0666 -- do not open that hole);
#   4. adds the current user to the input group.
#
# Why these steps are needed:
#   * The Python side uses only the standard library, so apart from python3
#     itself there are no pip dependencies;
#   * there are two ways to inject mouse and keyboard events: X11 goes through
#     libX11/libXtst (shipped by the distribution), while Wayland -- or a
#     machine without X -- goes through /dev/uinput. The latter is writable by
#     root only by default, so the udev rule plus the input group are what hand
#     that permission to an ordinary user; without them the client fails with a
#     permission error as soon as it starts;
#   * the clipboard is implemented by calling external tools (the least painful
#     way across Wayland/X11), so both wl-clipboard and xclip are installed.
#
# Usage:  bash tools/install_linux.sh
# Note:  this file was created on Windows, so the executable bit does not travel
#        with it; invoke it as `bash <script>`, not as ./install_linux.sh. The
#        file itself uses LF line endings, so bash needs no dos2unix.
set -euo pipefail

UDEV_RULE=/etc/udev/rules.d/99-crosspc-uinput.rules
MODULES_CONF=/etc/modules-load.d/crosspc-uinput.conf
PKGS=(python3 python3-tk xclip wl-clipboard)

log()  { printf '\033[1;34m[crosspc]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[crosspc]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[crosspc]\033[0m %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------- basics
if [ "$(uname -s)" != "Linux" ]; then
  die "This script only runs on Linux (Debian 12+/Ubuntu 22.04+ recommended); current system is $(uname -s)."
fi

if ! command -v apt-get >/dev/null 2>&1; then
  die "apt-get not found: this script is written for Debian/Ubuntu. On other
  distributions, install python3 / python3-tk / xclip / wl-clipboard by hand and
  write your own udev rule for /dev/uinput."
fi

# Installing packages and writing to /etc both need root; if we are not root,
# borrow sudo and say so up front
SUDO=()
if [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null 2>&1; then
    warn "Not running as root; sudo will be used for the rest (it may ask for a password)."
    SUDO=(sudo)
  else
    die "Not running as root and sudo is unavailable. Run as root instead: su -c 'bash tools/install_linux.sh'"
  fi
fi

# --------------------------------------------------------------- 1. packages
log "Updating the apt index and installing: ${PKGS[*]}"
"${SUDO[@]}" apt-get update -y
# Install one package at a time: a single package missing from an old release
# (a very old Debian has no wl-clipboard, for example) must not fail the whole
# command -- half a clipboard or half a cursor is still better than nothing.
for pkg in "${PKGS[@]}"; do
  if dpkg -s "$pkg" >/dev/null 2>&1; then
    log "Already installed, skipping: $pkg"
    continue
  fi
  if "${SUDO[@]}" apt-get install -y --no-install-recommends "$pkg"; then
    log "Installed: $pkg"
  else
    warn "Failed to install $pkg (the name may not exist in your sources); continuing."
  fi
done

# Confirm whether the two most critical tools are really there
for pkg in python3 xclip; do
  if ! command -v "$pkg" >/dev/null 2>&1; then
    warn "Warning: $pkg is still unavailable; the matching CrossPC feature will be limited."
  fi
done
command -v wl-copy >/dev/null 2>&1 || \
  warn "Note: wl-copy (Wayland clipboard) is missing, so clipboard sync will be unavailable in a Wayland session."

# --------------------------------------------------------------- 2. uinput module
log "Loading the uinput kernel module"
if "${SUDO[@]}" modprobe uinput 2>/dev/null; then
  log "modprobe uinput succeeded"
else
  warn "modprobe uinput failed: the kernel may have been built without uinput
  (common with self-compiled kernels), or this is a container / module-less
  environment. X11 injection is unaffected; use a distribution kernel if you
  need uinput.
  Note: you do not need to mknod by hand after 'sudo modprobe uinput'; since
  2.6.24 the uinput driver registers its own misc device and creates
  /dev/uinput when the udev rule matches."
fi

# Load it automatically at boot (idempotent: no rewrite when the content matches)
if [ "$(cat "$MODULES_CONF" 2>/dev/null || true)" != "uinput" ]; then
  log "Writing $MODULES_CONF (load uinput automatically at boot)"
  printf 'uinput\n' | "${SUDO[@]}" tee "$MODULES_CONF" >/dev/null
else
  log "Already loaded automatically at boot, skipping: $MODULES_CONF"
fi

# --------------------------------------------------------------- 3. udev rule
# MODE 0660 + GROUP input: read/write for the input group only; static_node=uinput
# makes udev prepare the node ownership even before the module is loaded, which
# avoids the race where the device appears before the module does.
RULE='KERNEL=="uinput", SUBSYSTEM=="misc", MODE="0660", GROUP="input", OPTIONS+="static_node=uinput"'
if [ "$(cat "$UDEV_RULE" 2>/dev/null || true)" = "$RULE" ]; then
  log "udev rule is already up to date, skipping: $UDEV_RULE"
else
  log "Writing udev rule: $UDEV_RULE"
  printf '%s\n' "$RULE" | "${SUDO[@]}" tee "$UDEV_RULE" >/dev/null
fi

log "Reloading the udev rules"
"${SUDO[@]}" udevadm control --reload-rules
"${SUDO[@]}" udevadm trigger --subsystem-match=misc || \
  "${SUDO[@]}" udevadm trigger || true

# --------------------------------------------------------------- 4. user group
TARGET_USER="${SUDO_USER:-$(id -un)}"
if [ "$TARGET_USER" = "root" ]; then
  warn "Logged in as root, skipping the group change (run the client as a normal user, not as root)."
elif id -nG "$TARGET_USER" 2>/dev/null | tr ' ' '\n' | grep -qx input; then
  log "User $TARGET_USER is already in the input group, skipping"
else
  log "Adding user $TARGET_USER to the input group"
  "${SUDO[@]}" usermod -aG input "$TARGET_USER"
  NEED_RELOGIN=1
fi

# --------------------------------------------------------------- 5. result
echo
log "Installation complete. Self-check:"
printf '  %-22s %s\n' "/dev/uinput" "$(ls -l /dev/uinput 2>/dev/null || echo 'missing (check the modprobe output above)')"
printf '  %-22s %s\n' "uinput module" "$(lsmod 2>/dev/null | awk '$1=="uinput"{print $0}' || echo 'not loaded')"
printf '  %-22s %s\n' "xclip" "$(command -v xclip || echo missing)"
printf '  %-22s %s\n' "wl-copy" "$(command -v wl-copy || echo missing)"
printf '  %-22s %s\n' "python3" "$(python3 -V 2>&1 || echo missing)"

if [ "${NEED_RELOGIN:-0}" = "1" ]; then
  echo
  warn "Important: group changes do not apply to sessions that are already logged"
  warn "in -- please **log out and log back in** (or reboot), otherwise the client"
  warn "will still report insufficient /dev/uinput permissions. As a quick check,"
  warn "you can start a shell with newgrp input."
fi

cat <<'EOF'

Next steps (on the Debian machine, as an ordinary user):
  1) Check that Python can import CrossPC:
       cd <CrossPC directory> && python3 -c "import crosspc; print(crosspc.__version__)"
  2) Connect to the Windows side (server):
       python3 -m crosspc client --host <windows-ip>
     A pure Wayland session also needs the screen resolution (uinput is an
     absolute-positioning device):
       CROSSPC_SCREEN=2560x1440 python3 -m crosspc client --host <windows-ip>
  3) Self-check:
       python3 -m crosspc doctor
     It reports, item by item, the injection method (X11/XTest or uinput), the
     clipboard tools and the /dev/uinput permissions.

For autostart (systemd), install it as a user service so it binds directly to
the graphical session:
  mkdir -p ~/.config/systemd/user
  cp tools/crosspc-client.service ~/.config/systemd/user/
  # open that file and set the IP / CROSSPC_SCREEN, then:
  systemctl --user import-environment DISPLAY WAYLAND_DISPLAY XAUTHORITY
  systemctl --user daemon-reload
  systemctl --user enable --now crosspc-client
  journalctl --user -u crosspc-client -f      # follow the logs

Note: on Linux, v1 can only act as client (injecting keyboard and mouse).
Capturing and suppressing local input needs evdev grabs coordinated with the
compositor; that is not implemented yet, so the machine without keyboard and
mouse is the one that acts as server (usually Windows).
EOF
