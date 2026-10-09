# CrossPC Windows-side install / check script
#
# What it does:
#   1. checks whether Python is usable (and detects the Microsoft Store stub)
#   2. checks / adds the firewall inbound rule (needs administrator)
#   3. creates a config file (if there is none yet)
#   4. prints the next steps
#
# Usage (PowerShell, normal privileges are enough; the firewall step needs
# administrator):
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
function Warn2($msg) { Write-Host "[CrossPC] [warning] $msg" -ForegroundColor Yellow }
function Bad($msg) { Write-Host "[CrossPC] [FAILED] $msg" -ForegroundColor Red }

Say "Working directory: $root"
Say ""

# ---------------------------------------------------------------- 1. Python
$python = $null
foreach ($cand in @('python', 'python3', 'py')) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    if ($cmd.Source -like '*WindowsApps*') {
        Warn2 "$cand points to the Microsoft Store stub, which does not really run code"
        Warn2 "  Please install the real Python: winget install -e --id Python.Python.3.12"
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
    Bad "No usable Python 3 found"
    Say "How to install it (pick either):"
    Say "  winget install -e --id Python.Python.3.12"
    Say "  or download it from https://www.python.org/downloads/windows/ and tick Add to PATH"
    exit 1
}

# CrossPC only relies on the standard library; this just confirms that
& $python -c "import ctypes, socket, json, threading; print('stdlib ok')" | Out-Null
Ok "Standard library check passed (CrossPC needs no pip installs)"

# ---------------------------------------------------------------- 2. firewall
$ruleName = 'CrossPC'
$existing = netsh advfirewall firewall show rule name="$ruleName" 2>$null
if ($LASTEXITCODE -eq 0 -and $existing -match 'CrossPC') {
    Ok "Firewall rule '$ruleName' already exists"
} else {
    $isAdmin = ([Security.Principal.WindowsPrincipal] `
        [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if ($AddFirewallRule -and $isAdmin) {
        netsh advfirewall firewall add rule name="$ruleName" dir=in action=allow `
            protocol=TCP localport=$Port | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Ok "Inbound rule added: TCP $Port"
        } else {
            Bad "Failed to add the firewall rule"
        }
    } else {
        Warn2 "Inbound TCP $Port is not allowed yet, so the client may not be able to connect. Run this from an administrator command prompt:"
        Say  "    netsh advfirewall firewall add rule name=`"CrossPC`" dir=in action=allow protocol=TCP localport=$Port"
        if (-not $IsAdmin -and $AddFirewallRule) {
            Say  "  (not running as administrator, so -AddFirewallRule had no effect)"
        }
    }
}

# ---------------------------------------------------------------- 3. config file
$cfg = Join-Path $root 'crosspc.json'
if (Test-Path $cfg) {
    Ok "Config file already exists: $cfg"
} else {
    & $python -m crosspc init --config $cfg
    Ok "Config file created: $cfg"
}

# ---------------------------------------------------------------- 4. self-check
Say ""
Say "Environment self-check (safe, will not take over the keyboard and mouse):"
& $python -m crosspc doctor

Say ""
Ok "Install check complete. Next steps:"
Say "  1) Set the relative position:  $python -m crosspc gui"
Say "  2) Start the server:           $python -m crosspc server"
Say "  3) On Debian, run:             python3 -m crosspc client --host <this machine's IP>"
