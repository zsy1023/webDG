# ══════════════════════════════════════════════════════════════
#  webDG 一键启动（发行版用）
#
#  用法（双击 start.cmd 即可，等价于本脚本无参运行）：
#     .\start.ps1                 首次：建 .venv + 装依赖 + 自检 → 起全部节点 + 开面板
#     .\start.ps1 -Setup          只装环境/自检，不起节点（排障用）
#     .\start.ps1 -Reinstall      强制重建 .venv（依赖装坏了/换机器）
#     .\start.ps1 -Role b         ★ 跨机对端：把 [bridge] role 写成 b 再启动
#     .\start.ps1 -NoRelay        不起本机的 V4 中继
#     .\start.ps1 -Port 8888      面板换端口
#
#  设计要点（都是踩过坑才这么写的）：
#   * **自带 .venv**：首次启动在本目录建独立环境，不污染系统 Python；
#     装过一次就盖个 `.venv\.coyotelink-ready` 标记，之后秒起。
#   * **装依赖走代理**：本机直连 PyPI 会超时（实测），所以自动探测
#     `http://127.0.0.1:7897`；探不到就直连试一次，失败时把命令原样打出来让你手敲。
#   * **节点在独立窗口里跑**：日志是彩色的、Ctrl+C 能停、窗口关掉不影响本脚本退出。
#   * 一切"环境缺东西"都在启动**前**说清楚，不留"起了一半才发现"。
# ══════════════════════════════════════════════════════════════
[CmdletBinding()]
param(
    [switch]$Setup,
    [switch]$Reinstall,
    [switch]$NoRelay,
    [switch]$NoBrowser,
    [switch]$NoPause,
    [ValidateSet("", "a", "b")] [string]$Role = "",
    [int]$Port = 8780,
    [int]$RelayPort = 9997,
    [string]$Proxy = "",
    #: Zenoh 多播发现用哪块网卡（不填 = 默认路由那块；换网卡：-ZenohNic "以太网"）
    [string]$ZenohNic = "",
    #: 樱花穿透（**被控端/主机**）：樱花的「域名:端口」转发到我这里，别人连我走它
    [string]$Frp = "",
    #: 樱花穿透（**控制端/加入方**）：我要连的对方「域名:端口」
    [string]$Join = "",
    #: 手机 App 那条 socket 的对外地址（樱花 HTTPS 隧道），写进 [coyote_v4] public_url
    [string]$FrpRelay = "",
    #: 被控端本地监听端口（樱花映射到它；默认 7451）
    [int]$TunnelPort = 7451
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

# 中文 + 彩色输出：UTF-8 代码页，并让 Write-Host 的颜色能用
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
try { chcp 65001 > $null } catch { }

$VERSION = if (Test-Path (Join-Path $Root "VERSION")) {
    (Get-Content (Join-Path $Root "VERSION") -Raw).Trim()
} else { "dev" }

function Say  ($m) { Write-Host $m }
function Head ($m) { Write-Host ""; Write-Host ("═" * 62) -ForegroundColor DarkYellow; Write-Host " $m" -ForegroundColor Yellow; Write-Host ("═" * 62) -ForegroundColor DarkYellow }
function Ok   ($m) { Write-Host "  ✓ $m" -ForegroundColor Green }
function Warn ($m) { Write-Host "  ! $m" -ForegroundColor Yellow }
function Bad  ($m) { Write-Host "  ✗ $m" -ForegroundColor Red }
function Info ($m) { Write-Host "  · $m" -ForegroundColor DarkGray }

function ConvertTo-NativeArgLine {
    # ⚠️ `Start-Process -ArgumentList` 传**数组**时是「用空格把元素连起来」，**不会**给元素加引号。
    #    所以参数里一带空格就会当场被拆开 —— 发行版装在 `D:\Program Files\zsy\webDG` 时实测：
    #      python.exe: can't open file 'D:\Program': [Errno 2] No such file or directory
    #      cli: error: unrecognized arguments: Files\zsy\webDG\agent\nodes.toml
    #    （装在 `C:\Users\<你>\Desktop\webDG` 这种没空格的路径下一直没露馅。）
    #    这里自己按 C 运行库的解析规则引号化：含空白/引号的元素加引号，内层引号转义成 \"，
    #    引号前的那串反斜杠翻倍，行尾反斜杠也翻倍。
    #    （不用 `-ArgumentList` 单个字符串的话没法兼顾"引号 + 反斜杠"这两种边界。）
    param([string[]]$Arguments)
    $parts = foreach ($a in $Arguments) {
        $s = [string]$a
        if ($s -eq "") { '""'; continue }
        if ($s -notmatch '[\s"]') { $s; continue }
        $escaped = [regex]::Replace($s, '(\\*)"', '$1$1\"')   # 引号前的反斜杠翻倍
        $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')  # 行尾反斜杠翻倍
        '"' + $escaped + '"'
    }
    return ($parts -join ' ')
}

function Invoke-Native {
    # ⚠️ 三个坑，一次说清：
    #   1. `$ErrorActionPreference = "Stop"`（本脚本开头设的）时，**native 命令往 stderr
    #      写字**（哪怕退出码 0）会被当成终止性错误 → 脚本当场死。这里临时退到 Continue。
    #   2. native 的 stdout 默认进 success 流：调用方写 `$code = Invoke-Native …` 时，
    #      pip 那一大坨输出会被**一起塞进 $code**。所以退出码走 `-ExitCode [ref]` 回传，
    #      函数自己不往 success 流写东西。
    #   3. **别把 native 输出喂给 Write-Host**：PS 5.1 会拿控制台编码再编一遍，
    #      中文会变成 `�����Լ�` 这种乱码（实测）。让它直通控制台最干净。
    param([string]$Exe, [string[]]$Arguments, [ref]$ExitCode, [switch]$Quiet)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        if ($Quiet) { & $Exe @Arguments 2>&1 | Out-Null }
        else        { & $Exe @Arguments }
    } finally {
        $ErrorActionPreference = $prev
    }
    if ($ExitCode) { $ExitCode.Value = $LASTEXITCODE }
}

