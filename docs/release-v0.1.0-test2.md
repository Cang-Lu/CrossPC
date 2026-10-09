# CrossPC v0.1.0-test2（测试版，**用这个**）

> 这是 v0.1.0-test1 的补丁版。test1 已经废弃，请用本版：
> 它带上了 MIT 许可证、CI 配置，并修掉了三项只在我的开发机上才能通过的测试。
> 建议直接跳到下面的 [下载](#下载)。

局域网内共享一套鼠标键盘：**Windows（接了键鼠的那台）= server**，
**Debian（没键鼠的那台）= client**。鼠标从一台的屏幕边缘推出去，就跑到另一台上；
键盘跟着走；剪贴板（文本 + 图片）双向同步。

## 下载

| | |
|---|---|
| 发行包 | `crosspc-0.1.0.zip`（46 个文件，171 KB，含 LICENSE） |
| 直链 | https://github.com/Cang-Lu/CrossPC/releases/download/v0.1.0-test2/crosspc-0.1.0.zip |
| SHA256 | `25a19caf4feb5bcd740a7b19c3e5d67f33f7a1e815104e5d0e3907f49cc13af7` |

解压后就是完整可运行的项目（纯 Python 标准库，**零第三方依赖**）。

## 相比 v0.1.0-test1 的变化

| 变化 | 说明 |
|---|---|
| 加 `LICENSE`（MIT） | 公开仓库没有许可证默认是"保留所有权利"。发行包里也带上了（MIT 要求许可声明随副本分发） |
| 加 GitHub Actions | 在**真 Linux**（Ubuntu 3.8 / 3.11 / 3.13）和 Windows 3.12 上跑全部测试，并跑一次端到端回环自测 |
| 修了 3 个测试用例 | 它们把"本机屏幕是 1920x1080"写死了，在 CI 的 1024x768 runner 上必红。**吸附逻辑本身没问题，是测试依赖了宿主机环境** —— 本机永远发现不了这类问题，这也是加 CI 的直接收益 |
| 修了 2 个测试用例 | Linux 后端测试里有三条断言的是"Windows 上必然抛异常"，在真 Linux 上 `/dev/uinput` 可用时会成功。改成契约化断言 + 平台守卫 |

CI 状态（本次发行时全绿）：5 个任务全部 success —— 打包与安装验证、Ubuntu 3.8 / 3.11 / 3.13、Windows 3.12。

## Windows（server，接键鼠的那台）

```powershell
# 1. 需要 Python 3.8+（推荐 3.12）。没有就先装:
winget install -e --id Python.Python.3.12

# 2. 解压后进目录
cd <解压出来的>\crosspc-0.1.0

# 3. 先体检（安全, 不会接管键鼠）
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
curl -L -O https://github.com/Cang-Lu/CrossPC/releases/download/v0.1.0-test2/crosspc-0.1.0.zip
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

## 已经验证过的 / 还没验证的

**已验证**：200 个单元/集成测试（Windows 3.12 + Ubuntu 3.8/3.11/3.13）、22 项端到端回环
自测（含真 Linux 内核上跑通）、真实 Windows 剪辑板图片往返逐像素一致、`pip install .` 与
发行包内容校验。

**还没验证**（需要真实两台机器，也就是你这次要做的事）：Windows 上真实的钩子捕获/接管、
Debian 上真实的 X11/uinput 注入、`install_linux.sh` 在真 Debian 上跑一遍、真实跨机剪贴板。
开发机是 Windows，这些在本机跑不了。

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
