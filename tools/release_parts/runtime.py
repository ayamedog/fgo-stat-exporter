"""只从审核过的源码集合构建桌面运行程序。"""

import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from fgo_local_exporter.game_profile import load_profile, profile_values
from fgo_local_exporter.reports import shared_report_revision
from release_parts.release_guard import require_source_free_release

APP_MODULES = (
    "__init__",
    "errors",
    "game_profile",
    "protocol",
    "statistics",
    "login_session",
    "local_files",
    "import_worker",
    "local_api",
    "settings",
    "reports",
    "sessions",
    "desktop",
    "desktop_bridge",
    "desktop_reports",
    "desktop_resources",
    "desktop_storage",
    "resource_catalog",
)
STATIC_FILES = (
    "ui/index.html",
    "ui/style.css",
    "ui/app.js",
    "ui/gacha-records.js",
    "ui/gacha-records.css",
    "ui/html-to-image.js",
    "ui/resource-status.js",
    "ui/captcha-frame.html",
    "ui/captcha-frame.js",
    "desktop_static/resources.json",
    "desktop_static/setup.html",
    "desktop_static/desktop.js",
    "desktop_static/desktop.css",
)
CORE_MODULES = (
    "__init__.py",
    "contracts/__init__.py",
    "contracts/formation_name.py",
    "contracts/master_name.py",
    "contracts/models.py",
    "contracts/report.py",
    "contracts/report_master.py",
    "contracts/gacha_records.py",
    "features/__init__.py",
    "features/capture_import/__init__.py",
    "features/capture_import/decoder.py",
    "features/capture_import/parser.py",
    "features/capture_import/schema.py",
    "features/report/__init__.py",
    "features/report/calculator.py",
    "features/report/gacha_records.py",
    "features/report/parameters.py",
    "features/report/skills.py",
    "infrastructure/__init__.py",
    "infrastructure/public_download.py",
    "infrastructure/native_support_adapter.py",
    "infrastructure/native_support_render.py",
    "infrastructure/native_support_ui/__init__.py",
    "infrastructure/native_support_ui/bitmap_font.py",
    "infrastructure/native_support_ui/renderer.py",
    "infrastructure/native_support_ui/typography.py",
    "infrastructure/report_assets.py",
    "infrastructure/report_fonts.py",
    "infrastructure/report_master.py",
    "infrastructure/report_ui/__init__.py",
    "infrastructure/report_ui/base_blocks.py",
    "infrastructure/report_ui/builder.py",
)
CORE_DATA = (
    "infrastructure/report_ui/template.html",
    "infrastructure/native_support_ui/asset_hashes.json",
    "infrastructure/native_support_ui/font_manifest.json",
    "infrastructure/native_support_ui/native_geometry.json",
)
RUNTIME_PACKAGES = (
    "pywebview",
    "pythonnet",
    "clr-loader",
    "cffi",
    "pycparser",
    "bottle",
    "proxy-tools",
    "typing-extensions",
    "Flask",
    "Werkzeug",
    "Jinja2",
    "MarkupSafe",
    "itsdangerous",
    "blinker",
    "click",
    "colorama",
    "qrcode",
    "pycryptodome",
    "pydantic",
    "pydantic-core",
    "annotated-types",
    "typing-inspection",
    "Pillow",
    "httpx",
    "httpcore",
    "anyio",
    "h11",
    "idna",
    "certifi",
    "tzdata",
    "pyinstaller",
    "pywin32-ctypes",
    "altgraph",
    "pefile",
    "packaging",
)


def desktop_build_profile(root, path=None):
    """扫码版必须具备完整的五项应用参数，不允许静默生成仅能导入的 EXE。"""
    profile = load_profile(Path(path) if path is not None else root / "game-protocol.json")
    if profile is None:
        raise RuntimeError("缺少扫码固定配置，已停止构建，避免发布无法扫码的 EXE")
    return profile


def stage_desktop_profile(work, profile):
    """重新序列化白名单应用参数，不复制原配置及任何额外字段。"""
    directory = work / "reviewed-runtime-config"
    directory.mkdir()
    path = directory / "game-protocol.json"
    raw = json.dumps(profile_values(profile), separators=(",", ":")).encode("utf-8")
    path.write_bytes(raw)
    if {item.name for item in directory.iterdir()} != {path.name}:
        raise RuntimeError("内置扫码参数目录超出白名单")
    return path


def build_runtime(root, work, resources, documents, *, profile):
    require_source_free_release()
    return _build_runtime(root, work, resources, documents, profile=profile)


def build_runtime_authorized_0415(root, work, resources, documents, *, profile):
    """仅用于用户明确授权前端页面与五项应用参数的 0.4.15 候选包。"""
    from fgo_local_exporter import __version__

    if __version__ != "0.4.15" or profile is None:
        raise RuntimeError("授权范围仅限 0.4.15 的完整桌面客户端")
    return _build_runtime(root, work, resources, documents, profile=profile)