function Fail($m, $hint) {
    Write-Host ""
    Bad $m
    if ($hint) { Warn $hint }
    if (-not $NoPause) {
        Write-Host ""
        Write-Host "按回车关闭…" -ForegroundColor DarkGray
        Read-Host | Out-Null
    }
    exit 1
}

function Pause-IfInteractive {
    # 双击运行时留一屏让用户看清；脚本化（-NoPause）时直接退出
    if ($NoPause) { return }
    Write-Host ""
    Write-Host "按回车关闭…" -ForegroundColor DarkGray
    Read-Host | Out-Null
}

function Write-Utf8NoBom([string]$Path, [string[]]$Lines) {
    # ⚠️ 写 `agent\config.toml` **必须不带 BOM**：
    #    PS 5.1 的 `Set-Content -Encoding UTF8` 会写 BOM，而 Python 的 tomllib 见到 BOM
    #    直接 `Invalid statement (at line 1, column 1)` → **12 个节点全部启动失败 + 重启风暴**，
    #    满屏 traceback 却看不出是 BOM（实测踩过，代价一整个回合）。
    [System.IO.File]::WriteAllLines($Path, $Lines, (New-Object System.Text.UTF8Encoding($false)))
}

function Assert-Toml([string]$Path) {
    # 启动**之前**验一次配置：让"配置文件写坏了"当场说清楚，
    # 而不是让 12 个节点各刷一屏 traceback（那种输出用户只会说"一堆报错"）。
    if (-not (Test-Path $Path)) { return $true }
    $check = 1
    Invoke-Native $VenvPython @(
        "-c",
        "import pathlib,sys,tomllib; tomllib.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))",
        $Path
    ) -Quiet -ExitCode ([ref]$check)
    if ($check -ne 0) {
        Fail "agent\config.toml 不是合法的 TOML（上面的报错就是原因）" `
             "最快的修法：删掉这个文件，再双击一次 start.cmd（会自动从模板重新生成）。"
    }
    return $true
}

Head "webDG $VERSION —— 远程联动控制系统"
Info "目录：$Root"
Info ("Python 环境：自带的 .venv（首次启动会自动创建，约几分钟）")

# ──────────────────────────────────────────────────────────────
# 1. 找一个基础 Python（只在**建 venv** 时用得上）
# ──────────────────────────────────────────────────────────────
function Find-BasePython {
    foreach ($cand in @(@("py", "-3"), @("python", ""), @("python3", ""))) {
        $exe = $cand[0]; $arg = $cand[1]
        $cmd = Get-Command $exe -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        # 同上：`py -3` 在没有 3.x 时会往 stderr 抱怨，Stop 模式下会当场炸
        $prev = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        $out = $null; $rc = 1
        try {
            $probe = "import sys;print('%d.%d'%sys.version_info[:2])"
            $out = if ($arg) { & $exe $arg -c $probe 2>$null } else { & $exe -c $probe 2>$null }
            $rc = $LASTEXITCODE
        } catch {
            $rc = 1
        } finally {
            $ErrorActionPreference = $prev
        }
        if ($rc -ne 0 -or -not $out) { continue }
        try {
            $ver = [version]($out | Select-Object -First 1)
            if ($ver -ge [version]"3.10") {
                return @{ Exe = $exe; Arg = $arg; Version = $out }
            }
        } catch { }
    }
    return $null
}

$VenvDir    = Join-Path $Root ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$ReadyMark  = Join-Path $VenvDir ".coyotelink-ready"

# `.venv` 里记的是**绝对路径**（pyvenv.cfg / Scripts 里的 shebang）——把这个目录**拷到
# 别的机器**（或换用户名）后它就是个坏的，光看"文件在不在"会以为环境是好的。
# 所以做一次**功能检查**：跑不起来就丢掉重建。这就是"拷走就能用"能不能成立的关键一步。
if (Test-Path $VenvPython) {
    $probe = 1
    Invoke-Native $VenvPython @("-c", "import sys;print(sys.prefix)") -Quiet -ExitCode ([ref]$probe)
    if ($probe -ne 0) {
        Warn ".venv 用不了（多半是从别的机器/别的用户拷来的，里面记的是绝对路径）→ 重建"
        Remove-Item $VenvDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

$NeedSetup  = $Reinstall -or (-not (Test-Path $VenvPython)) -or (-not (Test-Path $ReadyMark))

if ($Reinstall -and (Test-Path $VenvDir)) {
    Warn "按 -Reinstall 重建 .venv（删旧环境）"
    Remove-Item $VenvDir -Recurse -Force
}

# ──────────────────────────────────────────────────────────────
# 2. 环境：建 venv → 装依赖（只做一次）
# ──────────────────────────────────────────────────────────────
function Resolve-Proxy {
    if ($Proxy) { return $Proxy }
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $client.Connect("127.0.0.1", 7897)
        $client.Close()
        return "http://127.0.0.1:7897"
    } catch { return "" }
}

function Install-Env {
    if (-not (Test-Path $VenvPython)) {
        $base = Find-BasePython
        if (-not $base) {
            Fail "找不到可用的 Python（需要 3.10+）。" `
                 "装一个 Python 3.13（https://www.python.org/downloads/，勾选 Add to PATH），或先把本机已装的 python 加进 PATH，然后重新双击 start.cmd。"
        }
        Info "基础解释器：$($base.Exe) $($base.Arg)  (Python $($base.Version))"
        Say ""
        Say "  正在创建独立环境 .venv …" -ForegroundColor Cyan
        $venvArgs = if ($base.Arg) { @($base.Arg, "-m", "venv", $VenvDir) }
                    else { @("-m", "venv", $VenvDir) }
        $code = 0; Invoke-Native $base.Exe $venvArgs -ExitCode ([ref]$code)
        if ($code -ne 0 -or -not (Test-Path $VenvPython)) {
            Fail "创建 .venv 失败（python -m venv 没成功）" `
                 "装上完整版 Python（含 venv/ensurepip）再试；或用已存在的解释器手动：python -m venv .venv"
        }
        Ok ".venv 已创建"
    } else {
        Ok ".venv 已存在"
    }

    $px = Resolve-Proxy
    if ($px) { Info "pip 走代理：$px" } else { Warn "没探测到本机代理（7897）；直连试试，慢或失败属正常" }

    $common = @("-m", "pip", "install", "--disable-pip-version-check")
    if ($px) { $common += @("--proxy", $px) }

    Say ""
    Say "  正在安装依赖（首次约几分钟）…" -ForegroundColor Cyan
    $req = Join-Path $Root "requirements.txt"
    $pipArgs = @($common + @("-r", $req))
    $code = 0; Invoke-Native $VenvPython $pipArgs -ExitCode ([ref]$code)
    if ($code -ne 0) {
        Write-Host ""
        Warn "依赖安装失败 —— 可以手动重试："
        $manual = ".venv\Scripts\python.exe " + ($pipArgs -join " ")
        Write-Host "    $manual" -ForegroundColor Cyan
        Fail "依赖没装成功，先不启动（半装状态下起节点会连环报错）。" `
             "常见原因：没开代理 / 代理端口不是 7897（用 -Proxy http://127.0.0.1:端口 指定）。"
    }
    Ok "依赖安装完成"

    # 郊狼 V4 官方 SDK：未上 PyPI，发行版里带了源码（vendor\），必须从本地装
    $sdk = Join-Path $Root "vendor\dglab-kit-python"
    if (Test-Path $sdk) {
        Info "安装郊狼 V4 SDK（vendor\dglab-kit-python）…"
        # `--no-cache-dir`：本机实测 pip 的 wheel 缓存目录会 [WinError 5] 拒绝访问，
        # 这一条只装一个小包，绕过缓存最稳。
        $sdkArgs = @($common + @("--no-cache-dir", $sdk))
        $code = 0; Invoke-Native $VenvPython $sdkArgs -ExitCode ([ref]$code)
        if ($code -ne 0) {
            Warn "V4 SDK 装不上 → coyote_v4 节点会以「手动传输模式」降级运行（其它节点不受影响）"
            Write-Host "    手动重试：" -ForegroundColor DarkGray
            Write-Host ("    .venv\Scripts\python.exe " + ($sdkArgs -join " ")) -ForegroundColor Cyan
        } else {
            $check = 1; Invoke-Native $VenvPython @("-c", "import dglab_kit_python") -Quiet -ExitCode ([ref]$check)
            if ($check -ne 0) { Warn "V4 SDK 装了但 import 不了 → coyote_v4 会降级" }
            else { Ok "V4 SDK 就绪" }
        }
    } else {
        Warn "没带 vendor\dglab-kit-python → coyote_v4 会降级（感知/面板仍可用）"
    }

    Set-Content -Path $ReadyMark -Value (Get-Date -Format "yyyy-MM-dd HH:mm:ss") -Encoding UTF8
    Ok "环境标记已写入（下次启动直接跳过安装）"
}

