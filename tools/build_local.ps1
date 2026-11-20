[CmdletBinding()]
param(
    [ValidateSet("menu", "exe", "apk", "all")]
    [string]$Target = "menu",
    [string]$ApkPath
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root
$Output = Join-Path $Root "output"
$LogDir = Join-Path $Output "build-logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$Log = Join-Path $LogDir "build-$Stamp.log"
Start-Transcript -Path $Log -Force | Out-Null

function Stop-Build([string]$Message, [int]$Code = 1) {
    Write-Host ""
    Write-Host $Message -ForegroundColor Red
    Write-Host "Log: $Log"
    Stop-Transcript | Out-Null
    exit $Code
}

function Require-Command([string]$Name, [string]$InstallHint) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        Stop-Build "$Name 未安装或不在 PATH 中。$InstallHint"
    }
}

function Build-Exe {
    Require-Command "uv" "请先安装 uv：https://docs.astral.sh/uv/getting-started/installation/"
    $sourceApk = $ApkPath
    if ([string]::IsNullOrWhiteSpace($sourceApk)) {
        $sourceApk = Read-Host "请输入用于提取应用级协议参数的 APK 完整路径（不会修改该 APK）"
    }
    if ([string]::IsNullOrWhiteSpace($sourceApk)) {
        Stop-Build "未提供 APK 路径。"
    }
    $sourceApk = [System.IO.Path]::GetFullPath($sourceApk)
    if (-not (Test-Path -LiteralPath $sourceApk -PathType Leaf)) {
        Stop-Build "找不到输入 APK：$sourceApk"
    }

    $builder = Join-Path $Root "tools\build_windows_candidate.ps1"
    if (-not (Test-Path -LiteralPath $builder)) {
        Stop-Build "缺少项目原有 Windows 构建脚本：tools\build_windows_candidate.ps1"
    }

    Write-Host "开始构建 Windows 候选 EXE..." -ForegroundColor Cyan
    & $builder -ApkPath $sourceApk
    if ($LASTEXITCODE -ne 0) {
        Stop-Build "Windows 构建或载荷审核失败，未交付 EXE。" $LASTEXITCODE
    }

    $candidate = Join-Path $Root "build\desktop\candidate\FGOStatDesktop.exe"
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        Stop-Build "构建命令结束，但未找到预期 EXE：$candidate"
    }
    $dest = Join-Path $Output "FGOStatDesktop.exe"
    Copy-Item -LiteralPath $candidate -Destination $dest -Force
    Write-Host "EXE 已生成：$dest" -ForegroundColor Green
    Write-Host "注意：目标 Windows 电脑需要 Microsoft Edge WebView2 Runtime。"
}

function Build-Apk {
    $gradleFile = Join-Path $Root "android\app\build.gradle"
    if (-not (Test-Path -LiteralPath $gradleFile -PathType Leaf)) {
        Stop-Build "找不到 Android Gradle 工程。"
    }
    $gradleText = Get-Content -LiteralPath $gradleFile -Raw
    if ($gradleText -match 'runtimeAuditIntegrated\s*=\s*false' -or
        $gradleText -match 'RUNTIME_PREAUDIT_REQUIRED') {
        Stop-Build @"
Android APK 构建已被项目自身的运行载荷预审保护阻止。
当前版本尚未把 Python、DEX、ELF 载荷审计完整接入 Gradle。
本脚本不会删除保护开关，也不会尝试绕过它。
请先完成并审核 Android 运行载荷预审集成，再启用 APK 构建。
"@
    }
    Stop-Build "检测到 APK 构建保护状态已变化，但当前脚本尚未获准执行正式 Android 发布流程；请先更新并审阅本地构建说明。"
}

if ($Target -eq "menu") {
    Write-Host "FGO 抓包统计 - 本地构建" -ForegroundColor Cyan
    Write-Host "1. 构建 Windows EXE"
    Write-Host "2. Android APK（当前被运行载荷预审保护阻止）"
    Write-Host "3. EXE + APK（APK 阶段将安全停止）"
    $choice = Read-Host "请选择 1 / 2 / 3"
    switch ($choice) {
        "1" { $Target = "exe" }
        "2" { $Target = "apk" }
        "3" { $Target = "all" }
        default { Stop-Build "无效选项。" }
    }
}

try {
    if ($Target -eq "exe" -or $Target -eq "all") { Build-Exe }
    if ($Target -eq "apk" -or $Target -eq "all") { Build-Apk }
    Write-Host "构建流程完成。" -ForegroundColor Green
    Stop-Transcript | Out-Null
    exit 0
}
catch {
    Write-Host $_ -ForegroundColor Red
    Write-Host "详细日志：$Log"
    Stop-Transcript | Out-Null
    exit 1
}
