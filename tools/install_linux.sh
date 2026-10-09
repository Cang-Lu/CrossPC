#!/usr/bin/env bash
# CrossPC —— Debian 端一键安装脚本(客户端角色)
#
# 做什么:
#   1. 安装 python3 / python3-tk / xclip / wl-clipboard;
#   2. 加载 uinput 内核模块, 并写成开机自动加载;
#   3. 写 udev 规则, 让 input 组可以读写 /dev/uinput(不是 0666, 别开这个口子);
#   4. 把当前用户加入 input 组。
#
# 为什么需要这些:
#   * Python 端只用标准库, 所以除了 python3 本身没有 pip 依赖;
#   * 注入鼠标键盘有两条路: X11 走 libX11/libXtst(发行版自带), Wayland
#     或者没有 X 的时候走 /dev/uinput —— 后者默认 root 才能写, 必须靠 udev
#     规则 + input 组把权限放给普通用户, 否则 client 一启动就报权限错误;
#   * 剪辑板是调外部工具实现的(跨 Wayland/X11 最省事), 所以要装
#     wl-clipboard 和 xclip 各一份。
#
# 用法:  bash tools/install_linux.sh
# 注意: 这个文件是在 Windows 上创建的, 可执行位不会被带过去; 请用 `bash 脚本名`
#       调用, 不要用 ./install_linux.sh。文件本身是 LF 换行, bash 不需要 dos2unix。
set -euo pipefail

UDEV_RULE=/etc/udev/rules.d/99-crosspc-uinput.rules
MODULES_CONF=/etc/modules-load.d/crosspc-uinput.conf
PKGS=(python3 python3-tk xclip wl-clipboard)

