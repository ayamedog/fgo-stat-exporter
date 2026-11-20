# 安卓构建与使用

应用使用 Python 和 WebView，登录请求直接发送到官方接口，字典和图片从公开数据源获取。不需要安装游戏、Root、代理或证书。

## 设备要求

- Android 7.0（API 24）或以上，64 位 ARM 或 x86 设备。
- Android System WebView 或 Chrome 83 以上，并支持 `WEB_MESSAGE_LISTENER`。旧版 WebView 会提示更新。
- 系统文件选择器用于导入文件和保存原始 `.php` 响应。图片直接写入系统相册的“FGO抓包统计”，不选择目录。
- Android 10 及以上保存相册不申请存储权限；Android 7–9 首次保存需要一次存储权限，拒绝时给出提示。程序不读取已有照片，不申请管理全部文件权限。
- 安装包包含 `arm64-v8a`、`x86_64`，不包含 32 位架构。测试环境和兼容范围见 [VALIDATION.md](VALIDATION.md)。

## 编译环境

构建使用 Python 3.13、JDK 17、Android SDK Platform 35 / Build Tools 35.0.0、NDK 28.2.13676358、CMake、Ninja、Rust stable。Gradle Wrapper 固定为 8.11.1，Android Gradle Plugin 8.9.2，Chaquopy 17.1.0。依赖版本在 `android/requirements.txt` 固定。

先在项目根目录安装 Python 依赖，并设置实际工具位置。以下路径请按实际安装位置修改：

```powershell
uv sync --extra desktop --group build --group dev
$env:JAVA_HOME = 'C:\Java\jdk-17'
$env:ANDROID_HOME = 'C:\Android\Sdk'
$ndkDirectory = Join-Path $env:ANDROID_HOME 'ndk\28.2.13676358'
rustup target add aarch64-linux-android x86_64-linux-android
```

Python 3.13 的安卓校验库和 16 KB 页对齐图像依赖通过固定上游源码构建。首次构建会下载并校验源码；原始材料及日志留在构建工作目录，不进入安装包：

```powershell
uv run python tools/build_android_wheels.py --ndk $ndkDirectory --work build\android-native --output build\android-wheels
uv run python tools/build_android_codecs.py --ndk $ndkDirectory --work build\android-codecs --output build\android-wheels
```

## 固定协议配置

将根目录的 `game-protocol.example.json` 复制为 `game-protocol.json` 并填写实际参数。源码运行和 APK 构建默认读取这份配置，文件已加入 `.gitignore`。APK 构建会将固定配置写入 `assets/game-protocol.json`，不将真实值写入代码。配置只能包含 `version`、`sdk_base`、`sdk_key`、`development_code`、`session_prefix`，不接受账号口令、Cookie、token 或登录 session。

没有配置仍能编译、导入文件和生成图片，但不能扫码取数。需要使用其他位置的 JSON 时传入 `--protocol 文件路径`。请勿把真实配置、构建暂存目录、签名文件或运行数据提交到仓库。

## 暂存与打包状态

`prepare_android.py` 可暂存白名单内的模块、界面、公开资源与许可证；输出目录必须不存在。可选 `--resources` 使用已有的完整资源目录，省略则从公开源准备字典和通用图片。

**当前分支暂时不能生成 APK。** `android/app/build.gradle` 中的 `runtimeAuditIntegrated = false` 会在 Gradle 任务图阶段明确抛出 `RUNTIME_PREAUDIT_REQUIRED`，阻止 APK 打包任务。唯一例外是严格限定的 `:app:proposeReleasePackageInputs` 预审候选任务：它只运行 Release 的必要中间任务并生成 AGP 输入路径/哈希候选清单，不执行 `packageRelease`、`assembleRelease`、`bundleRelease` 或签名。候选清单不代表审核通过，必须独立审核后才能用于后续验证。原因是 Python、DEX、ELF 第三方运行载荷的去调试和打包前审计尚未完整接入 Gradle。不要通过删除该保护开关来强行打包。

原始 toplogin 响应的捕获、Android 文件选择器和字节原样写入已接入代码。候选清单可通过 `tools/android_preaudit_candidate/propose_inputs.py` 生成，验证器 `frozen_inputs.py` 只接受独立审核后的清单及其固定 SHA-256。完成真实 Python/DEX/ELF 载荷审计、Gradle 变换集成、最终 APK 复核后，才能生成可交付 APK。正式包还需要仓库外的签名密钥，不能把密钥或口令提交到仓库。

## 使用

安装后查看资源状态；启动时会检查一次更新，也可点击“检查资源更新”重新检查。旧版可在新版下载时继续使用。新版本通过校验后自动启用，首次生成统计图时按需下载图片。

国服扫码时选择角色所在的 iOS 或安卓区服，核对授权应用为“命运-冠位指定”。导入文件支持国服、日服，自动识别区服。扫码取得 toplogin 响应后可保存原始 `.php` 文件；即使统计解析不兼容，只要服务器响应已收到仍可保存。点击“保存完整长图”直接存入系统相册；超长图可选择分段保存。每张图片使用不同文件名，不覆盖已有照片。统计与授权会话只在内存中短时处理，退出或到期清除；原始响应仅在用户主动保存时写入 `.php` 文件，该文件可能包含个人角色数据，请勿公开分享。

资源更新跟随 Chaldea、Atlas Academy 等公开源的发布时间。数据格式、登录协议或页面代码变化需要发布新版应用，资源检查不会下载执行代码。