if ($NeedSetup) { Install-Env } else { Ok "Python 环境已就绪（.venv）" }

# ──────────────────────────────────────────────────────────────
# 3. 配置：没有 config.toml 就从模板拷一份
# ──────────────────────────────────────────────────────────────
$ConfigPath = Join-Path $Root "agent\config.toml"
$Example    = Join-Path $Root "agent\config.example.toml"
if (-not (Test-Path $ConfigPath)) {
    if (Test-Path $Example) {
        Copy-Item $Example $ConfigPath
        Ok "首次运行：已从模板生成 agent\config.toml（要改串口/relay 地址就编辑它）"
    } else {
        Warn "没有 agent\config.toml 也没有模板 → 全部走默认值"
    }
} else {
    Ok "配置文件：agent\config.toml"
}

# ──────────────────────────────────────────────────────────────
# TOML 段写入的**唯一入口**（所有配置写入都走它，别各写各的）⚠️ 实测坑
# ──────────────────────────────────────────────────────────────
# 踩过的坑：几个函数各自遍历配置、用「当前遍历到的段是不是它」来定位，
# 一旦该段**后面还跟着别的段**（例如 [zenoh.raw_json5] 之后还有 [bridge]），
# 就会在文件末尾**再补一个同名表头** → TOML 里同名表出现两次 → `tomllib` 直接报错
# → 12 个节点全部起不来（那台 NAS 上实测就是这样，日志只说"配置文件不是合法 TOML"）。
# 规则：段只认**第一次**出现，重复表头直接丢掉（内容合并）；同名键覆盖；
# 值传 $null = 删掉这个键（用于清理旧版本写错的键）。
function Set-TomlSection([string]$Section, [hashtable]$Pairs) {
    if (-not (Test-Path $ConfigPath)) { return }
    $lines = @(Get-Content $ConfigPath -Encoding UTF8)
    $out = New-Object System.Collections.Generic.List[string]
    $inSec = $false; $seen = $false; $done = @{}
    foreach ($line in $lines) {
        if ($line -match '^\s*\[([^\]]+)\]\s*$') {
            if ($inSec) {                                  # 离开本段 → 把没写到的键补上
                foreach ($k in $Pairs.Keys) {
                    if ($null -ne $Pairs[$k] -and -not $done.ContainsKey($k)) { $out.Add("$k = $($Pairs[$k])"); $done[$k] = $true }
                }
                $inSec = $false
            }
            $name = $Matches[1].Trim()
            if ($name -eq $Section) {
                if ($seen) { continue }                    # 重复表头 → 合并进前一个
                $seen = $true; $inSec = $true
            }
            $out.Add($line); continue
        }
        if ($inSec) {
            $m = [regex]::Match($line, '^\s*("?[^"=\s]+"?)\s*=')
            if ($m.Success -and $Pairs.ContainsKey($m.Groups[1].Value.Trim())) {
                $k = $m.Groups[1].Value.Trim()
                if ($null -ne $Pairs[$k] -and -not $done.ContainsKey($k)) { $out.Add("$k = $($Pairs[$k])"); $done[$k] = $true }
                continue                                   # $null → 删掉旧键
            }
        }
        $out.Add($line)
    }
    if ($inSec) {
        foreach ($k in $Pairs.Keys) {
            if ($null -ne $Pairs[$k] -and -not $done.ContainsKey($k)) { $out.Add("$k = $($Pairs[$k])") }
        }
    }
    if (-not $seen) {
        $out.Add(""); $out.Add("[$Section]")
        foreach ($k in $Pairs.Keys) { if ($null -ne $Pairs[$k]) { $out.Add("$k = $($Pairs[$k])") } }
    }
    Write-Utf8NoBom $ConfigPath $out
}

