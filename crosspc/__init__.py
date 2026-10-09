"""CrossPC -- a KVM tool that shares one keyboard and mouse across a LAN.

Roles:
    server  the machine with the physical keyboard and mouse. It captures input
            and uses the relative positions in the "virtual desktop" to decide
            whether to run it locally or forward it to a client, and it is
            responsible for broadcasting the clipboard.
    client  a machine with no keyboard or mouse of its own. It receives remote
            events and injects them locally, and also syncs the clipboard.

This package depends only on the Python 3.8+ standard library:
    Windows     ctypes calls user32/kernel32 directly
    Linux       ctypes calls libX11/libXtst directly, or writes /dev/uinput
"""

__all__ = ["__version__", "PROTOCOL_VERSION"]

__version__ = "0.1.0"

#: Wire protocol version; both ends must agree on it
PROTOCOL_VERSION = 1
