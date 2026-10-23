# 安卓构建与使用

应用使用 Python 和 WebView，登录请求直接发送到官方接口，字典和图片从公开数据源获取。不需要安装游戏、Root、代理或证书。

## 设备要求

- Android 7.0（API 24）或以上，64 位 ARM 或 x86 设备。
- Android System WebView 或 Chrome 83 以上，并支持 `WEB_MESSAGE_LISTENER`。旧版 WebView 会提示更新。
- 系统文件选择器用于导入文件和保存 JSON。图片直接写入系统相册的“FGO抓包统计”，不选择目录。
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

## 暂存与打包

`prepare_android.py` 只暂存白名单内的模块、界面、公开资源与许可证；输出目录必须不存在。可选 `--resources` 使用已有的完整资源目录，省略则直接从公开源准备字典和通用图片。

```powershell
uv run python tools/prepare_android.py --output build\android-prepared
$preparedDirectory = (Resolve-Path build\android-prepared).Path
$wheelDirectory = (Resolve-Path build\android-wheels).Path
$pythonExecutable = (Resolve-Path .venv\Scripts\python.exe).Path
.\android\gradlew.bat -p android assembleDebug "-PpreparedDir=$preparedDirectory" "-PbuildPython=$pythonExecutable" "-PwheelDir=$wheelDirectory"
```

调试 APK 位于 `android/app/build/outputs/apk/debug/app-debug.apk`。发布时使用正式签名包。

正式签名使用仓库外的密钥库，通过 `FGO_ANDROID_KEYSTORE`、`FGO_ANDROID_STORE_PASSWORD`、`FGO_ANDROID_KEY_ALIAS`、`FGO_ANDROID_KEY_PASSWORD` 环境变量输入，再运行 `assembleRelease`。密钥、口令和配置文件不得写入命令示例或提交；更换签名无法直接覆盖旧安装。

发布前测试安装、文件导入、扫码取数、出图、文件保存和资源更新。使用 `tools/review_android.py --apk 安装包路径 --prepared 暂存目录 --output 审核记录路径` 检查全部路径、嵌套归档、编译模块、资源载荷和原生库页对齐；该脚本拒绝依赖测试目录、开发源码和调试符号。`zipalign -c -P 16 4` 和 `apksigner verify` 分别检查压缩对齐与签名。检查记录、构建暂存目录和日志不随安装包分发。

## 使用

安装后查看资源状态；启动时会检查一次更新，也可点击“检查资源更新”重新检查。旧版可在新版下载时继续使用。新版本通过校验后自动启用，首次生成统计图时按需下载图片。

国服扫码时选择角色所在的 iOS 或安卓区服，核对授权应用为“命运-冠位指定”。导入文件支持国服、日服，自动识别区服。统计就绪后可保存 JSON、生成统计图，点击“保存完整长图”直接存入系统相册；超长图可选择分段保存。每张图片使用不同文件名，不覆盖已有照片。统计与授权会话只在内存中短时处理，退出或到期清除；用户主动保存的文件和公开资源缓存保留在设备上。

资源更新跟随 Chaldea、Atlas Academy 等公开源的发布时间。数据格式、登录协议或页面代码变化需要发布新版应用，资源检查不会下载执行代码。
