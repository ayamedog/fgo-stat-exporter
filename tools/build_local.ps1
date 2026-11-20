[CmdletBinding()]
param(
    [ValidateSet("menu", "exe", "apk", "audit", "all")]
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

    # Remove only known generated files so repeated builds work with the upstream builder.
    $privateProtocol = Join-Path $Root "build\private\game-protocol.json"
    $desktopBuild = Join-Path $Root "build\desktop"
    $previousExe = Join-Path $Output "FGOStatDesktop.exe"
    if (Test-Path -LiteralPath $privateProtocol) {
        Remove-Item -LiteralPath $privateProtocol -Force
    }
    if (Test-Path -LiteralPath $desktopBuild) {
        $resolvedBuild = [System.IO.Path]::GetFullPath($desktopBuild)
        $expectedBuild = [System.IO.Path]::GetFullPath((Join-Path $Root "build\desktop"))
        if ($resolvedBuild -ne $expectedBuild) { Stop-Build "拒绝清理非预期构建目录。" }
        Remove-Item -LiteralPath $desktopBuild -Recurse -Force
    }
    if (Test-Path -LiteralPath $previousExe) {
        Remove-Item -LiteralPath $previousExe -Force
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

function Build-AndroidAuditProposal {
    Require-Command "uv" "请先安装 uv：https://docs.astral.sh/uv/getting-started/installation/"
    if ([string]::IsNullOrWhiteSpace($env:JAVA_HOME) -or
        -not (Test-Path -LiteralPath (Join-Path $env:JAVA_HOME "bin\\java.exe") -PathType Leaf)) {
        Stop-Build "请先设置有效的 JAVA_HOME（JDK 17）。"
    }
    if ([string]::IsNullOrWhiteSpace($env:ANDROID_HOME) -or
        -not (Test-Path -LiteralPath $env:ANDROID_HOME -PathType Container)) {
        Stop-Build "请先设置有效的 ANDROID_HOME（Android SDK Platform 35 / Build Tools 35.0.0）。"
    }

    $prepared = Join-Path $Root "build\\android-prepared"
    $wheelDir = Join-Path $Root "build\\android-wheels"
    if (-not (Test-Path -LiteralPath $prepared -PathType Container) -or
        -not (Test-Path -LiteralPath $wheelDir -PathType Container)) {
        Stop-Build "缺少 build\\android-prepared 或 build\\android-wheels。请先按 ANDROID.md 完成暂存、资源审核及固定 wheels 准备。"
    }

    $gradle = Join-Path $Root "android\\gradlew.bat"
    if (-not (Test-Path -LiteralPath $gradle -PathType Leaf)) {
        Stop-Build "缺少 Android Gradle Wrapper：$gradle"
    }
    $python = (& uv run python -c "import sys; print(sys.executable)").Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($python) -or
        -not (Test-Path -LiteralPath $python -PathType Leaf)) {
        Stop-Build "无法定位项目 Python 3.13 解释器。"
    }

    $proposalDir = Join-Path $Root "build\\android-preaudit"
    New-Item -ItemType Directory -Force -Path $proposalDir | Out-Null
    $proposal = Join-Path $proposalDir "agp-inputs-candidate.json"
    if (Test-Path -LiteralPath $proposal) {
        Stop-Build "候选清单已存在，拒绝覆盖：$proposal。请先人工归档或移走旧候选清单。"
    }
    $expectedBuild = Join-Path $Root "android\\app\\build"
    $gradleArgs = @(
        "--no-build-cache",
        "--no-configuration-cache",
        "--rerun-tasks",
        ":app:proposeReleasePackageInputs",
        "-PpreparedDir=$prepared",
        "-PbuildPython=$python",
        "-PwheelDir=$wheelDir",
        "-PapprovedFrontendAndroidRelease=2026-11-10",
        "-PpreauditExpectedBuildDir=$expectedBuild",
        "-PpreauditProposalManifest=$proposal"
    )

    Write-Host "开始生成 Android AGP 输入候选清单（不会生成 APK）..." -ForegroundColor Cyan
    Push-Location (Join-Path $Root "android")
    try {
        & $gradle @gradleArgs
        $gradleExit = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
    if ($gradleExit -ne 0) {
        Stop-Build "Android 预审候选材料生成失败。未生成 APK。"
    }
    if (-not (Test-Path -LiteralPath $proposal -PathType Leaf)) {
        Stop-Build "Gradle 任务结束，但没有生成候选清单：$proposal"
    }
    Write-Host "候选清单已生成：$proposal" -ForegroundColor Green
    Write-Host "注意：这只是待独立审核的输入清单，不是审核通过证明，也不是 APK。"
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
    Write-Host "4. Android 预审候选清单（不生成 APK）"
    $choice = Read-Host "请选择 1 / 2 / 3"
    switch ($choice) {
        "1" { $Target = "exe" }
        "2" { $Target = "apk" }
        "3" { $Target = "all" }
        "4" { $Target = "audit" }
        default { Stop-Build "无效选项。" }
    }
}

try {
    if ($Target -eq "exe" -or $Target -eq "all") { Build-Exe }
    if ($Target -eq "audit") { Build-AndroidAuditProposal }
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