# `-Role b`：跨机对端用。写进 [bridge] 段（没有就补一段）
function Set-BridgeRole([string]$r) {
    Set-TomlSection 'bridge' @{ 'role' = "`"$r`"" }
    Ok "[bridge] role = `"$r`"（跨机两端要一 a 一 b；用 bridge\README.md 里的排查表）"
}
if ($Role) { Set-BridgeRole $Role }

# ──────────────────────────────────────────────────────────────
# 选 Zenoh 用哪块网卡（多播发现用）—— 直接决定"同机节点能不能互相看见"
# ──────────────────────────────────────────────────────────────
# 为什么值得单独做成一个开关：本机 6 张网卡在 Up（以太网 / VMware VMnet1+VMnet8 /
# Radmin VPN / vEthernet / 蓝牙）时，zenoh 的多播发现会**只在部分网卡上生效**，
# 12 个节点被拆成两组 —— 症状就是面板点「开火 / 导入 / 加规则」没反应、状态不更新，
# 而发布方日志里明明写了"已 publish"（目标节点一条都没收到）。
# 所以：默认钉到**默认路由那块网卡**，不满意就用 `-ZenohNic "<网卡名>"` 换。
#
# ⚠️ 写进配置的值必须是**合法 JSON5**（`zenoh.Config.insert_json5` 直接吃这个字符串）：
#    正确：`"scouting/multicast/interface" = "\"以太网\""`
#    错误：`"scouting/multicast/interface" = "以太网"`   ← 裸字符串，session 直接打不开，
#          表现为**所有节点启动即退出**（这个坑我踩过）。
function Get-ZenohNicValue([string]$want) {
    $adapters = @(Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object { $_.Status -eq "Up" } |
        Sort-Object ifIndex)
    if (-not $adapters) { return "" }
    if ($want) {
        $hit = $adapters | Where-Object { $_.Name -eq $want -or $_.InterfaceDescription -eq $want } |
            Select-Object -First 1
        if ($hit) { return $hit.Name }
        Warn "指定的网卡 `"$want`" 不在活动网卡里 —— 列一下，改用默认路由那块："
        $adapters | ForEach-Object { Info ("    {0}（{1}）" -f $_.Name, $_.InterfaceDescription) }
    }
    # 默认：默认路由所在的网卡
    $route = Get-NetRoute -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue |
        Sort-Object RouteMetric, InterfaceMetric | Select-Object -First 1
    if ($route) {
        $hit = $adapters | Where-Object { $_.ifIndex -eq $route.InterfaceIndex } | Select-Object -First 1
        if ($hit -and $hit.Name) { return $hit.Name }
    }
    return ""
}

function Set-ZenohNic([string]$nicName) {
    # `scouting/multicast/interface` 写在 `[zenoh.raw_json5]` 里，值必须带 JSON5 引号
    if ($nicName) {
        Set-TomlSection 'zenoh.raw_json5' @{ '"scouting/multicast/interface"' = "`"\`"$nicName\`"`"" }
    } else {
        # 没有可用网卡（比如指定的名字这台机器上不存在）→ 删掉旧值，交给 zenoh 自己挑，
        # 比钉一个不存在的网卡好（后者会让多播发现直接失效）
        Set-TomlSection 'zenoh.raw_json5' @{ '"scouting/multicast/interface"' = $null }
    }
    Assert-Toml $ConfigPath | Out-Null
    return $nicName
}

function Get-ConfigZenohNic {
    if (-not (Test-Path $ConfigPath)) { return "" }
    foreach ($line in (Get-Content $ConfigPath -Encoding UTF8)) {
        if ($line -match '^\s*"scouting/multicast/interface"\s*=\s*"\s*\\"([^\\"]+)\\"\s*"') { return $Matches[1] }
    }
    return ""
}

$nicWant = $ZenohNic
if (-not $nicWant) { $nicWant = Get-ConfigZenohNic }     # 上次选的记着
$nicName = Get-ZenohNicValue $nicWant
$nicName = Set-ZenohNic $nicName
# ★ 樱花穿透的配置写入**绝不能拦住启动**：写错最多"链路不通"，不该让 12 个节点起不来。
# ──────────────────────────────────────────────────────────────
# 樱花穿透（跨机链路）：把「域名:端口」写进配置 —— 两端写法不同
# ──────────────────────────────────────────────────────────────
# 被控端/主机（`-Frp "域名:端口"`）：樱花把外面那个地址转发到本机 → 它 listen 出 TLS 端口；
# 加入方（`-Join "域名:端口"`）：连对方的 TLS 端点。
# 证书：自签（`tools/make_tls_cert.py` 生成到 data/tls），加入方**关掉名字校验**
# （用户明确说"证书没有验证无所谓"）。⚠️ 这只是链路加密，不是身份认证 ——
# 要防中间人就两边都配 `root_ca_certificate` + `enable_mtls = true`。
# 另外 `-FrpRelay "域名:端口"`：手机 App 那条 socket 走樱花（通常用樱花的 HTTPS 隧道，
# 证书由樱花提供；我们只把它写进配对二维码，所以是 wss://）。
function Split-HostPort([string]$value) {
    $v = ($value -replace '^[a-z]+://', '').Trim().TrimEnd('/')
    if ($v -notmatch '^([^:/]+):(\d+)$') { return $null }
    return @{ host = $Matches[1]; port = [int]$Matches[2] }
}

function Ensure-TunnelCert([string]$cn) {
    $dir = Join-Path $Root "data\tls"
    $cert = Join-Path $dir "cert.pem"
    $key = Join-Path $dir "key.pem"
    if ((Test-Path $cert) -and (Test-Path $key)) { return $dir }
    $script = Join-Path $Root "tools\make_tls_cert.py"
    if (-not (Test-Path $script)) {
        Warn "缺少 tools\make_tls_cert.py —— 没法生成 TLS 证书，TLS 链路用不了"
        return ""
    }
    $args = @($script, "--cn", $cn, "--dir", $dir)
    $code = 0
    Invoke-Native -Exe $VenvPython -Arguments $args -ExitCode ([ref]$code) -Quiet
    if ($code -ne 0 -or -not (Test-Path $cert)) {
        Warn "自签证书生成失败（exit=$code）—— 将退回不加密的 tcp/ 端点"
        return ""
    }
    Info "已生成自签证书：$dir"
    return $dir
}

