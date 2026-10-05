# ══════════════════════════════════════════════════════════════
#  停止 webDG —— 只杀**本发行版目录里**的那些进程
#
#  为什么按"命令行里带本目录路径"来筛：绝不误杀你自己另外跑的 python。
#  （等价于在启动窗口里按 Ctrl+C，只是窗口找不到 / 关不掉时用这个。）
#
#  ⚠️ 中文放在这里（.ps1 + BOM），不放 .cmd：cmd 按 OEM 代码页解码，
#     UTF-8 的中文注释会被它当命令执行（实测踩过）。stop.cmd 只做 ASCII 转发。
# ══════════════════════════════════════════════════════════════
[CmdletBinding()]
param([switch]$NoPause)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

# ⚠️ venv 的 python.exe 会再 exec 一份同命令行的子进程（实测），
#    所以同一个节点在进程表里可能出现两次 —— 一起杀，别留孤儿。
#
# ⚠️⚠️ 而且**节点解释器不一定在本目录里**：发行版目录含中文时，启动器会让节点用
#    基础解释器跑（OpenCV 才能打开真摄像头，见 start.ps1 里那段长注释），
#    这时子进程的命令行里只有 `-m agent.nodes.xxx`，**不含本目录**。
#    所以先按"命令行带本目录"找到 supervisor，再**顺着父子关系把它整棵树杀干净**。
function Get-Tree([int[]]$seed) {
    $all = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Select-Object ProcessId, ParentProcessId)
    $seen = New-Object System.Collections.Generic.HashSet[int]
    $queue = New-Object System.Collections.Generic.Queue[int]
    foreach ($p in $seed) { $queue.Enqueue($p) }
    while ($queue.Count -gt 0) {
        $cur = $queue.Dequeue()
        if (-not $seen.Add($cur)) { continue }
        foreach ($child in ($all | Where-Object { $_.ParentProcessId -eq $cur })) {
            $queue.Enqueue([int]$child.ProcessId)
        }
    }
    return @($seen)
}

function Get-DistProcesses {
    $roots = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains($Root) })
    if (-not $roots) { return @() }
    $tree = Get-Tree @($roots | ForEach-Object { [int]$_.ProcessId })
    return @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $tree -contains [int]$_.ProcessId })
}

$procs = @(Get-DistProcesses)

Write-Host ""
if (-not $procs) {
    Write-Host "  · 没找到本目录下的进程（可能已经停了）" -ForegroundColor DarkGray
} else {
    foreach ($p in $procs) {
        Write-Host "  停止 pid=$($p.ProcessId)  $($p.Name)" -ForegroundColor Yellow
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
}
Start-Sleep -Milliseconds 500

$left = @(Get-DistProcesses)
if ($left.Count -gt 0) {
    Write-Host "  ! 还有 $($left.Count) 个没退掉，再试一次（强杀）" -ForegroundColor Yellow
    foreach ($p in $left) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 500
}
Write-Host ""
Write-Host "  ✓ 完成（本目录下的节点已停止）" -ForegroundColor Green
Write-Host ""

if (-not $NoPause) {
    Write-Host "按回车关闭…" -ForegroundColor DarkGray
    Read-Host | Out-Null
}