def _build_runtime(root, work, resources, documents, *, profile):
    stage = work / "source"
    stage.mkdir(parents=True)
    selected = ["run_desktop.py"]
    selected += ["src/fgo_local_exporter/" + name + ".py" for name in APP_MODULES]
    selected += ["src/fgo_stat_core/" + name for name in CORE_MODULES]
    selected += ["src/fgo_stat_core/" + name for name in CORE_DATA]
    selected += ["src/fgo_local_exporter/" + name for name in STATIC_FILES]
    source_hashes = {}
    for name in selected:
        source = root / name
        if not source.is_file() or source.is_symlink():
            raise RuntimeError("构建输入不合法：" + name)
        raw = source.read_bytes()
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        source_hashes[name] = hashlib.sha256(raw).hexdigest()
    actual = {path.relative_to(stage).as_posix() for path in stage.rglob("*") if path.is_file()}
    if actual != set(selected):
        raise RuntimeError("构建输入集合与白名单不符")
    protocol = stage_desktop_profile(work, profile)
    revision = shared_report_revision(root / "src")
    manifest = work / "engine-manifest.json"
    manifest.write_text(json.dumps({
        "revision": revision,
        "files": source_hashes,
        "protocol_sha256": hashlib.sha256(protocol.read_bytes()).hexdigest(),
    }, indent=2))
    args = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name",
        "FGOStatDesktop",
        "--icon",
        str(root / "assets/app.ico"),
        "--paths",
        str(stage / "src"),
        "--distpath",
        str(work / "binary"),
        "--workpath",
        str(work / "pyinstaller"),
        "--additional-hooks-dir",
        str(root / "tools/hooks"),
        "--specpath",
        str(work),
        "--add-data",
        str(manifest) + ":.",
        "--add-data",
        str(resources) + ":report-resources",
        "--add-data",
        str(documents) + ":documents",
        "--add-data",
        str(protocol) + ":runtime-config",
    ]
    for name in STATIC_FILES:
        args += [
            "--add-data",
            str(stage / "src/fgo_local_exporter" / name)
            + ":fgo_local_exporter/"
            + str(Path(name).parent).replace("\\", "/"),
        ]
    for name in CORE_DATA:
        args += [
            "--add-data",
            str(stage / "src/fgo_stat_core" / name)
            + ":fgo_stat_core/"
            + str(Path(name).parent).replace("\\", "/"),
        ]
    for name in (
        "pytest",
        "mypy",
        "ruff",
        "unittest",
        "flask.testing",
        "doctest",
        "pydoc",
        "setuptools",
        "pip",
        "IPython",
        "fitz",
        "pymupdf",
        "weasyprint",
        "matplotlib",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "Crypto.SelfTest",
        "cefpython3",
        "gi",
    ):
        args += ["--exclude-module", name]
    args += [str(stage / "run_desktop.py")]
    environment = os.environ.copy()
    windows = Path(os.environ.get("WINDIR", "C:/Windows"))
    environment["PATH"] = os.pathsep.join(str(path) for path in (
        Path(sys.executable).parent, Path(sys.base_prefix), Path(sys.base_prefix) / "DLLs",
        windows / "System32", windows,
    ))
    environment["PYTHONPATH"] = str(stage / "src")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment.pop("PYTHONHOME", None)
    with (work / "build.log").open("wb") as output:
        result = subprocess.run(
            args, stdout=output, stderr=subprocess.STDOUT, cwd=stage, env=environment
        )
    if result.returncode:
        raise RuntimeError("程序编译失败，请查看构建日志")
    return work / "binary/FGOStatDesktop.exe", source_hashes


def collect_licenses(root, output):
    expected = []
    hashes = {}

    def record(path):
        hashes[path.relative_to(output).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()

    for package in RUNTIME_PACKAGES:
        distribution = importlib.metadata.distribution(package)
        names = []
        for file in distribution.files or ():
            if file.name.lower().startswith(("license", "copying")) and (
                "dist-info" in str(file) or "licenses" in str(file)
            ):
                source = Path(distribution.locate_file(file))
                if source.is_file() and source.stat().st_size <= 256 * 1024:
                    target = output / package.lower().replace("_", "-") / file.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists() and target.read_bytes() != source.read_bytes():
                        target = target.with_name(source.parent.name + "-" + file.name)
                    shutil.copyfile(source, target)
                    record(target)
                    names.append(target.relative_to(output).as_posix())
        if not names and package == "proxy-tools":
            source = root / "licenses/proxy-tools-BSD.txt"
            target = output / "proxy-tools/LICENSE.txt"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            record(target)
            names.append(target.relative_to(output).as_posix())
        if not names:
            raise RuntimeError("运行依赖缺少许可证：" + package)
        expected.append({"name": package, "version": distribution.version, "licenses": names})
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        python_license = Path(sys.base_prefix) / "LICENSE"
    if not python_license.is_file():
        raise RuntimeError("缺少 Python 运行时许可证")
    shutil.copyfile(python_license, output / "Python.txt")
    record(output / "Python.txt")
    for name in ("html-to-image-MIT.txt", "WebView2-LICENSE.txt", "WebView2-NOTICE.txt"):
        shutil.copyfile(root / "licenses" / name, output / name)
        record(output / name)
    webview_license = Path(
        importlib.metadata.distribution("pywebview").locate_file("webview/lib/LICENSE")
    )
    if webview_license.is_file():
        shutil.copyfile(webview_license, output / "WebView2.txt")
        record(output / "WebView2.txt")
    actual = {}
    for path in output.rglob("*"):
        if path.is_symlink():
            raise RuntimeError("许可证目录不能包含符号链接")
        if path.is_file():
            relative = path.relative_to(output).as_posix()
            actual[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != hashes:
        raise RuntimeError("许可证目录与收集白名单不符")
    return expected, hashes