# ──────────────────────────────────────────────────────────────
# ★ 每次启动都校正一次「跨机 TLS 证书路径」⚠️ 实测坑
# ──────────────────────────────────────────────────────────────
# 配置里存的是**绝对路径**（zenoh 只吃绝对路径）。一旦发行版被复制/移动到别的目录
# （实测：C:\...\web控制\webDG → C:\...\Desktop\webDG），这两个路径就成了死引用 →
# bridge 的 zenoh.open 抛 "Invalid TLS private key file" → 节点崩 → 监督器每 1~30 秒重启一次
# 刷满屏 traceback，而 7451 从头到尾没监听（跨机链路整个不存在）。
# 所以：只要配置里还有 `[bridge] listen` 且带 tls/，就按**当前 $Root** 重指证书；
# 缺证书就现生成；实在不行就把 listen 摘掉并明确告警 ——
# 宁可"没有跨机链路"，也绝不进入崩溃重启循环。
function Get-ConfigSectionText([string]$Section) {
    $out = New-Object System.Collections.Generic.List[string]
    $in = $false
    foreach ($line in (Get-Content $ConfigPath -Encoding UTF8)) {
        if ($line -match '^\s*\[([^\]]+)\]\s*$') {
            $in = ($Matches[1].Trim() -eq $Section)
            if ($in) { continue }
        }
        if ($in) { $out.Add($line) }
    }
    return ($out -join "`n")
}

function Repair-TunnelCertPaths {
    if (-not (Test-Path $ConfigPath)) { return }
    $bridgeText = Get-ConfigSectionText 'bridge'
    if ($bridgeText -notmatch '\blisten\s*=') { return }   # 没 listen → 不涉及证书
    if ($bridgeText -notmatch 'tls/') { return }           # 纯 tcp/ 端点 → 不需要证书
    $dir = Join-Path $Root "data\tls"
    if (-not ((Test-Path (Join-Path $dir 'cert.pem')) -and (Test-Path (Join-Path $dir 'key.pem')))) {
        $dir = Ensure-TunnelCert "host"
    }
    if (-not $dir) {
        Warn "跨机 TLS 证书不可用 → 摘掉 [bridge] listen（本机房间照常；跨机链路先关闭，跑 tools\make_tls_cert.py 可恢复）"
        Set-TomlSection 'bridge' @{ 'listen' = $null }
        Assert-Toml $ConfigPath | Out-Null
        return
    }
    $cert = (Join-Path $dir "cert.pem") -replace '\\', '/'
    $key = (Join-Path $dir "key.pem") -replace '\\', '/'
    Set-TomlSection 'bridge.raw_json5' @{
        '"transport/link/tls/listen_certificate"' = "`"\`"$cert\`"`""
        '"transport/link/tls/listen_private_key"' = "`"\`"$key\`"`""
        '"transport/link/tls/root_ca_certificate"' = "`"\`"$cert\`"`""
        '"transport/link/tls/verify_name_on_connect"' = 'false'
        '"transport/link/tls/enable_mtls"' = 'false'
    }
    Assert-Toml $ConfigPath | Out-Null
    Ok "跨机 TLS 证书已按当前目录校正：$cert"
}

