# Windows 本地一键构建

在 Windows 10/11 x64 上，双击仓库根目录的 `build.bat`，选择构建目标。也可以在 PowerShell 中执行：

```powershell
.\tools\build_local.ps1 -Target exe -ApkPath .\your-client.apk
```

## Windows EXE

- 需要安装 `uv`，安装说明：https://docs.astral.sh/uv/getting-started/installation/
- 首次运行会通过 uv 准备 Python 3.13 和项目固定依赖。
- 输入的 APK 仅用于从 `assets/game-protocol.json` 提取应用级固定协议配置；不会修改 APK，也不会把密钥打印到终端。
- EXE 会先经过仓库现有的候选包构建与载荷审核，再复制到 `output/FGOStatDesktop.exe`。
- 目标电脑需要 Microsoft Edge WebView2 Runtime。
- 临时配置位于本机 `build/private` 和候选构建目录。请勿把 `build`、`output`、真实协议 JSON 或输入 APK 上传到公开仓库。

## Android APK 与预审材料

**当前仍不能生成 APK。** `android/app/build.gradle` 继续以 `RUNTIME_PREAUDIT_REQUIRED` 阻止所有 APK 打包任务。现在仅增加了一个严格限定的 `:app:proposeReleasePackageInputs` 任务，用来收集真实 AGP 打包输入并生成待独立审核的候选清单；该任务不执行 `packageRelease`、`assembleRelease`、`bundleRelease` 或签名，也不会解除正式构建保护。

候选任务需要先按 `ANDROID.md` 准备 Python 3.13 暂存目录、固定 Android wheels、JDK 17、Android SDK/NDK，并完成公开资源清单审核。准备好 `build\\android-prepared` 和 `build\\android-wheels` 后，可在 PowerShell 中执行：

```powershell
$python = (uv run python -c "import sys; print(sys.executable)").Trim()
$prepared = (Resolve-Path .\\build\\android-prepared).Path
$wheelDir = (Resolve-Path .\\build\\android-wheels).Path
$expectedBuild = (Resolve-Path .\\android\\app\\build).Path
$proposalDir = Join-Path $PWD "build\\android-preaudit"
New-Item -ItemType Directory -Force -Path $proposalDir | Out-Null
$proposal = Join-Path $proposalDir "agp-inputs-candidate.json"
Push-Location .\\android
.\\gradlew.bat --no-build-cache --no-configuration-cache --rerun-tasks :app:proposeReleasePackageInputs `
  -PpreparedDir="$prepared" -PbuildPython="$python" -PwheelDir="$wheelDir" `
  -PapprovedFrontendAndroidRelease=2026-11-10 `
  -PpreauditExpectedBuildDir="$expectedBuild" `
  -PpreauditProposalManifest="$proposal"
Pop-Location
```

输出的 `agp-inputs-candidate.json` 只是实际输入的路径/哈希清单候选，不是审核通过证明。必须由独立审核确认后，才能把其哈希作为后续验证输入；不能直接把候选文件当作已批准清单。若构建任务图包含 APK/AAB 打包或签名任务，Gradle 会拒绝执行。

本地一键脚本的 `apk` 选项仍会安全停止，不会自动运行这个候选任务，更不会绕过正式发布保护。Android 工程目标环境见 `ANDROID.md`。仅安装构建工具不能解除预审保护。

## 日志与输出

- EXE：`output/FGOStatDesktop.exe`
- 构建日志：`output/build-logs/build-时间戳.log`
- 构建中间文件：`build/`

该入口不会自动提交或上传任何文件，也不会触发 GitHub Release。
