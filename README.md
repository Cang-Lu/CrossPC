# CrossPC —— 局域网内共享一套鼠标键盘

两台电脑（一台 Windows 接了键鼠，一台 Debian 没接），**鼠标从这台屏幕的边缘推出去，
就跑到另一台屏幕上继续用**；键盘跟着鼠标走；两边的复制粘贴内容也能互相同步。

界面上把两台机器的方框拖成"左右相邻"或"上下相邻"，就完成全部配置。

```
        Windows(server, 接了键鼠)                Debian(client, 没键鼠)
   ┌──────────────────────────────┐        ┌───────────────────────────┐
   │                              │        │                           │
   │        1920 x 1080           │  ───►  │       2560 x 1440         │
   │                              │        │                           │
   └──────────────────────────────┘        └───────────────────────────┘
        鼠标推到右边缘继续推 → 光标出现在 Debian 的左边线上 → 键盘也跟着过去
        从 Debian 左边再往回推 → 光标回到 Windows 原来的位置(按键自动抬起)
```

---

## 目录

- [特性](#特性)
- [它是怎么工作的](#它是怎么工作的)
- [快速开始](#快速开始)
- [安装](#安装)
- [日常使用](#日常使用)
- [配置文件](#配置文件)
- [命令行参考](#命令行参考)
- [排错](#排错)
- [已知限制（请务必看一遍）](#已知限制请务必看一遍)
- [安全性说明](#安全性说明)
- [项目结构与开发](#项目结构与开发)

---

## 特性

| 能力 | 说明 |
|---|---|
| 屏幕相对位置可配 | 图形界面里拖方框，自动吸附成无缝拼接；也支持直接写 JSON |
| 边缘穿越 | 鼠标顶到边缘继续推就切到另一台；回来时回到原来的位置 |
| 键鼠转发 | 用**扫描码**转发，两端键盘布局不同也不会串键 |
| 剪贴板双向同步 | 纯文本 **+ 图片（截图）**，双向往返自动防回环 |
| 防粘键 | 切换机器 / 断线 / 退出时，自动把按住的键全部抬起 |
| 紧急收回 | `Ctrl+Alt+F12` 无条件把控制权收回本机 |
| 锁定模式 | `Ctrl+Alt+L` 锁在当前机器，鼠标顶到边缘也不切走 |
| 自动发现 | client 不填 IP 也能用 UDP 广播找到 server（手写 IP 永远更可靠） |
| 配置热加载 | server 运行中改完位置直接生效，不用重启 |
| 自检工具 | `doctor` / `selftest` / `capturetest` / `injecttest` / `clipboardtest`，出问题先跑它们 |
| 零第三方依赖 | 只用 Python 标准库（Windows 走 ctypes 调 user32，Linux 走 ctypes 调 libX11 / uinput，图片的 DIB↔PNG 编解码也是自己写的） |

---

## 它是怎么工作的

角色是按你的场景定的：**接了物理键鼠的那台 = server，另一台 = client**。

**server（Windows）**

- 用两个低层钩子（`WH_KEYBOARD_LL` / `WH_MOUSE_LL`）观察全局输入。
  没接管时只"顺便看一眼"，输入照常送到本机，你完全感觉不到它存在。
- 接管时钩子返回 1 把按键、滚轮、鼠标键**吞掉**，转发给 client。
  鼠标**移动**没法被钩子拦住（光标由系统输入栈直接更新），所以用了"回中"的办法：
  每收到一次移动就把光标 `SetCursorPos` 拉回停靠点，位移量则用相邻两次
  `pt` 的差值算出来 —— 这样既拿到了不受限的位移，本机光标又老实待在屏幕边角。
- 光标的"虚拟位置"累加在同一个坐标系里，落在哪个方框里就归哪台机器处理。
  坐标落在方框之间的缝里时，会吸附到最近的一台，光标永远不会丢。

**client（Debian）**

- 收到事件注入本机。X11 下用 `XTestFakeMotionEvent` 绝对定位（不需要额外权限）；
  没有 X（Wayland）时在 `/dev/uinput` 上合成一个**绝对定位**的虚拟指针设备
  —— 相对设备会被合成器施加指针加速，坐标会越用越偏。
- 剪辑板读写走 `xclip`/`xsel`/`wl-clipboard` 子进程。

**安全底线**（这部分比功能更重要）

1. 只有 client 真的连着才进入接管模式；
2. client 链路断开 / 心跳超时（5 秒）→ 立刻把控制权收回本机；
3. 钩子线程意外退出 → 看门狗恢复本机输入；
4. `Ctrl+Alt+F12` 任何时候都能强行收回；
5. 进程崩了 Windows 会自动摘掉钩子，`atexit` 里也会解除接管。

---

## 快速开始

以下假设两台机器在同一个局域网、能互相 ping 通。

### 1. Windows（接键鼠的那台，server）

```powershell
cd C:\Users\User\CrossPC

# 生成配置（会顺便探测本机分辨率）
python -m crosspc init

# 打开界面，把 Debian 的方框拖到本机右边（拖完点"保存配置"）
python -m crosspc gui

# 启动 server
python -m crosspc server
```

首次运行建议先做体检（**安全，不会接管键鼠**）：

```powershell
python -m crosspc doctor          # 显示器/钩子/注入/剪辑板/端口/热键 一次看全
python -m crosspc selftest        # 单机回环自测：不碰真实键鼠, 22 项检查(含图片剪贴板)
python -m crosspc clipboardtest   # 真机剪辑板: 文本与图片写进去再读回来比对
python -m crosspc injecttest      # 真机注入: 注入无副作用的键并回读系统状态
python -m crosspc capturetest --seconds 5 --takeover   # 真机捕获: 真接管 5 秒
```

> ⚠️ **`capturetest` / `injecttest` 必须在你自己打开的 PowerShell 窗口里跑。**
> 如果你是在 DSH 这类 AI 助手的受限会话里执行，宿主会出于安全考虑屏蔽
> `SendInput` / `SetCursorPos` / 全局钩子（`SetCursorPos` 返回 0 但不报错、
> `SendInput` 返回成功却毫无效果），于是这两条命令必然"全部失败"。
> `injecttest` 能识别这种情况并直接告诉你（退出码 2，不是你的代码有问题）。

`capturetest --takeover` 那 5 秒内本机键鼠会失效（这是特性），
期间按 `Ctrl+Alt+F12` 可以立即恢复，到时间也会自动恢复。

### 2. Debian（没键鼠的那台，client）

```bash
# 一次性安装依赖（需要 sudo）
sudo bash tools/install_linux.sh

# 拷贝/克隆 CrossPC 目录到 Debian，然后：
cd CrossPC
python3 -m crosspc client --host 192.168.1.10        # 换成 Windows 的 IP
# 或者不写 IP，让它自己广播找：
python3 -m crosspc client
```

Windows 上 `python -m crosspc server` 启动时会打印"局域网地址"，
`python -m crosspc doctor` 也会列出来。

### 3. 设置相对位置

在 Windows 上开着 server 也能同时开界面（界面只改配置文件，不碰输入）：

```powershell
python -m crosspc gui
```

- 左边列表选中 `client`，在画布上把方块拖到本机的**右侧/左侧/上方/下方**；
- 松手会自动吸附成严丝合缝（差几个像素也算对齐），也支持上下居中对齐；
- "屏幕宽/高"填 Debian 的真实分辨率（client 连上来之后会自动填好真实值）；
- 点"保存配置"。**server 会在 2 秒内自动重新加载**，不用重启。

### 4. 验收

**最省事的办法：一键验收脚本**（Windows 上双击 `tools\run-tests.cmd`，或任何机器上
`python tools/run-tests.py`）。它会按顺序跑完环境自检 → 单机回环 → 剪辑板 → 注入 →
捕获，跳过本平台不支持的项，最后问你一句要不要做"接管测试"。每一步的输出都会写进
`logs\*.log`，跑完把 `logs` 目录发出来（或让 AI 助手读一下）就能定位问题。

> ⚠️ **`capturetest` / `injecttest` 必须在你自己打开的窗口里跑。**
> AI 助手（DSH 之类）的受限会话会屏蔽 `SendInput` / `SetCursorPos` / 全局钩子
> （`SetCursorPos` 返回 0 但不报错、`SendInput` 返回成功却毫无效果）。这两条命令
> 会识别出这种情况并告诉你「环境不允许」（退出码 2），而不是假装失败。

手工逐项跑也可以：

```powershell
python -m crosspc doctor --log-file logs\doctor.log        # 环境自检
python -m crosspc selftest --log-file logs\selftest.log    # 单机回环(22 项)
python -m crosspc clipboardtest                            # 真实剪辑板(文本+图片)
python -m crosspc injecttest                               # 真实注入(安全)
python -m crosspc capturetest --seconds 5                  # 抓 5 秒, 期间动动鼠标
python -m crosspc capturetest --seconds 5 --takeover       # 真接管 5 秒
```

`capturetest --takeover` 那 5 秒内本机键鼠会失效（这是特性），
期间按 `Ctrl+Alt+F12` 可以立即恢复，到时间也会自动恢复。

然后把鼠标从 Windows 屏幕的一侧边缘继续往外推：

- 光标应该出现在 Debian 屏幕上，键盘操作的是 Debian；
- 从 Debian 那一侧再往回推，光标回到 Windows；
- 在 Windows 上 `Ctrl+C` 复制一段文字（或截图后 `Ctrl+V` 图片），在 Debian 上
  `Ctrl+V` 应该能粘贴（反过来也一样）。

两台机器联调时给两端都加上 `--log-file`，出问题就有据可查：

```powershell
# Windows
python -m crosspc server --log-file logs\server.log
```
```bash
# Debian
python3 -m crosspc client --host 192.168.1.10 --log-file logs/client.log
```

---

## 安装

### Windows

只需要 Python 3.8+（推荐 3.12），**不需要 pip 装任何东西**。

```powershell
# 如果提示找不到 python，先装一个：
winget install -e --id Python.Python.3.12

# 检查
python --version

# 放行防火墙入站端口（需要"管理员"命令提示符）
netsh advfirewall firewall add rule name="CrossPC" dir=in action=allow protocol=TCP localport=39987
```

也可以直接跑 `tools\install_windows.ps1` 让它把这几步检查一遍。

> 注意：`python` 若指向 Microsoft Store 的占位程序（`WindowsApps\python.exe`），
> 它不会真的执行代码。用 `python --version` 确认能打印版本号。

### Debian

最省事的办法是先打一个发行包，拷过去解压：

```powershell
# 在 Windows 上执行(不需要联网, 纯标准库)
python tools\make_release.py            # 生成 dist\crosspc-0.1.0.zip, 并打印 SHA256
```

```bash
# 把 zip 拷到 Debian(U 盘 / scp / 共享目录都行), 然后:
unzip crosspc-0.1.0.zip -d ~/CrossPC && cd ~/CrossPC
sudo bash tools/install_linux.sh
python3 -m crosspc client --host <Windows的IP>
```

`tools/install_linux.sh` 会做：

| 做的事 | 为什么 |
|---|---|
| `apt install python3 python3-tk xclip wl-clipboard` | 运行环境 + 界面 + 剪辑板工具（图片同步需要 xclip 或 wl-clipboard） |
| 写 `/etc/udev/rules.d/99-crosspc-uinput.rules` | 让普通用户可以打开 `/dev/uinput`（Wayland 注入必需） |
| `modprobe uinput` + 开机自动加载 | 没有这个模块就没有虚拟键鼠设备 |
| 把当前用户加入 `input` 组 | 免 sudo 使用 uinput |

**加完组要重新登录一次**（或者 `newgrp input`）才生效。

X11 桌面（Xorg）下不需要 uinput，装完 python3 就能用。

也可以不打包，直接把整个目录拷过去（或用 `pip install .`，项目是零依赖的）。

---

## 日常使用

### 热键（在 server 上识别）

| 热键 | 作用 |
|---|---|
| `Ctrl+Alt+F12` | **紧急收回**：不管当前在哪台机器，立刻把控制权拿回本机 |
| `Ctrl+Alt+L` | 锁定/解锁：锁定后鼠标顶到边缘也不会切走（适合全屏游戏/演示） |

在配置文件里可以改（`hotkeys.panic` / `hotkeys.lock`），写法如 `"ctrl+alt+q"`、
`"ctrl+shift+f9"`。识别用的是扫描码，不吃输入法/键盘布局影响。

### 剪贴板

- 双向同步：Windows 复制 → Debian 粘贴；Debian 复制 → Windows 粘贴。
- **文本**：上限 256KB（可配），超了会跳过并提示。
- **图片**：上限 4MB（可配）。Windows 侧会自动在 `CF_DIB`（老程序认）与注册格式
  `PNG`（新程序认，无损带透明）之间转换，Debian 侧走 `xclip`/`wl-copy` 的
  `image/png`。两边都是**像素级一致**（`crosspc clipboardtest` 会实测这一点）。
- 同时有文本和图片时（例如从 Excel 复制图表），默认发**文本**，因为那通常更轻也更符合
  直觉；想优先图片就设 `"clipboard": {"prefer": "image"}`。
- Windows 上轮询间隔 300ms（读剪辑板序号，很便宜）；Linux 没有等价 API，
  会自动放宽到 800ms 以上（每次都要起一个 `xclip`/`wl-paste` 进程）。
- 不想用就在配置里 `"clipboard": {"enabled": false}`，只想要文字就
  `"clipboard": {"images": false}`。

### 查看状态

```bash
python -m crosspc server --stats        # 每 10 秒打印一次转发统计
python -m crosspc server --log-level debug --debug-events   # 每个事件都打印(会很卡)
python -m crosspc discover              # 在局域网里找 server
```

---

## 配置文件

默认位置（按顺序找）：`./crosspc.json` → `%APPDATA%\CrossPC\crosspc.json`（Windows）
/ `~/.config/crosspc/crosspc.json`（Linux）。也可以用 `--config` 指定。

```jsonc
{
  "version": 1,
  "name": "win11",              // 本机名字(日志和界面里显示)
  "port": 39987,                // server 监听端口, 两端要一致
  "discovery_port": 39988,      // UDP 自动发现端口
  "token": "",                  // 设了就必须两端一致, 防止陌生机器接入
  "bind": "0.0.0.0",            // 监听地址
  "server_host": "",            // client 角色用: server 的 IP(留空则自动发现)
  "screen": {"w": 2560, "h": 1440},   // client 角色用: 本机分辨率(uinput 必须准确)
  "server_screen": {"w": 1920, "h": 1080},  // 仅界面预览用, 会自动写入
  "clipboard": {
    "enabled": true,
    "poll_ms": 300,
    "max_bytes": 262144,          // 文本上限
    "images": true,               // 是否同步图片(截图)
    "max_image_bytes": 4194304,   // 图片上限(PNG 字节数)
    "prefer": "text"              // 文本与图片同时存在时先同步哪个: text / image
  },
  "hotkeys": {
    "panic": "ctrl+alt+f12",     // 紧急收回
    "lock": "ctrl+alt+l"         // 锁定/解锁
  },
  "log_level": "info",           // debug / info / warn / error
  "debug_events": false,         // true 会打印每个事件(排查用, 明显增加延迟)
  "clients": [
    {
      "name": "debian",          // 必须和 client 端配置里的 name 一致
      "host": "192.168.1.10",     // 仅记录用途, server 不需要主动连 client
      "rect": {"x": 1920, "y": 0, "w": 2560, "h": 1440},
      "enabled": true
    }
  ]
}
```

关于 `rect`：

- 坐标系是"虚拟桌面"，**server 的左上角固定是 (0,0)**，单位物理像素；
- `rect` 整段省略 → server 会把这台 client 自动摆到已有最右侧机器的右边，
  尺寸用 client 上次上报的真实分辨率（缓存在 `crosspc.cache.json`）；
- 只写 `x`/`y` 不写 `w`/`h` → 位置照用，尺寸走自动探测；
- 两台机器的框**必须共边**（GUI 会自动吸附）。手工写坐标时如果留了缝，
  CrossPC 会容错 128 像素以内的小缝，缝太大就不让穿过去了。

配置文件被界面改动后，server 会在 2 秒内自动重新加载布局（控制权会先收回本机）。
配置写坏了也不影响已经跑着的 server —— 它会继续用旧布局并打印警告。

---

## 命令行参考

| 命令 | 说明 |
|---|---|
| `crosspc init` | 生成配置文件，顺便探测本机分辨率 |
| `crosspc gui` | 图形界面设置相对位置（只写配置，不接管输入） |
| `crosspc server` | 以 server 身份运行（接键鼠的那台） |
| `crosspc client` | 以 client 身份运行（没键鼠的那台） |
| `crosspc doctor` | 环境自检：显示器、钩子、注入、剪辑板、端口、热键、自动发现 |
| `crosspc selftest` | 单机回环自测（假后端，不碰真实键鼠），22 项检查 |
| `crosspc capturetest` | 真机捕获验收：抓 N 秒键鼠并统计，`--takeover` 会真接管 |
| `crosspc injecttest` | 真机注入验收：注入修饰键/扩展键/锁定键并回读状态，`--window` 会往自建窗口真打字 |
| `crosspc clipboardtest` | 真机剪辑板验收：文本与图片写入后读回**逐像素**比对（会临时改剪辑板，结束还原） |
| `crosspc discover` | 广播查找局域网里的 server |

常用参数：

```bash
--config PATH        指定配置文件
--port N             覆盖端口
--token SECRET       覆盖口令
--host IP            client: server 地址
--backend NAME       强制后端: windows / x11 / uinput / fake
--log-level LEVEL    debug / info / warn / error
--log-file PATH      把日志同时写到文件(UTF-8, 追加模式, 自动建目录)
--debug-events       打印每个输入事件(排查粘键/丢事件时有用)
```

`--log-file` 是排查联调问题的关键：在受限会话里跑不了真机测试，但让用户在自己窗口里
带上这个参数跑一遍，日志就留在文件里了（`logs/` 目录）。

`server` 还有 `--bind`、`--stats`、`--dry-run`（不装钩子，只验证网络/握手）；
`client` 还有 `--once`、`--no-clipboard`。

---

## 排错

**先跑这两条，绝大多数问题它会直接告诉你：**

```bash
python -m crosspc doctor            # server 上跑
python -m crosspc selftest          # 任意一台机器上跑都能跑
```

| 症状 | 可能原因与处理 |
|---|---|
| 鼠标推到边缘没反应 | 位置没设对。`crosspc gui` 检查两个方框是否共边；`server` 启动日志会打印 `虚拟桌面` 布局，确认 client 的位置和你以为的一致 |
| client 连不上 | ① Windows 防火墙没放行 TCP 39987（见上面的 `netsh` 命令）；② IP 写错（`crosspc doctor` 会列出本机地址）；③ 两台不在同一网段 |
| `doctor` 里"键鼠钩子"失败 | 杀软/输入法的"按键保护"拦截了全局钩子；换成管理员权限运行，或把 CrossPC 加进白名单 |
| 剪切板不同步 | `doctor` 看"剪辑板"一项；Linux 上确认装了 `xclip` 或 `wl-clipboard`；Wayland 下确认 `wl-copy` 可用 |
| Debian 上没有鼠标光标 | client 在 X11 下应该有；如果是 Wayland 且 `doctor` 说用了 uinput，确认 `ls -l /dev/uinput` 存在且你有权限（跑过 `install_linux.sh` 并重新登录） |
| Debian 上坐标整体偏移 | uinput 是绝对定位设备，需要准确的分辨率：在 client 配置里写 `"screen": {"w": 2560, "h": 1440}`，或 `CROSSPC_SCREEN=2560x1440 python3 -m crosspc client ...` |
| 打字偶尔粘连（一直按着 Ctrl） | 正常情况下切换/断线会自动抬键。如果复现了，用 `--debug-events` 抓日志发我 |
| server 卡住、键鼠失灵 | 按 `Ctrl+Alt+F12`。仍然不行就直接关掉 server 进程：钩子会随进程一起消失 |
| 有按键在 Debian 上没反应 | `Ctrl+Alt+Del` 属于 Windows 安全注意序列，钩子拿不到，任何同类工具都转发不了 |

---

## 已知限制（请务必看一遍）

1. **Linux 只能当 client（注入端），不能当 server。**
   在 Linux 上"捕获并吞掉本机输入"需要独占所有 evdev 设备（`EVIOCGRAB`）
   并与各家的 Wayland 合成器分别协商，抢错设备或进程崩掉会直接把用户的键鼠弄废，
   所以这一版没有做。你的场景里 Debian 本来就没键鼠，不影响使用。
2. **剪贴板**：文本与图片（PNG）都支持；**文件列表、富文本（HTML/RTF）不同步**。
   Linux 侧的图片同步需要 `wl-clipboard` 或 `xclip`（`xsel` 只能处理文本）。
3. **管理员窗口（UAC 提权程序）收不到注入。** Windows 的 UIPI 机制限制，
   想让 CrossPC 能操作管理员窗口，需要以管理员身份运行 CrossPC。
4. **`Ctrl+Alt+Del` 转发不了**（安全注意序列只在 winlogon 桌面处理）。
5. **Wayland 下无法拦截/注入某些合成器自有的手势**，且需要一个 udev 规则（脚本已处理）。
6. **多显示器**：以整块虚拟桌面为一块屏幕参与共享（也就是把 server 的所有显示器
   当成一个大方块），不是每个显示器单独共享。
7. **DPI 缩放**：已按物理像素处理（进程声明 per-monitor-v2），所以 Windows 缩放
   125%/150% 都不会错位；但如果某个应用自己做了奇怪的坐标处理，可能表现不一致。
8. **无加密、无认证（除 `token` 明文比对）**：设计前提是"可信局域网"。
   同网段的任何人都能连上你的 server（未设 token 时）。不要暴露到公网。
9. **延迟**：TCP + 批量发送，局域网内通常感觉不到；但它是软件方案，
   比不上硬件 KVM。

---

## 安全性说明

- 只监听入站 TCP（server 端），client 主动连 server；
- `token` 是明文的简单校验，只用于挡住"误连"，不是加密；
- 剪辑板内容是明文过网的；
- server 会记录连上来的机器名，未知机器名会**自动登记**到最右侧并写入配置
  （方便首次使用，但请确认 `crosspc server` 日志里没有陌生机器）。

如果对保密有要求：设一个 `token`，并且只在受信任的网段用。

---

## 项目结构与开发

```
crosspc/
  __main__.py       python -m crosspc 入口
  cli.py            子命令解析
  config.py         配置读写 + 布局装配 + 分辨率缓存
  layout.py         虚拟桌面几何(纯计算, 全测试覆盖)
  router.py         输入路由状态机(纯逻辑, 全测试覆盖)
  protocol.py       线协议: 分帧 + JSON/二进制编解码
  net.py            TCP 链路(批量发送/心跳) + UDP 自动发现
  clipboard.py      剪辑板同步(文本+图片, 防回环)
  image.py          纯标准库图片编解码: PNG <-> RGBA <-> Windows DIB
  server.py         server 应用(钩子/网络/剪贴板/热键编排)
  client.py         client 应用(连接/注入/重连)
  gui.py            Tkinter 相对位置设置界面
  selftest.py       回环自测 + 真机捕获验收 + 真机剪辑板验收
  injecttest.py     真机注入验收(不抢焦点的那种)
  hotkey.py         热键解析与识别
  keys.py           扫描码 ↔ keysym/evdev/VK 映射表
  util.py           日志/坐标换算/临时目录挑选
  events.py         归一化输入事件
  backend/
    base.py         平台后端接口(冻结契约)
    windows.py      ctypes: 低层钩子 / SendInput / 剪辑板(文本+CF_DIB/DIBV5/PNG)
    linux.py        Linux 门面(注入策略 + 剪辑板工具)
    linux_x11.py    ctypes: libX11/libXtst(XTest 注入)
    linux_uinput.py ctypes: /dev/uinput(绝对定位虚拟指针)
    fake.py         假后端, 用于回环自测
tests/              199 个单元/集成测试(全部不需要真实键鼠)
tools/              安装脚本、systemd 服务、启动包装、一键验收、发行包打包
logs/               真机测试的日志(自动生成, 已被 .gitignore 忽略)
```

跑测试（不需要第二台机器，也不会碰真实键鼠）：

```bash
python -m unittest discover -s tests -t .      # 199 项
python -m crosspc selftest                     # 端到端回环(22 项检查)
python tools/run-tests.py                      # 一键真机验收(含上面两项)
```

### 和 Barrier / Deskflow / InputLeap 的关系

它们是成熟的开源同类工具，功能比 CrossPC 多（多平台、图片剪贴板、TLS 等）。
CrossPC 的定位是：**零依赖、代码量小到能自己读懂并改动、专门针对
"Windows 带键鼠 + Debian 当副屏"这一种拓扑**。想看它的实现，
从 `layout.py` + `router.py`（位置计算）和 `backend/windows.py`（钩子与注入）
三个文件读起就够了。