function Set-TunnelConfig {
    if (-not ($Frp -or $Join -or $FrpRelay)) { return }
    $bridgeKeys = @{}
    $tlsKeys = @{}
    if ($Frp) {
        $hp = Split-HostPort $Frp
        if (-not $hp) { Warn "-Frp 格式要写成「域名:端口」，例如 -Frp `"abc.natfrp.com:12345`""; return }
        $tlsDir = Ensure-TunnelCert $hp.host
        if (-not $tlsDir) { return }
        # ★ 端点写给 **bridge 节点**，写进 `[bridge] listen/connect` —— 它只读这里
        #   （agent/nodes/bridge/node.py: cfg.get("listen") / cfg.get("connect")）。
        #   写 `[zenoh] listen` 是**错的**：那是 12 个节点各自 session 的配置，
        #   结果是每个节点都去抢 7451 端口，而 bridge 自己反而"未配置端点"（实测踩过）。
        $bridgeKeys['listen'] = "[`"tls/0.0.0.0:$TunnelPort`"]"
        $cert = (Join-Path $tlsDir "cert.pem") -replace '\\', '/'
        $key = (Join-Path $tlsDir "key.pem") -replace '\\', '/'
        # ⚠️ 下面是 JSON5 片段 → 字符串值必须**再转义一层引号**
        #    （正确：`"key" = "\"C:/x.pem\""`；漏了转义整个 session 打不开、所有节点起不来，实测踩过）
        $tlsKeys['"transport/link/tls/listen_certificate"'] = "`"\`"$cert\`"`""
        $tlsKeys['"transport/link/tls/listen_private_key"'] = "`"\`"$key\`"`""
        $tlsKeys['"transport/link/tls/root_ca_certificate"'] = "`"\`"$cert\`"`""
    }
    if ($Join) {
        $hp = Split-HostPort $Join
        if (-not $hp) { Warn "-Join 格式要写成「域名:端口」，例如 -Join `"abc.natfrp.com:12345`""; return }
        if (-not (Ensure-TunnelCert "client")) { return }
        $bridgeKeys['connect'] = "[`"tls/$($hp.host):$($hp.port)`"]"
    }
    # 只对 **bridge 的远端 session** 关校验（`[bridge.raw_json5]` 就是喂给它的）
    $tlsKeys['"transport/link/tls/verify_name_on_connect"'] = 'false'
    $tlsKeys['"transport/link/tls/enable_mtls"'] = 'false'
    # 清掉旧版本写错位置的键（写在 [zenoh] / [zenoh.raw_json5] 里 —— 那会儿链路根本起不来）
    Set-TomlSection 'zenoh' @{ 'listen' = $null; 'connect' = $null }
    Set-TomlSection 'zenoh.raw_json5' @{
        '"transport/link/tls/verify_name_on_connect"' = $null
        '"transport/link/tls/enable_mtls"' = $null
        '"transport/link/tls/listen_certificate"' = $null
        '"transport/link/tls/listen_private_key"' = $null
        '"transport/link/tls/root_ca_certificate"' = $null
    }
    Set-TomlSection 'bridge' $bridgeKeys
    Set-TomlSection 'bridge.raw_json5' $tlsKeys
    Assert-Toml $ConfigPath | Out-Null
    if ($Frp) {
        Ok "樱花（被控端）：外面 → $Frp 转发到本机 $TunnelPort，链路走 TLS（自签，不校验）"
        Info "   同房间的对端用：start.cmd -Join `"$Frp`" -Role b"
    }
    if ($Join) { Ok "樱花（控制端）：连 $Join（TLS，不校验证书）" }
    if ($FrpRelay) {
        $relayUrl = if ($FrpRelay -match '^wss?://') { $FrpRelay } else { "wss://$FrpRelay" }
        $lines = Get-Content $ConfigPath -Encoding UTF8
        $out = New-Object System.Collections.Generic.List[string]
        $inCoyote = $false; $had = $false
        foreach ($line in $lines) {
            if ($line -match '^\s*\[([^\]]+)\]\s*$') {
                if ($inCoyote -and -not $had) { $out.Add("public_url = `"$relayUrl`""); $had = $true }
                $inCoyote = ($Matches[1].Trim() -eq "coyote_v4")
                $out.Add($line); continue
            }
            if ($inCoyote -and $line -match '^\s*public_url\s*=') {
                $out.Add("public_url = `"$relayUrl`""); $had = $true; continue
            }
            $out.Add($line)
        }
        if ($inCoyote -and -not $had) { $out.Add("public_url = `"$relayUrl`"") }
        if (-not $had) { Warn "配置里没有 [coyote_v4] 段，public_url 没写进去" }
        Write-Utf8NoBom $ConfigPath $out
        Assert-Toml $ConfigPath | Out-Null
        Ok "手机 App 那条 socket 对外地址：$relayUrl（写进 [coyote_v4] public_url，配对二维码会用它）"
    }
}

try { Set-TunnelConfig } catch { Warn "樱花穿透配置写入失败（不影响本机运行）：$_" }
# ★ 每次启动都校正一次跨机 TLS 证书路径（发行版被复制/移动后，配置里的绝对路径会失效 →
#   否则 bridge 会因 "Invalid TLS private key file" 崩进重启循环、7451 永远起不来）
try { Repair-TunnelCertPaths } catch { Warn "跨机 TLS 证书路径校正失败（不影响本机运行）：$_" }
if ($nicName) {
    Ok "Zenoh 多播发现用网卡：$nicName（换一块：start.cmd -ZenohNic `"<网卡名>`"）"
    $up = @(Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object { $_.Status -eq "Up" })
    if ($up.Count -gt 1) {
        Info ("   活动中还有 {0} 块网卡：{1}" -f ($up.Count - 1), (($up | Where-Object { $_.Name -ne $nicName } | ForEach-Object { $_.Name }) -join " / "))
    }
} else {
    Warn "没挑到网卡 → Zenoh 多播发现用默认设置（同机节点可能有丢命令的毛病）"
}

# 读一下配置里的关键值（给下面的提示用）
function Get-LanIp {    # 本机在局域网里的地址（手机要连的那个）。挑**默认路由**所在的网卡，
    # 这样自动避开 VMware / Radmin / Hyper-V 那些虚拟网卡。
    try {
        $route = Get-NetRoute -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue |
            Sort-Object RouteMetric, InterfaceMetric | Select-Object -First 1
        if ($route) {
            $ip = Get-NetIPAddress -InterfaceIndex $route.InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
                Where-Object { $_.IPAddress -notmatch "^127\." } | Select-Object -First 1
            if ($ip) { return $ip.IPAddress }
        }
    } catch { }
    return ""
}


function Get-ConfigValue([string]$section, [string]$key) {
    if (-not (Test-Path $ConfigPath)) { return "" }
    $cur = ""; $val = ""
    foreach ($line in (Get-Content $ConfigPath -Encoding UTF8)) {
        if ($line -match '^\s*\[([^\]]+)\]\s*$') { $cur = $Matches[1].Trim(); continue }
        if ($cur -ne $section) { continue }
        if ($line -match "^\s*$key\s*=\s*(.+?)\s*(#.*)?$") { $val = $Matches[1].Trim().Trim('"'); }
    }
    return $val
}
$V4Url   = Get-ConfigValue "coyote_v4" "url"
$SerialP = Get-ConfigValue "sim_coyote" "port"
Assert-Toml $ConfigPath | Out-Null          # 改过/生成过配置 → 启动前先验一次

# ──────────────────────────────────────────────────────────────
# 4. 模型自检（发行版已打包，缺了才需要下载）
# ──────────────────────────────────────────────────────────────
$modelsDir = Join-Path $Root "models"
$hasPose = @(Get-ChildItem $modelsDir -Filter *.task -ErrorAction SilentlyContinue).Count -gt 0
$hasAsr  = @(Get-ChildItem $modelsDir -Filter "sherpa-onnx-*" -Directory -ErrorAction SilentlyContinue).Count -gt 0
if ($hasPose) { Ok "姿态/表情模型：已就绪" } else { Warn "缺 MediaPipe 模型 → 跑 .venv\Scripts\python.exe tools\download_models.py 下载" }
if ($hasAsr)  { Ok "语音识别模型：已就绪" } else { Warn "缺语音识别模型 → 面板仍能用，只是识别结果为空" }

# ──────────────────────────────────────────────────────────────
# 5. 自检（-Setup 就到这里为止）
# ──────────────────────────────────────────────────────────────
if ($Setup) {
    Say ""
    Say "  环境自检（tools\doctor.py）…" -ForegroundColor Cyan
    Invoke-Native $VenvPython @((Join-Path $Root "tools\doctor.py"))
    Head "环境已就绪（-Setup 模式：没起节点）"
    Info "双击 start.cmd 就会启动全部节点 + 打开面板"
    Pause-IfInteractive
    exit 0
}

# ──────────────────────────────────────────────────────────────
# 6. 起服务
# ──────────────────────────────────────────────────────────────
# ★ 跑业务（中继 + 12 个节点）用哪个解释器 —— 这里踩了一个很隐蔽的坑：
#
#   **经 .venv 的 launcher 起的进程，在这台机器上打不开真摄像头。**
#
#   2026-10-04 反复实测（同一份 cv2 5.0.0.93、同一份 pyvenv.cfg、同一个发行版目录，
#   直接用项目自己的 agent.core.devices.CameraSource 验）：
#     .venv\Scripts\python.exe（launcher：253KB，会 CreateProcess 一个基础解释器子进程）
#        → 4 台里只剩 OBS Virtual Camera 能开；HDI/EOS/ACER 一律
#          `打开失败：HDI Webcam USB（设备忙 / 被其它程序占用 / 已被拔掉）`
#          （同一时刻 ffmpeg 能抓到 HDI 的真实帧 ⇒ 相机根本没被占用）
#     基础解释器 D:\Miniconda3\python.exe + PYTHONPATH=<.venv>\Lib\site-packages
#        → HDI/EOS/ACER/OBS 四台全开，640x480 正常出帧 ✓
#     把发行版整目录改名成纯 ASCII（webcontrol）后再用 .venv 的解释器启动
#        → **照样失败**（所以不是"路径含中文"，是 launcher 那条路）
#
#   后果链条（用户就是这么看到"选了摄像头但没图像"的）：
#   camera 节点自动回退到 **OBS Virtual Camera（占位画面、没人）** →
#   pose 永远 present=False → **骨骼画布空白**，面板只能说"虚拟摄像头"。
#   （开发期一直用 conda 的解释器直接跑，所以"以前是好的"。）
#
#   所以：**节点/中继一律用基础解释器跑，依赖仍从 .venv 那套来**（PYTHONPATH 指过去）。
#   管环境（pip / venv / doctor）继续用 .venv 的 python，两者不混。
function Test-AsciiOnly([string]$p) {
    if (-not $p) { return $false }
    return (-not ($p -match '[^\u0020-\u007E]'))
}
function Get-VenvBaseHome([string]$venvDir) {
    # pyvenv.cfg 里的 `home` = 建这个 venv 时用的基础 Python 所在目录
    $cfg = Join-Path $venvDir "pyvenv.cfg"
    if (-not (Test-Path $cfg)) { return "" }
    foreach ($line in (Get-Content $cfg -Encoding UTF8)) {
        if ($line -match '^\s*home\s*=\s*(.+?)\s*$') { return $Matches[1].Trim().Trim('"') }
    }
    return ""
}

$Python     = $VenvPython      # 兜底：连基础解释器都找不到才用它（那时摄像头会退化到虚拟摄像头）
$NodePyPath = ""
$baseHome   = Get-VenvBaseHome $VenvDir
$baseExe    = if ($baseHome) { Join-Path $baseHome "python.exe" } else { "" }
if ($baseExe -and (Test-Path $baseExe)) {
    $Python     = $baseExe
    $NodePyPath = Join-Path $VenvDir "Lib\site-packages"
    Ok "节点用基础解释器运行：$Python"
    Info "   依赖来自　：$NodePyPath（.venv 里那套，一个不少）"
    Info "   为什么要这样：经 .venv 的 launcher 起的进程打不开真摄像头（只剩 OBS 虚拟摄像头）"
    if (-not (Test-AsciiOnly $baseExe)) {
        Warn "   ⚠️ 基础解释器路径含非 ASCII：真摄像头可能还是打不开 → 换个纯 ASCII 路径装 Python"
    }
} else {
    Warn "找不到 .venv 对应的基础解释器（$VenvDir\pyvenv.cfg 的 home）→ 退回用 .venv 的解释器"
    Warn "   ⚠️ 实测这条路真摄像头多半打不开，面板只剩 OBS Virtual Camera（画面里没人 → 没有姿态数据）"
    Warn "   解法：装一个完整 Python（含 venv），删掉 .venv 后重新双击 start.cmd"
}
if ($NodePyPath) { $env:PYTHONPATH = $NodePyPath }

# 6a. V4 中继（单机演示用；对端地址不是本机就跳过）
if (-not $NoRelay -and $V4Url) {
    $isLocal = $V4Url -match '(?i)ws://(127\.0\.0\.1|localhost)'
    if ($isLocal) {
        $relayRoom = if ($V4Url -match '[?&]room=([A-Za-z0-9]+)') { $Matches[1] } else { "" }
        if (-not $relayRoom) {
            # 本地中继但配置里没带口令 → **自动补一个并写回配置**。
            # 中继要求两端都带 ?room=，不补的话郊狼那条链路一直是红的，
            # 用户会当成"功能坏了"（第一次跑发行版就会撞上）。
            $chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789".ToCharArray()
            $relayRoom = -join (1..6 | ForEach-Object { $chars[(Get-Random -Maximum $chars.Length)] })
            $newUrl = "ws://127.0.0.1:${RelayPort}?room=$relayRoom"
            $out = New-Object System.Collections.Generic.List[string]
            $cur = ""; $done = $false
            foreach ($line in (Get-Content $ConfigPath -Encoding UTF8)) {
                if ($line -match '^\s*\[([^\]]+)\]\s*$') { $cur = $Matches[1].Trim(); $out.Add($line); continue }
                if ($cur -eq "coyote_v4" -and $line -match '^\s*url\s*=') {
                    $out.Add("url = `"$newUrl`""); $done = $true; continue
                }
                $out.Add($line)
            }
            if (-not $done) { $out.Add(""); $out.Add("[coyote_v4]"); $out.Add("url = `"$newUrl`"") }
            Write-Utf8NoBom $ConfigPath $out
            Assert-Toml $ConfigPath | Out-Null      # 刚写过配置 → 立刻验，别等 12 个节点崩
            $V4Url = $newUrl
            Ok "本地中继没配口令 → 已生成并写进配置：$newUrl"
        }
        Info "启动 V4 中继：ws://0.0.0.0:${RelayPort}?room=$relayRoom（监听全网卡，手机才连得进来）"
        # ⚠️ 以前写死 `--host 127.0.0.1`（为躲防火墙询问）→ 手机从局域网连不进来
        #    （用户实测："改了 ip 还是连不上！你是不是没放行"）。现在监听 0.0.0.0，
        #    首次会弹一次"是否允许访问网络"→ 点允许；放行命令也打印在下面。
        #    跨机时对端按打印出来的地址连即可。
        Start-Process -FilePath $Python `
            -ArgumentList (ConvertTo-NativeArgLine @((Join-Path $Root "tools\relay.py"), "--host", "0.0.0.0",
                                                     "--port", "$RelayPort", "--room", $relayRoom)) `
            -WorkingDirectory $Root -NoNewWindow | Out-Null
        # ★ 必须监听 0.0.0.0：手机 App 要**从局域网**连进来（扫的就是配对二维码）。
        #   以前这里写死 127.0.0.1（为了躲防火墙询问）→ 手机怎么都连不上
        #   （用户实测："改了 ip 还是连不上啊！！！你是不是没放行"）。
        #   首次监听非回环地址时 Windows 会弹一次"是否允许访问网络"→ 点允许即可。
        $LanIp = Get-LanIp
        if ($LanIp) { Info "手机要连的地址：ws://${LanIp}:${RelayPort}?room=$relayRoom（配对二维码用的就是它）" }
        $ruleName = "webDG relay ${RelayPort}"
        $hasRule = (netsh advfirewall firewall show rule name="$ruleName" 2>$null) -match "Rule Name"
        if (-not $hasRule) {
            Warn "手机连不上？先在**管理员** PowerShell 里跑一次这个放行 $RelayPort："
            Info "  netsh advfirewall firewall add rule name=`"$ruleName`" dir=in action=allow protocol=TCP localport=${RelayPort}"
        }
    } else {
        Info "对端 relay 在别的机器（$V4Url）→ 本机不起中继"
    }
} elseif ($NoRelay) {
    Info "按 -NoRelay：不起 V4 中继"
} else {
    Warn "没配 [coyote_v4] url → 郊狼走「手动传输模式」（其它功能不受影响）"
}

