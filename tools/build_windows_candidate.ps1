param(
    [Parameter(Mandatory = $true)]
    [string]$ApkPath
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $ApkPath -PathType Leaf)) {
    throw "找不到 APK：$ApkPath"
}

uv sync --extra desktop --group build --group dev
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

uv run python tools/extract_game_protocol.py --apk "$ApkPath" --output game-protocol.json
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

uv run python tools/build_desktop_authorized_0415.py --protocol game-protocol.json --output build/desktop
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

uv run python -c "import sys; sys.path.insert(0, 'tools'); from pathlib import Path; from review_desktop import review_authorized_0415; review_authorized_0415(Path('build/desktop'), Path('build/desktop/candidate/FGOStatDesktop.exe'))"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Windows 候选 EXE 已生成并通过载荷审核：build/desktop/candidate/FGOStatDesktop.exe"
Write-Host "game-protocol.json 保留在本机并已被 .gitignore 忽略；请勿提交或公开。"
