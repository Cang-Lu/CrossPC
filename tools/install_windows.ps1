# CrossPC Windows 端安装/检查脚本
#
# 做的事:
#   1. 检查 Python 是否可用(并识别 Microsoft Store 占位程序)
#   2. 检查/添加防火墙入站规则(需要管理员)
#   3. 生成一份配置文件(如果还没有)
#   4. 打印下一步
#
# 用法(PowerShell, 普通权限即可; 加防火墙规则那步需要管理员):
#   powershell -ExecutionPolicy Bypass -File tools\install_windows.ps1
#   powershell -ExecutionPolicy Bypass -File tools\install_windows.ps1 -Port 39987 -AddFirewallRule

[CmdletBinding()]
param(
    [int]$Port = 39987,
    [switch]$AddFirewallRule
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

function Say($msg) { Write-Host "[CrossPC] $msg" }
function Ok($msg) { Write-Host "[CrossPC] [OK] $msg" -ForegroundColor Green }
function Warn2($msg) { Write-Host "[CrossPC] [注意] $msg" -ForegroundColor Yellow }
function Bad($msg) { Write-Host "[CrossPC] [失败] $msg" -ForegroundColor Red }

Say "工作目录: $root"
Say ""

# ---------------------------------------------------------------- 1. Python
$python = $null
foreach ($cand in @('python', 'python3', 'py')) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    if ($cmd.Source -like '*WindowsApps*') {
        Warn2 "$cand 指向 Microsoft Store 的占位程序, 它不会真的运行代码"
        Warn2 "  请安装真正的 Python: winget install -e --id Python.Python.3.12"
        continue
    }
    try {
        $ver = & $cmd.Source -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null
    } catch { continue }
    if ($LASTEXITCODE -eq 0 -and $ver) {
        $python = $cmd.Source
        Ok "Python $ver ($python)"
        break
    }
}

if (-not $python) {
    Bad "没有找到可用的 Python 3"
    Say "安装方式(任选其一):"
    Say "  winget install -e --id Python.Python.3.12"
    Say "  或去 https://www.python.org/downloads/windows/ 下载安装并勾选 Add to PATH"
    exit 1
}

# CrossPC 只依赖标准库, 这里只是确认一下
& $python -c "import ctypes, socket, json, threading; print('stdlib ok')" | Out-Null
Ok "标准库检查通过(CrossPC 不需要 pip 安装任何东西)"

# ---------------------------------------------------------------- 2. 防火墙
$ruleName = 'CrossPC'
$existing = netsh advfirewall firewall show rule name="$ruleName" 2>$null
if ($LASTEXITCODE -eq 0 -and $existing -match 'CrossPC') {
    Ok "防火墙规则 '$ruleName' 已存在"
} else {
    $isAdmin = ([Security.Principal.WindowsPrincipal] `
        [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if ($AddFirewallRule -and $isAdmin) {
        netsh advfirewall firewall add rule name="$ruleName" dir=in action=allow `
            protocol=TCP localport=$Port | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Ok "已添加入站规则: TCP $Port"
        } else {
            Bad "添加防火墙规则失败"
        }
    } else {
        Warn2 "还没有放行入站 TCP $Port, client 可能连不上。用管理员命令提示符执行:"
        Say  "    netsh advfirewall firewall add rule name=`"CrossPC`" dir=in action=allow protocol=TCP localport=$Port"
        if (-not $IsAdmin -and $AddFirewallRule) {
            Say  "  (当前不是管理员, -AddFirewallRule 未生效)"
        }
    }
}

# ---------------------------------------------------------------- 3. 配置文件
$cfg = Join-Path $root 'crosspc.json'
if (Test-Path $cfg) {
    Ok "配置文件已存在: $cfg"
} else {
    & $python -m crosspc init --config $cfg
    Ok "已生成配置文件: $cfg"
}

# ---------------------------------------------------------------- 4. 自检
Say ""
Say "环境自检(安全, 不会接管键鼠):"
& $python -m crosspc doctor

Say ""
Ok "安装检查完成。下一步:"
Say "  1) 设置相对位置:  $python -m crosspc gui"
Say "  2) 启动 server:    $python -m crosspc server"
Say "  3) Debian 上运行:  python3 -m crosspc client --host <本机IP>"