# 6b. 全部节点：`run-all` 作为**本窗口的子进程**跑
#
# 为什么不是"另开一个窗口"：实测在某些环境（非交互式会话 / 受限沙箱 / 组策略）
# `Start-Process cmd.exe` 根本开不出窗口，节点一个都不起，而启动器却以为启动成功了。
# 放在本窗口里最稳：日志（彩色）就在眼前，Ctrl+C 一次停全部 —— 所见即所得。

# 幂等：已经在跑就别再起一份（实测撞过"两个 supervisor、每个节点两份"的尴尬）
function Get-RunningSupervisors {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains($Root) -and $_.CommandLine -match 'run-all' }
}

$running = @(Get-RunningSupervisors)
if ($running.Count -gt 0) {
    Ok "已经在运行（supervisor pid=$($running[0].ProcessId)）—— 不重复启动"
    $url0 = "http://127.0.0.1:${Port}"
    Info "面板：$url0"
    Info "要重启：先双击 stop.cmd，再双击 start.cmd"
    if (-not $NoBrowser) { Start-Process $url0 | Out-Null }
    Pause-IfInteractive
    exit 0
}

Info "启动全部节点（12 个进程，Ctrl+C 可停全部）…"
Say ""
# ⚠️ 显式传 `--config <本目录>\agent\nodes.toml`：
#   解释器可能不在本目录里（见上面"跑业务用哪个解释器"），而 stop.ps1 是靠
#   "命令行里带本发行版目录"来认进程的 —— 不传这个参数，节点就变成"杀不掉的孤儿"。
$nodesToml = Join-Path $Root "agent\nodes.toml"
$runAllArgs = @("-m", "agent.core.cli", "run-all")
if (Test-Path $nodesToml) { $runAllArgs += @("--config", $nodesToml) }
$nodes = Start-Process -FilePath $Python `
    -ArgumentList (ConvertTo-NativeArgLine $runAllArgs) `
    -WorkingDirectory $Root -NoNewWindow -PassThru

