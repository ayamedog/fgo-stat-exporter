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

## Android APK

**当前不能通过本地脚本生成 APK。** 仓库的 `android/app/build.gradle` 明确以 `RUNTIME_PREAUDIT_REQUIRED` 阻止 APK 构建，因为 Python、DEX、ELF 第三方运行载荷的去调试与打包前审核尚未完整接入 Gradle。

脚本会检测该保护并安全停止，不会删除保护开关、不伪造审核结果，也不会通过其他 Gradle 任务绕过限制。完成独立载荷审计、将审计结果接入 Gradle 并验证后，才可以启用 APK 构建。

Android 工程文档列出的目标环境为 JDK 17、Android SDK Platform 35 / Build Tools 35.0.0、NDK 28.2.13676358、CMake、Ninja、Rust stable，以及仓库锁定的 Gradle / Chaquopy 版本。仅安装这些工具并不能解除上述预审保护。

## 日志与输出

- EXE：`output/FGOStatDesktop.exe`
- 构建日志：`output/build-logs/build-时间戳.log`
- 构建中间文件：`build/`

该入口不会自动提交或上传任何文件，也不会触发 GitHub Release。
