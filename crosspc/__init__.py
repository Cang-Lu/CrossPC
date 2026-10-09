"""CrossPC —— 局域网内共享一套鼠标键盘的 KVM 工具。

角色:
    server  接了物理键鼠的机器。捕获输入, 按"虚拟桌面"的位置关系决定
            是本机执行还是转发给 client, 并负责剪辑板广播。
    client  没有键鼠的机器。接收远端事件并注入本机, 同时同步剪辑板。

本包只依赖 Python 3.8+ 标准库:
    Windows 端   ctypes 直接调用 user32/kernel32
    Linux 端     ctypes 直接调用 libX11/libXtst, 或写 /dev/uinput
"""

__all__ = ["__version__", "PROTOCOL_VERSION"]

__version__ = "0.1.0"

#: 线协议版本, 两端必须一致
PROTOCOL_VERSION = 1