# 6c. 等面板起来（冷启动要加载模型，给足 90s），再开浏览器
$url = "http://127.0.0.1:${Port}"
Write-Host "  等待面板应答" -NoNewline -ForegroundColor DarkGray
$up = $false
for ($i = 0; $i -lt 180; $i++) {
    Start-Sleep -Milliseconds 500
    if ($nodes.HasExited) { break }
    try {
        $resp = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 "${url}/healthz"
        if ($resp.StatusCode -eq 200) { $up = $true; break }
    } catch { }
    if ($i % 10 -eq 9) { Write-Host "." -NoNewline -ForegroundColor DarkGray }
}
Write-Host ""

Head "webDG 已启动"
if ($up) {
    Ok "面板就绪：$url"
} elseif ($nodes.HasExited) {
    Bad "节点进程已退出（exit=$($nodes.ExitCode)）—— 上面的日志就是原因"
} else {
    Warn "面板还没应答（$url）—— 看上面的日志；首次启动要加载模型，慢是正常的"
}
Info "房间：面板上「创建房间 / 加入房间」选一个码；跨机两端选同一个码即可（房间 + 加密链路一次配好）"
Info "串口：ESP32 假主机在 config.toml 的 [sim_coyote] port = `"$SerialP`"（不对的话在面板的设备下拉里热切换）"
Info "日志：logs\*.log（纯文本）；本窗口里是彩色的"
Info "停止：在本窗口按 Ctrl+C，或双击 stop.cmd"
Info "首次启动若弹出「是否允许 python.exe 访问网络」—— 那是防火墙询问，不是要管理员权限："
Info "  只做本机演示点「取消」即可；要跨机就点「允许」并只勾专用网络。全部节点都在本窗口里，没有后台服务。"
if (-not $NoBrowser -and $up) { Start-Process $url | Out-Null }
if ($NoPause) {
    if (-not $nodes.HasExited) { $nodes.WaitForExit() | Out-Null }
    exit 0
}
Say ""
Write-Host "节点就在本窗口里跑（Ctrl+C 停全部，或双击 stop.cmd）…" -ForegroundColor DarkGray
if (-not $nodes.HasExited) { $nodes.WaitForExit() | Out-Null }
