# CrossPC v0.1.0-test1（测试版）

局域网内共享一套鼠标键盘：**Windows（接了键鼠的那台）= server**，
**Debian（没键鼠的那台）= client**。鼠标从一台的屏幕边缘推出去，就跑到另一台上；
键盘跟着走；剪贴板（文本 + 图片）双向同步。

这是一版**测试版**，目的是让你在真实两台机器上跑通并反馈问题。
这台开发机上已经验证过的：199 个单元/集成测试、22 项端到端回环自测、
真实 Windows 剪辑板图片往返（逐像素一致）、真实系统剪辑板文本往返。

## 下载

| | |
|---|---|
| 发行包 | `crosspc-0.1.0.zip`（45 个文件，169 KB） |
| 直链 | https://github.com/Cang-Lu/CrossPC/releases/download/v0.1.0-test1/crosspc-0.1.0.zip |
| SHA256 | `0554cc050a4d3123d546c297748f740ff866280e2f26727f293b676f931e6f85` |

解压后就是一个完整可运行的项目（纯 Python 标准库，**零第三方依赖**）。

## Windows（server，接键鼠的那台）

```powershell
# 1. 需要 Python 3.8+（推荐 3.12）。没有就先装:
winget install -e --id Python.Python.3.12

# 2. 把 zip 解开后进目录
cd <解压出来的>\crosspc-0.1.0

# 3. 先体检（安全，不接管键鼠）
python tools\run-tests.cmd            # 或双击它

# 4. 配置 + 启动
python -m crosspc init
python -m crosspc gui                 # 把 debian 的方框拖到本机右边, 保存
python -m crosspc server --log-file logs\server.log
```

首次启动会提示放行防火墙（需要管理员命令提示符）：

```
netsh advfirewall firewall add rule name="CrossPC" dir=in action=allow protocol=TCP localport=39987
```

## Debian（client，没键鼠的那台）

```bash
# 1. 传包过去（或直接下载）
curl -L -O https://github.com/Cang-Lu/CrossPC/releases/download/v0.1.0-test1/crosspc-0.1.0.zip
unzip crosspc-0.1.0.zip -d ~/CrossPC && cd ~/CrossPC

# 2. 一键装依赖（apt 包 + uinput udev 规则 + input 组）
sudo bash tools/install_linux.sh
#    装完要重新登录一次(组变更对已登录会话不生效)

# 3. 体检 + 连接
python3 tools/run-tests.py
python3 -m crosspc client --host <Windows的IP> --log-file logs/client.log
```

Windows 上 `python -m crosspc doctor` 会打印局域网地址，填到 `--host` 里。

## 用法

| 操作 | 说明 |
|---|---|
| 切换机器 | 鼠标推到屏幕边缘继续推 |
| 紧急收回 | `Ctrl+Alt+F12`（任何时候都能把键鼠拿回本机） |
| 锁定 | `Ctrl+Alt+L`（锁在远端，鼠标顶到边缘也不切走） |
| 剪贴板 | 自动双向；文本 + 截图 |

## 请重点帮我验证这几件事

1. `python -m crosspc capturetest --seconds 5`（**这 5 秒里动动鼠标、敲几下键盘**）
   —— 必须在自己打开的窗口里跑，不能在被 AI 助手接管的受限会话里跑。
2. `python -m crosspc injecttest`（Debian 上也跑一次）
3. 鼠标双向穿越：从 Windows 推过去、再从 Debian 推回来，光标位置是否符合直觉。
4. 剪贴板：Windows 复制文字/截图 → Debian 粘贴；Debian 复制 → Windows 粘贴。
5. 断线兜底：把 Debian 的 client 杀掉，Windows 这边应该**立刻**恢复本机键鼠。

两端都加上 `--log-file`，出问题把 `logs/` 目录发出来即可定位。

## 已知限制

- **Linux 端只能当 client**：捕获并吞掉本机输入需要独占 evdev 设备并与各家
  Wayland 合成器协商，弄不好会把用户的键鼠搞废，这一版没有做。
- 剪贴板只同步文本与图片；**文件列表、富文本（HTML/RTF）不同步**。
- **管理员窗口（UAC 提权程序）收不到注入**（Windows 的 UIPI 限制）；
  想让 CrossPC 操作管理员窗口，CrossPC 自己也要以管理员身份运行。
- **`Ctrl+Alt+Del` 转发不了**（安全注意序列只在 winlogon 桌面处理）。
- **无加密**：`token` 是明文比对，设计前提是可信局域网。别暴露到公网。
- 多显示器：以整块虚拟桌面为一块屏幕参与共享。

## 反馈

把 `logs/` 目录和你说清楚的现象一起发出来就行（`--log-file` 会把每一步都记下来）。