log()  { printf '\033[1;34m[crosspc]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[crosspc]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[crosspc]\033[0m %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------- 基本检查
if [ "$(uname -s)" != "Linux" ]; then
  die "这个脚本只能在 Linux(建议 Debian 12+/Ubuntu 22.04+)上运行, 当前是 $(uname -s)。"
fi

if ! command -v apt-get >/dev/null 2>&1; then
  die "没找到 apt-get: 这脚本是为 Debian/Ubuntu 写的。其它发行版请手动安装
  python3 / python3-tk / xclip / wl-clipboard, 并自己写 /dev/uinput 的 udev 规则。"
fi

# 需要 root 才能装包和写 /etc; 不是 root 就借 sudo, 并提前说清楚
SUDO=()
if [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null 2>&1; then
    warn "当前不是 root, 后面会用 sudo 提权(可能会问密码)。"
    SUDO=(sudo)
  else
    die "当前不是 root 而且没有 sudo。请用 root 运行: su -c 'bash tools/install_linux.sh'"
  fi
fi

# --------------------------------------------------------------- 1. 装包
log "更新 apt 索引并安装: ${PKGS[*]}"
"${SUDO[@]}" apt-get update -y
# 逐个装: 某一个包在旧发行版里没有(例如很老的 Debian 没有 wl-clipboard)
# 也不该让整条命令失败, 剪辑板/光标至少还能用一半。
for pkg in "${PKGS[@]}"; do
  if dpkg -s "$pkg" >/dev/null 2>&1; then
    log "已安装, 跳过: $pkg"
    continue
  fi
  if "${SUDO[@]}" apt-get install -y --no-install-recommends "$pkg"; then
    log "装好了: $pkg"
  else
    warn "安装 $pkg 失败(可能源里没有这个名字), 继续。"
  fi
done

# 确认最关键的两个工具到底有没有
for pkg in python3 xclip; do
  if ! command -v "$pkg" >/dev/null 2>&1; then
    warn "警告: $pkg 仍然不可用, CrossPC 的对应功能会受限。"
  fi
done
command -v wl-copy >/dev/null 2>&1 || \
  warn "提示: 没有 wl-copy(Wayland 剪辑板), 在 Wayland 会话里剪辑板同步会不可用。"

# --------------------------------------------------------------- 2. uinput 模块
log "加载 uinput 内核模块"
if "${SUDO[@]}" modprobe uinput 2>/dev/null; then
  log "modprobe uinput 成功"
else
  warn "modprobe uinput 失败: 内核可能没编 uinput(自编译内核常见), 或者这是个
  容器/无模块的环境。X11 注入不受影响; 需要 uinput 时请换用发行版内核。
  注意: 不需要 'sudo modprobe uinput' 之后再手动 mknod, 2.6.24+ 的 uinput
  驱动会自己注册 misc 设备并在 udev 规则命中时创建 /dev/uinput。"
fi

# 开机自动加载(幂等: 内容相同就不重写)
if [ "$(cat "$MODULES_CONF" 2>/dev/null || true)" != "uinput" ]; then
  log "写入 $MODULES_CONF(开机自动加载 uinput)"
  printf 'uinput\n' | "${SUDO[@]}" tee "$MODULES_CONF" >/dev/null
else
  log "已是开机自动加载, 跳过: $MODULES_CONF"
fi

# --------------------------------------------------------------- 3. udev 规则
# MODE 0660 + GROUP input: 只给 input 组读写; static_node=uinput 让 udev 在
# 模块还没加载时就准备好节点属主, 免得"先插设备后加载模块"那种时序问题。
RULE='KERNEL=="uinput", SUBSYSTEM=="misc", MODE="0660", GROUP="input", OPTIONS+="static_node=uinput"'
if [ "$(cat "$UDEV_RULE" 2>/dev/null || true)" = "$RULE" ]; then
  log "udev 规则已是最新, 跳过: $UDEV_RULE"
else
  log "写入 udev 规则: $UDEV_RULE"
  printf '%s\n' "$RULE" | "${SUDO[@]}" tee "$UDEV_RULE" >/dev/null
fi

log "重新加载 udev 规则"
"${SUDO[@]}" udevadm control --reload-rules
"${SUDO[@]}" udevadm trigger --subsystem-match=misc || \
  "${SUDO[@]}" udevadm trigger || true

# --------------------------------------------------------------- 4. 用户组
TARGET_USER="${SUDO_USER:-$(id -un)}"
if [ "$TARGET_USER" = "root" ]; then
  warn "当前是 root 登录, 跳过加组(建议用普通用户跑 client, 而不是 root)。"
elif id -nG "$TARGET_USER" 2>/dev/null | tr ' ' '\n' | grep -qx input; then
  log "用户 $TARGET_USER 已在 input 组, 跳过"
else
  log "把用户 $TARGET_USER 加入 input 组"
  "${SUDO[@]}" usermod -aG input "$TARGET_USER"
  NEED_RELOGIN=1
fi

# --------------------------------------------------------------- 5. 结果
echo
log "安装完成。自检:"
printf '  %-22s %s\n' "/dev/uinput" "$(ls -l /dev/uinput 2>/dev/null || echo '不存在(检查上面的 modprobe 输出)')"
printf '  %-22s %s\n' "uinput 模块" "$(lsmod 2>/dev/null | awk '$1=="uinput"{print $0}' || echo '未加载')"
printf '  %-22s %s\n' "xclip" "$(command -v xclip || echo 缺失)"
printf '  %-22s %s\n' "wl-copy" "$(command -v wl-copy || echo 缺失)"
printf '  %-22s %s\n' "python3" "$(python3 -V 2>&1 || echo 缺失)"

if [ "${NEED_RELOGIN:-0}" = "1" ]; then
  echo
  warn "重要: 组变更对已登录的会话不生效, 请**注销后重新登录**(或重启), 否则"
  warn "client 仍然会报 /dev/uinput 权限不足。临时可用 newgrp input 起一个 shell 验证。"
fi

cat <<'EOF'

下一步(在 Debian 这台机器上, 用普通用户执行):
  1) 确认 Python 能导入 CrossPC:
       cd <CrossPC 目录> && python3 -c "import crosspc; print(crosspc.__version__)"
  2) 连接 Windows 端(server):
       python3 -m crosspc client --host <windows-ip>
     纯 Wayland 会话需要额外告诉 CrossPC 屏幕分辨率(uinput 是绝对定位设备):
       CROSSPC_SCREEN=2560x1440 python3 -m crosspc client --host <windows-ip>
  3) 自检:
       python3 -m crosspc doctor
     它会逐项报告注入方式(X11/XTest 还是 uinput)、剪辑板工具、/dev/uinput 权限。

开机自启(systemd)建议装成用户服务, 这样能直接绑定图形会话:
  mkdir -p ~/.config/systemd/user
  cp tools/crosspc-client.service ~/.config/systemd/user/
  # 打开这个文件改好 IP / CROSSPC_SCREEN, 然后:
  systemctl --user import-environment DISPLAY WAYLAND_DISPLAY XAUTHORITY
  systemctl --user daemon-reload
  systemctl --user enable --now crosspc-client
  journalctl --user -u crosspc-client -f      # 看日志

注意: Linux 端 v1 只能作 client(注入键鼠)。捕获/抑制本机输入需要 evdev 抓取
与合成器配合, 暂未实现, 所以没键鼠的那台机器才当 server(通常是 Windows)。
EOF
